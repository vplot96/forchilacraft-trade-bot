from __future__ import annotations

import csv
import logging
import os
from decimal import Decimal, InvalidOperation
from io import StringIO
from typing import List, Optional

import requests
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


COL_TELEGRAM_USER = "Пользователь"
COL_GAME_LOGIN = "Логин"
COL_BALANCE = "Баланс"


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return

    try:
        user = update.effective_user
        identity = _telegram_identity(user)

        value = build_balance_value(identity)
        if value is None:
            await update.message.reply_text("Не удалось найти данные по вашему аккаунту.")
            return

        await update.message.reply_text(f"Ваш баланс: {_format_decimal(value)} монет.")

    except Exception:
        logger.exception("BALANCE command failed")
        await update.message.reply_text("Не удалось получить данные баланса. Попробуйте позже.")


def build_balance_value(identity: str) -> Optional[Decimal]:
    sheet_id = _require_env("SHEET_ID")
    gid_accounts = _require_env("GID_ACCOUNTS")

    rows = _fetch_rows_dict(sheet_id, gid_accounts)
    if not rows:
        return None

    _ensure_columns(rows, [COL_TELEGRAM_USER, COL_BALANCE], "Счета")

    for r in rows:
        if str(r.get(COL_TELEGRAM_USER, "")).strip() != identity:
            continue

        raw = str(r.get(COL_BALANCE, "")).strip()
        if not raw:
            raise RuntimeError("empty balance")

        return _parse_decimal(raw)

    return None


def _telegram_identity(user) -> str:
    username = getattr(user, "username", None)
    if username:
        return f"@{username}"

    full_name = getattr(user, "full_name", "") or ""
    return full_name.strip()


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
        raise RuntimeError("missing columns")


def _parse_decimal(x: str) -> Decimal:
    s = str(x or "").strip().replace(" ", "").replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation as e:
        raise RuntimeError("bad decimal") from e


def _format_decimal(value: Decimal) -> str:
    if value == value.to_integral():
        return str(value.quantize(Decimal("1")))

    s = format(value, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s