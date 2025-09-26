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
from telegram.ext import Application, CommandHandler, ContextTypes

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

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Доступные команды:\n/balance – узнать свой баланс\n/price <название товара> – узнать текущий курс товара\n/pay <имя пользователя> <сумма> – сделать перевод")

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username
    if not username:
        await update.message.reply_text("У вас не задан Telegram username (@...).")
        return
    try:
        rows = _load_accounts_rows()
    except Exception as e:
        await update.message.reply_text(f"Ошибка доступа к таблице: {e}")
        return
    acc = _find_account(rows, username)
    if not acc:
        await update.message.reply_text(f"Не могу найти аккаунт с именем @{username}.")
        return
    bal = _parse_balance_to_decimal(acc.get("Баланс"))
    await update.message.reply_text(f"Ваш баланс: {bal} джк")

def lookup_price_by_product_name(query: str, cutoff: float = 0.45):
    if not GID_PRICES:
        raise RuntimeError("GID_PRICES is not set")
    rows = fetch_rows(GID_PRICES)
    qn = normalize(query)
    names = [normalize(str(r.get("Название товара",""))) for r in rows]
    import difflib
    best = difflib.get_close_matches(qn, names, n=1, cutoff=cutoff)
    if not best:
        return None
    idx = names.index(best[0])
    row = rows[idx]
    return (str(row.get("Название товара","")).strip(), row.get("Текущая цена",""))

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /price <название товара>\n\n<название товара> – название предмета из игры. Может быть не точным.")
        return
    q = " ".join(context.args).strip()
    try:
        found = lookup_price_by_product_name(q)
    except Exception as e:
        await update.message.reply_text(f"Ошибка доступа к таблице: {e}")
        return
    if not found:
        await update.message.reply_text(f"Товар, похожий на '{q}', не найден.")
        return
    name, price_val = found
    await update.message.reply_text(f"{name} = {price_val} джк")

async def pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not (FORM_ID and ENTRY_SENDER and ENTRY_RECIPIENT and ENTRY_SUM and FORM_POST_URL):
        await update.message.reply_text("Не настроены параметры формы перевода.")
        return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Использование: /pay <имя пользователя> <сумма>\n\n<имя пользователя> – username пользователя из Телеграм без символа @.\n<сумма> – число, может быть с двумя знаками после запятой.")
        return

    recipient = context.args[0].strip()
    amount_raw = " ".join(context.args[1:]).strip()
    sender_username = (update.effective_user.username or "").strip()
    if not sender_username:
        await update.message.reply_text("У вас не задан Telegram username.")
        return
    if normalize(sender_username) == normalize(recipient):
        await update.message.reply_text("Нельзя осуществить перевод себе.")
        return
    try:
        amount = _parse_amount_arg(amount_raw)
    except Exception:
        await update.message.reply_text("Не могу понять указанную сумму.")
        return
    if amount <= 0:
        await update.message.reply_text("Сумма должна быть больше 0.")
        return

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

    sender_balance = _parse_balance_to_decimal(sender_row.get("Баланс"))
    if sender_balance < amount:
        await update.message.reply_text("На вашем балансе недостаточно средств.")
        return

    payload = {
        ENTRY_SENDER: sender_username,
        ENTRY_RECIPIENT: recipient,
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


# ---------- /ops <число> — последние операции пользователя ----------
def _fetch_ops_rows():
    if not GID_OPS:
        raise RuntimeError("GID_OPS is not set")
    return fetch_rows(GID_OPS)

def _parse_date_safe(s: str):
    s = (s or "").strip()
    for fmt in ("%d.%m.%y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None

def _format_op_line(row):
    # Ожидаемые колонки: Название, Операция, Число, Сумма, Пользователь, Дата
    title = str(row.get("Название", "")).strip()
    op = str(row.get("Операция", "")).strip()  # "Покупка" / "Продажа"
    qty = str(row.get("Число", "")).strip()
    amount = str(row.get("Сумма", "")).strip()
    sign = "−" if op.lower().startswith("покуп") else "+"  # минус для Покупка, плюс для Продажа
    return title, op, qty, amount, sign

async def ops(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Сколько выводить (по умолчанию 5)
    limit = 5
    if context.args:
        arg = "".join(context.args).strip()
        if arg.isdigit():
            limit = max(1, min(50, int(arg)))  # ограничим до 50

    # Определяем отправителя и его Имя
    username = (update.effective_user.username or "").strip()
    if not username:
        await update.message.reply_text("У вас не задан Telegram username (@...).")
        return

    try:
        accounts = _load_accounts_rows()
    except Exception as e:
        await update.message.reply_text(f"Ошибка доступа к таблице: {e}")
        return

    me = _find_account(accounts, username)
    if not me:
        await update.message.reply_text(f"Не могу найти аккаунт с именем @{username}.")
        return

    player_name = str(me.get("Имя", "")).strip()
    if not player_name:
        await update.message.reply_text("В вашей записи не указано поле «Имя». Обратитесь к администратору.")
        return

    # Грузим операции и фильтруем по имени
    try:
        ops_rows = _fetch_ops_rows()
    except Exception as e:
        await update.message.reply_text(f"Ошибка доступа к листу «Операции»: {e}")
        return

    mine = []
    for r in ops_rows:
        if str(r.get("Пользователь", "")).strip() == player_name:
            dt = _parse_date_safe(r.get("Дата", ""))
            if dt is None:
                dt = datetime.min
            mine.append((dt, r))

    if not mine:
        await update.message.reply_text("Для вас ещё нет операций.")
        return

    # Сортируем по дате по убыванию и берём limit
    mine.sort(key=lambda x: x[0], reverse=True)
    mine = mine[:limit]

    # Формируем строки
    lines = []
    for dt, row in mine:
        date_str = dt.strftime("%d.%m.%y") if dt != datetime.min else str(row.get("Дата", "")).strip()
        title, op, qty, amount, sign = _format_op_line(row)
        lines.append(f'{date_str} {op} "{title}" ({qty}): {sign}{amount} джк')

    await update.message.reply_text("\n".join(lines))

# Entrypoint
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("pay", pay))
    app.add_handler(CommandHandler("ops", ops))
    app.run_polling()

if __name__ == "__main__":
    main()
