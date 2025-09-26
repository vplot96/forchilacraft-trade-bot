import os, csv, io, requests, re, difflib
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from decimal import Decimal, ROUND_HALF_UP  # (для /pay)

# Load env
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SHEET_ID = os.getenv("SHEET_ID")
GID_ACCOUNTS = os.getenv("GID_ACCOUNTS")  # "Счета"
GID_PRICES = os.getenv("GID_PRICES")      # "Товары"

# --- ПЕРЕМЕННЫЕ ДЛЯ /pay (из окружения Railway) ---
FORM_ID = os.getenv("FORM_ID")
ENTRY_USER = os.getenv("ENTRY_USER")
ENTRY_SUM = os.getenv("ENTRY_SUM")
FORM_POST_URL = f"https://docs.google.com/forms/d/e/{FORM_ID}/formResponse" if FORM_ID else None

# future placeholder
GID_OPS = os.getenv("GID_OPS")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not SHEET_ID:
    raise RuntimeError("SHEET_ID is not set")
if not GID_ACCOUNTS:
    raise RuntimeError("GID_ACCOUNTS is not set")
# Проверим переменные формы, чтобы /pay мог работать предсказуемо
if not FORM_ID or not ENTRY_USER or not ENTRY_SUM:
    # Не падаем при запуске, но /pay вернёт понятную ошибку
    pass

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

# ---------- /price (fuzzy by 'Название товара' on "Товары") ----------

def lookup_price_by_product_name(query: str, cutoff: float = 0.45):
    if not GID_PRICES:
        raise RuntimeError("GID_PRICES is not set (лист 'Товары')")
    rows = fetch_rows(GID_PRICES)
    q = normalize(query)
    # Список нормализованных названий, и карта нормализованное -> оригинальная строка
    names_norm = []
    index_map = {}
    for i, row in enumerate(rows):
        title = str(row.get("Название товара", "")).strip()
        n = normalize(title)
        names_norm.append(n)
        # если одинаковые нормализованные имена, пусть остаётся первое вхождение
        if n not in index_map:
            index_map[n] = i

    best = difflib.get_close_matches(q, names_norm, n=1, cutoff=cutoff)
    if not best:
        return None
    chosen_norm = best[0]
    row = rows[index_map[chosen_norm]]
    display_name = str(row.get("Название товара", "")).strip() or query
    price = row.get("Текущая цена", "")
    return display_name, price

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /price <название товара>")
        return
    query = " ".join(context.args).strip()
    try:
        found = lookup_price_by_product_name(query)
    except Exception as e:
        await update.message.reply_text(f"Ошибка доступа к таблице: {e}")
        return

    if not found:
        await update.message.reply_text(f"Товар, похожий на '{query}', не найден.")
        return

    display_name, price = found
    await update.message.reply_text(f"{display_name} = {price} джк")

# ---------- /pay (по Username на 'Счета' + пуш в Google Form) ----------

def _parse_balance_to_decimal(value) -> Decimal:
    """
    Преобразует значение из колонки 'Баланс' в Decimal.
    Поддерживает '1234', '1234,56', '1234.56'.
    """
    s = str(value or "").strip().replace(" ", "").replace(",", ".")
    if not s:
        return Decimal("0.00")
    try:
        q = Decimal(s)
    except Exception:
        return Decimal("0.00")
    return q.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def _parse_amount_arg(raw: str) -> Decimal:
    """
    Парсит сумму пользователя (разрешая ',' и '.'), оставляя ровно 2 знака.
    Бросает ValueError при неверном вводе.
    """
    cleaned = re.sub(r"[^\d,.\-]", "", raw).replace(",", ".")
    if cleaned in ("", ".", "-", "-.", ".-"):
        raise ValueError("empty")
    q = Decimal(cleaned)
    return q.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def _fmt_amount_comma2(amount: Decimal) -> str:
    """Возвращает строку вида '12,34'."""
    return f"{amount:.2f}".replace(".", ",")

def _load_accounts_rows():
    return fetch_rows(GID_ACCOUNTS)

def _find_account(rows, username: str):
    u = normalize(username)
    for r in rows:
        if normalize(str(r.get("Username", ""))) == u:
            return r
    return None

async def pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Проверка наличия переменных формы
    if not FORM_ID or not ENTRY_USER or not ENTRY_SUM:
        await update.message.reply_text("Не настроены параметры формы перевода. Обратитесь к администратору.")
        return

    # Аргументы
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Использование: /pay <username> <sum>")
        return

    recipient = context.args[0].strip()
    amount_raw = " ".join(context.args[1:]).strip()

    sender_username = (update.effective_user.username or "").strip()
    if not sender_username:
        await update.message.reply_text("У вас не задан Telegram username.")
        return

    # Валидация суммы
    try:
        amount = _parse_amount_arg(amount_raw)
    except Exception:
        await update.message.reply_text("Некорректная сумма. Пример: 10 или 12,50")
        return
    if amount <= 0:
        await update.message.reply_text("Сумма должна быть больше 0.")
        return

    # Читаем список аккаунтов
    try:
        rows = _load_accounts_rows()
    except Exception as e:
        await update.message.reply_text(f"Ошибка доступа к таблице: {e}")
        return

    sender_row = _find_account(rows, sender_username)
    if not sender_row:
        await update.message.reply_text(f"Аккаунт {sender_username} не найден.")
        return

    recipient_row = _find_account(rows, recipient)
    if not recipient_row:
        await update.message.reply_text(f"Аккаунт {recipient} не найден.")
        return

    # Проверяем баланс
    sender_balance = _parse_balance_to_decimal(sender_row.get("Баланс"))
    if sender_balance < amount:
        await update.message.reply_text("На балансе недостаточно средств.")
        return

    # Пуш в форму
    payload = {
        ENTRY_USER: recipient,
        ENTRY_SUM: _fmt_amount_comma2(amount),
    }

    try:
        resp = requests.post(
            FORM_POST_URL,
            data=payload,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ForchilacraftBot/1.0)"},
            timeout=10,
        )
        ok = resp.status_code in (200, 302)
    except requests.RequestException:
        ok = False

    if ok:
        await update.message.reply_text("Ваш перевод подтверждён.")
    else:
        await update.message.reply_text("Не удалось отправить перевод. Попробуйте позже.")

# ---------- common ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Команды:\n/balance — ваш баланс по username\n/price <название товара> — цена товара (нечёткий поиск)\n/pay <username> <sum> — перевод средств")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Доступные команды: /start, /help, /balance, /price <название товара>, /pay <username> <sum>")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("pay", pay))  # <--- добавили обработчик /pay
    app.run_polling()

if __name__ == "__main__":
    main()
