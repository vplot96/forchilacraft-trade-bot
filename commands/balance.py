from __future__ import annotations

import csv
import io
import os
from decimal import Decimal, InvalidOperation

import httpx
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router()

# =========================
# Sheet schema (strict)
# =========================
COL_USERNAME = "Пользователь"   # Telegram username (@...) or full_name
COL_BALANCE = "Баланс"          # Numeric balance


class BalanceUnavailable(Exception):
    pass


class AccountNotFound(Exception):
    pass


@router.message(Command("balance"))
async def balance_handler(message: Message) -> None:
    identity = _get_telegram_identity(message)

    try:
        balance = await _get_balance(identity)
    except AccountNotFound:
        await message.answer("Не удалось найти данные по вашему аккаунту.")
        return
    except BalanceUnavailable:
        await message.answer("Не удалось получить данные баланса. Попробуйте позже.")
        return

    if balance == balance.to_integral():
        value = str(balance.quantize(Decimal("1")))
    else:
        value = _format_decimal(balance)

    await message.answer(f"Ваш баланс: {value} джк.")


def _get_telegram_identity(message: Message) -> str:
    user = message.from_user
    if user and user.username:
        return f"@{user.username}"
    return (user.full_name or "").strip()


async def _get_balance(identity: str) -> Decimal:
    rows = await _fetch_accounts_rows()

    if not rows:
        raise BalanceUnavailable()

    header = rows[0]
    try:
        user_idx = header.index(COL_USERNAME)
        balance_idx = header.index(COL_BALANCE)
    except ValueError as e:
        raise BalanceUnavailable() from e

    for row in rows[1:]:
        if user_idx >= len(row):
            continue

        if (row[user_idx] or "").strip() != identity:
            continue

        if balance_idx >= len(row):
            raise BalanceUnavailable()

        raw = (row[balance_idx] or "").strip()
        if raw == "":
            raise BalanceUnavailable()

        try:
            normalized = raw.replace(" ", "").replace(",", ".")
            return Decimal(normalized)
        except InvalidOperation as e:
            raise BalanceUnavailable() from e

    raise AccountNotFound()


async def _fetch_accounts_rows() -> list[list[str]]:
    sheet_id = os.getenv("SHEET_ID", "").strip()
    gid = os.getenv("GID_ACCOUNTS", "").strip()

    if not sheet_id or not gid:
        raise BalanceUnavailable()

    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export"
    params = {"format": "csv", "gid": gid}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
    except Exception as e:
        raise BalanceUnavailable() from e

    try:
        reader = csv.reader(io.StringIO(response.text))
        return [row for row in reader]
    except Exception as e:
        raise BalanceUnavailable() from e


def _format_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text