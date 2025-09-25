# Forchilacraft Trade Bot — Username Mode

Теперь команда `/balance` **не принимает аргументов**: бот определяет игрока по Telegram username отправителя и ищет его в колонке **Username** на листе «Счета».

## Требования к листу «Счета»
Первая строка — заголовки. Должны быть как минимум:
```
Имя,Баланс,Username
```
Пример:
```
Имя,Баланс,Username
Алиса,120,alice123
Боб,75,bobka
```
> Username — без @ (т.е. `alice123`, не `@alice123`).

## Переменные окружения
- `BOT_TOKEN` — токен из @BotFather
- `SHEET_ID` — ID документа Google Sheets (между /d/ и /edit)
- `GID_ACCOUNTS` — gid листа «Счета»
- (на будущее) `GID_PRICES`, `GID_OPS` — gid других листов

## Доступ к таблице
Включи «По ссылке → Читатель» для документа (или публикацию).

## Проверка CSV вручную
```
https://docs.google.com/spreadsheets/d/SHEET_ID/export?format=csv&gid=GID
```
