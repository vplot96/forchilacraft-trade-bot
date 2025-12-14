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
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

print("IMPORT: bot.py loaded")
logger.info("IMPORT: logger works")

app = FastAPI()

tg_app: Application | None = None
tg_task: asyncio.Task | None = None
TELEGRAM_STARTED = False

def require_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"{name} is not set")
    return v

def optional_env(name: str) -> str | None:
    v = os.getenv(name)
    return v if v else None

BOT_TOKEN = require_env("BOT_TOKEN")
SHEET_ID = require_env("SHEET_ID")
GID_ACCOUNTS = require_env("GID_ACCOUNTS")

GID_PRICES = optional_env("GID_PRICES")
GID_OPS = optional_env("GID_OPS")

def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def csv_url_for(gid: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}"

def fetch_rows(gid: str):
    r = requests.get(csv_url_for(gid), timeout=20)
    r.raise_for_status()
    reader = csv.DictReader(StringIO(r.content.decode("utf-8-sig")))
    return list(reader)

def _parse_decimal(value) -> Decimal:
    s = str(value or "").strip().replace(" ", "").replace(",", ".")
    try:
        return Decimal(s)
    except Exception:
        return Decimal("0")

def _parse_balance_to_decimal(value) -> Decimal:
    return _parse_decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def _find_account(rows, username: str):
    u = normalize(username)
    for r in rows:
        if normalize(str(r.get("Username", ""))) == u:
            return r
    return None

def _load_accounts_rows():
    return fetch_rows(GID_ACCOUNTS)

from commands.help import help_cmd
from commands.balance import balance, init_balance_helpers
from commands.price import price, price_followup_listener, init_price_helpers
from commands.pay import pay as pay_cmd, init_pay_helpers
from commands.ops import ops, init_ops_helpers
from commands.sell import sell, sell_confirm_listener

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Готов к работе! Введите /help для вывода списка команд.")

init_balance_helpers(_load_accounts_rows, _find_account, _parse_balance_to_decimal)

def _clear_pending_all(context: ContextTypes.DEFAULT_TYPE):
    for k in list(context.user_data.keys()):
        if isinstance(k, str) and k.startswith("pending_"):
            context.user_data.pop(k, None)

async def _pre_command_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context:
        _clear_pending_all(context)

async def _unified_text_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or context is None:
        return
    if context.user_data.get("pending_sell"):
        await sell_confirm_listener(update, context)
        return
    if context.user_data.get("pending_price"):
        await price_followup_listener(update, context)
        return

async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled error", exc_info=context.error)

def build_telegram_app() -> Application:
    tga = Application.builder().token(BOT_TOKEN).build()
    tga.add_handler(MessageHandler(filters.COMMAND, _pre_command_cancel, block=False), group=0)
    tga.add_handler(CommandHandler("start", start), group=1)
    tga.add_handler(CommandHandler("help", help_cmd), group=1)
    tga.add_handler(CommandHandler("balance", balance), group=1)
    tga.add_handler(CommandHandler("price", price), group=1)
    tga.add_handler(CommandHandler("pay", pay_cmd), group=1)
    tga.add_handler(CommandHandler("ops", ops), group=1)
    tga.add_handler(CommandHandler("sell", sell), group=1)
    tga.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _unified_text_listener), group=2)
    tga.add_error_handler(_on_error)
    return tga

async def _telegram_runner():
    global tg_app
    tg_app = build_telegram_app()
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling()
    await tg_app.updater.idle()

@app.on_event("startup")
async def on_startup():
    global TELEGRAM_STARTED, tg_task
    if TELEGRAM_STARTED:
        logger.warning("Telegram already started, skipping")
        return
    TELEGRAM_STARTED = True
    logger.info("Starting Telegram polling (single instance)")
    tg_task = asyncio.create_task(_telegram_runner())

@app.on_event("shutdown")
async def on_shutdown():
    global tg_app, tg_task
    logger.info("Stopping Telegram bot")
    if tg_app:
        await tg_app.stop()
        await tg_app.shutdown()
    if tg_task:
        tg_task.cancel()

@app.get("/")
def root():
    return {"status": "ok"}
