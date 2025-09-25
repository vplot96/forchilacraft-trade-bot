
import os, csv, io, requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Load env
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SHEET_ID = os.getenv("SHEET_ID")
GID_ACCOUNTS = os.getenv("GID_ACCOUNTS")

# future placeholders
GID_PRICES = os.getenv("GID_PRICES")
GID_OPS = os.getenv("GID_OPS")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not SHEET_ID:
    raise RuntimeError("SHEET_ID is not set")
if not GID_ACCOUNTS:
    raise RuntimeError("GID_ACCOUNTS is not set")

def csv_url_for(gid: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}"

def fetch_rows(gid: str):
    url = csv_url_for(gid)
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    text = r.content.decode('utf-8-sig')
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)

def normalize(s: str) -> str:
    return (s or "").strip().lower()

def lookup_balance_by_username(username: str):
    rows = fetch_rows(GID_ACCOUNTS)
    u = normalize(username)
    for row in rows:
        cell = str(row.get("Username", "")).strip()
        if normalize(cell) == u:
            name = str(row.get("Имя", "")).strip()
            bal = row.get("Баланс", "")
            return name or username, bal
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Команда: /balance — показать ваш баланс по Telegram username.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Использование: /balance (без аргументов). Username должен быть в колонке 'Username' листа 'Счета'.")

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username  # может быть None
    if not username:
        await update.message.reply_text("У вас не задан Telegram username (@...). Задайте его в настройках Telegram и обратитесь к администратору, чтобы он добавил вас в таблицу.")
        return

    try:
        found = lookup_balance_by_username(username)
    except Exception as e:
        await update.message.reply_text(f"Ошибка доступа к таблице: {e}")
        return

    if not found:
        await update.message.reply_text(f"Пользователь @{username} не найден в таблице. Обратитесь к администратору.")
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
