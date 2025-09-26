#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import logging
from decimal import Decimal, ROUND_HALF_UP

import requests
import gspread
from google.oauth2.service_account import Credentials

from telegram.ext import Updater, CommandHandler

# -------------------- Logging --------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# -------------------- Config --------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SHEET_ACCOUNTS = "Счета"             # Лист с аккаунтами
COL_USERNAME = "Username"            # Точное имя заголовка
COL_BALANCE = "Баланс"               # Точное имя заголовка

# Google service-account creds: путь в GOOGLE_APPLICATION_CREDENTIALS
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

# ---- Google Form (пуш транзакции) ----
FORM_ID = "1FAIpQLSegZNyaElRcoul_YMDZiZtp5ZLZlhBDiWf04UG-smUeAu6y9A"
ENTRY_USER = "entry.2015621373"
ENTRY_SUM = "entry.40410086"
FORM_POST_URL = f"https://docs.google.com/forms/d/e/{FORM_ID}/formResponse"


# -------------------- Google Sheets helpers (без кеша) --------------------
def gs_client():
    creds_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if not creds_path:
        raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS не задан")
    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    return gspread.authorize(creds)


def read_accounts():
    """Читает Актуальные данные листа 'Счета' (без кеширования)."""
    gc = gs_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.worksheet(SHEET_ACCOUNTS)
    # Возвращает список словарей по заголовкам
    return ws.get_all_records()


def find_account(rows, username: str):
    """Ищет запись аккаунта по точному Username."""
    if not username:
        return None
    u = str(username).strip()
    for r in rows:
        if str(r.get(COL_USERNAME, "")).strip() == u:
            return r
    return None


def parse_decimal_2(value) -> Decimal:
    """
    Универсальный парсер чисел из таблицы/ввода.
    Поддержка '1234', '1234,56', '1234.56'. Округляет до 2-х знаков HALF_UP.
    """
    if value is None:
        return Decimal("0.00")
    s = str(value).strip().replace(" ", "").replace(",", ".")
    if s == "":
        return Decimal("0.00")
    try:
        q = Decimal(s)
    except Exception:
        return Decimal("0.00")
    return q.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def parse_amount_arg(raw: str) -> Decimal:
    """
    Парсит сумму пользователя (разрешая ',' и '.'), оставляя ровно 2 знака.
    Бросает ValueError при неверном вводе.
    """
    cleaned = re.sub(r"[^\d,.\-]", "", raw).replace(",", ".")
    if cleaned in ("", ".", "-", "-.", ".-"):
        raise ValueError("empty")
    q = Decimal(cleaned)
    return q.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def fmt_amount_comma2(amount: Decimal) -> str:
    """Возвращает строку вида '12,34'."""
    return f"{amount:.2f}".replace(".", ",")


# -------------------- Bot commands --------------------
def start(update, context):
    update.message.reply_text(
        "Привет! Доступные команды:\n"
        "/balance — показать ваш баланс\n"
        "/pay <username> <sum> — перевести средства"
    )


def balance(update, context):
    """Показывает баланс текущего пользователя (по Telegram username)."""
    msg = update.effective_message
    username = update.effective_user.username
    if not username:
        msg.reply_text("У вас не задан Telegram username.")
        return

    try:
        rows = read_accounts()
    except Exception as e:
        logger.exception("Не удалось прочитать таблицу: %s", e)
        msg.reply_text("Не удалось получить данные счетов. Повторите позже.")
        return

    row = find_account(rows, username)
    if not row:
        msg.reply_text(f"Аккаунт {username} не найден.")
        return

    bal = parse_decimal_2(row.get(COL_BALANCE))
    # Выводим так же, как раньше (джк), но без лишних форматов
    msg.reply_text(f"Баланс {username}: {bal} джк")


def pay(update, context):
    """
    /pay <username> <sum>
    Проверяет наличие аккаунтов отправителя/получателя и достаточность средств.
    Пушит транзакцию в Google Form (две цифры после запятой).

    Сообщения:
    - На балансе недостаточно средств.
    - Ваш перевод подтверждён.
    - Аккаунт <username> не найден.
    """
    msg = update.effective_message
    args = context.args if hasattr(context, "args") else []

    if len(args) < 2:
        msg.reply_text("Использование: /pay <username> <sum>")
        return

    recipient = args[0].strip()
    amount_raw = " ".join(args[1:]).strip()

    # Telegram username отправителя
    sender_username = update.effective_user.username
    if not sender_username:
        msg.reply_text("У вас не задан Telegram username.")
        return

    # Валидация ника получателя
    if not re.match(r"^[A-Za-z0-9_.\-]{1,32}$", recipient):
        msg.reply_text("Некорректный <username>.")
        return

    # Парсим сумму с точностью до 2-х знаков
    try:
        amount = parse_amount_arg(amount_raw)
    except Exception:
        msg.reply_text("Некорректная сумма. Пример: 10 или 12,50")
        return

    if amount <= 0:
        msg.reply_text("Сумма должна быть больше 0.")
        return

    # Читаем актуальные данные
    try:
        rows = read_accounts()
    except Exception as e:
        logger.exception("Не удалось прочитать таблицу: %s", e)
        msg.reply_text("Не удалось получить данные счетов. Повторите позже.")
        return

    # Проверяем отправителя
    sender_row = find_account(rows, sender_username)
    if not sender_row:
        msg.reply_text(f"Аккаунт {sender_username} не найден.")
        return

    sender_balance = parse_decimal_2(sender_row.get(COL_BALANCE))
    if sender_balance < amount:
        msg.reply_text("На балансе недостаточно средств.")
        return

    # Проверяем получателя
    recipient_row = find_account(rows, recipient)
    if not recipient_row:
        msg.reply_text(f"Аккаунт {recipient} не найден.")
        return

    # Отправляем данные в форму (ровно 2 знака, запятая как разделитель)
    payload = {
        ENTRY_USER: recipient,
        ENTRY_SUM: fmt_amount_comma2(amount),
    }

    try:
        resp = requests.post(
            FORM_POST_URL,
            data=payload,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ForchilacraftBot/1.0)"},
            timeout=10,
        )
        ok = resp.status_code in (200, 302)
    except requests.RequestException as e:
        logger.exception("Ошибка отправки формы: %s", e)
        ok = False

    if ok:
        msg.reply_text("Ваш перевод подтверждён.")
    else:
        msg.reply_text("Не удалось отправить перевод. Попробуйте позже.")


# -------------------- Entrypoint --------------------
def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN не задан")

    updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("balance", balance))
    dp.add_handler(CommandHandler("pay", pay))

    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
