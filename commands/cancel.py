#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import csv
import logging
import os
import re
from dataclasses import dataclass
from io import StringIO
from typing import Dict, List, Optional, Tuple

import requests
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

_PENDING_KEY = "pending_cancel"
_cfg: Optional[dict] = None

PER_PAGE = 50

# Sheets
SHEET_ACCOUNTS_NAME = "Счета"
SHEET_LOTS_NAME = "Лоты"

# Accounts columns
ACCOUNTS_COL_USER = "Пользователь"
ACCOUNTS_COL_LOGIN = "Логин"

# Open lots columns
LOTS_COL_DATE = "Дата"
LOTS_COL_SALE_ID = "Id продажи"
LOTS_COL_SELLER = "Продавец"
LOTS_COL_ITEM_ID = "Id товара"
LOTS_COL_ITEM_NAME = "Товар"
LOTS_COL_AMOUNT = "Количество"
LOTS_COL_PRICE = "Цена"


@dataclass(frozen=True)
class OpenLot:
    ts: str
    sale_id: str
    seller_login: str
    item_id: str
    item_name: str
    remaining: int
    price: int


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return

    cfg = _load_cancel_cfg()
    if not cfg:
        await update.message.reply_text(
            "Возникла ошибка при использовании команды. Похоже сервис не настроен корректно. Обратитесь к администратору."
        )
        return

    tg_user = _tg_nick_or_fullname(update)

    try:
        accounts = await asyncio.to_thread(_fetch_rows_dict, cfg["sheet_id"], cfg["gid_accounts"])
    except Exception:
        logger.exception("CANCEL: failed to fetch accounts")
        await update.message.reply_text("Не удалось получить данные аккаунта. Попробуйте позже.")
        return

    game_username = _resolve_game_username(accounts, tg_user)
    if game_username is None or not game_username:
        await update.message.reply_text("Не удалось найти ваш аккаунт. Обратитесь к администратору.")
        return

    await update.message.reply_text("Ищу ваши открытые лоты...")

    lots = await _load_user_lots(cfg, game_username)
    if not lots:
        _clear_pending(context)
        await update.message.reply_text("У вас нет открытых продаж на бирже.")
        return

    _set_pending(
        context,
        {
            "game_username": game_username,
            "page": 0,
            "lots": [l.__dict__ for l in lots],  # сериализуем
        },
    )

    await update.message.reply_text(_render_page_text(lots, page=0))


async def cancel_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return

    pending = _get_pending(context)
    if not pending:
        return

    text = (update.message.text or "").strip()
    norm = _normalize(text)

    cfg = _load_cancel_cfg()
    if not cfg:
        _clear_pending(context)
        await update.message.reply_text(
            "Возникла ошибка при использовании команды. Похоже сервис не настроен корректно. Обратитесь к администратору."
        )
        return

    game_username = str(pending.get("game_username", "")).strip()
    if not game_username:
        _clear_pending(context)
        await update.message.reply_text("Не удалось получить данные аккаунта. Попробуйте позже.")
        return

    lots = [OpenLot(**d) for d in (pending.get("lots") or [])]
    page = int(pending.get("page", 0))
    total_pages = max(1, (len(lots) + PER_PAGE - 1) // PER_PAGE)

    # paging
    if norm == "далее":
        if page + 1 >= total_pages:
            await update.message.reply_text(_render_page_text(lots, page=page))
            return
        page += 1
        pending["page"] = page
        _set_pending(context, pending)
        await update.message.reply_text(_render_page_text(lots, page=page))
        return

    # selection by number
    idx = _parse_int_safe(text, default=None)
    if idx is None:
        await update.message.reply_text('Введите номер лота или "далее".')
        return

    if idx < 1 or idx > len(lots):
        await update.message.reply_text("Номер лота указан неверно.")
        return

    chosen = lots[idx - 1]

    # re-check lot still exists and belongs to user, then cancel full remaining
    try:
        now_lots = await asyncio.to_thread(_fetch_open_lots, cfg["sheet_id"], cfg["gid_open_lots"])
    except Exception:
        logger.exception("CANCEL: failed to fetch open lots")
        await update.message.reply_text("Не удалось получить список лотов. Попробуйте позже.")
        return

    now_by_sale: Dict[str, OpenLot] = {l.sale_id: l for l in now_lots}
    actual = now_by_sale.get(chosen.sale_id)

    if actual is None or actual.remaining <= 0:
        await update.message.reply_text("Этот лот уже недоступен. Обновляю список...")
        lots = await _load_user_lots(cfg, game_username)
        if not lots:
            _clear_pending(context)
            await update.message.reply_text("У вас больше нет открытых продаж на бирже.")
            return
        pending["lots"] = [l.__dict__ for l in lots]
        pending["page"] = 0
        _set_pending(context, pending)
        await update.message.reply_text(_render_page_text(lots, page=0))
        return

    if _normalize(actual.seller_login) != _normalize(game_username):
        await update.message.reply_text("Этот лот уже недоступен. Обновляю список...")
        lots = await _load_user_lots(cfg, game_username)
        if not lots:
            _clear_pending(context)
            await update.message.reply_text("У вас больше нет открытых продаж на бирже.")
            return
        pending["lots"] = [l.__dict__ for l in lots]
        pending["page"] = 0
        _set_pending(context, pending)
        await update.message.reply_text(_render_page_text(lots, page=0))
        return

    try:
        payload = {
            cfg["buy_entry_sale_id"]: str(actual.sale_id),
            cfg["buy_entry_seller"]: str(actual.seller_login),
            cfg["buy_entry_buyer"]: str(actual.seller_login),  # самовыкуп
            cfg["buy_entry_item_id"]: str(actual.item_id),
            cfg["buy_entry_amount"]: str(int(actual.remaining)),
            cfg["buy_entry_price"]: str(int(actual.price)),
        }
        status, _preview = await asyncio.to_thread(_submit_trade, cfg["buy_form_url"], payload)
        if not (200 <= status < 400):
            await update.message.reply_text("Не удалось отменить продажу. Попробуйте позже.")
            return
    except Exception:
        logger.exception("CANCEL: submit buy form failed")
        await update.message.reply_text("Не удалось отменить продажу. Попробуйте позже.")
        return

    await update.message.reply_text(
        'Продажа вашего товара отменена. Если вы хотите отменить ещё одну продажу, напишите номер лота.'
    )

    # refresh list after cancellation
    lots = await _load_user_lots(cfg, game_username)
    if not lots:
        _clear_pending(context)
        await update.message.reply_text("У вас больше нет открытых продаж на бирже.")
        return

    pending["lots"] = [l.__dict__ for l in lots]
    pending["page"] = min(page, max(0, (len(lots) - 1) // PER_PAGE))
    _set_pending(context, pending)
    await update.message.reply_text(_render_page_text(lots, page=int(pending["page"])))


# ============================================================
# Helpers
# ============================================================

def _load_cancel_cfg() -> Optional[dict]:
    global _cfg
    if _cfg is not None:
        return _cfg

    sheet_id = os.getenv("SHEET_ID") or None
    gid_open_lots = os.getenv("GID_OPEN_LOTS") or None
    gid_accounts = os.getenv("GID_ACCOUNTS") or None

    buy_form_id = os.getenv("FORM_BUY_ID") or None
    buy_entry_sale_id = os.getenv("FORM_BUY_ENTRY_SALE_ID") or None
    buy_entry_seller = os.getenv("FORM_BUY_ENTRY_SELLER") or None
    buy_entry_buyer = os.getenv("FORM_BUY_ENTRY_BUYER") or None
    buy_entry_item_id = os.getenv("FORM_BUY_ENTRY_ITEM_ID") or None
    buy_entry_amount = os.getenv("FORM_BUY_ENTRY_AMOUNT") or None
    buy_entry_price = os.getenv("FORM_BUY_ENTRY_PRICE") or None

    if not all(
        [
            sheet_id,
            gid_open_lots,
            gid_accounts,
            buy_form_id,
            buy_entry_sale_id,
            buy_entry_seller,
            buy_entry_buyer,
            buy_entry_item_id,
            buy_entry_amount,
            buy_entry_price,
        ]
    ):
        _cfg = None
        return None

    _cfg = {
        "sheet_id": sheet_id,
        "gid_open_lots": gid_open_lots,
        "gid_accounts": gid_accounts,
        "buy_form_url": f"https://docs.google.com/forms/d/e/{buy_form_id}/formResponse",
        "buy_entry_sale_id": f"entry.{buy_entry_sale_id}",
        "buy_entry_seller": f"entry.{buy_entry_seller}",
        "buy_entry_buyer": f"entry.{buy_entry_buyer}",
        "buy_entry_item_id": f"entry.{buy_entry_item_id}",
        "buy_entry_amount": f"entry.{buy_entry_amount}",
        "buy_entry_price": f"entry.{buy_entry_price}",
    }
    return _cfg


def _set_pending(context: ContextTypes.DEFAULT_TYPE, payload: dict) -> None:
    context.user_data[_PENDING_KEY] = payload


def _get_pending(context: ContextTypes.DEFAULT_TYPE) -> Optional[dict]:
    return context.user_data.get(_PENDING_KEY)


def _clear_pending(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(_PENDING_KEY, None)


def _tg_nick_or_fullname(update: Update) -> str:
    u = update.effective_user
    if u and u.username:
        return f"@{u.username}"
    return (u.full_name if u else "unknown").strip()


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _parse_int_safe(s: str, *, default: Optional[int] = 0) -> Optional[int]:
    try:
        return int(str(s).strip())
    except Exception:
        return default


def _csv_export_url(sheet_id: str, gid: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def _fetch_rows_dict(sheet_id: str, gid: str) -> List[dict]:
    url = _csv_export_url(sheet_id, gid)
    r = requests.get(url, timeout=25)
    r.raise_for_status()
    reader = csv.DictReader(StringIO((r.content or b"").decode("utf-8-sig", errors="replace")))
    return list(reader)


def _resolve_game_username(accounts: List[dict], tg_nick: str) -> Optional[str]:
    n = (tg_nick or "").strip()
    if not n:
        return None
    for row in accounts:
        if str(row.get(ACCOUNTS_COL_USER, "")).strip() == n:
            return str(row.get(ACCOUNTS_COL_LOGIN, "")).strip()
    return None


def _fetch_open_lots(sheet_id: str, gid_open_lots: str) -> List[OpenLot]:
    url = _csv_export_url(sheet_id, gid_open_lots)
    r = requests.get(url, timeout=25)
    r.raise_for_status()
    txt = (r.content or b"").decode("utf-8-sig", errors="replace")

    reader = csv.DictReader(StringIO(txt))

    required_cols = {
        LOTS_COL_DATE,
        LOTS_COL_SALE_ID,
        LOTS_COL_SELLER,
        LOTS_COL_ITEM_ID,
        LOTS_COL_ITEM_NAME,
        LOTS_COL_AMOUNT,
        LOTS_COL_PRICE,
    }
    if not required_cols.issubset(reader.fieldnames or []):
        missing = required_cols - set(reader.fieldnames or [])
        raise ValueError(f"В таблице '{SHEET_LOTS_NAME}' отсутствуют колонки: {', '.join(sorted(missing))}")

    lots: List[OpenLot] = []
    for row in reader:
        sale_id = str(row.get(LOTS_COL_SALE_ID, "")).strip()
        if not sale_id:
            continue

        remaining = _parse_int_safe(row.get(LOTS_COL_AMOUNT, ""), default=0) or 0
        price = _parse_int_safe(row.get(LOTS_COL_PRICE, ""), default=0) or 0

        lot = OpenLot(
            ts=str(row.get(LOTS_COL_DATE, "")).strip(),
            sale_id=sale_id,
            seller_login=str(row.get(LOTS_COL_SELLER, "")).strip(),
            item_id=str(row.get(LOTS_COL_ITEM_ID, "")).strip(),
            item_name=str(row.get(LOTS_COL_ITEM_NAME, "")).strip(),
            remaining=int(remaining),
            price=int(price),
        )

        if lot.remaining > 0 and lot.price >= 0:
            lots.append(lot)

    return lots


async def _load_user_lots(cfg: dict, game_username: str) -> List[OpenLot]:
    try:
        all_lots = await asyncio.to_thread(_fetch_open_lots, cfg["sheet_id"], cfg["gid_open_lots"])
    except Exception:
        logger.exception("CANCEL: failed to fetch/parse open lots")
        return []

    gu = _normalize(game_username)
    mine = [l for l in all_lots if _normalize(l.seller_login) == gu]
    # стабильная сортировка: сначала более ранние
    mine.sort(key=lambda x: (x.ts, x.sale_id))
    return mine


def _render_page_text(lots: List[OpenLot], page: int) -> str:
    total = len(lots)
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    start = page * PER_PAGE
    end = min(start + PER_PAGE, total)
    chunk = lots[start:end]

    need_paging = total > PER_PAGE
    if need_paging:
        head = "Какую из ваших продаж вы хотели бы отменить? Напишите номер лота или «далее» для перехода на следующую страницу."
    else:
        head = "Какую из ваших продаж вы хотели бы отменить? Напишите номер лота."

    lines: List[str] = [head, ""]
    for i, l in enumerate(chunk, start=start + 1):
        item = l.item_name or l.item_id
        lines.append(f"{i}. {item} ({l.remaining} шт.) по {l.price} джк")

    if need_paging:
        lines.append("")
        lines.append(f"Стр. {page + 1} из {total_pages}")

    return "\n".join(lines)


def _submit_trade(form_url: str, payload: dict) -> Tuple[int, str]:
    r = requests.post(form_url, data=payload, timeout=25, allow_redirects=False)
    preview = ""
    try:
        preview = (r.text or "")[:200]
    except Exception:
        pass
    return r.status_code, preview