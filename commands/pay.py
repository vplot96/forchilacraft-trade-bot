#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""commands/pay.py

Самодостаточная команда /pay.

Принцип:
- bot.py всегда регистрирует хендлер /pay
- команда сама проверяет наличие env и необходимых данных
- если конфигурации нет — отвечает пользователю, но не ломает запуск бота
"""

import os
import re
import csv
from io import StringIO
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

import requests
from telegram import Update
from telegram.ext import ContextTypes


def _require_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"{name} is not set")
    return v


def _optional_env(name: str) -> Optional[str]:
    v = os.getenv(name)
    return v if v else None


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _parse_decimal(value) -> Decimal:
    s = str(value or "").strip().replace(" ", "").replace(",", ".")
    if not s:
        return Decimal("0")
    try:
        return Decimal(s)
    except Exception:
        return Decimal("0")


def _to_money_2(value) -> Decimal:
    return _parse_decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _parse_amount_arg(raw: str) -> Decimal:
    cleaned = re.sub(r"[^\d,.-]", "", (raw or "")).replace(",", ".")
    if cleaned in ("", ".", "-", "-.", ".-"):
        raise ValueError("empty")
    amount = Decimal(cleaned).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return amount


def _fmt_amount_for_form(amount: Decimal) -> str:
    return f"{amount:.2f}".replace(".", ",")


def _csv_url(sheet_id: str, gid: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def _fetch_rows(sheet_id: str, gid: str):
    r = requests.get(_csv_url(sheet_id, gid), timeout=20)
    r.raise_for_status()
    text = r.content.decode("utf-8-sig")
    return list(csv.DictReader(StringIO(text)))


def _find_account_by_username(rows, username: str) -> Optional[dict]:
    u = _normalize(username)
    for row in rows:
        if _normalize(str(row.get("Username", ""))) == u:
            return row
    return None


def _get_balance_from_account(row: dict) -> Decimal:
    for key in ("Баланс", "Balance", "balance"):
        if key in row and str(row.get(key) or "").strip():
            return _to_money_2(row.get(key))
    for key, val in row.items():
        if _normalize(key) in ("баланс", "balance"):
            return _to_money_2(val)
    return Decimal("0.00")


_cached_cfg = None


def _load_pay_cfg():
    global _cached_cfg
    if _cached_cfg is not None:
        return _cached_cfg

    form_id = _optional_env("FORM_PAY_ID")
    entry_sender = _optional_env("FORM_PAY_ENTRY_SENDER")
    entry_recipient = _optional_env("FORM_PAY_ENTRY_RECIPIENT")
    entry_sum = _optional_env("FORM_PAY_ENTRY_SUM")

    if not all([form_id, entry_sender, entry_recipient, entry_sum]):
        _cached_cfg = None
        return None

    _cached_cfg = {
        "form_id": form_id,
        "post_url": f"https://docs.google.com/forms/d/e/{form_id}/formResponse",
        "entry_sender": entry_sender,
        "entry_recipient": entry_recipient,
        "entry_sum": entry_sum,
    }
    return _cached_cfg


def init_pay_helpers(
    form_id: str,
    entry_sender: str,
    entry_recipient: str,
    entry_sum: str,
    post_url: str,
    *_args,
    **_kwargs,
):
    global _cached_cfg
    _cached_cfg = {
        "form_id": form_id,
        "post_url": post_url,
        "entry_sender": entry_sender,
        "entry_recipient": entry_recipient,
        "entry_sum": entry_sum,
    }


async def pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = _load_pay_cfg()
    if cfg is None:
        await update.message.reply_text("Команда /pay временно недоступна: не настроены переменные окружения.")
        return

    sender_username = (update.effective_user.username or "").strip()
    if not sender_username:
        await update.message.reply_text("Не удалось определить ваш username в Telegram. Установите username и попробуйте снова.")
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Использование: /pay <username> <сумма>")
        return

    recipient_username = context.args[0].lstrip("@").strip()
    raw_sum = " ".join(context.args[1:]).strip()

    if not recipient_username:
        await update.message.reply_text("Укажите получателя: /pay <username> <сумма>")
        return

    if _normalize(recipient_username) == _normalize(sender_username):
        await update.message.reply_text("Нельзя осуществить перевод себе.")
        return

    try:
        amount = _parse_amount_arg(raw_sum)
    except Exception:
        await update.message.reply_text("Некорректная сумма. Пример: /pay Prince_Joy 10,00")
        return

    if amount <= Decimal("0.00"):
        await update.message.reply_text("Сумма должна быть больше нуля.")
        return

    try:
        sheet_id = _require_env("SHEET_ID")
        gid_accounts = _require_env("GID_ACCOUNTS")
    except Exception:
        await update.message.reply_text("Команда /pay временно недоступна: не настроены переменные таблицы.")
        return

    try:
        accounts = _fetch_rows(sheet_id, gid_accounts)
    except Exception:
        await update.message.reply_text("Не удалось получить данные счетов. Попробуйте позже.")
        return

    sender_row = _find_account_by_username(accounts, sender_username)
    if sender_row is None:
        await update.message.reply_text("Ваш аккаунт не найден в таблице «Счета». Проверьте Telegram username.")
        return

    sender_balance = _get_balance_from_account(sender_row)
    if sender_balance < amount:
        await update.message.reply_text("На балансе не достаточно средств")
        return

    payload = {
        cfg["entry_sender"]: sender_username,
        cfg["entry_recipient"]: recipient_username,
        cfg["entry_sum"]: _fmt_amount_for_form(amount),
    }

    try:
        r = requests.post(cfg["post_url"], data=payload, timeout=20)
        if r.status_code >= 400:
            await update.message.reply_text("Не удалось выполнить перевод. Попробуйте позже.")
            return
    except Exception:
        await update.message.reply_text("Не удалось выполнить перевод. Попробуйте позже.")
        return

    await update.message.reply_text("Ваш перевод подтверждён")
