# -*- coding: utf-8 -*-
"""
Forchilacraft Trade Bot — PTB v20+ compatible
Changes vs previous:
- Migrated from Updater/Dispatcher to Application (python-telegram-bot 20+)
- Handlers are async
- Keeps: CSV cache, username helper, /balance, /price, /pay via Google Form
"""

import os
import io
import csv
import time
import decimal
from decimal import Decimal
import logging
import requests

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------- Config from ENV ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]
SHEET_ID = os.environ["SHEET_ID"]
GID_ACCOUNTS = os.environ["GID_ACCOUNTS"]
GID_PRICES = os.environ["GID_PRICES"]

# Google Form config (required for /pay to actually register transfers)
FORM_ID = os.environ.get("FORM_ID", "").strip()
ENTRY_USER = os.environ.get("ENTRY_USER", "").strip()  # e.g. "entry.2015621373"
ENTRY_SUM  = os.environ.get("ENTRY_SUM", "").strip()   # e.g. "entry.40410086"

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("forchilacraft-bot")

# ---------- CSV cache ----------
_CSV_CACHE = {}  # {gid: (ts, text)}
_CSV_TTL_SEC = int(os.getenv("CSV_CACHE_TTL", "60"))

def _csv_url(gid: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}"

def fetch_csv_cached(gid: str, timeout=8) -> str:
    now = time.time()
    cached = _CSV_CACHE.get(gid)
    if cached and (now - cached[0] < _CSV_TTL_SEC):
        return cached[1]
    resp = requests.get(_csv_url(gid), timeout=timeout)
    resp.raise_for_status()
    text = resp.text
    _CSV_CACHE[gid] = (now, text)
    return text

def read_csv_as_rows(gid: str):
    buf = io.StringIO(fetch_csv_cached(gid))
    return list(csv.DictReader(buf))

def invalidate_csv_cache_for_gid(gid: str):
    _CSV_CACHE.pop(gid, None)

# ---------- Helpers ----------
def normalize_username(u: str) -> str:
    return (u or "").strip().lstrip("@").lower()

def get_sender_username_from_tg(update: Update) -> str:
    u = update.effective_user.username if update and update.effective_user else None
    return normalize_username(u) if u else ""

def load_accounts_index():
    """
    Returns:
      users_by_username: {username -> row_dict} (from 'Счета' sheet)
      row_index: {username -> 1-based row_number}
      header_index: {colname_lower -> 1-based col_number}
    Expected cols at least: Username, Balance (case-insensitive).
    """
    rows = read_csv_as_rows(GID_ACCOUNTS)
    users_by_username, row_index = {}, {}
    header = rows[0].keys() if rows else []
    header_index = {h.lower(): i+1 for i, h in enumerate(header)}
    for i, r in enumerate(rows, start=2):  # header at row 1
        u = normalize_username(r.get("Username") or r.get("username") or "")
        if u:
            users_by_username[u] = r
            row_index[u] = i
    return users_by_username, row_index, header_index

def parse_balance(value: str) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value).replace(",", ".")).quantize(Decimal("0.01"))

def format_amount(d: Decimal) -> str:
    q = d.quantize(Decimal("0.01"))
    if q == 0:
        return "0"
    s = format(q, "f")           # например "10.50"
    if "." in s:
        s = s.rstrip("0").rstrip(".")  # -> "10.5" или "10"
    return s

# ---------- Google Form submit ----------
def _form_action_url() -> str:
    if not FORM_ID:
        return ""
    return f"https://docs.google.com/forms/d/e/{FORM_ID}/formResponse"

def submit_to_google_form(username: str, amount: Decimal, timeout=8) -> bool:
    """
    POST to Google Form. Returns True for 200/302.
    """
    action = _form_action_url()
    if not action or not ENTRY_USER or not ENTRY_SUM:
        return False

    payload = {
        ENTRY_USER: username,
        ENTRY_SUM:  format_amount(amount),
    }
    headers = {
        "User-Agent": "forchilacraft-trade-bot/1.0",
        "Referer": f"https://docs.google.com/forms/d/e/{FORM_ID}/viewform",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        r = requests.post(action, data=payload, headers=headers, timeout=timeout, allow_redirects=False)
        ok = r.status_code in (200, 302)
        if not ok:
            log.warning("Form submit failed: status=%s text=%s", r.status_code, r.text[:200])
        return ok
    except Exception as e:
        log.exception("Form submit error: %s", e)
        return False

# ---------- Commands (async) ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Доступные команды: /balance, /price <товар>, /pay <кому> <сумма>")

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender = get_sender_username_from_tg(update)
    if not sender:
        await update.message.reply_text("У вас не задан username в Telegram. Задайте его в настройках профиля.")
        return
    users, _, _ = load_accounts_index()
    row = users.get(sender)
    if not row:
        await update.message.reply_text(f"Аккаунт {sender} не найден.")
        return
    bal = parse_balance(row.get("Balance") or row.get("balance"))
    await update.message.reply_text(f"Баланс {sender}: {format_amount(bal)} джк")

async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Simple price lookup by exact (case-insensitive) name,
    then prefix match if no exact match.
    Expected headers on 'Товары': 'Название товара' and 'Цена' (case-insensitive).
    """
    args = context.args or []
    if not args:
        await update.message.reply_text("Использование: /price <название товара>")
        return
    query = " ".join(args).strip().lower()
    if not query:
        await update.message.reply_text("Использование: /price <название товара>")
        return

    rows = read_csv_as_rows(GID_PRICES)
    name_keys = ["Название товара", "название товара", "name", "название"]
    price_keys = ["Цена", "цена", "price"]

    def get_col(row, keys):
        for k in keys:
            if k in row:
                return row.get(k)
            for kk in row.keys():
                if kk.lower() == k.lower():
                    return row.get(kk)
        return None

    exact = None
    for r in rows:
        name = get_col(r, name_keys)
        if name and name.strip().lower() == query:
            exact = r
            break
    if exact:
        price = get_col(exact, price_keys)
        await update.message.reply_text(f"{get_col(exact, name_keys)} = {price} джк")
        return

    suggestions = []
    for r in rows:
        name = get_col(r, name_keys)
        if name and name.strip().lower().startswith(query):
            suggestions.append(r)
            if len(suggestions) >= 3:
                break
    if suggestions:
        lines = [f"{get_col(r, name_keys)} = {get_col(r, price_keys)} джк" for r in suggestions]
        await update.message.reply_text("\n".join(lines))
        return

    await update.message.reply_text("Товар не найден. Попробуйте точнее.")

async def cmd_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /pay <recipient_username> <amount>
    - Sender inferred from Telegram username, must exist in 'Счета'.Username
    - Recipient resolved by argument in the same column
    - Amount must be > 0 and <= sender balance
    - Writes via Google Form submit (FORM_ID, ENTRY_USER, ENTRY_SUM)
    - After submit: invalidate cache and try to read refreshed balance with short retry
    """
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text("Использование: /pay <кому> <сумма>\nПример: /pay @alice 15 или /pay alice 15.5")
        return

    recipient_raw = args[0]
    amount_raw = " ".join(args[1:])

    sender = get_sender_username_from_tg(update)
    if not sender:
        await update.message.reply_text("У вас не задан username в Telegram. Задайте его в настройках профиля.")
        return

    recipient = normalize_username(recipient_raw)

    try:
        amount = Decimal(amount_raw.replace(",", "."))
    except Exception:
        await update.message.reply_text("Некорректная сумма. Пример: 10 или 10,5")
        return
    if amount <= 0:
        await update.message.reply_text("Сумма должна быть больше нуля.")
        return
    amount = amount.quantize(Decimal("0.01"), rounding=decimal.ROUND_HALF_UP)

    users, _, _ = load_accounts_index()
    sender_row = users.get(sender)
    if not sender_row:
        await update.message.reply_text(f"Аккаунт {sender} не найден.")
        return
    recipient_row = users.get(recipient)
    if not recipient_row:
        await update.message.reply_text(f"Аккаунт {recipient} не найден.")
        return

    sender_bal = parse_balance(sender_row.get("Balance") or sender_row.get("balance"))
    if sender_bal < amount:
        await update.message.reply_text(f"Недостаточно средств. Доступно: {format_amount(sender_bal)} джк.")
        return

    if not FORM_ID or not ENTRY_USER or not ENTRY_SUM:
        await update.message.reply_text("Переводы пока не настроены (не заданы FORM_ID/ENTRY_USER/ENTRY_SUM).")
        return

    ok = submit_to_google_form(recipient, amount)
    if not ok:
        await update.message.reply_text("Не удалось отправить запрос в Google Form. Попробуйте позже.")
        return

    invalidate_csv_cache_for_gid(GID_ACCOUNTS)

    new_sender_balance = None
    for _ in range(6):  # ~5 seconds total
        try:
            users_after, _, _ = load_accounts_index()
            row_after = users_after.get(sender)
            if row_after:
                new_sender_balance = parse_balance(row_after.get("Balance") or row_after.get("balance"))
                break
        except Exception:
            pass
        time.sleep(0.8)

    if new_sender_balance is None:
        await update.message.reply_text(
            f"Перевод отправлен: {format_amount(amount)} джк → {recipient}.\n"
            "Обновление баланса появится в таблице через несколько секунд."
        )
    else:
        await update.message.reply_text(
            f"Перевод выполнен: {format_amount(amount)} джк → {recipient}\n"
            f"Ваш новый баланс: {format_amount(new_sender_balance)} джк."
        )

# ---------- Bootstrap ----------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("pay", cmd_pay))

    log.info("Bot started. Commands: /start /balance /price /pay")
    app.run_polling()

if __name__ == "__main__":
    main()
