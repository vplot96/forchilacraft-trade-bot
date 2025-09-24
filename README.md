# Forchilacraft Trade Bot — GID CSV Version

Этот бот подключается к Google Sheets через CSV-экспорт (без Google Cloud API).  
Сейчас реализована команда `/balance <имя>` для листа «Счета».  
Код уже подготовлен к расширению для других листов (например, «Цены», «Операции»).

## Настройка Google Sheets
1. Включи общий доступ к таблице: «По ссылке → Читатель».
2. Найди в URL документа `SHEET_ID` (между `/d/` и `/edit`).
3. Для листа «Счета» скопируй число после `#gid=` в URL — это `GID_ACCOUNTS`.

## Переменные окружения
- `BOT_TOKEN` — токен из @BotFather.
- `SHEET_ID` — ID документа Google Sheets.
- `GID_ACCOUNTS` — gid листа «Счета».
- (на будущее) `GID_PRICES`, `GID_OPS` — gid других листов.

## Проверка CSV вручную
Собери URL и открой в браузере:
```
https://docs.google.com/spreadsheets/d/SHEET_ID/export?format=csv&gid=GID
```

## Запуск на Railway/Render
1. Залей файлы в GitHub.
2. Подключи репозиторий на Railway/Render.
3. В Variables добавь `BOT_TOKEN`, `SHEET_ID`, `GID_ACCOUNTS`.
4. Перезапусти сервис.
