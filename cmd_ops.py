from telegram import Update
from telegram.ext import ContextTypes
from datetime import datetime

# Хелперы заполняются из bot.py
_load_accounts_rows = None
_find_account = None
_fetch_ops_rows = None
_parse_date_safe = None
_format_op_line = None

def init_ops_helpers(load_accounts_func, find_account_func, fetch_ops_func, parse_date_func, format_op_func):
    global _load_accounts_rows, _find_account, _fetch_ops_rows, _parse_date_safe, _format_op_line
    _load_accounts_rows = load_accounts_func
    _find_account = find_account_func
    _fetch_ops_rows = fetch_ops_func
    _parse_date_safe = parse_date_func
    _format_op_line = format_op_func

async def ops(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Сколько выводить (по умолчанию 5)
    limit = 5
    if context.args:
        arg = "".join(context.args).strip()
        if arg.isdigit():
            limit = max(1, min(50, int(arg)))  # ограничим до 50

    # Определяем отправителя и его Имя
    username = (update.effective_user.username or "").strip()
    if not username:
        await update.message.reply_text("У вас не задан Telegram username (@...).")
        return

    try:
        accounts = _load_accounts_rows()
    except Exception as e:
        await update.message.reply_text(f"Ошибка доступа к таблице: {e}")
        return

    me = _find_account(accounts, username)
    if not me:
        await update.message.reply_text(f"Не могу найти аккаунт с именем @{username}.")
        return

    player_name = str(me.get("Имя", "")).strip()
    if not player_name:
        await update.message.reply_text("В вашей записи не указано поле «Имя». Обратитесь к администратору.")
        return

    # Грузим операции и фильтруем по имени
    try:
        ops_rows = _fetch_ops_rows()
    except Exception as e:
        await update.message.reply_text(f"Ошибка доступа к листу «Операции»: {e}")
        return

    mine = []
    for r in ops_rows:
        if str(r.get("Пользователь", "")).strip() == player_name:
            dt = _parse_date_safe(r.get("Дата", ""))
            if dt is None:
                dt = datetime.min
            mine.append((dt, r))

    if not mine:
        await update.message.reply_text("Для вас ещё нет операций.")
        return

    # Сортируем по дате по убыванию и берём limit
    mine.sort(key=lambda x: x[0], reverse=True)
    mine = mine[:limit]

    # Формируем строки
    lines = []
    for dt, row in mine:
        date_str = dt.strftime("%d.%m.%y") if dt != datetime.min else str(row.get("Дата", "")).strip()
        title, op, qty, amount, sign = _format_op_line(row)
        lines.append(f'{date_str} {op} "{title}" ({qty}): {sign}{amount} джк')

    await update.message.reply_text("\n".join(lines))

# --- Поддержка количества в конце строки (для /price) ---
def _split_query_and_qty(text: str):
    """
    Делит строку на (название товара, количество).
    Примеры:
      'алмаз' -> ('алмаз', 1)
      'алмаз 10' -> ('алмаз', 10)
    """
    s = (text or "").strip()
    m = re.search(r"\s+(\d+)$", s)
    if m:
        name = s[:m.start()].strip()
        qty = int(m.group(1))
        if qty <= 0:
            qty = 1
        return name, qty
    return s, 1

def _money_to_decimal(value) -> Decimal:
    """
    Переводит строку цены из таблицы в Decimal.
    Поддерживает '1234', '1 234', '1234,56', '1234.56'.
    """
    s = str(value or "").strip().replace(" ", "").replace(",", ".")
    if not s:
        return Decimal("0")
    try:
        q = Decimal(s)
    except Exception:
        q = Decimal("0")
    return q

def _fmt_total(amount: Decimal) -> str:
    """
    Красивый вывод общей суммы:
    - без копеек, если целое;
    - иначе 2 знака (с запятой).
    """
    amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amount == amount.to_integral():
        return f"{int(amount)}"
    return f"{amount}".replace(".", ",")

