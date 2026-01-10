# commands/info.py
from __future__ import annotations

import csv
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from io import StringIO
from typing import Dict, List, Optional, Tuple

import requests
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

TOP_N = 7
DAYS_30 = 30
UTC = timezone.utc


async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /info
    /info <название товара>
    """
    if update.message is None:
        return

    try:
        arg = " ".join(context.args) if context and context.args else ""

        # /info (общая сводка): всегда показываем ожидание
        if not arg:
            wait_msg = await update.message.reply_text("Собираю актуальную статистику...")
            text = build_global_text()
            await wait_msg.edit_text(text, parse_mode="Markdown")
            return

        # /info <товар>: сначала быстрый поиск товара в "Товары"
        sheet_id, gid_items, gid_open_lots, gid_sales = _load_cfg()
        id_to_name, name_to_id = _load_items_map(sheet_id, gid_items)

        q = (arg or "").strip()
        item_id = q if q in id_to_name else name_to_id.get(_normalize(q))
        if not item_id:
            await update.message.reply_text("Не нашёл товаров по вашему запросу. Возможно этот товар не продаётся на бирже.")
            return

        wait_msg = await update.message.reply_text("Собираю актуальную статистику...")
        text = build_item_text(item_id=item_id, item_name=id_to_name.get(item_id, item_id))
        await wait_msg.edit_text(text, parse_mode="Markdown")

    except Exception:
        logger.exception("INFO command failed")
        await update.message.reply_text("Не удалось выполнить команду /info.")


def build_global_text() -> str:
    sheet_id, gid_items, gid_open_lots, gid_sales = _load_cfg()

    id_to_name, _ = _load_items_map(sheet_id, gid_items)

    lots_rows = _fetch_rows_dict(sheet_id, gid_open_lots)
    sales_rows = _fetch_rows_dict(sheet_id, gid_sales)

    _ensure_columns(lots_rows, ["Id товара", "Количество", "Цена"], "Лоты")
    _ensure_columns(sales_rows, ["Отметка времени", "Id товара", "Количество", "Цена"], "Сделки")

    # ---- sales grouped by item_id ----
    sales_by_item: Dict[str, List[Tuple[datetime, float, float]]] = {}
    for r in sales_rows:
        dt = _parse_dt(r.get("Отметка времени"))
        if not dt:
            continue
        item_id = str(r.get("Id товара", "")).strip()
        qty = _to_float(r.get("Количество"))
        price = _to_float(r.get("Цена"))
        if not item_id or qty <= 0 or price <= 0:
            continue
        sales_by_item.setdefault(item_id, []).append((dt, qty, price))

    # ---- lots grouped by item_id ----
    lots_by_item: Dict[str, List[Tuple[float, float]]] = {}
    stock_by_item: Dict[str, float] = {}
    for r in lots_rows:
        item_id = str(r.get("Id товара", "")).strip()
        qty = _to_float(r.get("Количество"))
        price = _to_float(r.get("Цена"))
        if not item_id or qty <= 0 or price <= 0:
            continue
        lots_by_item.setdefault(item_id, []).append((price, qty))
        stock_by_item[item_id] = stock_by_item.get(item_id, 0.0) + qty

    now = datetime.now(tz=UTC)
    cutoff_30d = now - timedelta(days=DAYS_30)

    def name(item_id: str) -> str:
        return id_to_name.get(item_id, item_id)

    # 1) Top turnover all-time
    top_turnover_all: List[Tuple[str, float, float]] = []
    for item_id, ss in sales_by_item.items():
        t = _turnover(ss)
        if t > 0:
            top_turnover_all.append((item_id, t, _sum_qty(ss)))
    top_turnover_all.sort(key=lambda x: x[1], reverse=True)
    top_turnover_all = top_turnover_all[:TOP_N]

    # 2) Top turnover last 30 days (оставляем как было)
    top_turnover_30d: List[Tuple[str, float, float]] = []
    for item_id, ss in sales_by_item.items():
        ss30 = [(dt, q, p) for (dt, q, p) in ss if dt >= cutoff_30d]
        if not ss30:
            continue
        t30 = _turnover(ss30)
        if t30 > 0:
            top_turnover_30d.append((item_id, t30, _sum_qty(ss30)))
    top_turnover_30d.sort(key=lambda x: x[1], reverse=True)
    top_turnover_30d = top_turnover_30d[:TOP_N]

    # 3) Самые дешёвые товары
    cheapest_items: List[Tuple[str, float, float]] = []  # (item_id, min_price, qty_at_min_price)
    for item_id, lots in lots_by_item.items():
        if not lots:
            continue
        min_price = min(p for p, _q in lots)
        qty_at_min = sum(q for p, q in lots if p == min_price)
        cheapest_items.append((item_id, min_price, qty_at_min))
    cheapest_items.sort(key=lambda x: x[1])
    cheapest_items = cheapest_items[:TOP_N]

    # 4) Самые редкие товары
    rarest_items: List[Tuple[str, float, float]] = []  # (item_id, min_price, total_stock)
    for item_id, stock in stock_by_item.items():
        if stock <= 0:
            continue
        lots = lots_by_item.get(item_id, [])
        if not lots:
            continue
        min_price = min(p for p, _q in lots)
        rarest_items.append((item_id, min_price, stock))
    rarest_items.sort(key=lambda x: x[2])  # по stock
    rarest_items = rarest_items[:TOP_N]

    def _render_turnover_rows(rows: List[Tuple[str, float, float]]) -> List[str]:
        out: List[str] = []
        for i, (item_id, t, q) in enumerate(rows, 1):
            out.append(f"{i}. {name(item_id)}: {_format_num(t)} джк ({_format_num(q)} шт.)")
        return out

    lines: List[str] = []
    lines.append("*Сводка биржи*")
    lines.append("")

    lines.append("*Топ товаров по обороту (за всё время)*")
    if top_turnover_all:
        lines.extend(_render_turnover_rows(top_turnover_all))
    else:
        lines.append("— нет данных")
    lines.append("")

    lines.append("*Топ товаров по обороту (за 30 дней)*")
    if top_turnover_30d:
        lines.extend(_render_turnover_rows(top_turnover_30d))
    else:
        lines.append("— нет данных")
    lines.append("")

    lines.append("*Самые дешёвые товары на бирже*")
    if cheapest_items:
        for i, (item_id, min_price, qty_at_min) in enumerate(cheapest_items, 1):
            lines.append(f"{i}. {name(item_id)} ({_format_num(qty_at_min)}) по {_format_num(min_price)} джк")
    else:
        lines.append("— нет данных")
    lines.append("")

    lines.append("*Самые редкие товары на бирже*")
    if rarest_items:
        for i, (item_id, min_price, stock) in enumerate(rarest_items, 1):
            lines.append(f"{i}. {name(item_id)} ({_format_num(stock)}) по {_format_num(min_price)} джк")
    else:
        lines.append("— нет данных")
    lines.append("")
    lines.append("Подробнее: /info <название товара>")

    return "\n".join(lines)


def build_item_text(item_id: str, item_name: str) -> str:
    sheet_id, gid_items, gid_open_lots, gid_sales = _load_cfg()

    lots_rows = _fetch_rows_dict(sheet_id, gid_open_lots)
    sales_rows = _fetch_rows_dict(sheet_id, gid_sales)

    _ensure_columns(lots_rows, ["Id товара", "Количество", "Цена"], "Лоты")
    _ensure_columns(sales_rows, ["Отметка времени", "Id товара", "Количество", "Цена"], "Сделки")

    # ---- соберём все продажи (для "в месяц" по истории биржи) ----
    all_sales: List[Tuple[datetime, float, float, str]] = []
    for r in sales_rows:
        dt = _parse_dt(r.get("Отметка времени"))
        if not dt:
            continue
        iid = str(r.get("Id товара", "")).strip()
        qty = _to_float(r.get("Количество"))
        price = _to_float(r.get("Цена"))
        if not iid or qty <= 0 or price <= 0:
            continue
        all_sales.append((dt, qty, price, iid))

    now = datetime.now(tz=UTC)
    first_sale_dt = min((dt for dt, _q, _p, _iid in all_sales), default=None)
    if first_sale_dt is None:
        months_since_first = 1.0
    else:
        days = max(1, (now - first_sale_dt).days)
        months_since_first = max(1.0, days / 30.0)

    # ---- продажи по этому товару ----
    sales_item: List[Tuple[datetime, float, float]] = []
    for dt, qty, price, iid in all_sales:
        if iid == item_id:
            sales_item.append((dt, qty, price))

    avg_all = _weighted_avg(sales_item)

    # последние 10 сделок (недавно)
    sales_item_sorted = sorted(sales_item, key=lambda x: x[0], reverse=True)
    last10 = sales_item_sorted[:10]
    avg_recent10 = _weighted_avg(last10)

    sold_all = _sum_qty(sales_item)
    sold_per_month = (sold_all / months_since_first) if sold_all > 0 else 0.0

    # ---- лоты по этому товару (группировка по цене) ----
    stock_market = 0.0
    grouped: Dict[float, float] = {}  # price -> total qty on market
    for r in lots_rows:
        if str(r.get("Id товара", "")).strip() != item_id:
            continue
        qty = _to_float(r.get("Количество"))
        price = _to_float(r.get("Цена"))
        if qty <= 0 or price <= 0:
            continue

        stock_market += qty
        pkey = round(float(price), 6)
        grouped[pkey] = grouped.get(pkey, 0.0) + float(qty)

    # ---- render ----
    lines: List[str] = []
    lines.append(f'*Сводка по товару "{item_name}"*')
    lines.append("")
    lines.append(f"Доступно на бирже: {_format_num(stock_market)} шт")
    lines.append("")

    lines.append("*Цена продажи (ср.)*")
    if avg_all is None:
        lines.append("— нет данных")
    else:
        lines.append(f"— {_format_num(avg_all)} джк (всё время)")
        if avg_recent10 is None:
            lines.append("— нет данных (недавно)")
        else:
            lines.append(f"— {_format_num(avg_recent10)} джк (недавно)")
    lines.append("")

    lines.append("*Объёмы продаж*")
    if sold_all <= 0:
        lines.append("— нет данных")
    else:
        lines.append(f"— {_format_num(sold_all)} шт (всё время)")
        lines.append(f"— {_format_num(sold_per_month)} шт (в месяц)")
    lines.append("")

    lines.append("*Сейчас продаются*")
    if not grouped:
        lines.append("— товар отсутствует")
        return "\n".join(lines)

    price_levels = sorted(grouped.items(), key=lambda x: x[0])  # (price, qty_sum)
    if len(price_levels) > TOP_N:
        shown = price_levels[: TOP_N - 1]
        for i, (price, qty_sum) in enumerate(shown, 1):
            lines.append(f"{i}. {item_name} ({_format_num(qty_sum)}) по {_format_num(price)} джк")
        lines.append(f"{TOP_N}. ...")
    else:
        for i, (price, qty_sum) in enumerate(price_levels, 1):
            lines.append(f"{i}. {item_name} ({_format_num(qty_sum)}) по {_format_num(price)} джк")

    return "\n".join(lines)


# ============================================================
# Helpers
# ============================================================

def _load_cfg() -> Tuple[str, str, str, str]:
    sheet_id = os.getenv("SHEET_ID", "").strip()
    gid_items = os.getenv("GID_ITEMS", "").strip()
    gid_open_lots = os.getenv("GID_OPEN_LOTS", "").strip()
    gid_sales = os.getenv("GID_SALES", "").strip()

    if not sheet_id:
        raise RuntimeError("SHEET_ID is not set")
    if not gid_items:
        raise RuntimeError("GID_ITEMS is not set")
    if not gid_open_lots:
        raise RuntimeError("GID_OPEN_LOTS is not set")
    if not gid_sales:
        raise RuntimeError("GID_SALES is not set")

    return sheet_id, gid_items, gid_open_lots, gid_sales


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
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _format_num(x: float) -> str:
    if abs(x - round(x)) < 1e-9:
        return f"{int(round(x)):,}".replace(",", " ")
    return f"{x:,.2f}".replace(",", " ").replace(".", ",")


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


def _weighted_avg(sales: List[Tuple[datetime, float, float]]) -> Optional[float]:
    denom = sum(q for _, q, _ in sales)
    if denom <= 0:
        return None
    num = sum(q * p for _, q, p in sales)
    return num / denom


def _turnover(sales: List[Tuple[datetime, float, float]]) -> float:
    return sum(q * p for _, q, p in sales)


def _sum_qty(sales: List[Tuple[datetime, float, float]]) -> float:
    return sum(q for _, q, _ in sales)