# commands/info.py
from __future__ import annotations

import os
import re
import csv
import logging
from io import StringIO
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

TOP_N = 7
CHEAP_UNITS_SAMPLE = 100
DAYS_30 = 30

UTC = timezone.utc

def require_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"{name} is not set")
    return v

SHEET_ID = require_env("SHEET_ID")
GID_ITEMS = require_env("GID_ITEMS")
GID_OPEN_LOTS = require_env("GID_OPEN_LOTS")
GID_SALES = require_env("GID_SALES")  # добавь в env

def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def csv_url_for(gid: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}"

def fetch_rows_dict(gid: str) -> List[dict]:
    r = requests.get(csv_url_for(gid), timeout=25)
    r.raise_for_status()
    reader = csv.DictReader(StringIO(r.content.decode("utf-8-sig")))
    return [row for row in reader if any((v or "").strip() for v in row.values())]

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

def _pick_key(row: dict, variants: List[str]) -> Optional[str]:
    keys = list(row.keys())
    nk = {normalize(k): k for k in keys}
    for v in variants:
        vv = normalize(v)
        if vv in nk:
            return nk[vv]
    return None

def _format_num(x: float) -> str:
    if abs(x - round(x)) < 1e-9:
        return f"{int(round(x)):,}".replace(",", " ")
    return f"{x:,.2f}".replace(",", " ").replace(".", ",")

# ---------- Load mappings ----------
def load_items_map() -> Tuple[Dict[str, str], Dict[str, str]]:
    rows = fetch_rows_dict(GID_ITEMS)
    if not rows:
        return {}, {}

    # определяем реальные имена колонок
    sample = rows[0]
    k_id = _pick_key(sample, ["Id товара", "item_id", "id"])
    k_name = _pick_key(sample, ["Название товара", "Название", "item_name", "name"])

    if not k_id or not k_name:
        raise RuntimeError("Лист 'Товары': нужны колонки 'Id товара' и 'Название товара'.")

    id_to_name: Dict[str, str] = {}
    name_to_id: Dict[str, str] = {}

    for r in rows:
        item_id = str(r.get(k_id, "")).strip()
        name = str(r.get(k_name, "")).strip()
        if not item_id:
            continue
        if not name:
            name = item_id
        id_to_name[item_id] = name
        name_to_id[normalize(name)] = item_id

    return id_to_name, name_to_id

# ---------- Calculations ----------
def _weighted_avg(sales: List[Tuple[datetime, float, float]]) -> Optional[float]:
    # sales: (ts, qty, price)
    denom = sum(q for _, q, _ in sales)
    if denom <= 0:
        return None
    num = sum(q * p for _, q, p in sales)
    return num / denom

def _turnover(sales: List[Tuple[datetime, float, float]]) -> float:
    return sum(q * p for _, q, p in sales)

def _sum_qty(sales: List[Tuple[datetime, float, float]]) -> float:
    return sum(q for _, q, _ in sales)

def _avg_price_of_cheapest_units(lots: List[Tuple[float, float]], units: int) -> Optional[Tuple[float, float]]:
    # lots: (price, qty)
    if not lots:
        return None
    lots = sorted(lots, key=lambda x: x[0])
    remaining = float(units)
    cost = 0.0
    taken = 0.0
    for price, qty in lots:
        if remaining <= 0:
            break
        take = min(qty, remaining)
        if take <= 0:
            continue
        cost += take * price
        taken += take
        remaining -= take
    if taken <= 0:
        return None
    return cost / taken, taken

# ---------- Builders ----------
def build_global_text() -> str:
    id_to_name, _ = load_items_map()

    lots_rows = fetch_rows_dict(GID_OPEN_LOTS)
    sales_rows = fetch_rows_dict(GID_SALES)

    # lots keys
    lot_sample = lots_rows[0] if lots_rows else {}
    lk_item = _pick_key(lot_sample, ["Id товара", "item_id"])
    lk_qty = _pick_key(lot_sample, ["Количество", "qty", "amount"])
    lk_price = _pick_key(lot_sample, ["Цена", "price"])

    if lots_rows and (not lk_item or not lk_qty or not lk_price):
        raise RuntimeError("Лист 'Лоты': нужны колонки 'Id товара', 'Количество', 'Цена'.")

    # sales keys
    sale_sample = sales_rows[0] if sales_rows else {}
    sk_ts = _pick_key(sale_sample, ["Отметка времени", "Timestamp", "Дата", "Время"])
    sk_item = _pick_key(sale_sample, ["Id товара", "item_id"])
    sk_qty = _pick_key(sale_sample, ["Количество", "qty", "amount"])
    sk_price = _pick_key(sale_sample, ["Цена", "price"])

    if sales_rows and (not sk_ts or not sk_item or not sk_qty or not sk_price):
        raise RuntimeError("Лист сделок: нужны 'Отметка времени', 'Id товара', 'Количество', 'Цена'.")

    # group sales
    sales_by_item: Dict[str, List[Tuple[datetime, float, float]]] = {}
    for r in sales_rows:
        dt = _parse_dt(r.get(sk_ts))
        if not dt:
            continue
        item_id = str(r.get(sk_item, "")).strip()
        qty = _to_float(r.get(sk_qty))
        price = _to_float(r.get(sk_price))
        if not item_id or qty <= 0 or price <= 0:
            continue
        sales_by_item.setdefault(item_id, []).append((dt, qty, price))

    # group lots
    lots_by_item: Dict[str, List[Tuple[float, float]]] = {}
    stock_by_item: Dict[str, float] = {}
    for r in lots_rows:
        item_id = str(r.get(lk_item, "")).strip()
        qty = _to_float(r.get(lk_qty))
        price = _to_float(r.get(lk_price))
        if not item_id or qty <= 0 or price <= 0:
            continue
        lots_by_item.setdefault(item_id, []).append((price, qty))
        stock_by_item[item_id] = stock_by_item.get(item_id, 0.0) + qty

    now = datetime.now(tz=UTC)
    cutoff_30d = now - timedelta(days=DAYS_30)

    def name(item_id: str) -> str:
        return id_to_name.get(item_id, item_id)

    # 1) top turnover all-time
    top_turnover = []
    for item_id, ss in sales_by_item.items():
        t = _turnover(ss)
        if t > 0:
            top_turnover.append((item_id, t, _sum_qty(ss)))
    top_turnover.sort(key=lambda x: x[1], reverse=True)
    top_turnover = top_turnover[:TOP_N]

    # 2) brightest avg price change (30d vs all)
    deltas = []
    for item_id, ss in sales_by_item.items():
        ss30 = [(dt, q, p) for (dt, q, p) in ss if dt >= cutoff_30d]
        if not ss30:
            continue
        avg_all = _weighted_avg(ss)
        avg_30 = _weighted_avg(ss30)
        if avg_all is None or avg_30 is None:
            continue
        if round(avg_all, 2) == round(avg_30, 2):
            continue
        delta = avg_30 - avg_all
        deltas.append((item_id, delta, avg_30, avg_all))
    deltas.sort(key=lambda x: abs(x[1]), reverse=True)
    deltas = deltas[:TOP_N]

    # 3) cheapest items by avg of 100 cheapest units
    cheapest = []
    for item_id, lots in lots_by_item.items():
        r = _avg_price_of_cheapest_units(lots, CHEAP_UNITS_SAMPLE)
        if not r:
            continue
        avg, taken = r
        cheapest.append((item_id, avg, taken))
    cheapest.sort(key=lambda x: x[1])
    cheapest = cheapest[:TOP_N]

    # 4) rarest by stock on market
    rarest = [(item_id, stock) for item_id, stock in stock_by_item.items() if stock > 0]
    rarest.sort(key=lambda x: x[1])
    rarest = rarest[:TOP_N]

    lines: List[str] = []
    lines.append("📊 *Сводка биржи*")
    lines.append("")

    lines.append("💰 *Топ по обороту (за всё время)*")
    if top_turnover:
        for i, (item_id, t, q) in enumerate(top_turnover, 1):
            lines.append(f"{i}. {name(item_id)} — {_format_num(t)} джк (продано: {_format_num(q)})")
    else:
        lines.append("— нет данных по сделкам")
    lines.append("")

    lines.append("📈 *Яркие изменения средней цены (30 дней vs всё время)*")
    if deltas:
        for i, (item_id, delta, avg30, avgall) in enumerate(deltas, 1):
            sign = "+" if delta > 0 else "−"
            lines.append(
                f"{i}. {name(item_id)} — {_format_num(avg30)} → {_format_num(avgall)} ({sign}{_format_num(abs(delta))})"
            )
    else:
        lines.append("— за последние 30 дней средняя цена не изменилась ни по одному товару")
    lines.append("")

    lines.append(f"💸 *Самые дешёвые товары (ср. по {CHEAP_UNITS_SAMPLE} самым дешёвым единицам)*")
    if cheapest:
        for i, (item_id, avg, taken) in enumerate(cheapest, 1):
            suffix = f" (ср. по {_format_num(taken)} шт)" if taken < CHEAP_UNITS_SAMPLE else ""
            lines.append(f"{i}. {name(item_id)} — ~{_format_num(avg)} джк{suffix}")
    else:
        lines.append("— нет открытых лотов")
    lines.append("")

    lines.append("💎 *Самые редкие товары (на рынке сейчас)*")
    if rarest:
        for i, (item_id, stock) in enumerate(rarest, 1):
            lines.append(f"{i}. {name(item_id)} — {_format_num(stock)} шт")
    else:
        lines.append("— нет открытых лотов")
    lines.append("")
    lines.append("ℹ️ Подробнее: `/info <название товара>`")

    return "\n".join(lines)

def build_item_text(query: str) -> str:
    id_to_name, name_to_id = load_items_map()

    q = (query or "").strip()
    if not q:
        return "❗ Укажи товар: `/info <название товара>`"

    item_id = q if q in id_to_name else name_to_id.get(normalize(q))
    if not item_id:
        return "❗ Не понял товар. Введи точное название как в таблице «Товары»."

    lots_rows = fetch_rows_dict(GID_OPEN_LOTS)
    sales_rows = fetch_rows_dict(GID_SALES)

    lot_sample = lots_rows[0] if lots_rows else {}
    lk_item = _pick_key(lot_sample, ["Id товара", "item_id"])
    lk_qty = _pick_key(lot_sample, ["Количество", "qty", "amount"])
    lk_price = _pick_key(lot_sample, ["Цена", "price"])
    lk_seller = _pick_key(lot_sample, ["Продавец", "seller", "Ник", "Пользователь", "Имя"])

    sale_sample = sales_rows[0] if sales_rows else {}
    sk_ts = _pick_key(sale_sample, ["Отметка времени", "Timestamp", "Дата", "Время"])
    sk_item = _pick_key(sale_sample, ["Id товара", "item_id"])
    sk_qty = _pick_key(sale_sample, ["Количество", "qty", "amount"])
    sk_price = _pick_key(sale_sample, ["Цена", "price"])

    now = datetime.now(tz=UTC)
    cutoff_30d = now - timedelta(days=DAYS_30)

    sales_item: List[Tuple[datetime, float, float]] = []
    sales_30: List[Tuple[datetime, float, float]] = []
    for r in sales_rows:
        if str(r.get(sk_item, "")).strip() != item_id:
            continue
        dt = _parse_dt(r.get(sk_ts))
        if not dt:
            continue
        qty = _to_float(r.get(sk_qty))
        price = _to_float(r.get(sk_price))
        if qty <= 0 or price <= 0:
            continue
        tup = (dt, qty, price)
        sales_item.append(tup)
        if dt >= cutoff_30d:
            sales_30.append(tup)

    lots_item = []
    stock_market = 0.0
    for r in lots_rows:
        if str(r.get(lk_item, "")).strip() != item_id:
            continue
        qty = _to_float(r.get(lk_qty))
        price = _to_float(r.get(lk_price))
        if qty <= 0 or price <= 0:
            continue
        seller = str(r.get(lk_seller, "")).strip() if lk_seller else ""
        lots_item.append((price, qty, seller))
        stock_market += qty

    avg_all = _weighted_avg(sales_item)
    avg_30 = _weighted_avg(sales_30)
    sold_all = _sum_qty(sales_item)
    sold_30 = _sum_qty(sales_30)

    lots_item.sort(key=lambda x: x[0])
    cheapest = lots_item[:TOP_N]

    name = id_to_name.get(item_id, item_id)
    lines: List[str] = []
    lines.append(f"📦 *{name}*")
    lines.append("")
    lines.append(f"🧺 На рынке сейчас: *{_format_num(stock_market)}* шт")
    lines.append("")
    lines.append("💰 *Цена продаж (по фактическим сделкам)*")
    lines.append(f"• средняя за всё время: {(_format_num(avg_all) + ' джк') if avg_all is not None else '— нет данных'}")
    lines.append(f"• средняя за 30 дней: {(_format_num(avg_30) + ' джк') if avg_30 is not None else '— нет данных'}")
    lines.append("")
    lines.append("📊 *Объёмы продаж (по сделкам)*")
    lines.append(f"• продано за всё время: {_format_num(sold_all)} шт")
    lines.append(f"• продано за 30 дней: {_format_num(sold_30)} шт")
    lines.append("")
    lines.append("🛒 *Самые дешёвые лоты (топ-7)*")
    if cheapest:
        for price, qty, seller in cheapest:
            sfx = f" — {seller}" if seller else ""
            lines.append(f"• {_format_num(price)} джк × {_format_num(qty)}{sfx}")
    else:
        lines.append("— сейчас лотов нет")

    return "\n".join(lines)

# ---------- Telegram handler ----------
async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.message is None:
            return

        # /info <...>
        arg = " ".join(context.args) if context and context.args else ""
        if not arg:
            text = build_global_text()
        else:
            text = build_item_text(arg)

        await update.message.reply_text(text, parse_mode="Markdown")

    except Exception:
        logger.exception("INFO command failed")
        if update.message:
            await update.message.reply_text(
                "❗ Не удалось сформировать /info. Проверь переменные окружения (GID_SALES) и заголовки таблиц."
            )