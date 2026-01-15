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

SHEET_ACCOUNTS_NAME = "Счета"
SHEET_LOTS_NAME = "Лоты"

# Accounts sheet columns
ACCOUNTS_COL_USER = "Пользователь"
ACCOUNTS_COL_LOGIN = "Логин"
ACCOUNTS_COL_BALANCE = "Баланс"

# Lots sheet columns
LOTS_COL_DATE = "Дата"
LOTS_COL_SALE_ID = "Id продажи"
LOTS_COL_SELLER = "Продавец"
LOTS_COL_ITEM_ID = "Id товара"
LOTS_COL_ITEM_NAME = "Товар"
LOTS_COL_AMOUNT = "Количество"
LOTS_COL_PRICE = "Цена"


async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return

    cfg = _load_buy_cfg()
    if not cfg:
        await update.message.reply_text("Возникла ошибка при использовании команды. Похоже сервис не настроен корректно. Обратитесь к администратору.")
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            'Эта команда используется со следующими параметрами:\n'
            '/buy <название товара> <количество>\n\n'
            '<название товара> – может быть несколько слов (допустимы опечатки)\n'
            '<количество> – целое число больше ноля\n\n'
            'После ввода команды, я сообщу вам стоимость покупки, после чего мне будет необходимо ваше подтверждение. Узнав стоимость, вы можете отменить покупку.'
            )
        return

    raw_amount = args[-1]
    raw_item = " ".join(args[:-1]).strip()

    amount = _parse_int(raw_amount, default=-1)
    if amount <= 0 or not raw_item:
        await update.message.reply_text('Введено некорректное количество товара. Ожидается целое число больше ноля.')
        return

    buyer_user = _tg_user(update)

    await update.message.reply_text("Ищу открытые лоты...")

    try:
        csv_text = await asyncio.to_thread(_fetch_csv_text, cfg["sheet_id"], cfg["gid_open_lots"])
        all_lots = _parse_open_lots(csv_text)
    except Exception:
        logger.exception("BUY: failed to fetch/parse open_lots")
        await update.message.reply_text("Возникла ошибка при использовании команды. Похоже что-то не так с базой данных. Обратитесь к администратору.")
        return

    if not all_lots:
        await update.message.reply_text("В данный момент на бирже нет товаров.")
        return

    resolved = _resolve_item_from_lots(all_lots, raw_item)
    if not resolved:
        await update.message.reply_text('Не нашёл товар по вашему запросу. Возможно этот товар не продаётся на бирже или название указано неверно.')
        return

    item_id, item_name = resolved
    matching = [l for l in all_lots if l.item_id == item_id]
    if not matching:
        await update.message.reply_text(f'В данный момент на бирже нет товара "{item_name}".')
        return

    total_available = sum(max(l.remaining, 0) for l in matching)
    if total_available < amount:
        await update.message.reply_text(f'В данный момент по вашему запросу недостаточно товара "{item_name}". Доступное количество: {total_available}.')
        return

    allocations, total_cost = _split_buy_across_lots(matching, amount)
    if not allocations:
        await update.message.reply_text(f'В данный момент на бирже нет товара "{item_name}".')
        return

    try:
        accounts = await asyncio.to_thread(_fetch_accounts, cfg["sheet_id"], cfg["gid_accounts"])
    except Exception:
        logger.exception("BUY: failed to fetch accounts")
        await update.message.reply_text("Не удалось проверить баланс. Попробуйте позже.")
        return

    buyer_info = _get_balance_and_login(accounts, buyer_user)
    if buyer_info is None:
        await update.message.reply_text('Ваш аккаунт не найден в базе. Возможно ваше имя пользователя было изменено. Обратитесь к администратору.')
        return

    balance, buyer_login = buyer_info
    if not buyer_login:
        await update.message.reply_text('Возникла ошибка в данных вашего аккаунта. Обратитесь к администратору.')
        return

    if balance < total_cost:
        await update.message.reply_text(
            f'Покупка {item_name} ({amount}) будет стоить {_fmt_money_human(total_cost)} джк.\n'
            f'К сожалению, на вашем счету сейчас {balance} джк — этого не достаточно для совершения операции.\n\n'
            f'Чтобы узнать стоимость доступных товаров используйте команду /info {item_name}'
        )
        return

    _set_pending(
        context,
        {
            "buyer_login": buyer_login,
            "item_id": item_id,
            "item_name": item_name,
            "amount": amount,
            "total_cost": total_cost,
            "allocations": [
                {
                    "sale_id": lot.sale_id,
                    "seller_login": lot.seller_login,
                    "item_id": lot.item_id,
                    "amount": take,
                    "price": lot.price,
                }
                for (lot, take) in allocations
            ],
        },
    )

    await update.message.reply_text(
        f'Покупка {item_name} ({amount}) будет стоить {_fmt_money_human(total_cost)} джк.\n'
        'Вы подтверждаете покупку? Ответьте "да" или "нет".'
    )


async def buy_confirm_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return

    pending = _get_pending(context)
    if not pending:
        return

    text = (update.message.text or "").strip()

    if _is_no(text):
        _clear_pending(context)
        await update.message.reply_text('Покупка отменена.')
        return

    if not _is_yes(text):
        await update.message.reply_text('Мне нужен ответ: "да" или "нет".')
        return

    cfg = _load_buy_cfg()
    if not cfg:
        _clear_pending(context)
        await update.message.reply_text('Возникла ошибка при использовании команды. Похоже сервис не настроен корректно. Обратитесь к администратору.')
        return

    buyer_login = str(pending.get("buyer_login", "")).strip()
    allocations = pending["allocations"]

    try:
        ok = await asyncio.to_thread(_pending_allocations_still_available, cfg, pending)
    except Exception:
        logger.exception("BUY: failed to re-check lots before confirm")
        _clear_pending(context)
        await update.message.reply_text(
            "При отправке покупки возникла ошибка. Похоже что-то не так с открытыми лотами. Попробуйте ещё раз или обратитесь к администратору."
        )
        return

    if not ok:
        _clear_pending(context)
        await update.message.reply_text(
            "Похоже кто-то успел приобрести этот товар раньше вас. Попробуйте совершить покупку ещё раз."
        )
        return

    # 1) FORM_BUY
    buy_form_url = cfg["buy_form_url"]
    try:
        for a in allocations:
            seller_login = str(a.get("seller_login", "")).strip()

            payload = {
                cfg["buy_entry_sale_id"]: str(a["sale_id"]),
                cfg["buy_entry_seller"]: seller_login,
                cfg["buy_entry_buyer"]: str(buyer_login),
                cfg["buy_entry_item_id"]: str(a["item_id"]),
                cfg["buy_entry_amount"]: str(int(a["amount"])),
                cfg["buy_entry_price"]: str(int(a["price"])),
            }
            status, preview = await asyncio.to_thread(_submit_trade, buy_form_url, payload)
            logger.info("BUY: submit(BUY) status=%s preview=%r", status, preview)
            if not (200 <= status < 400):
                _clear_pending(context)
                await update.message.reply_text('Произошла ошибка при совершении покупки. Обратитесь к администратору.')
                return
    except Exception:
        logger.exception("BUY: BUY form submit failed")
        await update.message.reply_text("Произошла ошибка при совершении покупки. Обратитесь к администратору.")
        _clear_pending(context)
        return

    # 2) FORM_PAY
    pay_form_url = cfg["pay_form_url"]
    item_name = str(pending.get("item_name", "")).strip()

    seller_totals: Dict[str, int] = {}
    for a in allocations:
        seller_login = str(a.get("seller_login", "")).strip()
        if not seller_login:
            continue
        if seller_login == buyer_login:
            continue
        seller_totals[seller_login] = (
            seller_totals.get(seller_login, 0)
            + int(a.get("amount", 0)) * int(a.get("price", 0))
        )

    if seller_totals:
        try:
            for seller_login, seller_sum in seller_totals.items():
                payload = {
                    cfg["pay_entry_sender"]: str(buyer_login),
                    cfg["pay_entry_sender_comment"]: f"Покупка: {item_name} ({seller_sum} джк)",
                    cfg["pay_entry_recipient"]: str(seller_login),
                    cfg["pay_entry_recipient_comment"]: f"Продажа: {item_name} ({seller_sum} джк)",
                    cfg["pay_entry_sum"]: str(int(seller_sum)),
                }
                status, preview = await asyncio.to_thread(_submit_trade, pay_form_url, payload)
                logger.info(
                    "BUY: submit(PAY) status=%s preview=%r recipient=%s sum=%s",
                    status,
                    preview,
                    seller_login,
                    seller_sum,
                )
                if not (200 <= status < 400):
                    _clear_pending(context)
                    await update.message.reply_text("Произошла ошибка при переводе средств. Обратитесь к администратору.")
                    return
        except Exception:
            logger.exception("BUY: PAY form submit failed")
            await update.message.reply_text("Произошла ошибка при переводе средств. Обратитесь к администратору.")
            _clear_pending(context)
            return

    _clear_pending(context)
    await update.message.reply_text("Покупка совершена.")

# ============================================================

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
            pay_form_id,
            pay_entry_sender,
            pay_entry_sender_comment,
            pay_entry_recipient,
            pay_entry_recipient_comment,
            pay_entry_sum,
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
        "pay_form_url": f"https://docs.google.com/forms/d/e/{pay_form_id}/formResponse",
        "pay_entry_sender": f"entry.{pay_entry_sender}",
        "pay_entry_sender_comment": f"entry.{pay_entry_sender_comment}",
        "pay_entry_recipient": f"entry.{pay_entry_recipient}",
        "pay_entry_recipient_comment": f"entry.{pay_entry_recipient_comment}",
        "pay_entry_sum": f"entry.{pay_entry_sum}",
    }
    return _cfg


def _is_yes(text: str) -> bool:
    t = _normalize(text)
    return t in {"да", "yes", "y", "ага", "ок", "окей"}


def _is_no(text: str) -> bool:
    t = _normalize(text)
    return t in {"нет", "no", "n", "не", "неа"}


def _set_pending(context: ContextTypes.DEFAULT_TYPE, payload: dict) -> None:
    context.user_data[_PENDING_KEY] = payload


def _get_pending(context: ContextTypes.DEFAULT_TYPE) -> Optional[dict]:
    return context.user_data.get(_PENDING_KEY)


def _clear_pending(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(_PENDING_KEY, None)


def _tg_user(update: Update) -> str:
    u = update.effective_user
    if u and u.username:
        return f"@{u.username}"
    return (u.full_name if u else "unknown").strip()


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _parse_int(s: str, *, default: int = 0) -> int:
    try:
        return int(float(str(s).replace(" ", "").replace(",", ".")))
    except Exception:
        return default


def _parse_money(s: str) -> int:
    return _parse_int(s, default=0)


def _fmt_money_human(x: int) -> str:
    return str(int(x))


def _csv_export_url(sheet_id: str, gid: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def _fetch_csv_text(sheet_id: str, gid: str) -> str:
    url = _csv_export_url(sheet_id, gid)
    r = requests.get(url, timeout=25)
    r.raise_for_status()
    return (r.content or b"").decode("utf-8-sig", errors="replace")


def _fetch_accounts(sheet_id: str, gid_accounts: str) -> List[dict]:
    txt = _fetch_csv_text(sheet_id, gid_accounts)
    reader = csv.DictReader(StringIO(txt))
    return list(reader)


def _find_account_by_user(accounts: List[dict], tg_user: str) -> Optional[dict]:
    u = (tg_user or "").strip()
    if not u:
        return None
    for row in accounts:
        if str(row.get(ACCOUNTS_COL_USER, "")).strip() == u:
            return row
    return None


def _get_balance_and_login(accounts: List[dict], tg_user: str) -> Optional[Tuple[int, str]]:
    row = _find_account_by_user(accounts, tg_user)
    if not row:
        return None
    balance = _parse_money(row.get(ACCOUNTS_COL_BALANCE, "0"))
    login = str(row.get(ACCOUNTS_COL_LOGIN, "")).strip()
    return balance, login


@dataclass(frozen=True)
class OpenLot:
    ts: str
    sale_id: str
    seller_login: str
    item_id: str
    item_name: str
    remaining: int
    price: int


def _parse_open_lots(csv_text: str) -> List[OpenLot]:
    reader = csv.DictReader(StringIO(csv_text))

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
        sale_id = str(row[LOTS_COL_SALE_ID]).strip()
        if not sale_id:
            continue

        lot = OpenLot(
            ts=str(row[LOTS_COL_DATE]).strip(),
            sale_id=sale_id,
            seller_login=str(row[LOTS_COL_SELLER]).strip(),
            item_id=str(row[LOTS_COL_ITEM_ID]).strip(),
            item_name=str(row[LOTS_COL_ITEM_NAME]).strip(),
            remaining=_parse_int(row[LOTS_COL_AMOUNT]),
            price=_parse_money(row[LOTS_COL_PRICE]),
        )

        if lot.remaining > 0 and lot.price >= 0:
            lots.append(lot)

    return lots


def _resolve_item_from_lots(lots: List[OpenLot], raw_query: str) -> Optional[Tuple[str, str]]:
    q = _normalize(raw_query)
    if not q:
        return None

    seen: Dict[str, str] = {}
    for l in lots:
        if l.item_id and l.item_name:
            seen[l.item_id] = l.item_name

    for item_id, name in seen.items():
        if _normalize(name) == q:
            return item_id, name

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


def _split_buy_across_lots(lots: List[OpenLot], need_amount: int) -> Tuple[List[Tuple[OpenLot, int]], int]:
    sorted_lots = sorted(lots, key=lambda l: (l.price, l.ts))
    allocations: List[Tuple[OpenLot, int]] = []
    left = need_amount
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


def _submit_trade(form_url: str, payload: dict) -> Tuple[int, str]:
    r = requests.post(form_url, data=payload, timeout=25, allow_redirects=False)
    body_preview = ""
    try:
        body_preview = (r.text or "")[:200]
    except Exception:
        pass
    return r.status_code, body_preview


def _pending_allocations_still_available(cfg: dict, pending: dict) -> bool:
    csv_text = _fetch_csv_text(cfg["sheet_id"], cfg["gid_open_lots"])
    lots_now = _parse_open_lots(csv_text)

    lots_by_sale_id: Dict[str, OpenLot] = {l.sale_id: l for l in lots_now}

    allocations = pending.get("allocations") or []
    for a in allocations:
        sale_id = str(a.get("sale_id", "")).strip()
        if not sale_id:
            return False

        lot = lots_by_sale_id.get(sale_id)
        if lot is None:
            return False

        if str(a.get("item_id", "")).strip() != lot.item_id:
            return False
        if str(a.get("seller_login", "")).strip() != lot.seller_login:
            return False
        if int(a.get("price", -1)) != lot.price:
            return False

        need_amount = int(a.get("amount", 0))
        if lot.remaining < need_amount:
            return False

    return True