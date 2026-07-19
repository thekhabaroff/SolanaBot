<h1 align="center">Solana Bot</h1>

<p align="center">
  Интерактивный CLI для пакетной работы с Solana-кошельками: ключи, балансы, переводы, swap через Jupiter и возврат rent из пустых токен аккаунтов.
</p>

<div align="center">

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Solana](https://img.shields.io/badge/Solana-Mainnet-9945FF?style=for-the-badge&logo=solana&logoColor=white)
![Jupiter](https://img.shields.io/badge/Jupiter-Swap-FF8B00?style=for-the-badge)
![License](https://img.shields.io/badge/License-MIT-blue?style=for-the-badge)
![Updated](https://img.shields.io/badge/Updated-19.07.2026-lightgrey?style=for-the-badge)
![Last%20Commit](https://img.shields.io/github/last-commit/thekhabaroff/SolanaBot?style=for-the-badge)
![Issues](https://img.shields.io/github/issues/thekhabaroff/SolanaBot?style=for-the-badge)

[Возможности](#возможности) • [Требования](#требования) • [Установка](#установка) • [Настройка](#настройка) • [Использование](#использование) • [Безопасность](#безопасность) • [Ограничения](#ограничения)

</div>

## Возможности

- создание независимых Solana keypair и BIP-39 seed-фраз на 12 или 24 слова;
- конвертация seed-фраз в ключи по стандартному пути `m/44'/501'/0'/0'` и в legacy-режиме для восстановления старых адресов;
- конвертация приватных ключей в публичные адреса;
- проверка SOL, токенов из конфигурации или одного указанного mint;
- массовые переводы SOL: «один → много» и «многие → один»;
- массовая отправка USDC «один → много» с созданием associated token account (ATA) получателя при необходимости;
- swap токенов: котировка, minimum received, price impact, отдельное подтверждение и preflight-симуляция;
- закрытие пустых SPL token accounts с возвратом rent-exempt SOL;
- резервные RPC, повтор запросов и ожидание статуса `confirmed` или `finalized`.

Вся прикладная логика, RPC-клиент и интерфейс находятся в одном файле `bot.py`.

## Требования

- Python 3.11 или новее;
- доступ к Solana RPC;
- Jupiter API key — только для swap;
- macOS, Linux или Windows с Python.

Прямые зависимости намеренно не закреплены: при установке подтягиваются их последние доступные версии. Перед работой с заметной суммой после обновления библиотек выполните небольшую тестовую транзакцию.

## Установка

1. Клонируйте репозиторий и перейдите в его папку.
2. Создайте виртуальное окружение.
3. Установите актуальные зависимости.
4. Настройте `config.toml` и запустите бота.

```bash
git clone https://github.com/thekhabaroff/SolanaBot.git
cd SolanaBot

pip3 install --upgrade pip
pip3 install -r requirements.txt
python bot.py
```

## Настройка

Программа читает [`config.toml`](config.toml). Перед первым swap укажите свой ключ Jupiter вместо `YOUR_API`:

```toml
rpc_url = "https://solana-rpc.publicnode.com"
rpc_alternatives = ["https://api.mainnet-beta.solana.com"]
default_slippage_bps = 50
delay_between_wallets_sec = 2
max_retries = 3
confirmation_timeout_sec = 60
swap_fee_reserve_lamports = 5000000

[api]
jupiter_api_url = "https://api.jup.ag/swap/v1"
jupiter_key = "YOUR_JUPITER_API_KEY"
```

`default_slippage_bps = 50` означает slippage 0.5%. `swap_fee_reserve_lamports` — запас SOL для комиссии и связанных расходов во время swap; не является фиксированной комиссией сети.

В секции `[tokens]` уже заданы SOL, USDC, USDT, JUP, RAY и GRASS. Можно добавить собственный токен, указав symbol, mint и decimals: при swap decimals дополнительно сверяются с данными сети.

## Локальные файлы

| Файл | Назначение |
| --- | --- |
| `phrases.txt` | BIP-39 seed-фразы, по одной на строку. |
| `keys.txt`    | Приватные ключи, по одному на строку. Поддерживаются base58, JSON-массив байтов и hex. |
| `wallets.txt` | Публичные Solana-адреса, по одному на строку. |
| `history.log` | Локальная история операций и статусов транзакций. |
| `config.toml` | RPC, параметры операций, токены и Jupiter API key. |

При создании кошельков и конвертации новые строки добавляются без удаления старых. Конвертация ключей в адреса перезаписывает `wallets.txt` только после явного подтверждения.

## Использование

Запустите:

```bash
python bot.py
```

Главное меню:

```text
1. Create wallets    — создание seed-фраз или keypair
2. Converter         — конвертация фраз, ключей и адресов
3. Balance Checker   — проверка SOL и токенов
4. Multisender       — массовые переводы SOL или USDC
5. Swap              — обмен через Jupiter
6. Refund            — закрытие пустых token accounts
0. Exit              — выход
```

### Массовые переводы

Для SOL доступны два режима:

1. **Один → много** — один ключ из `keys.txt` отправляет одинаковую сумму каждому адресу из `wallets.txt`.
2. **Многие → один** — остаток SOL со всех ключей из `keys.txt` собирается на выбранный адрес из `wallets.txt` с учётом комиссии.

Для USDC доступен режим **один → много**. Если у получателя ещё нет ATA для USDC, бот создаст его в той же транзакции; стоимость создания оплачивает отправитель.

### Swap через Jupiter

1. Выберите входной и выходной токен из `config.toml` или укажите mint вручную.
2. Выберите один кошелёк либо все ключи из `keys.txt`.
3. Укажите сумму или режим обмена всего доступного баланса.
4. Проверьте котировку: expected output, minimum received и price impact.
5. Подтвердите swap.

До отправки бот симулирует подписанную транзакцию. Симуляция не публикует её в сети, но не отменяет необходимость проверять параметры операции.

### Refund

Пункт **Refund** сканирует token accounts ключей из `keys.txt`, находит accounts с нулевым балансом и закрывает те, для которых текущий ключ имеет право закрытия. Rent возвращается на кошелёк-владельца только после подтверждения транзакции.

## Безопасность

- Никогда не публикуйте `keys.txt`, `phrases.txt` или реальный `config.toml` с API key.
- Не используйте рабочие кошельки: хранение ключей в текстовых файлах подходит только для тестовых средств.
- Ограничьте доступ к локальным секретам: `chmod 600 keys.txt phrases.txt config.toml`.
- Перед массовой отправкой проверьте содержимое `wallets.txt`, сумму на одного получателя и итоговую сумму.
- При статусе `pending`, timeout или ошибке RPC сначала найдите транзакцию по подписи в обозревателе, а не повторяйте операцию вслепую.
- Не используйте durable nonce, stake или program accounts как отправителя: для перевода, swap и закрытия аккаунтов необходим обычный system account с `space = 0`.

## Ограничения

- Бот рассчитан на mainnet и не переключает кластер автоматически.
- Прямой multisender поддерживает SOL и USDC; другие токены отправляйте через swap или расширяйте код отдельно.
- Token-2022 mint с extensions пока не поддерживается для прямой отправки USDC.
- Jupiter возвращает сериализованную swap-транзакцию: бот выполняет её preflight-симуляцию, но не выполняет полную семантическую валидацию каждой инструкции Jupiter.
- API, ликвидность, цена, priority fee и сетевая комиссия могут меняться между котировкой и подтверждением.

## Структура проекта

```text
.
├── bot.py            # CLI, модели, RPC, Jupiter и операции с кошельками
├── config.toml       # локальная конфигурация
├── requirements.txt  # runtime-зависимости без фиксации версий
├── phrases.txt       # локальные seed-фразы
├── keys.txt          # локальные приватные ключи
├── wallets.txt       # публичные адреса
├── history.log       # история операций
├── LICENSE           # MIT License
└── README.md
```

## Лицензия

[MIT](LICENSE) — используйте свободно, упоминание автора приветствуется.

---

<div align="center">⭐ Поставьте звезду, если проект оказался полезным.</div>
