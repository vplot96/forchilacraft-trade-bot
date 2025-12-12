from telegram import Update
from telegram.ext import ContextTypes

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Доступные команды:\n/balance – узнать свой баланс\n/price <название товара> – узнать текущий курс товара\n/pay <имя пользователя> <сумма> – сделать перевод\n/ops <число> – посмотреть последние операции")
