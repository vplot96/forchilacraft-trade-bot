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


# -----------------------------
# helpers
# -----------------------------
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


def _csv_export_url(sheet_id: str, gid: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def _fetch_csv_text(sheet_id: str, gid: str) -> str:
    url = _csv_export_url(sheet_id, gid)
    r = requests.get(url, timeout=25)
    r.raise_for_status()
    # utf-8-sig removes BOM if present
    return (r.content or b"").decode("utf-8-sig", errors="replace")


def _fetch_rows(sheet_id: str, gid: str) -> List[Dict[str, str]]:
    txt = _fetch_csv_text(sheet_id, gid)
    reader = csv.DictReader(StringIO(txt))
    logger.info("SELL CSV headers(gid=%s): %r", gid, reader.fieldnames)
    return list(reader)


def _tg_nick_or_fullname(update: Update) -> str:
    """Return current Telegram identifier: @username if present else full_name."""
    u = update.effective_user
    if u and u.username:
        return f"@{u.username}"
    return (u.full_name if u else "unknown").strip()


# -----------------------------
# accounts lookup
# -----------------------------
def _find_account_by_nick(accounts: List[dict], nick: str) -> Optional[dict]:
    n = (nick or "").strip()
    if not n:
        return None
    for row in accounts:
        if str(row.get("Ник", "")).strip() == n:
            return row
    return None


def _resolve_game_username(accounts: List[dict], nick: str) -> Optional[str]:
    row = _find_account_by_nick(accounts, nick)
    if not row:
        return None
    return str(row.get("Имя пользователя", "")).strip()


# -----------------------------
# products lookup
# -----------------------------
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


# -----------------------------
# config (env)
# -----------------------------
def _load_sell_cfg() -> Optional[dict]:
    """Loads Google Form configuration for SELL from env vars once."""
    global _cfg
    if _cfg is not None:
        return _cfg

    form_id = os.getenv("FORM_SELL_ID") or None
    if not form_id:
        _cfg = None
        return None

    entry_sale_id = os.getenv("FORM_SELL_ENTRY_SALE_ID") or None
    entry_seller = os.getenv("FORM_SELL_ENTRY_SELLER") or None
    entry_item_id = os.getenv("FORM_SELL_ENTRY_ITEM_ID") or None
    entry_amount = os.getenv("FORM_SELL_ENTRY_AMOUNT") or None
    entry_price = os.getenv("FORM_SELL_ENTRY_PRICE") or None

    if not all([entry_sale_id, entry_seller, entry_item_id, entry_amount, entry_price]):
        _cfg = None
        return None

    _cfg = {
        "form_id": form_id,
        "post_url": f"https://docs.google.com/forms/d/e/{form_id}/formResponse",
        "entry_sale_id": f"entry.{entry_sale_id}",
        "entry_seller": f"entry.{entry_seller}",
        "entry_item_id": f"entry.{entry_item_id}",
        "entry_amount": f"entry.{entry_amount}",
        "entry_price": f"entry.{entry_price}",
    }
    return _cfg


# -----------------------------
# pending
# -----------------------------
def _set_pending(context: ContextTypes.DEFAULT_TYPE, payload: dict) -> None:
    context.user_data[_PENDING_KEY] = payload


def _get_pending(context: ContextTypes.DEFAULT_TYPE) -> Optional[dict]:
    return context.user_data.get(_PENDING_KEY)


def _clear_pending(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(_PENDING_KEY, None)


def _is_yes(s: str) -> bool:
    s = _normalize(_strip_trailing_punct(s))
    return s in {"да"}


def _is_no(s: str) -> bool:
    s = _normalize(_strip_trailing_punct(s))
    return s in {"нет"}


# -----------------------------
# forms
# -----------------------------
def _submit_form(url: str, payload: dict) -> Tuple[int, str]:
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "forchilacraft-trade-bot/1.0",
    }
    r = requests.post(url, data=payload, headers=headers, timeout=25, allow_redirects=True)
    preview = (r.text or "")[:500]
    return r.status_code, preview


# -----------------------------
# public handlers
# -----------------------------
async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /sell <товар> <количество> <цена>"""
    if update.message is None:
        return

    cfg = _load_sell_cfg()
    if cfg is None:
        await update.message.reply_text("Команда /sell выполнилась с ошибкой")
        return

    sheet_id = os.getenv("SHEET_ID") or None
    gid_items = os.getenv("GID_ITEMS") or None
    gid_accounts = os.getenv("GID_ACCOUNTS") or None
    if not sheet_id or not gid_items or not gid_accounts:
        await update.message.reply_text(
            "Команда /sell выполнилась с ошибкой"
        )
        return

    if not context.args or len(context.args) < 3:
        await update.message.reply_text(
            'Использование: /sell <товар> <количество> <цена>'
        )
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

    # 1) берём текущий ник/имя в Telegram
    tg_user = _tg_nick_or_fullname(update)

    # 2) ищем в листе "Счета" строку, где "Ник" == tg_user
    try:
        accounts = await asyncio.to_thread(_fetch_rows, sheet_id, gid_accounts)
    except Exception:
        logger.exception("SELL: failed to fetch accounts from sheet")
        await update.message.reply_text("Возникла ошибка при чтении данных.")
        return

    game_username = _resolve_game_username(accounts, tg_user)
    if game_username is None:
        await update.message.reply_text("Не удалось найти ваш аккаунт. Уточните ваши данные аккаунта.")
        return

    if not game_username:
        await update.message.reply_text("Ошибка при обращении к вашему аккаунту. Уточните ваши данные аккаунта.")
        return

    # 3) ищем товар в таблице товаров
    try:
        rows = await asyncio.to_thread(_fetch_rows, sheet_id, gid_items)
        logger.info("SELL: fetched %s products", len(rows))
    except Exception:
        logger.exception("SELL: failed to fetch products from sheet")
        await update.message.reply_text("Не удалось получить список товаров. Попробуйте позже.")
        return

    product = _find_product_by_name(rows, raw_item)
    if not product:
        await update.message.reply_text("Не нашёл товар по вашему запросу. Возможно этот товар не продаётся на бирже.")
        return

    product_name = str(product.get("Название", "")).strip()
    item_id = str(product.get("Id товара", "")).strip()

    if not item_id:
        await update.message.reply_text("Ошибка при получении данных об этом товаре.")
        return

    # 4) формируем payload: seller = внутриигровое имя
    payload = {
        cfg["entry_sale_id"]: str(uuid.uuid4()),
        cfg["entry_seller"]: str(game_username),
        cfg["entry_item_id"]: item_id,
        cfg["entry_amount"]: str(qty),
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

    await update.message.reply_text('Мне нужен ответ: "да" или "нет".')


def get_handlers():
    return [
        CommandHandler("sell", sell),
        MessageHandler(filters.TEXT & ~filters.COMMAND, sell_confirm_listener),
    ]
