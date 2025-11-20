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
from cmd_help import help_cmd
from cmd_balance import balance, init_balance_helpers
from cmd_price import price, price_followup_listener, init_price_helpers
from cmd_pay import pay, init_pay_helpers
from cmd_ops import ops, init_ops_helpers

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
ENTRY_SENDER = os.getenv("ENTRY_SENDER")       # Отправитель
ENTRY_RECIPIENT = os.getenv("ENTRY_RECIPIENT") # Получатель
ENTRY_SUM = os.getenv("ENTRY_SUM")             # Сумма
FORM_POST_URL = f"https://docs.google.com/forms/d/e/{FORM_ID}/formResponse" if FORM_ID else None

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not SHEET_ID:
    raise RuntimeError("SHEET_ID is not set")
if not GID_ACCOUNTS:
    raise RuntimeError("GID_ACCOUNTS is not set")

# Helpers
def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def csv_url_for(gid: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}"

def fetch_rows(gid: str):
    url = csv_url_for(gid)
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    text = r.content.decode('utf-8-sig')
    reader = csv.DictReader(StringIO(text))
    return list(reader)

def _parse_balance_to_decimal(value) -> Decimal:
    s = str(value or "").strip().replace(" ", "").replace(",", ".")
    if not s:
        return Decimal("0.00")
    try:
        q = Decimal(s)
    except Exception:
        return Decimal("0.00")
    return q.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def _parse_amount_arg(raw: str) -> Decimal:
    cleaned = re.sub(r"[^\d,.\-]", "", raw).replace(",", ".")
    if cleaned in ("", ".", "-", "-.", ".-"):
        raise ValueError("empty")
    q = Decimal(cleaned)
    return q.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def _fmt_amount_comma2(amount: Decimal) -> str:
    return f"{amount:.2f}".replace(".", ",")

def _find_account(rows, username: str):
    u = normalize(username)
    for r in rows:
        if normalize(str(r.get("Username", ""))) == u:
            return r
    return None

def _load_accounts_rows():
    return fetch_rows(GID_ACCOUNTS)

# Commands
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Готов к работе! Введите /help для вывода списка команд.")














# Инициализация хелперов для команд
init_balance_helpers(_load_accounts_rows, _find_account, _parse_balance_to_decimal)
init_price_helpers(lookup_price_by_product_name, _split_query_and_qty, _money_to_decimal, _fmt_total)
init_pay_helpers(FORM_ID, ENTRY_SENDER, ENTRY_RECIPIENT, ENTRY_SUM, FORM_POST_URL,
                 normalize, _parse_amount_arg, _load_accounts_rows, _find_account,
                 _parse_balance_to_decimal, _fmt_amount_comma2)
init_ops_helpers(_load_accounts_rows, _find_account, _fetch_ops_rows, _parse_date_safe, _format_op_line)

# Entrypoint
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
