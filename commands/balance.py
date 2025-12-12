from telegram import Update
from telegram.ext import ContextTypes

# Хелперы заполняются из bot.py
_load_accounts_rows = None
_find_account = None
_parse_balance_to_decimal = None

def init_balance_helpers(load_accounts_func, find_account_func, parse_balance_func):
    global _load_accounts_rows, _find_account, _parse_balance_to_decimal
    _load_accounts_rows = load_accounts_func
    _find_account = find_account_func
    _parse_balance_to_decimal = parse_balance_func

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username
    if not username:
        await update.message.reply_text("У вас не задан Telegram username (@...).")
        return
    try:
        rows = _load_accounts_rows()
    except Exception as e:
        await update.message.reply_text(f"Ошибка доступа к таблице: {e}")
        return
    acc = _find_account(rows, username)
    if not acc:
        await update.message.reply_text(f"Не могу найти аккаунт с именем @{username}.")
        return
    bal = _parse_balance_to_decimal(acc.get("Баланс"))
    await update.message.reply_text(f"Ваш баланс: {bal} джк")

def lookup_price_by_product_name(query: str, cutoff: float = 0.45):
    if not GID_PRICES:
        raise RuntimeError("GID_PRICES is not set")
    rows = fetch_rows(GID_PRICES)
    qn = normalize(query)
    names = [normalize(str(r.get("Название товара",""))) for r in rows]
    import difflib
    best = difflib.get_close_matches(qn, names, n=1, cutoff=cutoff)
    if not best:
        return None
    idx = names.index(best[0])
    row = rows[idx]
    return (str(row.get("Название товара","")).strip(), row.get("Текущая цена",""))
