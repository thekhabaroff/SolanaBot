# Solana Bot

Интерактивный CLI-инструмент для пакетной работы с Solana-кошельками: создание и конвертация ключей, проверка балансов, мультисендер, swap и закрытие пустых токен аккаунтов.

## Требования и запуск

- Python 3.10+
- Доступ к Solana RPC
- API key Jupiter для swap

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python bot.py
```

## Структура

```text
Solana/
├── bot.py            # CLI и операции с кошельками
├── config.json       # RPC, Jupiter, токены, retries и резервы
├── requirements.txt  # Зафиксированные runtime-зависимости
├── phrases.txt       # BIP-39 сид-фразы
├── keys.txt          # Приватные ключи
├── wallets.txt       # Публичные адреса
└── history.log       # История операций и статусы TX
```

## Функции

1. **Create wallets**

   Создаёт 12- или 24-словные BIP-39 фразы либо независимые Solana keypairs. Новые строки добавляются без перезаписи ранее созданных данных.
2. **Converter**

   Конвертирует сид-фразы в ключи и ключи в адреса. Для фраз доступны:

   - стандартный Solana/SLIP-0010 path `m/44'/501'/0'/0'`;
   - legacy-режим v0.19 `seed[:32]` для восстановления ранее созданных адресов.

   Новые ключи добавляются без дубликатов; замена непустого `wallets.txt` требует отдельного подтверждения.
3. **Balance Checker**

   Проверяет SOL, настроенные токены или один указанный mint. Если у владельца несколько token accounts одного mint, их balances суммируются. Рыночная оценка в USDT не выполняется.
4. **Multisender**

   Отправляет только SOL в режимах «один → много» и «много → один». Дубликаты и self-transfer исключаются, а комиссия получается через `getFeeForMessage`.
5. **Swap**

   Выполняет ExactIn swap через Jupiter Metis `/swap/v1`. Mint и decimals проверяются on-chain. В режиме «swap all» для SPL используется баланс associated token account; auxiliary accounts показываются отдельно.
6. **Refund**

   Сканирует legacy SPL Token и Token-2022, закрывает нулевые accounts только при подходящем close authority и считает возврат только после `confirmed`/`finalized`.

## Надёжность транзакций

- Все денежные расчёты выполняются в целых lamports/atomic units, без `float`.
- `sendTransaction` использует preflight и `maxRetries`.
- Успехом считается только `confirmed` или `finalized`; `processed`, timeout и expired отчитываются отдельно.
- Вместе с blockhash сохраняется `lastValidBlockHeight`.
- При transport-ошибке RPC используются те же подписанные байты и резервные RPC, чтобы не создать двойную выплату.

## Проверка тестов

```bash
python -m unittest discover -s tests -v
```
