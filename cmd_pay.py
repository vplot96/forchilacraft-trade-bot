from telegram import Update
from telegram.ext import ContextTypes
import requests

# Параметры и хелперы заполняются из bot.py
FORM_ID = None
ENTRY_SENDER = None
ENTRY_RECIPIENT = None
ENTRY_SUM = None
FORM_POST_URL = None
normalize = None
_parse_amount_arg = None
_load_accounts_rows = None
_find_account = None
_parse_balance_to_decimal = None
_fmt_amount_comma2 = None

def init_pay_helpers(form_id, entry_sender, entry_recipient, entry_sum, form_post_url,
                     normalize_func, parse_amount_func, load_accounts_func,
                     find_account_func, parse_balance_func, fmt_amount_func):
    global FORM_ID, ENTRY_SENDER, ENTRY_RECIPIENT, ENTRY_SUM, FORM_POST_URL
    global normalize, _parse_amount_arg, _load_accounts_rows, _find_account, _parse_balance_to_decimal, _fmt_amount_comma2
    FORM_ID = form_id
    ENTRY_SENDER = entry_sender
    ENTRY_RECIPIENT = entry_recipient
    ENTRY_SUM = entry_sum
    FORM_POST_URL = form_post_url
    normalize = normalize_func
    _parse_amount_arg = parse_amount_func
    _load_accounts_rows = load_accounts_func
    _find_account = find_account_func
    _parse_balance_to_decimal = parse_balance_func
    _fmt_amount_comma2 = fmt_amount_func

async def pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not (FORM_ID and ENTRY_SENDER and ENTRY_RECIPIENT and ENTRY_SUM and FORM_POST_URL):
        await update.message.reply_text("Не настроены параметры формы перевода.")
        return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Использование: /pay <имя пользователя> <сумма>\n\n<имя пользователя> – username пользователя из Телеграм без символа @.\n<сумма> – число, может быть с двумя знаками после запятой.")
        return

    recipient = context.args[0].strip()
    amount_raw = " ".join(context.args[1:]).strip()
    sender_username = (update.effective_user.username or "").strip()
    if not sender_username:
        await update.message.reply_text("У вас не задан Telegram username.")
        return
    if normalize(sender_username) == normalize(recipient):
        await update.message.reply_text("Нельзя осуществить перевод себе.")
        return
    try:
        amount = _parse_amount_arg(amount_raw)
    except Exception:
        await update.message.reply_text("Не могу понять указанную сумму.")
        return
    if amount <= 0:
        await update.message.reply_text("Сумма должна быть больше 0.")
        return

    try:
        rows = _load_accounts_rows()
    except Exception as e:
        await update.message.reply_text(f"Ошибка доступа к таблице: {e}")
        return

    sender_row = _find_account(rows, sender_username)
    if not sender_row:
        await update.message.reply_text(f"Аккаунт {sender_username} не найден.")
        return
    recipient_row = _find_account(rows, recipient)
    if not recipient_row:
        await update.message.reply_text(f"Аккаунт {recipient} не найден.")
        return

    sender_balance = _parse_balance_to_decimal(sender_row.get("Баланс"))
    if sender_balance < amount:
        await update.message.reply_text("На вашем балансе недостаточно средств.")
        return

    payload = {
        ENTRY_SENDER: sender_username,
        ENTRY_RECIPIENT: recipient,
        ENTRY_SUM: _fmt_amount_comma2(amount),
    }
    try:
        resp = requests.post(
            FORM_POST_URL,
            data=payload,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ForchilacraftBot/1.0)"},
            timeout=10,
        )
        ok = resp.status_code in (200, 302)
    except requests.RequestException:
        ok = False

    if ok:
        await update.message.reply_text("Ваш перевод подтверждён.")
    else:
        await update.message.reply_text("Не удалось отправить перевод. Попробуйте позже.")


# ---------- /ops <число> — последние операции пользователя ----------
def _fetch_ops_rows():
    if not GID_OPS:
        raise RuntimeError("GID_OPS is not set")
    return fetch_rows(GID_OPS)

def _parse_date_safe(s: str):
    s = (s or "").strip()
    for fmt in ("%d.%m.%y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None

def _format_op_line(row):
    # Ожидаемые колонки: Название, Операция, Число, Сумма, Пользователь, Дата
    title = str(row.get("Название", "")).strip()
    op = str(row.get("Операция", "")).strip()  # "Покупка" / "Продажа"
    qty = str(row.get("Число", "")).strip()
    amount = str(row.get("Сумма", "")).strip()
    sign = "−" if op.lower().startswith("покуп") else "+"  # минус для Покупка, плюс для Продажа
    return title, op, qty, amount, sign
