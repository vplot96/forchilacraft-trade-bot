import os
import json
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import gspread
from google.oauth2.service_account import Credentials

# Load env (locally); on hosting, envs provided by platform
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SHEET_ID = os.getenv("SHEET_ID")
SHEET_TAB = os.getenv("SHEET_TAB", "Счета")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not SHEET_ID:
    raise RuntimeError("SHEET_ID is not set")
if not GOOGLE_CREDENTIALS_JSON:
    raise RuntimeError("GOOGLE_CREDENTIALS_JSON is not set")

# Build credentials from JSON in env (no file on disk needed)
try:
    creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
except json.JSONDecodeError as e:
    raise RuntimeError("GOOGLE_CREDENTIALS_JSON is not valid JSON") from e

scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]
credentials = Credentials.from_service_account_info(creds_info, scopes=scopes)

gc = gspread.authorize(credentials)
sh = gc.open_by_key(SHEET_ID)
ws = sh.worksheet(SHEET_TAB)  # open by title

def normalize(s: str) -> str:
    return (s or "").strip().lower()

def lookup_balance(player_name: str):
    """Return (name, balance) or None. Prefers exact match; falls back to first partial."""
    name_q = normalize(player_name)
    # Read all as records (expects headers in first row: Имя, Баланс)
    rows = ws.get_all_records()  # list[dict]
    exact = None
    partial = None
    for row in rows:
        name = str(row.get("Имя", "")).strip()
        bal = row.get("Баланс", "")
        if normalize(name) == name_q:
            exact = (name, bal)
            break
        if partial is None and name_q and name_q in normalize(name):
            partial = (name, bal)
    return exact or partial

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Команда: /balance <имя>. Пример: /balance Алиса")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Использование: /balance <имя>. Лист: '{SHEET_TAB}'.")

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Укажите имя: /balance <имя>")
        return
    query_name = " ".join(context.args).strip()
    try:
        found = lookup_balance(query_name)
    except Exception as e:
        await update.message.reply_text(f"Ошибка доступа к таблице: {e}")
        return
    if not found:
        await update.message.reply_text(f"Игрок '{query_name}' не найден на листе '{SHEET_TAB}'.")
        return
    name, bal = found
    await update.message.reply_text(f"Баланс {name}: {bal}")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("balance", balance))
    app.run_polling()

if __name__ == "__main__":
    main()
