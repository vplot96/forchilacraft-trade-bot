#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import csv
import logging
import asyncio
from io import StringIO
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime

import requests
from fastapi import FastAPI
import uvicorn
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Команды (папка commands/ должна содержать __init__.py)
from commands.help import help_cmd
from commands.balance import balance, init_balance_helpers
from commands.price import price, price_followup_listener, init_price_helpers
from commands.pay import pay as pay_cmd, init_pay_helpers
from commands.ops import ops, init_ops_helpers
from commands.sell import sell, sell_confirm_listener


# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# -----------------------------
# FastAPI app (для платформ, которые ожидают web-процесс)
# -----------------------------
app = FastAPI()
tg_app: Application | None = None
_tg_task: asyncio.Task | None = None


# -----------------------------
# Env helpers
# -----------------------------
def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


def optional_env(name: str) -> str | None:
    v = os.getenv(name)
    return v if v else None


# -----------------------------
# Core env (обязательные)
# -----------------------------
BOT_TOKEN = require_env("BOT_TOKEN")
SHEET_ID = require_env("SHEET_ID")
GID_ACCOUNTS = require_env("GID_ACCOUNTS")

# Эти env могут отсутствовать — команды сами сообщат, что недоступны
GID_PRICES = optional_env("GID_PRICES")
GID_OPS = optional_env("GID_OPS")


# -----------------------------
# Общие хелперы
# -----------------------------
def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def csv_url_for(gid: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}"


def fetch_rows(gid: str):
    url = csv_url_for(gid)
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    text = r.content.decode("utf-8-sig")
    reader = csv.DictReader(StringIO(text))
    return list(reader)


def _parse_decimal(value) -> Decimal:
    s = str(value or "").strip().replace(" ", "").replace(",", ".")
    if not s:
        return Decimal("0")
    try:
        return Decimal(s)
    except Exception:
        return Decimal("0")


def _parse_balance_to_decimal(value) -> Decimal:
    q = _parse_decimal(value)
    return q.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _parse_amount_arg(raw: str) -> Decimal:
    cleaned = re.sub(r"[^\d,.\-]", "", (raw or "")).replace(",", ".")
    if cleaned in ("", ".", "-", "-.", ".-"):
        raise ValueError("empty")
    q = Decimal(cleaned)
    return q.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _fmt_amount_comma2(amount: Decimal) -> str:
    return f"{amount:.2f}".replace(".", ",")


def _fmt_amount_trim(amount: Decimal) -> str:
    s = _fmt_amount_comma2(amount)
    return s[:-3] if s.endswith(",00") else s


def _find_account(rows, username: str):
    u = normalize(username)
    for r in rows:
        if normalize(str(r.get("Username", ""))) == u:
            return r
    return None


def _load_accounts_rows():
    return fetch_rows(GID_ACCOUNTS)


# -----------------------------
# Хелперы для /price (подключаются через init_price_helpers)
# -----------------------------
def _money_to_decimal(value) -> Decimal:
    s = str(value or "").strip().replace(" ", "").replace(",", ".")
    if not s:
        return Decimal("0.00")
    try:
        q = Decimal(s)
    except Exception:
        q = Decimal("0.00")
    return q.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _split_query_and_qty(raw: str):
    raw = (raw or "").strip()
    m = re.match(r"^(.*?)(?:\s+(\d+))?$", raw)
    if not m:
        return raw, None, False
    query = (m.group(1) or "").strip()
    qty_s = m.group(2)
    if qty_s is None:
        return query, None, False
    try:
        qty = int(qty_s)
    except Exception:
        return query, None, False
    return query, qty, True


def lookup_price_by_product_name(query: str):
    if not GID_PRICES:
        return None

    rows = fetch_rows(GID_PRICES)
    q = normalize(query)

    name_cols = ("Название", "Name", "Товар", "Название товара")
    price_cols = ("Цена", "Курс", "Price", "Текущая цена")

    for r in rows:
        name_val = None
        for c in name_cols:
            v = str(r.get(c) or "").strip()
            if v:
                name_val = v
                break
        if not name_val:
            continue
        if normalize(name_val) != q:
            continue

        price_val = None
        for c in price_cols:
            v = str(r.get(c) or "").strip()
            if v:
                price_val = v
                break
        if price_val is None:
            continue

        return (name_val, _money_to_decimal(price_val))

    return None


def _fmt_total(name: str, unit: Decimal, qty: int | None, qty_specified: bool) -> str:
    if not qty_specified or qty is None:
        return f"{name} = {_fmt_amount_trim(unit)} джк"
    total = (unit * Decimal(qty)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{name} ({qty}) = {_fmt_amount_trim(total)} джк"


# -----------------------------
# Хелперы для /ops
# -----------------------------
def _fetch_ops_rows():
    if not GID_OPS:
        return []
    return fetch_rows(GID_OPS)


def _parse_date_safe(value: str):
    s = str(value or "").strip()
    for fmt in ("%d.%m.%y", "%d.%m.%Y", "%Y-%m-%d", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    return None


def _format_op_line(row: dict) -> str:
    name = str(row.get("Название", "") or "").strip()
    op = str(row.get("Операция", "") or "").strip()
    qty_raw = row.get("Число", "")
    sum_raw = row.get("Сумма", "")
    date_raw = row.get("Дата", "")

    qty = int(_parse_decimal(qty_raw)) if str(qty_raw or "").strip() else 0
    amount = _parse_balance_to_decimal(sum_raw)
    date_dt = _parse_date_safe(date_raw)
    date_str = date_dt.strftime("%d.%m.%y") if date_dt else str(date_raw or "").strip()

    is_buy = normalize(op) == "покупка"
    sign = "−" if is_buy else "+"
    amount_str = _fmt_amount_trim(amount)

    return f'{date_str} {op} "{name}" ({qty}): {sign}{amount_str} джк'


# -----------------------------
# /start остаётся в bot.py
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Готов к работе! Введите /help для вывода списка команд.")


# -----------------------------
# Инициализация хелперов для команд
# -----------------------------
init_balance_helpers(_load_accounts_rows, _find_account, _parse_balance_to_decimal)

init_price_helpers(
    lookup_price_by_product_name,
    _split_query_and_qty,
    _money_to_decimal,
    _fmt_total,
)

init_ops_helpers(
    _load_accounts_rows,
    _find_account,
    _fetch_ops_rows,
    _parse_date_safe,
    _format_op_line,
)


# -----------------------------
# Lazy init для /pay
# -----------------------------
_pay_inited = False


def _try_init_pay() -> bool:
    global _pay_inited
    if _pay_inited:
        return True

    form_id = optional_env("FORM_PAY_ID")
    entry_sender = optional_env("FORM_PAY_ENTRY_SENDER")
    entry_recipient = optional_env("FORM_PAY_ENTRY_RECIPIENT")
    entry_sum = optional_env("FORM_PAY_ENTRY_SUM")

    if not all([form_id, entry_sender, entry_recipient, entry_sum]):
        return False

    post_url = f"https://docs.google.com/forms/d/e/{form_id}/formResponse"
    init_pay_helpers(
        form_id,
        entry_sender,
        entry_recipient,
        entry_sum,
        post_url,
        normalize,
        _parse_amount_arg,
        _load_accounts_rows,
        _find_account,
        _parse_balance_to_decimal,
        _fmt_amount_comma2,
    )
    _pay_inited = True
    return True


async def pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _try_init_pay():
        await update.message.reply_text("Команда /pay временно недоступна: не настроены переменные окружения.")
        return
    await pay_cmd(update, context)


# -----------------------------
# Unified "single queue" mechanics
# -----------------------------
def _clear_pending_all(context: ContextTypes.DEFAULT_TYPE):
    for k in list(context.user_data.keys()):
        if isinstance(k, str) and k.startswith("pending_"):
            context.user_data.pop(k, None)


async def _pre_command_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Сбрасываем ожидание при вводе ЛЮБОЙ команды.
    if context is None:
        return
    _clear_pending_all(context)


async def _unified_text_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or context is None:
        return

    # Приоритет: sell подтверждение важнее, чем price
    if context.user_data.get("pending_sell") is not None:
        try:
            await sell_confirm_listener(update, context)
        except Exception:
            logger.exception("Unhandled error in sell_confirm_listener")
            await update.message.reply_text("Произошла ошибка при обработке ответа. Попробуйте ещё раз.")
        return

    if context.user_data.get("pending_price") is not None:
        try:
            await price_followup_listener(update, context)
        except Exception:
            logger.exception("Unhandled error in price_followup_listener")
            await update.message.reply_text("Произошла ошибка при обработке ответа. Попробуйте ещё раз.")
        return

    return


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled error", exc_info=context.error)


# -----------------------------
# Telegram app builder
# -----------------------------
def build_telegram_app() -> Application:
    tga = Application.builder().token(BOT_TOKEN).build()

    # 1) Предварительный сброс ожиданий на любую команду.
    # Важно: block=False, иначе команды не будут выполняться.
    tga.add_handler(MessageHandler(filters.COMMAND, _pre_command_cancel, block=False), group=0)

    # 2) Команды
    tga.add_handler(CommandHandler("start", start), group=1)
    tga.add_handler(CommandHandler("help", help_cmd), group=1)
    tga.add_handler(CommandHandler("balance", balance), group=1)
    tga.add_handler(CommandHandler("price", price), group=1)
    tga.add_handler(CommandHandler("pay", pay), group=1)
    tga.add_handler(CommandHandler("ops", ops), group=1)
    tga.add_handler(CommandHandler("sell", sell), group=1)

    # 3) Один listener для ответов на интерактивные команды
    tga.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _unified_text_listener), group=2)

    # 4) Ошибки в логи
    tga.add_error_handler(_on_error)

    return tga


async def _telegram_runner():
    global tg_app
    tg_app = build_telegram_app()
    await tg_app.initialize()
    await tg_app.start()
    if tg_app.updater is None:
        raise RuntimeError("Telegram Application.updater is None. Проверь версию python-telegram-bot.")
    await tg_app.updater.start_polling()
    await tg_app.updater.idle()


@app.on_event("startup")
async def _on_startup():
    global _tg_task
    logger.info("Starting Telegram bot polling in background...")
    _tg_task = asyncio.create_task(_telegram_runner())


@app.on_event("shutdown")
async def _on_shutdown():
    global _tg_task, tg_app
    logger.info("Stopping Telegram bot...")
    try:
        if tg_app and tg_app.updater:
            await tg_app.updater.stop()
        if tg_app:
            await tg_app.stop()
            await tg_app.shutdown()
    finally:
        if _tg_task:
            _tg_task.cancel()


@app.get("/")
def root():
    return {"status": "ok"}


def main():
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("bot:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
