
import os, csv, io, requests, re
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Load env
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SHEET_ID = os.getenv("SHEET_ID")
GID_ACCOUNTS = os.getenv("GID_ACCOUNTS")  # "Счета"
GID_PRICES = os.getenv("GID_PRICES")      # "Товары" (required for /price)

# future placeholder
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
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

# ---------- /balance (by Username on "Счета") ----------

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
    await update.message.reply_text(f"Ваш баланс: {bal} джк")

# ---------- /price (by 'Название в игре' on "Товары") ----------

def lookup_price_by_game_name(game_name: str):
    if not GID_PRICES:
        raise RuntimeError("GID_PRICES is not set (лист 'Товары')")
    rows = fetch_rows(GID_PRICES)
    q = normalize(game_name)
    for row in rows:
        name_in_game = str(row.get("Название в игре", "")).strip()
        if normalize(name_in_game) == q:
            display_name = str(row.get("Название товара", "")).strip() or name_in_game
            price = row.get("Текущая цена", "")
            return display_name, price
    return None

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /price <название в игре>")
        return
    game_name = " ".join(context.args).strip()  # поддерживает пробелы
    try:
        found = lookup_price_by_game_name(game_name)
    except Exception as e:
        await update.message.reply_text(f"Ошибка доступа к таблице: {e}")
        return

    if not found:
        await update.message.reply_text(f"Товар '{game_name}' не найден.")
        return

    display_name, price = found
    await update.message.reply_text(f"{display_name} = {price} джк")

# ---------- common ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Команды:\n/balance — ваш баланс по username\n/price <название в игре> — цена товара")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Доступные команды: /start, /help, /balance, /price <название в игре>")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("price", price))
    app.run_polling()

if __name__ == "__main__":
    main()
