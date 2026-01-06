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

_PENDING_KEY = "pending_buy"
_cfg: Optional[dict] = None


# -----------------------------
# helpers
# -----------------------------
def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _parse_int(s: str, *, default: int = 0) -> int:
    try:
        # "10", "10.0" -> 10
        return int(float(str(s).replace(" ", "").replace(",", ".")))
    except Exception:
        return default


def _parse_money(s: str) -> int:
    # джк у тебя целые, но на всякий: 10 / 10.0 / "10,0"
    return _parse_int(s, default=0)


def _csv_export_url(sheet_id: str, gid: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def _fetch_csv_text(sheet_id: str, gid: str) -> str:
    url = _csv_export_url(sheet_id, gid)
    r = requests.get(url, timeout=25)
    r.raise_for_status()
    # utf-8-sig убирает BOM, если он есть
    return (r.content or b"").decode("utf-8-sig", errors="replace")


@dataclass(frozen=True)
class OpenLot:
    ts: str
    sale_id: str
    seller_user: str
    item_id: str
    item_name: str
    remaining: int
    price: int


def _parse_open_lots(csv_text: str) -> List[OpenLot]:
    """
    Парсим open_lots без зависимости от заголовков:
    - если есть заголовки, пропустим строку по эвристике (sale_id == 'Id операции' и т.п.)
    - если заголовков нет, первая строка будет данными — ок
    """
    rows: List[List[str]] = []
    reader = csv.reader(StringIO(csv_text))
    for r in reader:
        if not r:
            continue
        rows.append(r)

    lots: List[OpenLot] = []
    for r in rows:
        # минимально 7 колонок
        if len(r) < 7:
            continue

        # эвристика для хедера
        maybe_header = _normalize(r[1]) in {"id операции", "id", "sale_id", "id продажи"} or _normalize(r[3]) in {"id товара", "item_id"}
        if maybe_header:
            continue

        ts, sale_id, seller_user, item_id, item_name, rem, price = r[0], r[1], r[2], r[3], r[4], r[5], r[6]
        sale_id = str(sale_id).strip()
        if not sale_id:
            continue

        lot = OpenLot(
            ts=str(ts).strip(),
            sale_id=sale_id,
            seller_user=str(seller_user).strip(),
            item_id=str(item_id).strip(),
            item_name=str(item_name).strip(),
            remaining=_parse_int(rem, default=0),
            price=_parse_money(price),
        )
        if lot.remaining > 0 and lot.price >= 0:
            lots.append(lot)

    return lots


def _resolve_item_from_lots(lots: List[OpenLot], raw_query: str) -> Optional[Tuple[str, str]]:
    """
    Выбираем ОДИН товар (item_id + item_name) по пользовательскому вводу.
    Дальше покупаем только лоты с этим item_id.
    """
    q = _normalize(raw_query)
    if not q:
        return None

    # уникальные варианты товаров из open_lots
    seen = {}
    for l in lots:
        if l.item_id and l.item_name:
            seen[l.item_id] = l.item_name

    # 1) точное совпадение по названию
    for item_id, name in seen.items():
        if _normalize(name) == q:
            return item_id, name

    # 2) "похожесть" (SequenceMatcher) — без score наружу
    from difflib import SequenceMatcher

    best_id = None
    best_name = None
    best_ratio = 0.0
    for item_id, name in seen.items():
        ratio = SequenceMatcher(None, q, _normalize(name)).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_id = item_id
            best_name = name

    if best_id is None:
        return None
    return best_id, best_name


def _split_buy_across_lots(lots: List[OpenLot], need_qty: int) -> Tuple[List[Tuple[OpenLot, int]], int]:
    """
    Выбираем самые дешёвые лоты и распределяем покупку.
    Возвращает список (лот, qty_из_лота) и итоговую стоимость.
    """
    # цена ↑, затем время ↑ (чтобы при одинаковой цене покупать более ранние)
    sorted_lots = sorted(lots, key=lambda l: (l.price, l.ts))
    allocations: List[Tuple[OpenLot, int]] = []
    left = need_qty
    total_cost = 0

    for lot in sorted_lots:
        if left <= 0:
            break
        take = min(left, max(lot.remaining, 0))
        if take <= 0:
            continue
        allocations.append((lot, take))
        total_cost += take * lot.price
        left -= take

    return allocations, total_cost


def _fmt_money_human(x: int) -> str:
    return str(int(x))


def _set_pending(context: ContextTypes.DEFAULT_TYPE, payload: dict) -> None:
    context.user_data[_PENDING_KEY] = payload


def _get_pending(context: ContextTypes.DEFAULT_TYPE) -> Optional[dict]:
    return context.user_data.get(_PENDING_KEY)


def _clear_pending(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(_PENDING_KEY, None)


def _load_buy_cfg() -> Optional[dict]:
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

    pay_form_id = os.getenv("FORM_PAY_ID") or None
    pay_entry_sender = os.getenv("FORM_PAY_ENTRY_SENDER") or None
    pay_entry_sender_comment = os.getenv("FORM_PAY_ENTRY_SENDER_COMMENT") or None
    pay_entry_recipient = os.getenv("FORM_PAY_ENTRY_RECIPIENT") or None
    pay_entry_recipient_comment = os.getenv("FORM_PAY_ENTRY_RECIPIENT_COMMENT") or None
    pay_entry_sum = os.getenv("FORM_PAY_ENTRY_SUM") or None

    if not all([
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
        pay_form_id,
        pay_entry_sender,
        pay_entry_sender_comment,
        pay_entry_recipient,
        pay_entry_recipient_comment,
        pay_entry_sum
    ]):
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
        "pay_form_url": f"https://docs.google.com/forms/d/e/{pay_form_id}/formResponse",
        "pay_entry_sender": f"entry.{pay_entry_sender}",
        "pay_entry_sender_comment": f"entry.{pay_entry_sender_comment}",
        "pay_entry_recipient": f"entry.{pay_entry_recipient}",
        "pay_entry_recipient_comment": f"entry.{pay_entry_recipient_comment}",
        "pay_entry_sum": f"entry.{pay_entry_sum}",
    }
    return _cfg

def _fetch_accounts(sheet_id: str, gid_accounts: str) -> List[dict]:
    """
    Ожидаем, что на листе Счета есть заголовки, включая:
      - Пользователь
      - Баланс
    """
    txt = _fetch_csv_text(sheet_id, gid_accounts)
    reader = csv.DictReader(StringIO(txt))
    return list(reader)


def _get_user_balance(accounts: List[dict], username: str) -> Optional[int]:
    u = (username or "").strip()
    if not u:
        return None

    # строгие названия колонок (как ты и хотел)
    user_col = "Пользователь"
    balance_col = "Баланс"

    for row in accounts:
        if str(row.get(user_col, "")).strip() == u:
            return _parse_money(row.get(balance_col, "0"))
    return None


def _submit_trade(form_url: str, payload: dict) -> Tuple[int, str]:
    """
    POST в Google Forms. Обычно 200 или 302.
    Возвращаем status и первые символы body (для дебага в логах).
    """
    r = requests.post(form_url, data=payload, timeout=25, allow_redirects=False)
    body_preview = ""
    try:
        body_preview = (r.text or "")[:200]
    except Exception:
        pass
    return r.status_code, body_preview


# -----------------------------
# public handlers
# -----------------------------
async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return

    cfg = _load_buy_cfg()
    if not cfg:
        await update.message.reply_text("Команда /buy недоступна: не настроены переменные окружения.")
        return

    # /buy <товар> <количество>
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text('Использование: /buy <товар> <количество>\nПример: /buy Медный блок 10')
        return

    raw_qty = args[-1]
    raw_item = " ".join(args[:-1]).strip()

    qty = _parse_int(raw_qty, default=-1)
    if qty <= 0 or not raw_item:
        await update.message.reply_text('Использование: /buy <товар> <количество>\nПример: /buy Медный блок 10')
        return

    sender = update.effective_user
    sender_username = f"@{sender.username}" if sender and sender.username else (sender.full_name if sender else "unknown")

    await update.message.reply_text("Ищу открытые лоты...")

    # 1) читаем open_lots
    try:
        csv_text = await asyncio.to_thread(_fetch_csv_text, cfg["sheet_id"], cfg["gid_open_lots"])
        all_lots = _parse_open_lots(csv_text)
    except Exception:
        logger.exception("BUY: failed to fetch/parse open_lots")
        await update.message.reply_text("Не удалось прочитать таблицу лотов. Попробуйте позже.")
        return

    if not all_lots:
        await update.message.reply_text("Открытых лотов сейчас нет.")
        return

    resolved = _resolve_item_from_lots(all_lots, raw_item)
    if not resolved:
        await update.message.reply_text(f'Не нашёл товар по запросу: "{raw_item}". Проверьте название.')
        return

    item_id, item_name = resolved
    matching = [l for l in all_lots if l.item_id == item_id]
    if not matching:
        await update.message.reply_text(f'Нет доступных лотов для: "{item_name}".')
        return

    total_available = sum(max(l.remaining, 0) for l in matching)
    if total_available < qty:
        await update.message.reply_text(f'Недостаточно товара "{item_name}". Доступно: {total_available}.')
        return

    allocations, total_cost = _split_buy_across_lots(matching, qty)
    if not allocations:
        await update.message.reply_text(f'Нет доступных лотов для: "{item_name}".')
        return

    _set_pending(context, {
        "buyer": sender_username,
        "item_id": item_id,
        "item_name": item_name,
        "qty": qty,
        "total_cost": total_cost,
        # список покупок по лотам
        "allocations": [
            {
                "sale_id": lot.sale_id,
                "seller": lot.seller_user,
                "item_id": lot.item_id,
                "qty": take,
                "price": lot.price,
            }
            for (lot, take) in allocations
        ],
    })

    await update.message.reply_text(
        f'Покупка {item_name} ({qty}) будет стоить {_fmt_money_human(total_cost)} джк.\n'
        'Вы подтверждаете покупку? Ответьте "да" или "нет".'
    )


def _is_yes(text: str) -> bool:
    t = _normalize(text)
    return t in {"да", "yes", "y", "ага", "ок", "окей"}


def _is_no(text: str) -> bool:
    t = _normalize(text)
    return t in {"нет", "no", "n", "не", "неа"}


async def buy_confirm_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return

    pending = _get_pending(context)
    if not pending:
        return

    text = (update.message.text or "").strip()

    if _is_no(text):
        _clear_pending(context)
        await update.message.reply_text("Покупка отменена.")
        return

    if not _is_yes(text):
        await update.message.reply_text('Мне нужен чёткий ответ: "да" или "нет".')
        return

    cfg = _load_buy_cfg()
    if not cfg:
        _clear_pending(context)
        await update.message.reply_text("Команда /buy недоступна: не настроены переменные окружения.")
        return

    buyer = pending["buyer"]
    total_cost = int(pending["total_cost"])

    # 2) проверяем баланс
    try:
        accounts = await asyncio.to_thread(_fetch_accounts, cfg["sheet_id"], cfg["gid_accounts"])
    except Exception:
        logger.exception("BUY: failed to fetch accounts")
        await update.message.reply_text("Не удалось проверить баланс. Попробуйте позже.")
        return

    balance = _get_user_balance(accounts, buyer)
    if balance is None:
        _clear_pending(context)
        await update.message.reply_text("Не удалось найти ваш счёт в таблице 'Счета'.")
        return

    if balance < total_cost:
        _clear_pending(context)
        await update.message.reply_text(f"Недостаточно средств. Нужно {total_cost} джк, у вас {balance} джк.")
        return

    # 3) отправляем покупки в Google Form (может быть несколько строк, если покупка из разных лотов)
    buy_form_url = cfg["buy_form_url"]
    allocations = pending["allocations"]

    try:
        for a in allocations:
            payload = {
                cfg["buy_entry_sale_id"]: str(a["sale_id"]),
                cfg["buy_entry_seller"]: str(a["seller"]),
                cfg["buy_entry_buyer"]: str(buyer),
                cfg["buy_entry_item_id"]: str(a["item_id"]),
                cfg["buy_entry_amount"]: str(int(a["qty"])),
                cfg["buy_entry_price"]: str(int(a["price"])),
            }
            status, preview = await asyncio.to_thread(_submit_trade, buy_form_url, payload)
            logger.info("BUY: submit status=%s preview=%r", status, preview)
            if not (200 <= status < 400):
                await update.message.reply_text("Не удалось отправить покупку (Google Forms). Попробуйте позже.")
                return
    except Exception:
        logger.exception("BUY: form submit failed")
        await update.message.reply_text("Не удалось отправить покупку (Google Forms). Попробуйте позже.")
        return

    

    # 4) отправляем переводы (FORM_PAY) — по одному на каждого продавца
    pay_form_url = cfg["pay_form_url"]
    item_name = pending.get("item_name", "")
    seller_totals = {}
    for a in allocations:
        seller = str(a.get("seller", "")).strip()
        if not seller:
            continue
        seller_totals[seller] = seller_totals.get(seller, 0) + int(a.get("qty", 0)) * int(a.get("price", 0))

    try:
        for seller, seller_sum in seller_totals.items():
            payload = {
                cfg["pay_entry_sender"]: str(buyer),
                cfg["pay_entry_sender_comment"]: f"Покупка: {item_name} ({seller_sum} джк)",
                cfg["pay_entry_recipient"]: str(seller),
                cfg["pay_entry_recipient_comment"]: f"Продажа: {item_name} ({seller_sum} джк)",
                cfg["pay_entry_sum"]: str(int(seller_sum)),
            }
            status, preview = await asyncio.to_thread(_submit_trade, pay_form_url, payload)
            logger.info("BUY: pay submit status=%s preview=%r seller=%s sum=%s", status, preview, seller, seller_sum)
            if not (200 <= status < 400):
                await update.message.reply_text("Не удалось отправить переводы (Google Forms). Попробуйте позже.")
                return
    except Exception:
        logger.exception("BUY: pay form submit failed")
        await update.message.reply_text("Не удалось отправить переводы (Google Forms). Попробуйте позже.")
        return

    _clear_pending(context)
    await update.message.reply_text("Покупка отправлена.")
