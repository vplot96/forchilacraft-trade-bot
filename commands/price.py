from telegram import Update
from telegram.ext import ContextTypes

# Хелперы заполняются из bot.py
_split_query_and_qty = None
lookup_price_by_product_name = None
_money_to_decimal = None
_fmt_total = None

def init_price_helpers(lookup_func, split_func, money_func, fmt_func):
    global lookup_price_by_product_name, _split_query_and_qty, _money_to_decimal, _fmt_total
    lookup_price_by_product_name = lookup_func
    _split_query_and_qty = split_func
    _money_to_decimal = money_func
    _fmt_total = fmt_func

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # если нет аргументов — спрашиваем и ждём следующего сообщения автора
    if not context.args:
        user_id = update.effective_user.id
        wait = context.chat_data.setdefault("price_wait", set())
        wait.add(user_id)
        await update.message.reply_text("Курс какого товара вы хотели бы узнать?")
        return

    # Поддержка количества в конце строки
    raw = " ".join(context.args).strip()
    name_query, qty = _split_query_and_qty(raw)

    try:
        found = lookup_price_by_product_name(name_query)
    except Exception as e:
        await update.message.reply_text(f"Ошибка доступа к таблице: {e}")
        return

    if not found:
        await update.message.reply_text(f"Товар, похожий на '{name_query}', не найден.")
        return

    display_name, unit_price_str = found
    unit = _money_to_decimal(unit_price_str)
    total = unit * qty
    await update.message.reply_text(f"{display_name} ({qty}) = { _fmt_total(total) } джк")


async def price_followup_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # реагируем только если пользователь в режиме ожидания
    user_id = update.effective_user.id
    wait = context.chat_data.get("price_wait", set())
    if user_id not in wait:
        return  # не мы ждали этот ответ

    query = (update.message.text or "").strip()
    # одно сообщение = один ответ, снимаем ожидание
    wait.discard(user_id)

    if not query:
        await update.message.reply_text("Не понял название товара. Попробуйте ещё раз: /price")
        return

    # поддержка количества в конце строки (например: 'алмаз 10')
    name_query, qty = _split_query_and_qty(query)

    try:
        found = lookup_price_by_product_name(name_query)
    except Exception as e:
        await update.message.reply_text(f"Ошибка доступа к таблице: {e}")
        return

    if not found:
        await update.message.reply_text(f"Товар, похожий на '{name_query}', не найден.")
        return

    display_name, unit_price_str = found
    unit = _money_to_decimal(unit_price_str)
    total = unit * qty

    # Условный вывод количества
    if qty == 1:
        await update.message.reply_text(f"{display_name} = { _fmt_total(total) } джк")
    else:
        await update.message.reply_text(f"{display_name} ({qty}) = { _fmt_total(total) } джк")
