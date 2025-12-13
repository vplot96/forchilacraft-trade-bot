#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import csv
import logging
from io import StringIO
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# Команды (лежать в папке commands/ с __init__.py)
from commands.help import help_cmd
from commands.balance import balance, init_balance_helpers
from commands.price import price, price_followup_listener, init_price_helpers
from commands.pay import pay, init_pay_helpers
from commands.ops import ops, init_ops_helpers


# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# Env variables
BOT_TOKEN = os.getenv("BOT_TOKEN")

SHEET_ID = os.getenv("SHEET_ID")
GID_ACCOUNTS = os.getenv("GID_ACCOUNTS")
GID_PRICES = os.getenv("GID_PRICES")
GID_OPS = os.getenv("GID_OPS")

FORM_ID = os.getenv("FORM_ID")
ENTRY_SENDER = os.getenv("ENTRY_SENDER")         # Отправитель
ENTRY_RECIPIENT = os.getenv("ENTRY_RECIPIENT")   # Получатель
ENTRY_SUM = os.getenv("ENTRY_SUM")               # Сумма
FORM_POST_URL = f"https://docs.google.com/forms/d/e/{FORM_ID}/formResponse" if FORM_ID else None


# Обязательные переменные (остальные — проверяются командами при необходимости)
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not SHEET_ID:
    raise RuntimeError("SHEET_ID is not set")
if not GID_ACCOUNTS:
    raise RuntimeError("GID_ACCOUNTS is not set")


# -----------------------------
# Общие хелперы
# -----------------------------
def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def csv_url_for(gid: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}"


def fetch_rows(gid: str):
    if not gid:
        raise RuntimeError("GID is not set")
    url = csv_url_for(gid)
    r = requests.get(url, timeout=15)
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
    """12,00 -> 12 ; 12,50 -> 12,5 ; 12,75 -> 12,75"""
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
# Хелперы для /price
# -----------------------------
def _split_query_and_qty(text: str):
    """"алмаз" -> ("алмаз", None) ; "алмаз 10" -> ("алмаз", 10)"""
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
    """Возвращает (display_name, unit_price_decimal) или None."""
    if not GID_PRICES:
        raise RuntimeError("GID_PRICES is not set")

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


# -----------------------------
# Хелперы для /ops
# -----------------------------
def _fetch_ops_rows():
    if not GID_OPS:
        raise RuntimeError("GID_OPS is not set")
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
# Команда /start (оставляем в bot.py)
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

init_pay_helpers(
    FORM_ID,
    ENTRY_SENDER,
    ENTRY_RECIPIENT,
    ENTRY_SUM,
    FORM_POST_URL,
    normalize,
    _parse_amount_arg,
    _load_accounts_rows,
    _find_account,
    _parse_balance_to_decimal,
    _fmt_amount_comma2,
)

init_ops_helpers(
    _load_accounts_rows,
    _find_account,
    _fetch_ops_rows,
    _parse_date_safe,
    _format_op_line,
)


# -----------------------------
# Entrypoint
# -----------------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, price_followup_listener))
    app.add_handler(CommandHandler("pay", pay))
    app.add_handler(CommandHandler("ops", ops))

    app.run_polling()


if __name__ == "__main__":
    main()
