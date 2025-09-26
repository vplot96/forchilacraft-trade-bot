# PTB v20+ fix

Эта версия совместима с `python-telegram-bot` 20+ (замена Updater → Application).

Изменения:
- Все хендлеры стали `async`.
- Используется `Application.builder().token(...).build()` и `app.run_polling()`.
- Сохранены кэш CSV, helper username, команды `/balance`, `/price`, `/pay`.
- `/pay` отправляет данные в Google Form, затем инвалидирует кэш и пытается прочитать новый баланс.

Если у вас в `requirements.txt` стоит `python-telegram-bot>=20`, используйте этот `bot.py`.
Если вы хотите остаться на 13.x — используйте предыдущий файл без изменений.

## Переменные окружения
См. раздел в README_PAY.md предыдущего архива; перечень переменных не изменился.
