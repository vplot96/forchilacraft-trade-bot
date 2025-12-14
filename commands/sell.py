#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import csv
import uuid
import asyncio
import logging
from io import StringIO
from typing import Optional, Tuple, List
from decimal import Decimal, ROUND_HALF_UP

import requests
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters

logger = logging.getLogger(__name__)

_cfg: Optional[dict] = None
_PENDING_KEY = "pending_sell"


def _require_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v


def _optional_env(name: str) -> Optional[str]:
    v = os.getenv(name)
    return v if v else None


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _strip_trailing_punct(s: str) -> str:
    return re.sub(r"[\s\.,;:!?]+$", "", (s or "").strip())


def _parse_int(raw: str) -> int:
    s = re.sub(r"[^\d-]", "", (raw or "").strip())
    if not s:
        raise ValueError("empty int")
    return int(s)


def _parse_money(raw: str) -> Decimal:
    s = (raw or "").strip().replace(" ", "")
    # allow both 12.34 and 12,34
    s = s.replace(",", ".")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", s):
        raise ValueError("bad money")
    return Decimal(s).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _fmt_money_form(v: Decimal) -> str:
    # Google Forms often expects comma for decimals in RU locales
    s = f"{v:.2f}"
    return s.replace(".", ",")


def _fmt_money_human(v: Decimal) -> str:
    s = f"{v:.2f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _csv_url(sheet_id: str, gid: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def _fetch_rows(sheet_id: str, gid: str) -> List[dict]:
    url = _csv_url(sheet_id, gid)
    r = requests.get(url, timeout=20)
    r.raise_for_status()

    txt = r.text or ""
    reader = csv.DictReader(StringIO(txt))
    return list(reader)


def _best_match_product(rows: List[dict], query: str) -> Optional[str]:
    # Tries to match by "Название товара" or "Название в игре" (case-insensitive, whitespace-normalized)
    q = _normalize(query)
    if not q:
        return None

    name_cols = ["Название товара", "Название в игре", "Название", "Товар", "Name"]
    for r in rows:
        name_val = None
        for c in name_cols:
            v = str(r.get(c) or "").strip()
            if v:
                name_val = v
                break
        if not name_val:
            continue
        if _normalize(name_val) == q:
            return name_val

    # fallback: contains
    for r in rows:
        for c in name_cols:
            v = str(r.get(c) or "").strip()
            if v and q in _normalize(v):
                return v

    return None


def _load_sell_cfg() -> Optional[dict]:
    global _cfg
    if _cfg is not None:
        return _cfg

    form_id = _optional_env("FORM_OPS_ID")
    if not form_id:
        _cfg = None
        return None

    entry_op_id = _optional_env("FORM_OPS_ENTRY_OP_ID")
    entry_user = _optional_env("FORM_OPS_ENTRY_USER")
    entry_type = _optional_env("FORM_OPS_ENTRY_TYPE")
    entry_item = _optional_env("FORM_OPS_ENTRY_ITEM")
    entry_qty = _optional_env("FORM_OPS_ENTRY_QTY")
    entry_price = _optional_env("FORM_OPS_ENTRY_PRICE")

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


def _set_pending(context: ContextTypes.DEFAULT_TYPE, payload: dict) -> None:
    context.user_data[_PENDING_KEY] = payload


def _get_pending(context: ContextTypes.DEFAULT_TYPE) -> Optional[dict]:
    return context.user_data.get(_PENDING_KEY)


def _clear_pending(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(_PENDING_KEY, None)


def _is_yes(s: str) -> bool:
    s = _normalize(_strip_trailing_punct(s))
    return s in {"да", "ага", "yes", "y", "ok", "ок"}


def _is_no(s: str) -> bool:
    s = _normalize(_strip_trailing_punct(s))
    return s in {"нет", "не", "no", "n"}


def _submit_form(url: str, payload: dict) -> Tuple[int, str]:
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "forchilacraft-trade-bot/1.0",
    }
    r = requests.post(url, data=payload, headers=headers, timeout=20, allow_redirects=True)
    preview = (r.text or "")[:500]
    return r.status_code, preview


async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return

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

    raw_item = " ".join(context.args[:-2]).strip()
    raw_qty = context.args[-2]
    raw_price = context.args[-1]

    try:
        qty = _parse_int(raw_qty)
    except Exception:
        await update.message.reply_text("Количество должно быть числом.")
        return

    if qty <= 0:
        await update.message.reply_text("Количество должно быть больше нуля.")
        return

    try:
        price = _parse_money(raw_price)
    except Exception:
        await update.message.reply_text("Цена должна быть числом. Пример: 45 или 45.5")
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
        rows = await asyncio.to_thread(_fetch_rows, sheet_id, gid_prices)
    except Exception:
        logger.exception("SELL: failed to fetch products from sheet")
        await update.message.reply_text("Не удалось получить список товаров. Попробуйте позже.")
        return

    matched_name = _best_match_product(rows, raw_item)
    if not matched_name:
        await update.message.reply_text(f'Не нашёл товар по запросу: "{raw_item}". Проверьте название.')
        return

    # IMPORTANT: pending MUST match what sell_confirm_listener reads
    op_id = str(uuid.uuid4())
    payload = {
        cfg["entry_op_id"]: op_id,
        cfg["entry_user"]: sender_username,
        cfg["entry_type"]: "Продажа",
        cfg["entry_item"]: matched_name,
        cfg["entry_qty"]: str(qty),
        cfg["entry_price"]: _fmt_money_form(price),
    }
    pending = {"form_url": cfg["post_url"], "payload": payload}
    _set_pending(context, pending)

    await update.message.reply_text(
        f'Вы собираетесь продать {matched_name} в количестве {qty} по {_fmt_money_human(price)}?\nОтветьте "да" или "нет".'
    )


async def sell_confirm_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or context is None:
        return

    pending = _get_pending(context)
    if not pending:
        return

    text = (update.message.text or "").strip()

    if _is_yes(text):
        try:
            form_url = pending["form_url"]
            payload = pending["payload"]

            status, body_preview = await asyncio.to_thread(_submit_form, form_url, payload)

            if status >= 200 and status < 300:
                _clear_pending(context)
                await update.message.reply_text("Операция отправлена.")
                return

            logger.error("SELL: form submit failed: status=%s preview=%s", status, body_preview)
            await update.message.reply_text("Не удалось отправить операцию. Попробуйте позже.")
            return

        except Exception:
            logger.exception("SELL: error while submitting form")
            await update.message.reply_text("Не удалось отправить операцию. Попробуйте позже.")
            return

    if _is_no(text):
        _clear_pending(context)
        await update.message.reply_text("Операция отменена.")
        return

    await update.message.reply_text('Мне нужен чёткий ответ: "да" или "нет".')


def get_handlers():
    return [
        CommandHandler("sell", sell),
        MessageHandler(filters.TEXT & ~filters.COMMAND, sell_confirm_listener),
    ]
