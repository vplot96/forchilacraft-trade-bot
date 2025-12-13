#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""commands/sell.py

Команда /sell:
  /sell <товар> <кол-во> <цена>

Пример:
  /sell алмаз 20 45
  -> бот ищет наиболее похожий товар в таблице товаров (лист цен/товаров),
     спрашивает подтверждение,
     после "да" отправляет Google Form.

Принцип:
- bot.py регистрирует хендлеры
- команда сама проверяет env и сообщает пользователю, если конфигурации нет
"""

import os
import re
import csv
import uuid
from io import StringIO
from decimal import Decimal, ROUND_HALF_UP
from difflib import SequenceMatcher
from typing import Optional

import requests
from telegram import Update
from telegram.ext import (
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)


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


def _parse_int(raw: str) -> int:
    s = re.sub(r"[^\d-]", "", (raw or "").strip())
    if not s:
        raise ValueError("empty int")
    return int(s)


def _parse_money(raw: str) -> Decimal:
    s = (raw or "").strip().replace(" ", "").replace(",", ".")
    s = re.sub(r"[^\d.\-]", "", s)
    if s in ("", ".", "-", "-.", ".-"):
        raise ValueError("empty money")
    q = Decimal(s).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return q


def _fmt_money_form(amount: Decimal) -> str:
    return f"{amount:.2f}".replace(".", ",")


def _fmt_money_human(amount: Decimal) -> str:
    s = _fmt_money_form(amount)
    if s.endswith(",00"):
        return s[:-3]
    s = s.rstrip("0")
    if s.endswith(","):
        s = s[:-1]
    return s


def _csv_url(sheet_id: str, gid: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def _fetch_rows(sheet_id: str, gid: str):
    r = requests.get(_csv_url(sheet_id, gid), timeout=20)
    r.raise_for_status()
    text = r.content.decode("utf-8-sig")
    return list(csv.DictReader(StringIO(text)))


def _best_match_product(rows: list[dict], query: str) -> Optional[str]:
    q = _normalize(query)
    if not q:
        return None

    name_cols = ("Название товара", "Название в игре", "Название", "Товар")

    best_name = None
    best_score = 0.0

    for row in rows:
        name_val = None
        for c in name_cols:
            v = str(row.get(c) or "").strip()
            if v:
                name_val = v
                break
        if not name_val:
            continue

        n = _normalize(name_val)

        if n == q:
            return name_val

        score = SequenceMatcher(None, n, q).ratio()
        if score > best_score:
            best_score = score
            best_name = name_val

    if best_name and best_score >= 0.55:
        return best_name
    return None


_cfg = None


def _load_sell_cfg():
    global _cfg
    if _cfg is not None:
        return _cfg

    form_id = _optional_env("FORM_SELL_ID")
    if not form_id:
        _cfg = None
        return None

    entry_op_id = _optional_env("FORM_SELL_ENTRY_OP_ID")
    entry_user = _optional_env("FORM_SELL_ENTRY_USER")
    entry_type = _optional_env("FORM_SELL_ENTRY_TYPE")
    entry_item = _optional_env("FORM_SELL_ENTRY_ITEM")
    entry_qty = _optional_env("FORM_SELL_ENTRY_QTY")
    entry_price = _optional_env("FORM_SELL_ENTRY_PRICE")

    if not all([entry_op_id, entry_user, entry_type, entry_item, entry_qty, entry_price]):
        _cfg = None
        return None

    _cfg = {
        "post_url": f"https://docs.google.com/forms/d/e/{form_id}/formResponse",
        "entry_op_id": entry_op_id,
        "entry_user": entry_user,
        "entry_type": entry_type,
        "entry_item": entry_item,
        "entry_qty": entry_qty,
        "entry_price": entry_price,
    }
    return _cfg


_PENDING_KEY = "pending_sell"


def _set_pending(context: ContextTypes.DEFAULT_TYPE, payload: dict):
    context.user_data[_PENDING_KEY] = payload


def _get_pending(context: ContextTypes.DEFAULT_TYPE) -> Optional[dict]:
    return context.user_data.get(_PENDING_KEY)


def _clear_pending(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop(_PENDING_KEY, None)


async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = _load_sell_cfg()
    if cfg is None:
        await update.message.reply_text("Команда /sell временно недоступна: не настроены переменные окружения.")
        return

    sheet_id = _optional_env("SHEET_ID")
    gid_prices = _optional_env("GID_PRICES")
    if not sheet_id or not gid_prices:
        await update.message.reply_text("Команда /sell временно недоступна: не настроены переменные таблицы товаров.")
        return

    if not context.args or len(context.args) < 3:
        await update.message.reply_text('Использование: /sell <товар> <количество> <цена>\nПример: /sell алмаз 20 45')
        return

    raw_qty = context.args[-2]
    raw_price = context.args[-1]
    raw_item = " ".join(context.args[:-2]).strip()

    try:
        qty = _parse_int(raw_qty)
    except Exception:
        await update.message.reply_text("Некорректное количество. Пример: /sell алмаз 20 45")
        return

    try:
        price = _parse_money(raw_price)
    except Exception:
        await update.message.reply_text("Некорректная цена. Пример: /sell алмаз 20 45")
        return

    if qty <= 0:
        await update.message.reply_text("Количество должно быть больше нуля.")
        return

    if price <= Decimal("0.00"):
        await update.message.reply_text("Цена должна быть больше нуля.")
        return

    sender_u = (update.effective_user.username or "").strip()
    if not sender_u:
        await update.message.reply_text("Не удалось определить ваш username в Telegram. Установите username и попробуйте снова.")
        return
    sender_username = f"@{sender_u}"

    try:
        rows = _fetch_rows(sheet_id, gid_prices)
    except Exception:
        await update.message.reply_text("Не удалось получить список товаров. Попробуйте позже.")
        return

    matched_name = _best_match_product(rows, raw_item)
    if not matched_name:
        await update.message.reply_text("Не удалось найти подходящий товар. Попробуйте уточнить название.")
        return

    pending = {
        "op_id": str(uuid.uuid4()),
        "user": sender_username,
        "type": "Продажа",
        "item": matched_name,
        "qty": qty,
        "price": price,
    }
    _set_pending(context, pending)

    await update.message.reply_text(
        f'Вы собираетесь продать {matched_name} в количестве {qty} по {_fmt_money_human(price)}?\nОтветьте "да" или "нет".'
    )


async def sell_confirm_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = _get_pending(context)
    if not pending:
        return

    text = (update.message.text or "").strip().lower()

    if text in ("да", "yes", "y"):
        cfg = _load_sell_cfg()
        if cfg is None:
            _clear_pending(context)
            await update.message.reply_text("Команда /sell временно недоступна: не настроены переменные окружения.")
            return

        payload = {
            cfg["entry_op_id"]: pending["op_id"],
            cfg["entry_user"]: pending["user"],
            cfg["entry_type"]: pending["type"],
            cfg["entry_item"]: pending["item"],
            cfg["entry_qty"]: str(pending["qty"]),
            cfg["entry_price"]: _fmt_money_form(pending["price"]),
        }

        try:
            r = requests.post(cfg["post_url"], data=payload, timeout=20)
            if r.status_code >= 400:
                _clear_pending(context)
                await update.message.reply_text("Не удалось отправить операцию. Попробуйте позже.")
                return
        except Exception:
            _clear_pending(context)
            await update.message.reply_text("Не удалось отправить операцию. Попробуйте позже.")
            return

        _clear_pending(context)
        await update.message.reply_text("Операция продажи отправлена.")
        return

    if text in ("нет", "no", "n"):
        _clear_pending(context)
        await update.message.reply_text("Ок, отменено.")
        return

    await update.message.reply_text('Пожалуйста, ответьте "да" или "нет".')


def get_handlers():
    return [
        CommandHandler("sell", sell),
        MessageHandler(filters.TEXT & ~filters.COMMAND, sell_confirm_listener),
    ]
