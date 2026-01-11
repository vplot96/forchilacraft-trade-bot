from __future__ import annotations

import csv
import logging
import os
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from io import StringIO
from typing import Dict, List, Optional, Tuple

import requests
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

UTC = timezone.utc

# =========================
# Telegram handler
# =========================

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /price <название товара>
    """
    if update.message is None:
        return

    query = " ".join(context.args).strip() if context and context.args else ""
    if not query:
        await update.message.reply_text(
            "Название товара не указано. Чтобы узнать курс товара используйте команду следующим образом:\n"
            "/price <название товара>"
        )
        return

    try:
        text = build_price_text(query)
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception:
        logger.exception("PRICE command failed")
        await update.message.reply_text("Не удалось выполнить команду /price.")


# =========================
# Core logic
# =========================

def build_price_text(query: str) -> str:
    sheet_id = _require_env("SHEET_ID")
    gid_items = _require_env("GID_ITEMS")
    gid_open_lots = _require_env("GID_OPEN_LOTS")
    gid_sales = _require_env("GID_SALES")

    id_to_name, _name_to_id = _load_items_map(sheet_id, gid_items)

    found = _find_item_by_name(id_to_name, query, threshold=0.65)
    if not found:
        return "Не нашёл товаров по введённому названию. Возможно этот товар не продаётся на бирже."

    item_id, item_name, _ratio = found

    lots_rows = _fetch_rows_dict(sheet_id, gid_open_lots)
    sales_rows = _fetch_rows_dict(sheet_id, gid_sales)

    _ensure_columns(lots_rows, ["Id товара", "Количество", "Цена"], "Лоты")
    _ensure_columns(sales_rows, ["Отметка времени", "Id товара", "Количество", "Цена", "Продавец", "Покупатель"], "Сделки")

    # ---- sales for this item ----
    sales_item: List[Tuple[datetime, float, float]] = []
    for r in sales_rows:
        if str(r.get("Id товара", "")).strip() != item_id:
            continue

        dt = _parse_dt(r.get("Отметка времени"))
        if not dt:
            continue

        seller = str(r.get("Продавец", "")).strip()
        buyer = str(r.get("Покупатель", "")).strip()
        if seller and buyer and _normalize(seller) == _normalize(buyer):
            continue

        qty = _to_float(r.get("Количество"))
        price = _to_float(r.get("Цена"))
        if qty <= 0 or price <= 0:
            continue

        sales_item.append((dt, qty, price))

    avg_all = _weighted_avg(sales_item)

    sales_item_sorted = sorted(sales_item, key=lambda x: x[0], reverse=True)
    last10 = sales_item_sorted[:10]
    avg_recent10 = _weighted_avg(last10)

    # ---- lots for this item (stock + min price + qty at min price) ----
    stock_market = 0.0
    min_price: Optional[float] = None
    qty_at_min_price = 0.0

    for r in lots_rows:
        if str(r.get("Id товара", "")).strip() != item_id:
            continue

        qty = _to_float(r.get("Количество"))
        price = _to_float(r.get("Цена"))
        if qty <= 0 or price <= 0:
            continue

        stock_market += qty

        if min_price is None or price < min_price:
            min_price = price
            qty_at_min_price = qty
        elif price == min_price:
            qty_at_min_price += qty

    # ---- render like screenshot ----
    lines: List[str] = []

    lines.append(f'*Цена продажи "{item_name}" (ср.)*')

    if avg_all is None:
        lines.append("— нет данных")
    else:
        lines.append(f"— {_format_num(avg_all)} джк (всё время)")
        lines.append(f"— {_format_num(avg_recent10)} джк (недавно)")

    lines.append("")  # blank line between blocks

    lines.append(f"*Сейчас продаются ({_format_num(stock_market)})*")
    if min_price is None or stock_market <= 0:
        lines.append("— товар отсутствует")
    else:
        # как на скрине: 1. Алмаз (20) по 45 джк
        lines.append(f"1. {item_name} ({_format_num(qty_at_min_price)}) по {_format_num(min_price)} джк")

    return "\n".join(lines)


# =========================
# Helpers (минимум необходимого)
# =========================

def _require_env(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"{name} is not set")
    return v


def _csv_export_url(sheet_id: str, gid: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def _fetch_rows_dict(sheet_id: str, gid: str) -> List[dict]:
    url = _csv_export_url(sheet_id, gid)
    r = requests.get(url, timeout=25)
    r.raise_for_status()
    reader = csv.DictReader(StringIO((r.content or b"").decode("utf-8-sig", errors="replace")))
    return [row for row in reader if any((v or "").strip() for v in row.values())]


def _ensure_columns(rows: List[dict], required: List[str], table_name: str) -> None:
    if not rows:
        return
    fields = set(rows[0].keys())
    missing = [c for c in required if c not in fields]
    if missing:
        raise RuntimeError(f"В таблице '{table_name}' отсутствуют колонки: {', '.join(missing)}")


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _to_float(x) -> float:
    s = str(x or "").strip().replace(" ", "").replace(",", ".")
    if not s:
        return 0.0
    try:
        return float(s)
    except Exception:
        return 0.0


def _parse_dt(x) -> Optional[datetime]:
    s = str(x or "").strip()
    if not s:
        return None

    fmts = [
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %I:%M %p",
    ]
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _format_num(x: float) -> str:
    if abs(x - round(x)) < 1e-9:
        return f"{int(round(x)):,}".replace(",", " ")
    return f"{x:,.2f}".replace(",", " ").replace(".", ",")


def _weighted_avg(sales: List[Tuple[datetime, float, float]]) -> Optional[float]:
    denom = sum(q for _, q, _ in sales)
    if denom <= 0:
        return None
    num = sum(q * p for _, q, p in sales)
    return num / denom


def _load_items_map(sheet_id: str, gid_items: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    rows = _fetch_rows_dict(sheet_id, gid_items)
    _ensure_columns(rows, ["Id товара", "Название"], "Товары")

    id_to_name: Dict[str, str] = {}
    name_to_id: Dict[str, str] = {}

    for r in rows:
        item_id = str(r.get("Id товара", "")).strip()
        name = str(r.get("Название", "")).strip()
        if not item_id:
            continue
        if not name:
            name = item_id
        id_to_name[item_id] = name
        name_to_id[_normalize(name)] = item_id

    return id_to_name, name_to_id


def _find_item_by_name(
    id_to_name: Dict[str, str],
    query: str,
    threshold: float,
) -> Optional[Tuple[str, str, float]]:
    q = _normalize(query)
    if not q:
        return None

    best_id: Optional[str] = None
    best_name: Optional[str] = None
    best_ratio = 0.0

    for item_id, name in id_to_name.items():
        n = _normalize(name)

        if n == q:
            return item_id, name, 1.0

        ratio = SequenceMatcher(None, q, n).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_id = item_id
            best_name = name

    if best_id is None or best_name is None:
        return None

    if best_ratio < threshold:
        return None

    return best_id, best_name, best_ratio