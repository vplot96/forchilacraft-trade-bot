import csv
import io
import os
from decimal import Decimal, InvalidOperation

import aiohttp


COL_TELEGRAM_USER = "Пользователь"
COL_GAME_LOGIN = "Логин"
COL_BALANCE = "Баланс"


async def balance_handler(message) -> None:
    identity = _telegram_identity(message)

    try:
        balance = await _get_balance(identity)
    except Exception:
        await message.answer("Не удалось получить данные баланса. Попробуйте позже.")
        return

    if balance is None:
        await message.answer("Не удалось найти данные по вашему аккаунту.")
        return

    await message.answer(f"Ваш баланс: {_format_decimal(balance)} монет.")


def _telegram_identity(message) -> str:
    user = getattr(message, "from_user", None)
    username = getattr(user, "username", None)
    if username:
        return f"@{username}"

    full_name = getattr(user, "full_name", "") or ""
    return full_name.strip()


async def _get_balance(identity: str) -> Decimal | None:
    rows = await _fetch_accounts_rows()
    if not rows:
        raise RuntimeError

    header = rows[0]
    user_idx = header.index(COL_TELEGRAM_USER)
    balance_idx = header.index(COL_BALANCE)

    for row in rows[1:]:
        if user_idx >= len(row):
            continue

        if (row[user_idx] or "").strip() != identity:
            continue

        if balance_idx >= len(row):
            raise RuntimeError

        raw = (row[balance_idx] or "").strip()
        if raw == "":
            raise RuntimeError

        try:
            normalized = raw.replace(" ", "").replace(",", ".")
            return Decimal(normalized)
        except InvalidOperation:
            raise RuntimeError

    return None


async def _fetch_accounts_rows() -> list[list[str]]:
    sheet_id = os.getenv("SHEET_ID", "").strip()
    gid = os.getenv("GID_ACCOUNTS", "").strip()
    if not sheet_id or not gid:
        raise RuntimeError

    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export"
    params = {"format": "csv", "gid": gid}

    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                raise RuntimeError
            text = await resp.text()

    return list(csv.reader(io.StringIO(text)))


def _format_decimal(value: Decimal) -> str:
    if value == value.to_integral():
        return str(value.quantize(Decimal("1")))

    s = format(value, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s