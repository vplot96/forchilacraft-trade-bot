# -*- coding: utf-8 -*-
"""
Forchilacraft Trade Bot — PTB v20+ (no-cache, robust headers)
Changes in this revision:
- Still NO local caching — every read fetches fresh CSV from Google Sheets.
- Robust column detection: insensitive to case, spaces, NBSP, dashes, underscores, and common Russian synonyms.
- /price uses fuzzy match (substring) + a couple of aliases (e.g., "эндер жемчуг" → "жемчуг края").
"""

import os
import io
import csv
import time
import decimal
from decimal import Decimal
import logging
import requests
import re

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

# ---------- CSV helpers (no cache) ----------
def _csv_url(gid: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}"

def read_csv_as_rows(gid: str, timeout: int = 10):
    """
    Always fetch fresh CSV from Google Sheets and return a list of DictReader rows.
    No caching.
    """
    resp = requests.get(_csv_url(gid), timeout=timeout)
    resp.raise_for_status()
    buf = io.StringIO(resp.text)
    return list(csv.DictReader(buf))

# ---------- Header normalization & picking ----------
_WS = re.compile(r"[\s\u00A0\u2007\u202F_–—\-]+", re.UNICODE)  # spaces, NBSP, thin, underscores, dashes
def _norm(s: str) -> str:
    if s is None:
        return ""
    s = str(s)
    s = s.replace("\u00A0", " ").replace("\u2007", " ").replace("\u202F", " ")  # NBSP variants
    s = s.lower()
    s = s.replace("ё", "е")  # treat ё == е
    s = _WS.sub("", s)       # drop whitespace-like and separators
    return s

def _pick_header(actual_headers, synonyms):
    """
    Try to locate a header among actual_headers using a list of synonyms.
    Returns the exact header string from the CSV if found, else None.
    """
    if not actual_headers:
        return None
    norm_map = { _norm(h): h for h in actual_headers }
    # direct match by normalized equality
    for syn in synonyms:
        n = _norm(syn)
        if n in norm_map:
            return norm_map[n]
    # fuzzy: substring either way
    for h in actual_headers:
        nh = _norm(h)
        for syn in synonyms:
            ns = _norm(syn)
            if ns and (ns in nh or nh in ns):
                return h
    return None

# Common synonyms
USERNAME_SYNS = ["username", "ник", "аккаунт", "логин", "user", "telegram", "tg", "пользователь"]
BALANCE_SYNS  = ["баланс", "balance", "счет", "счёт"]
ITEM_SYNS     = ["название товара", "товар", "предмет", "название", "item", "наименование", "имя"]
PRICE_SYNS    = ["цена", "стоимость", "price", "cost", "ценник"]

# Known aliases for queries
ALIASES = {
    "эндержемчуг": "жемчугкрая",
    "зловещаябутылочка": "зловещаябутылка",
}

# ---------- Helpers ----------
def normalize_username(u: str) -> str:
    return (u or "").strip().lstrip("@").lower()

def get_sender_username_from_tg(update: Update) -> str:
    u = update.effective_user.username if update and update.effective_user else None
    return normalize_username(u) if u else ""

def parse_balance(value: str) -> Decimal:
    if value is None:
        return Decimal("0")
    s = str(value).replace('\xa0', '').replace(' ', '').replace(',', '.')
    if s == '' or s == '.':
        return Decimal("0")
    try:
        return Decimal(s).quantize(Decimal("0.01"))
    except Exception:
        return Decimal("0")

def format_amount(d: Decimal) -> str:
    q = d.quantize(Decimal("0.01"))
    if q == 0:
        return "0"
    if q == q.to_integral():
        s = f"{q.normalize():f}"
        s = s.rstrip('0').rstrip('.')
        return s if s else "0"
    return f"{q:.2f}"

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

# ---------- Data access ----------
def load_accounts_index():
    """
    Build indices from 'Счета' sheet using robust header picking.
    Returns:
      users_by_username: {username -> row_dict}
      row_index: {username -> 1-based row_number}
      cols: {"username": <header>, "balance": <header>}
    """
    rows = read_csv_as_rows(GID_ACCOUNTS)
    header = list(rows[0].keys()) if rows else []
    col_username = _pick_header(header, USERNAME_SYNS)
    col_balance  = _pick_header(header, BALANCE_SYNS)

    if not col_username:
        raise RuntimeError("Не найдена колонка Username/Ник в листе 'Счета'.")
    if not col_balance:
        log.warning("Колонка Баланс не найдена по синонимам; все балансы будут 0.")
    users_by_username, row_index = {}, {}
    for i, r in enumerate(rows, start=2):  # header at 1
        u = normalize_username(r.get(col_username, ""))
        if u:
            users_by_username[u] = r
            row_index[u] = i
    return users_by_username, row_index, {"username": col_username, "balance": col_balance}

def load_prices_table():
    """
    Load 'Товары' (prices) with robust header picking.
    Returns: (rows, {"name": name_col, "price": price_col})
    """
    rows = read_csv_as_rows(GID_PRICES)
    header = list(rows[0].keys()) if rows else []
    col_name  = _pick_header(header, ITEM_SYNS)
    col_price = _pick_header(header, PRICE_SYNS)
    if not col_name or not col_price:
        log.warning("Не удалось надёжно определить колонки с названием и ценой на листе цен.")
    return rows, {"name": col_name, "price": col_price}

# ---------- Commands (async) ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Доступные команды: /balance, /price <товар>, /pay <кому> <сумма>")

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender = get_sender_username_from_tg(update)
    if not sender:
        await update.message.reply_text("У вас не задан username в Telegram. Задайте его в настройках профиля.")
        return
    try:
        users, _, cols = load_accounts_index()
    except Exception as e:
        await update.message.reply_text(f"Ошибка чтения таблицы счетов: {e}")
        return
    row = users.get(sender)
    if not row:
        await update.message.reply_text(f"Аккаунт {sender} не найден.")
        return
    bal_col = cols.get("balance")
    bal_val = row.get(bal_col) if bal_col else None
    bal = parse_balance(bal_val)
    await update.message.reply_text(f"Баланс {sender}: {format_amount(bal)} джк")

async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Fuzzy price lookup:
      - exact (normalized) equality
      - else: substring anywhere (shows up to 5 suggestions)
    """
    args = context.args or []
    if not args:
        await update.message.reply_text("Использование: /price <название товара>")
        return
    query_raw = " ".join(args).strip()
    if not query_raw:
        await update.message.reply_text("Использование: /price <название товара>")
        return

    qn = _norm(query_raw)
    if qn in ALIASES:
        qn = ALIASES[qn]

    rows, cols = load_prices_table()
    name_col, price_col = cols.get("name"), cols.get("price")
    if not name_col or not price_col:
        await update.message.reply_text("Не получилось определить колонки с товарами и ценами. Проверьте заголовки в таблице.")
        return

    # Build normalized name cache
    norm_names = []
    for r in rows:
        name = str(r.get(name_col, "")).strip()
        price = r.get(price_col, "")
        norm_name = _norm(name)
        norm_names.append((norm_name, name, price))

    # Exact
    for nn, name, price in norm_names:
        if nn == qn:
            await update.message.reply_text(f"{name} = {price} джк")
            return

    # Substring suggestions (anywhere)
    sugest = []
    for nn, name, price in norm_names:
        if qn and qn in nn:
            sugest.append((name, price))
            if len(sugest) >= 5:
                break

    if sugest:
        lines = [f"{n} = {p} джк" for n, p in sugest]
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
    - After submit: read fresh balance with short retry (still no caching).
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

    try:
        users, _, cols = load_accounts_index()
    except Exception as e:
        await update.message.reply_text(f"Ошибка чтения таблицы счетов: {e}")
        return

    sender_row = users.get(sender)
    if not sender_row:
        await update.message.reply_text(f"Аккаунт {sender} не найден.")
        return
    recipient_row = users.get(recipient)
    if not recipient_row:
        await update.message.reply_text(f"Аккаунт {recipient} не найден.")
        return

    bal_col = cols.get("balance")
    sender_bal = parse_balance(sender_row.get(bal_col) if bal_col else None)
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

    # No cache to invalidate; just poll for an updated balance a few times.
    new_sender_balance = None
    for _ in range(7):  # ~6 seconds total
        try:
            users_after, _, cols_after = load_accounts_index()
            row_after = users_after.get(sender)
            if row_after:
                bal_col2 = cols_after.get("balance")
                new_sender_balance = parse_balance(row_after.get(bal_col2) if bal_col2 else None)
                break
        except Exception:
            pass
        time.sleep(0.9)

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
