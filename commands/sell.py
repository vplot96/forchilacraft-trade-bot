#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import csv
import uuid
import asyncio
import logging
from io import StringIO
from typing import Optional, List, Dict, Tuple
from decimal import Decimal, ROUND_HALF_UP
from difflib import SequenceMatcher

import requests
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters

logger = logging.getLogger(__name__)

_PENDING_KEY = "pending_sell"
_cfg: Optional[dict] = None


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
    s = s.replace(",", ".")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", s):
        raise ValueError("bad money")
    return Decimal(s).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _fmt_money_form(v: Decimal) -> str:
    # RU Google Forms often expects comma as decimal separator
    return f"{v:.2f}".replace(".", ",")


def _fmt_money_human(v: Decimal) -> str:
    s = f"{v:.2f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _fetch_rows(sheet_id: str, gid: str) -> List[Dict[str, str]]:
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    r = requests.get(url, timeout=25)
    r.raise_for_status()

    txt = (r.content or b"").decode("utf-8-sig")
    reader = csv.DictReader(StringIO(txt))
    logger.info("SELL CSV headers: %r", reader.fieldnames)
    return list(reader)

# Поиск строки в таблице товаров
def _find_product_by_name(rows, query):
    q = _normalize(query)
    if not q:
        return None

    best_row = None
    best_ratio = 0.0

    for row in rows:
        name = str(row.get("Название", "")).strip()
        if not name:
            continue

        n = _normalize(name)

        if n == q:
            return row

        ratio = SequenceMatcher(None, q, n).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_row = row

    return best_row


def _load_sell_cfg() -> Optional[dict]:
    """Loads Google Form configuration from env vars once."""
    global _cfg
    if _cfg is not None:
        return _cfg

    form_id = os.getenv("FORM_OPS_ID") or None
    if not form_id:
        _cfg = None
        return None

    entry_op_id = os.getenv("FORM_OPS_ENTRY_OP_ID") or None
    entry_user = os.getenv("FORM_OPS_ENTRY_USER") or None
    entry_type = os.getenv("FORM_OPS_ENTRY_TYPE") or None
    entry_item = os.getenv("FORM_OPS_ENTRY_ITEM") or None
    entry_qty = os.getenv("FORM_OPS_ENTRY_QTY") or None
    entry_price = os.getenv("FORM_OPS_ENTRY_PRICE") or None

    if not all([entry_op_id, entry_user, entry_type, entry_item, entry_qty, entry_price]):
        _cfg = None
        return None

    _cfg = {
        "form_id": form_id,
        "post_url": f"https://docs.google.com/forms/d/e/{form_id}/formResponse",
        "entry_op_id": f"entry.{entry_op_id}",
        "entry_user": f"entry.{entry_user}",
        "entry_type": f"entry.{entry_type}",
        "entry_item": f"entry.{entry_item}",
        "entry_qty": f"entry.{entry_qty}",
        "entry_price": f"entry.{entry_price}",
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
    r = requests.post(url, data=payload, headers=headers, timeout=25, allow_redirects=True)
    preview = (r.text or "")[:500]
    return r.status_code, preview


async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /sell <товар> <количество> <цена>"""
    if update.message is None:
        return

    cfg = _load_sell_cfg()
    if cfg is None:
        await update.message.reply_text("Команда /sell недоступна: не настроены переменные Google Forms.")
        return

    sheet_id = os.getenv("SHEET_ID") or None
    gid_items = os.getenv("GID_ITEMS") or None
    if not sheet_id or not gid_items:
        await update.message.reply_text("Команда /sell недоступна: не настроены переменные таблицы товаров (SHEET_ID/GID_ITEMS).")
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
        await update.message.reply_text("Количество должно быть целым числом.")
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
        rows = await asyncio.to_thread(_fetch_rows, sheet_id, gid_items)
        logger.info("SELL: fetched %s products", len(rows))
    except Exception:
        logger.exception("SELL: failed to fetch products from sheet")
        await update.message.reply_text("Не удалось получить список товаров. Попробуйте позже.")
        return

    product = _find_product_by_name(rows, raw_item)
    if not product:
        await update.message.reply_text(f'Не нашёл товар по запросу: "{raw_item}". Проверьте название.')
        return
        
    product_name = str(product["Название"]).strip()

    payload = {
        cfg["entry_op_id"]: str(uuid.uuid4()),
        cfg["entry_user"]: sender_username,
        cfg["entry_type"]: "Продажа",
        cfg["entry_item"]: str(product["Id товара"]).strip(),
        cfg["entry_qty"]: str(qty),
        cfg["entry_price"]: _fmt_money_form(price),
    }
    _set_pending(context, {"form_url": cfg["post_url"], "payload": payload})

    await update.message.reply_text(
        f'Вы собираетесь продать {product_name} в количестве {qty} по {_fmt_money_human(price)}?\n'
        'Ответьте "да" или "нет".'
    )


async def sell_confirm_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return

    pending = _get_pending(context)
    if not pending:
        return

    text = (update.message.text or "").strip()

    if _is_yes(text):
        try:
            form_url = pending["form_url"]
            payload = pending["payload"]

            # Вреенные логи
            logger.info("SELL: posting to form_url=%s", form_url)

            status, body_preview = await asyncio.to_thread(_submit_form, form_url, payload)
            logger.info("SELL: form submit status=%s preview=%r", status, (body_preview or "")[:200])

            # Google Forms often returns 200 or 302
            if 200 <= status < 400:
                _clear_pending(context)
                await update.message.reply_text("Операция отправлена.")
                return

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
