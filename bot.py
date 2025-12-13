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
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# Команды (папка commands/ должна содержать __init__.py)
from commands.help import help_cmd
from commands.balance import balance, init_balance_helpers
from commands.price import price, price_followup_listener, init_price_helpers
from commands.pay import pay, init_pay_helpers
from commands.ops import ops, init_ops_helpers


# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
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

# Эти листы нужны не всегда — команды будут подключаться только если они заданы
GID_PRICES = optional_env("GID_PRICES")
GID_OPS = optional_env("GID_OPS")


# -----------------------------
# Общие хелперы (таблица/форматирование)
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
    # 12,00 -> 12 ; 12,50 -> 12,5 ; 12,75 -> 12,75
    s = _fmt_amount_comma2(amount)
    if s.endswith(",00"):
        return s[:-3]
    s = s.rstrip("0")
    if s.endswith(","):
        s = s[:-1]
    return s


def _find_account(rows, username: str):
    u = normalize(username)
    for r in rows:
        if normalize(str(r.get("Username", ""))) == u:
            return r
    return None


def _load_accounts_rows():
    return fetch_rows(GID_ACCOUNTS)


# -----------------------------
# /price helpers
# -----------------------------
def _split_query_and_qty(text: str):
    # "алмаз" -> ("алмаз", None) ; "алмаз 10" -> ("алмаз", 10)
    raw = (text or "").strip()
    if not raw:
        return "", None
    m = re.match(r"^(.*?)(?:\s+(\d+))?$", raw)
    if not m:
        return raw, None
    name = (m.group(1) or "").strip()
    qty_str = m.group(2)
    qty = int(qty_str) if qty_str is not None else None
    if qty is not None and qty <= 0:
        qty = None
    return name, qty


def _money_to_decimal(value) -> Decimal:
    return _parse_balance_to_decimal(value)


def _fmt_total(product_name: str, unit_price: Decimal, qty: int | None) -> str:
    if qty is None:
        return f"{product_name} = {_fmt_amount_trim(unit_price)} джк"
    total = (unit_price * Decimal(qty)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{product_name} ({qty}) = {_fmt_amount_trim(total)} джк"


def lookup_price_by_product_name(query: str):
    if not GID_PRICES:
        return None

    q = normalize(query)
    if not q:
        return None

    rows = fetch_rows(GID_PRICES)

    name_cols = ("Название товара", "Название в игре", "Название", "Товар")
    price_cols = ("Текущая цена", "Цена", "Стоимость", "Cost", "Price")

    for r in rows:
        name_val = None
        for c in name_cols:
            v = str(r.get(c) or "").strip()
            if v:
                name_val = v
                break
        if not name_val or normalize(name_val) != q:
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


# -----------------------------
# /ops helpers
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
    # Колонки листа "Операции": Название, Операция, Число, Сумма, Пользователь, Дата
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
# (пока оставляем, чтобы не ломать существующие команды;
#  дальше мы сможем переносить логику внутрь самих команд)
# -----------------------------
init_balance_helpers(_load_accounts_rows, _find_account, _parse_balance_to_decimal)

# price и ops подключаем только если заданы нужные GID
if GID_PRICES:
    init_price_helpers(lookup_price_by_product_name, _split_query_and_qty, _money_to_decimal, _fmt_total)
else:
    logger.warning("GID_PRICES is not set: /price будет недоступна.")

if GID_OPS:
    init_ops_helpers(_load_accounts_rows, _find_account, _fetch_ops_rows, _parse_date_safe, _format_op_line)
else:
    logger.warning("GID_OPS is not set: /ops будет недоступна.")


# pay-конфиг читаем явно, но делаем опциональным — чтобы bot.py не падал, пока ты перекладываешь pay внутрь команды
FORM_PAY_ID = optional_env("FORM_PAY_ID")
FORM_PAY_ENTRY_SENDER = optional_env("FORM_PAY_ENTRY_SENDER")
FORM_PAY_ENTRY_RECIPIENT = optional_env("FORM_PAY_ENTRY_RECIPIENT")
FORM_PAY_ENTRY_SUM = optional_env("FORM_PAY_ENTRY_SUM")
FORM_PAY_POST_URL = f"https://docs.google.com/forms/d/e/{FORM_PAY_ID}/formResponse" if FORM_PAY_ID else None

_pay_ready = all([FORM_PAY_ID, FORM_PAY_ENTRY_SENDER, FORM_PAY_ENTRY_RECIPIENT, FORM_PAY_ENTRY_SUM, FORM_PAY_POST_URL])
if _pay_ready:
    init_pay_helpers(
        FORM_PAY_ID,
        FORM_PAY_ENTRY_SENDER,
        FORM_PAY_ENTRY_RECIPIENT,
        FORM_PAY_ENTRY_SUM,
        FORM_PAY_POST_URL,
        normalize,
        _parse_amount_arg,
        _load_accounts_rows,
        _find_account,
        _parse_balance_to_decimal,
        _fmt_amount_comma2,
    )
else:
    logger.warning("FORM_PAY_* не настроены: /pay будет недоступна.")


# -----------------------------
# Telegram app builder
# -----------------------------
def build_telegram_app() -> Application:
    tga = Application.builder().token(BOT_TOKEN).build()

    tga.add_handler(CommandHandler("start", start))
    tga.add_handler(CommandHandler("help", help_cmd))
    tga.add_handler(CommandHandler("balance", balance))

    if GID_PRICES:
        tga.add_handler(CommandHandler("price", price))
        tga.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, price_followup_listener))

    if _pay_ready:
        tga.add_handler(CommandHandler("pay", pay))

    if GID_OPS:
        tga.add_handler(CommandHandler("ops", ops))

    return tga


async def _telegram_runner():
    global tg_app
    tg_app = build_telegram_app()

    await tg_app.initialize()
    await tg_app.start()

    if tg_app.updater is None:
        raise RuntimeError("Telegram Application.updater is None. Проверь версию python-telegram-bot (polling должен поддерживаться).")

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
