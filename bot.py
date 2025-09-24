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

def lookup_balance(player_name: str):
    rows = fetch_rows(GID_ACCOUNTS)
    name_q = normalize(player_name)
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
    await update.message.reply_text("Доступные команды: /start, /help, /balance <имя>")

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
        await update.message.reply_text(f"Игрок '{query_name}' не найден.")
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
