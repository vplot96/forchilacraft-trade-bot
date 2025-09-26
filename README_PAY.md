# Forchilacraft Trade Bot — обновление

В этом архиве новая версия `bot.py` с тремя изменениями:
1) Кэш CSV для Google Sheets (уменьшает задержки и лимиты). TTL настраивается переменной `CSV_CACHE_TTL` (секунды), по умолчанию 60.
2) Единый helper для username. `/balance` теперь ищет аккаунт строго по колонке `Username` (в нижнем регистре, без `@`). Сообщение об ошибке: `Аккаунт <username> не найден.`
3) Новая команда `/pay <кому> <сумма>`. Запись перевода идёт **через Google Form**. После отправки бот инвалидирует кэш и пытается сразу прочитать обновлённый баланс (несколько коротких попыток).

## Переменные окружения

Обязательные:
- `BOT_TOKEN`
- `SHEET_ID`
- `GID_ACCOUNTS` — gid листа «Счета»
- `GID_PRICES`   — gid листа «Товары»

Для формы (обязательны, чтобы работал `/pay`):
- `FORM_ID` — ID формы (кусок между `/d/e/` и `/viewform`), напр.: `1FAIpQLSegZNyaElRcoul_YMDZiZtp5ZLZlhBDiWf04UG-smUeAu6y9A`
- `ENTRY_USER` — id поля пользователя, напр.: `entry.2015621373`
- `ENTRY_SUM`  — id поля суммы, напр.: `entry.40410086`

Опционально:
- `CSV_CACHE_TTL` — секунды кэша CSV (по умолчанию `60`).

## Требования

- `python-telegram-bot` 13.x
- `requests`

(Если в вашем `requirements.txt` эти версии уже указаны — оставьте как есть.)

## Быстрый запуск

```bash
export BOT_TOKEN=...
export SHEET_ID=...
export GID_ACCOUNTS=...
export GID_PRICES=...
export FORM_ID=1FAIpQLSegZNyaElRcoul_YMDZiZtp5ZLZlhBDiWf04UG-smUeAu6y9A
export ENTRY_USER=entry.2015621373
export ENTRY_SUM=entry.40410086

python bot.py
```

## Примечания

- Убедитесь, что форма доступна без логина и без CAPTCHA и привязана к нужной таблице (Responses → Create spreadsheet).
- `/price` реализован простым точным/префикс-поиском (без «fuzzy»). Его легко заменить на вашу реализацию.
- Кэш CSV работает и для «Счета», и для «Товары». При `/pay` кэш «Счета» инвалидируется принудительно.
