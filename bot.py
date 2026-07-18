#!/usr/bin/env python3

import json
import sys
import asyncio
from pathlib import Path
from datetime import datetime
from typing import Any, List, Optional, Dict, Tuple
from decimal import Decimal, InvalidOperation
import time
import base58

from solders.keypair import Keypair
from solders.system_program import TransferParams, transfer
from solders.message import Message
from solders.pubkey import Pubkey
from solders.hash import Hash
from solders.transaction import Transaction, VersionedTransaction
from solders.instruction import Instruction, AccountMeta
from mnemonic import Mnemonic

from solana_bot import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    DEFAULT_CONFIRMATION_TIMEOUT_SEC,
    DEFAULT_DERIVATION_PATH,
    WRAPPED_SOL_MINT,
    BlockhashInfo,
    JupiterSwapClient,
    RpcError,
    RpcHelper,
    TOKEN_PROGRAM_ID,
    TokenInfo,
    decimal_to_raw,
    derive_slip10_ed25519_seed,
    format_raw_amount,
)

# ==================== CONFIG MANAGER ====================

class ConfigManager:
    """Управление конфигурацией приложения"""
    
    def __init__(self, config_path: str = "config.json"):
        self.config_path = config_path
        self.config = self.load_config()
    
    def load_config(self) -> dict:
        """Загрузить конфиг из JSON файла"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
        except FileNotFoundError as exc:
            raise RuntimeError(f"Файл {self.config_path} не найден") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Некорректный JSON в {self.config_path}: {exc}") from exc
        if not isinstance(config, dict):
            raise RuntimeError("Корневое значение config.json должно быть объектом")
        return config
    
    def get_rpc_endpoint(self) -> str:
        """Получить основной RPC endpoint"""
        if 'network' in self.config and 'rpc' in self.config['network']:
            return self.config['network']['rpc']
        return self.config.get('rpc_url', 'https://api.mainnet-beta.solana.com')
    
    def get_alternative_rpc(self) -> List[str]:
        """Получить альтернативные RPC endpoints"""
        if 'network' in self.config and 'rpc_alternatives' in self.config['network']:
            alternatives = self.config['network']['rpc_alternatives']
        else:
            alternatives = self.config.get('rpc_alternatives', [])
        if isinstance(alternatives, str):
            return [alternatives]
        if not isinstance(alternatives, list) or not all(isinstance(item, str) for item in alternatives):
            raise ValueError("rpc_alternatives должен быть списком URL")
        return alternatives
    
    def get_jupiter_api_key(self) -> Optional[str]:
        """Получить Jupiter API key из конфига"""
        if 'api' in self.config and 'jupiter_key' in self.config['api']:
            return self.config['api']['jupiter_key']
        return self.config.get('jupiter_api_key')
    
    def get_jupiter_api_url(self) -> str:
        """Получить базовый Jupiter API URL"""
        if 'api' in self.config and 'jupiter_api_url' in self.config['api']:
            return self.config['api']['jupiter_api_url']
        return self.config.get('jupiter_api_url', 'https://api.jup.ag/swap/v1')
    
    def get_priority_settings(self) -> dict:
        """Получить настройки приоритета"""
        priority = self.config.get('priority_settings', {})
        if not isinstance(priority, dict):
            raise ValueError("priority_settings должен быть объектом")
        level = str(priority.get('priority_level', 'none'))
        max_lamports = int(priority.get('max_lamports', 0))
        if max_lamports < 0:
            raise ValueError("max_lamports не может быть отрицательным")
        return {
            'level': level,
            'max_lamports': max_lamports,
        }
    
    def get_default_slippage(self) -> int:
        """Получить slippage по умолчанию в bps"""
        slippage = int(self.config.get('default_slippage_bps', 50))
        if slippage < 1 or slippage > 5_000:
            raise ValueError("default_slippage_bps должен быть от 1 до 5000")
        return slippage
    
    def get_delay_between_wallets(self) -> float:
        """Получить задержку между кошельками"""
        return max(0.0, float(self.config.get('delay_between_wallets_sec', 2)))

    def get_max_retries(self) -> int:
        """Получить число повторов RPC-запроса."""
        return max(1, int(self.config.get('max_retries', 3)))

    def get_confirmation_timeout(self) -> int:
        """Получить таймаут подтверждения транзакции."""
        return max(1, int(self.config.get('confirmation_timeout_sec', DEFAULT_CONFIRMATION_TIMEOUT_SEC)))

    def get_swap_fee_reserve_lamports(self) -> int:
        """Получить консервативный резерв SOL для swap и создания ATA."""
        return max(0, int(self.config.get('swap_fee_reserve_lamports', 5_000_000)))

    def get_rpc_endpoints(self) -> List[str]:
        """Получить основной и уникальные резервные RPC endpoints."""
        endpoints = [self.get_rpc_endpoint(), *self.get_alternative_rpc()]
        endpoints = list(dict.fromkeys(endpoint for endpoint in endpoints if endpoint))
        if not endpoints or any(
            not isinstance(endpoint, str) or not endpoint.startswith(('http://', 'https://'))
            for endpoint in endpoints
        ):
            raise ValueError("RPC endpoints должны быть корректными HTTP(S) URL")
        return endpoints
    
    def get_all_tokens(self) -> Dict[str, dict]:
        """Получить все токены из конфига"""
        raw_tokens = self.config.get('tokens', self.config.get('popular_tokens', {}))
        
        if not raw_tokens:
            return {}
        
        tokens_dict = {}
        
        for symbol, token_data in raw_tokens.items():
            if isinstance(token_data, dict):
                tokens_dict[symbol] = {
                    'mint': token_data.get('mint', ''),
                    'decimals': token_data.get('decimals', 6),
                    'symbol': token_data.get('symbol', symbol)
                }
            elif isinstance(token_data, str):
                decimals = 9 if symbol == 'SOL' else 6
                tokens_dict[symbol] = {
                    'mint': token_data,
                    'decimals': decimals,
                    'symbol': symbol
                }
        
        return tokens_dict


# ==================== FILE MANAGER ====================

class FileManager:
    """Управление файлами проекта"""
    
    def __init__(self, config: dict):
        default_files = {
            'phrases': 'phrases.txt',
            'keys': 'keys.txt',
            'wallets': 'wallets.txt',
            'history': 'history.txt'
        }
        configured_files = config.get('files', {})
        if not isinstance(configured_files, dict):
            raise ValueError("files в config.json должен быть объектом")
        self.config = {**default_files, **configured_files}
        if not all(isinstance(path, str) and path for path in self.config.values()):
            raise ValueError("Все пути в files должны быть непустыми строками")
        self._ensure_files_exist()
    
    def _ensure_files_exist(self):
        """Убедиться что файлы существуют"""
        for file_key, file_name in self.config.items():
            if file_key != 'history':
                Path(file_name).touch()
    
    def read_lines(self, file_key: str) -> List[str]:
        """Прочитать строки из файла"""
        file_name = self.config.get(file_key, '')
        try:
            with open(file_name, 'r', encoding='utf-8') as f:
                return [line.strip() for line in f if line.strip()]
        except FileNotFoundError:
            return []
    
    def write_lines(
        self,
        file_key: str,
        lines: List[str],
        append: bool = False,
        unique: bool = False,
    ) -> int:
        """Записать строки в файл"""
        file_name = self.config.get(file_key, '')
        if unique:
            existing = set(self.read_lines(file_key)) if append else set()
            filtered_lines = []
            for line in lines:
                if line not in existing:
                    existing.add(line)
                    filtered_lines.append(line)
            lines = filtered_lines
        mode = 'a' if append else 'w'
        with open(file_name, mode, encoding='utf-8') as f:
            for line in lines:
                f.write(line + '\n')
        return len(lines)
    
    def append_history(self, message: str):
        """Добавить запись в историю"""
        file_name = self.config.get('history', 'history.txt')
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(file_name, 'a', encoding='utf-8') as f:
            f.write(f"[{timestamp}] {message}\n")


# ==================== WALLET MANAGER ====================

class WalletManager:
    """Управление кошельками"""

    # Только порог наличия средств для SPL → SOL, когда RPC не умеет
    # рассчитать fee для versioned-сообщения Jupiter. Это не списываемая комиссия.
    MIN_SPL_SWAP_FEE_RESERVE_LAMPORTS = 100_000
    SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")
    
    def __init__(self, config_manager: ConfigManager, file_manager: FileManager):
        self.config = config_manager.config
        self.config_mgr = config_manager
        self.file_mgr = file_manager
        self.mnemonic = Mnemonic("english")
        self.rpc = RpcHelper(
            config_manager.get_rpc_endpoints(),
            max_retries=config_manager.get_max_retries(),
        )
        
        api_key = config_manager.get_jupiter_api_key()
        if api_key:
            print("✅ Менеджер кошельков инициализирован (с Jupiter API key)")
        else:
            print("✅ Менеджер кошельков инициализирован (без Jupiter API key)")
    
    def _keys_to_addresses_list(self, keys: List[str]) -> List[str]:
        """Преобразовать ключи в адреса"""
        addresses = []
        for key in keys:
            try:
                address = str(self.parse_private_key(key).pubkey())
                addresses.append(address)
            except Exception:
                pass
        return addresses
    
    @staticmethod
    def parse_private_key(key_string: str) -> Keypair:
        """Парсит приватный ключ из разных форматов"""
        key_string = key_string.strip()
        
        # JSON array format [1,2,3,...]
        if key_string.startswith("["):
            bytes_list = json.loads(key_string)
            return Keypair.from_bytes(bytes(bytes_list))
        
        # Base58 format
        try:
            key_bytes = base58.b58decode(key_string)
            return Keypair.from_bytes(key_bytes)
        except Exception:
            pass
        
        # Hex format
        if len(key_string) == 128:
            key_bytes = bytes.fromhex(key_string)
            return Keypair.from_bytes(key_bytes)
        
        raise ValueError("Неизвестный формат ключа")

    def derive_mnemonic_keypair(
        self,
        phrase: str,
        *,
        legacy: bool = False,
        path: str = DEFAULT_DERIVATION_PATH,
    ) -> Keypair:
        """Восстановить keypair по стандартному Solana path или legacy-алгоритму v0.19."""
        phrase = ' '.join(phrase.strip().split())
        if not self.mnemonic.check(phrase):
            raise ValueError("Некорректная BIP-39 фраза или checksum")
        seed = self.mnemonic.to_seed(phrase)
        key_seed = bytes(seed[:32]) if legacy else derive_slip10_ed25519_seed(seed, path)
        return Keypair.from_seed(key_seed)

    @staticmethod
    def _validate_pubkeys(addresses: List[str]) -> Tuple[List[str], List[str]]:
        """Валидировать и дедуплицировать адреса с сохранением порядка."""
        valid: List[str] = []
        invalid: List[str] = []
        seen = set()
        for address in addresses:
            try:
                normalized = str(Pubkey.from_string(address.strip()))
            except Exception:
                invalid.append(address)
                continue
            if normalized not in seen:
                seen.add(normalized)
                valid.append(normalized)
        return valid, invalid

    @staticmethod
    def _build_transfer_message(
        sender: Keypair,
        recipient: Pubkey,
        lamports: int,
        blockhash: Optional[str] = None,
    ) -> Message:
        instruction = transfer(TransferParams(
            from_pubkey=sender.pubkey(),
            to_pubkey=recipient,
            lamports=lamports,
        ))
        if blockhash is not None:
            return Message.new_with_blockhash(
                [instruction],
                sender.pubkey(),
                Hash.from_string(blockhash),
            )
        return Message([instruction], payer=sender.pubkey())

    def _submit_and_confirm(
        self,
        transaction: Transaction,
        blockhash_info: BlockhashInfo,
    ) -> Tuple[str, Optional[str]]:
        """Отправить одну транзакцию и вернуть её фактический статус."""
        return self._submit_with_last_valid_height(
            transaction,
            blockhash_info.last_valid_block_height,
        )

    def _submit_with_last_valid_height(
        self,
        transaction: Any,
        last_valid_block_height: Optional[int],
    ) -> Tuple[str, Optional[str]]:
        """Общий submit/confirm для legacy и versioned transactions."""
        local_signature = str(transaction.signatures[0])
        signature = self.rpc.send_transaction(bytes(transaction))
        if signature is None:
            if not self.rpc.last_error_was_transport:
                return 'failed', None
            # RPC мог принять транзакцию до обрыва ответа.
            signature = local_signature

        state = self.rpc.wait_for_confirmation_sync(
            signature,
            timeout=self.config_mgr.get_confirmation_timeout(),
            last_valid_block_height=last_valid_block_height,
        )
        return state, signature
    
    # =========== 1. CREATE WALLETS ===========
    
    def create_wallets(self):
        """🔐 Создание новых кошельков"""
        print("\n" + "="*60)
        print("🔐 CREATE WALLETS - Создание кошельков")
        print("="*60)
        
        print("\nВыберите тип кошелька:")
        print("1. Сид-фразы (mnemonic)")
        print("2. Приватные ключи (keypair)")
        
        choice = input("\nВыбор (1 или 2): ").strip()
        
        if choice not in ['1', '2']:
            print("❌ Неверный выбор!")
            return
        
        try:
            count = int(input("Количество кошельков: "))
            if count <= 0:
                print("❌ Количество должно быть положительным числом!")
                return
        except ValueError:
            print("❌ Неверное число!")
            return
        
        if choice == '1':
            self._create_mnemonics(count)
        else:
            self._create_keypairs(count)
    
    def _create_mnemonics(self, count: int):
        """Создание сид-фраз"""
        print("\nДлина фразы:")
        print("1. 12 слов")
        print("2. 24 слова")
        
        length_choice = input("Выбор (1 или 2): ").strip()
        if length_choice not in {'1', '2'}:
            print("❌ Неверный выбор!")
            return
        strength = 128 if length_choice == '1' else 256
        
        mnemonics = []
        for i in range(count):
            phrase = self.mnemonic.generate(strength=strength)
            mnemonics.append(phrase)
            print(f"✓ {i+1}/{count} - Фраза создана")
        
        saved_count = self.file_mgr.write_lines('phrases', mnemonics, append=True, unique=True)
        print(f"\n✅ Создано {count} сид-фраз")
        print(f"➕ Добавлено новых строк: {saved_count} (старые данные не перезаписаны)")
        print(f"📁 Сохранено в: {self.file_mgr.config.get('phrases', 'phrases.txt')}")
        self.file_mgr.append_history(f"Created {count} mnemonic phrases")
    
    def _create_keypairs(self, count: int):
        """Создание приватных ключей"""
        keypairs = []
        for i in range(count):
            kp = Keypair()
            secret_key = base58.b58encode(bytes(kp)).decode()
            keypairs.append(secret_key)
            print(f"✓ {i+1}/{count} - Ключ создан")
        
        saved_count = self.file_mgr.write_lines('keys', keypairs, append=True, unique=True)
        print(f"\n✅ Создано {count} приватных ключей")
        print(f"➕ Добавлено новых строк: {saved_count} (старые данные не перезаписаны)")
        print(f"📁 Сохранено в: {self.file_mgr.config.get('keys', 'keys.txt')}")
        self.file_mgr.append_history(f"Created {count} keypairs")
    
    # =========== 2. CONVERTER ===========
    
    def convert_keys(self):
        """🔄 Конвертация ключей"""
        print("\n" + "="*60)
        print("🔄 CONVERTER - Конвертация ключей")
        print("="*60)
        
        print("\nВыберите направление конвертации:")
        print("1. Сид-фразы → Приватные ключи")
        print("2. Приватные ключи → Публичные адреса")
        
        choice = input("\nВыбор (1 или 2): ").strip()
        
        if choice == '1':
            self._phrases_to_keys()
        elif choice == '2':
            self._keys_to_addresses()
        else:
            print("❌ Неверный выбор!")
    
    def _phrases_to_keys(self):
        """Конвертация сид-фраз в приватные ключи"""
        phrases = self.file_mgr.read_lines('phrases')
        if not phrases:
            print("❌ Файл phrases.txt пуст!")
            return

        print("\nВыберите способ деривации:")
        print(f"1. Стандартный Solana {DEFAULT_DERIVATION_PATH} (рекомендуется)")
        print("2. Legacy v0.19 seed[:32] (только для восстановления старых адресов)")
        derivation_choice = input("Выбор (Enter = 1): ").strip() or '1'
        if derivation_choice not in {'1', '2'}:
            print("❌ Неверный выбор!")
            return
        legacy = derivation_choice == '2'
        
        keys = []
        for i, phrase in enumerate(phrases, 1):
            try:
                kp = self.derive_mnemonic_keypair(phrase, legacy=legacy)
                secret_key = base58.b58encode(bytes(kp)).decode()
                keys.append(secret_key)
                print(f"✓ {i}/{len(phrases)} - Конвертировано")
            except Exception as e:
                print(f"❌ Ошибка на строке {i}: {str(e)[:50]}")
        
        if keys:
            saved_count = self.file_mgr.write_lines('keys', keys, append=True, unique=True)
            print(f"\n✅ Конвертировано {len(keys)} ключей")
            print(f"➕ Добавлено новых ключей: {saved_count}")
            print(f"📁 Сохранено в: {self.file_mgr.config.get('keys', 'keys.txt')}")
            self.file_mgr.append_history(f"Converted {len(keys)} phrases to keys")
    
    def _keys_to_addresses(self):
        """Конвертация приватных ключей в публичные адреса"""
        keys = self.file_mgr.read_lines('keys')
        if not keys:
            print("❌ Файл keys.txt пуст!")
            return
        
        addresses = []
        for i, key in enumerate(keys, 1):
            try:
                kp = self.parse_private_key(key)
                address = str(kp.pubkey())
                addresses.append(address)
                print(f"✓ {i}/{len(keys)} - Адрес получен")
            except Exception as e:
                print(f"❌ Ошибка на строке {i}: {str(e)[:50]}")
        
        if addresses:
            existing_addresses = self.file_mgr.read_lines('wallets')
            if existing_addresses:
                confirm = input(
                    f"⚠️  Файл wallets содержит {len(existing_addresses)} строк. "
                    "Заменить его адресами текущих ключей? (yes/no): "
                ).strip().lower()
                if confirm != 'yes':
                    print("❌ Запись wallets отменена")
                    return
            self.file_mgr.write_lines('wallets', addresses)
            print(f"\n✅ Получено {len(addresses)} адресов")
            print(f"📁 Сохранено в: {self.file_mgr.config.get('wallets', 'wallets.txt')}")
            self.file_mgr.append_history(f"Converted {len(addresses)} keys to addresses")
    
    # =========== 3. BALANCE CHECKER ===========
    
    def check_balance(self):
        """📊 Проверка баланса кошельков"""
        print("\n" + "="*60)
        print("📊 BALANCE CHECKER - Проверка баланса")
        print("="*60)
        
        addresses = self.file_mgr.read_lines('wallets')
        
        if not addresses:
            keys = self.file_mgr.read_lines('keys')
            if not keys:
                print("❌ Файлы пусты (wallets.txt и keys.txt)!")
                return
            addresses = self._keys_to_addresses_list(keys)
            if not addresses:
                print("❌ Не удалось конвертировать ключи!")
                return

        addresses, invalid_addresses = self._validate_pubkeys(addresses)
        if invalid_addresses:
            print(f"⚠️  Пропущено некорректных адресов: {len(invalid_addresses)}")
        if not addresses:
            print("❌ Нет корректных адресов!")
            return
        
        print("\nВыберите какие токены проверять:")
        print("1. Только SOL")
        print("2. SOL + популярные SPL токены")
        print("3. Конкретный SPL токен (mint адрес)")
        
        token_choice = input("\nВыбор (1-3): ").strip()
        
        tokens_to_check = {}
        if token_choice == '1':
            tokens_to_check = {'SOL': None}
        elif token_choice == '2':
            tokens_to_check = {'SOL': None}
            popular = self.config_mgr.get_all_tokens()
            for symbol, token_info in popular.items():
                if symbol != 'SOL':
                    tokens_to_check[symbol] = token_info.get('mint')
        elif token_choice == '3':
            token_mint = input("Введите mint адрес токена: ").strip()
            token_name = input("Введите название токена: ").strip()
            try:
                Pubkey.from_string(token_mint)
            except Exception:
                print("❌ Некорректный mint адрес!")
                return
            if not token_name:
                print("❌ Название токена не может быть пустым!")
                return
            tokens_to_check = {token_name: token_mint}
        else:
            print("❌ Неверный выбор!")
            return
        
        print(f"\n⏳ Проверяем {len(addresses)} кошельков...")
        print(f"📋 Токены: {', '.join(tokens_to_check.keys())}")
        print("-" * 60)
        
        balances_by_token = {token: Decimal(0) for token in tokens_to_check}
        success_count = 0
        
        for i, address in enumerate(addresses, 1):
            print(f"\n{i}. {address[:20]}...")
            wallet_has_balance = False
            
            for token_name, token_mint in tokens_to_check.items():
                if token_name == 'SOL':
                    balance = self.rpc.get_balance(address)
                    if balance is not None:
                        balances_by_token['SOL'] += balance
                        if balance > 0:
                            wallet_has_balance = True
                        print(f"   💰 {token_name}: {format(balance, 'f')}")
                    else:
                        print(f"   ❌ {token_name}: Ошибка подключения")
                else:
                    token_info = self.rpc.get_token_balance(address, token_mint)
                    if token_info:
                        amount = token_info['uiAmount']
                        balances_by_token[token_name] += amount
                        if amount > 0:
                            wallet_has_balance = True
                        print(f"   💱 {token_name}: {amount}")
                    else:
                        if self.rpc.last_error:
                            print(f"   ❌ {token_name}: ошибка RPC")
                        else:
                            print(f"   • {token_name}: 0 (no account)")
            
            if wallet_has_balance:
                success_count += 1
        
        print("\n" + "-" * 60)
        print("\n📊 ИТОГО ПО ТОКЕНАМ:")
        for token_name, total in balances_by_token.items():
            print(f"  💰 {token_name}: {format(total, 'f')}")
        
        print(f"\n✅ Кошельков с балансом: {success_count}/{len(addresses)}")
        self.file_mgr.append_history(f"Checked {success_count} wallets")
    
    # =========== 4. MULTISENDER ===========
    
    def multisender(self):
        """📤 Множественная отправка SOL или USDC"""
        print("\n" + "="*60)
        print("📤 MULTISENDER - Множественная отправка")
        print("="*60)

        print("\nТокен:")
        print("1. SOL")
        print("2. USDC (только один → много)")
        token_choice = input("Выбор (1 или 2): ").strip()
        if token_choice == '2':
            self._send_usdc_one_to_many()
            return
        if token_choice != '1':
            print("❌ Неверный выбор!")
            return
        
        print("\nВыберите режим:")
        print("1. С одного кошелька на несколько")
        print("2. С нескольких кошельков на один")
        
        choice = input("\nВыбор (1 или 2): ").strip()
        
        if choice == '1':
            self._send_one_to_many()
        elif choice == '2':
            self._send_many_to_one()
        else:
            print("❌ Неверный выбор!")

    def _build_usdc_transfer_message(
        self,
        sender: Keypair,
        recipient: Pubkey,
        amount: int,
        blockhash: str,
        recipient_ata_exists: bool,
    ) -> Message:
        """Собрать USDC transferChecked и при необходимости создать ATA получателя."""
        mint = Pubkey.from_string(self.config_mgr.get_all_tokens()['USDC']['mint'])
        sender_ata = self.rpc._associated_token_address(
            str(sender.pubkey()), str(mint), TOKEN_PROGRAM_ID,
        )
        recipient_ata = self.rpc._associated_token_address(
            str(recipient), str(mint), TOKEN_PROGRAM_ID,
        )
        instructions = []
        if not recipient_ata_exists:
            instructions.append(Instruction(
                program_id=ASSOCIATED_TOKEN_PROGRAM_ID,
                accounts=[
                    AccountMeta(sender.pubkey(), True, True),
                    AccountMeta(recipient_ata, False, True),
                    AccountMeta(recipient, False, False),
                    AccountMeta(mint, False, False),
                    AccountMeta(self.SYSTEM_PROGRAM_ID, False, False),
                    AccountMeta(TOKEN_PROGRAM_ID, False, False),
                ],
                data=bytes([1]),  # CreateIdempotent
            ))
        instructions.append(Instruction(
            program_id=TOKEN_PROGRAM_ID,
            accounts=[
                AccountMeta(sender_ata, False, True),
                AccountMeta(mint, False, False),
                AccountMeta(recipient_ata, False, True),
                AccountMeta(sender.pubkey(), True, False),
            ],
            data=bytes([12]) + amount.to_bytes(8, 'little') + bytes([6]),
        ))
        return Message.new_with_blockhash(
            instructions, sender.pubkey(), Hash.from_string(blockhash),
        )

    def _send_usdc_one_to_many(self):
        """Отправить USDC на несколько адресов с созданием ATA при необходимости."""
        keys = self.file_mgr.read_lines('keys')
        addresses = self.file_mgr.read_lines('wallets')
        if not keys or not addresses:
            print("❌ Нужны ключи в keys.txt и получатели в wallets.txt!")
            return
        try:
            sender_idx = int(input(f"Выберите отправителя (1-{len(keys)}): ")) - 1
            sender = self.parse_private_key(keys[sender_idx])
        except (ValueError, IndexError, Exception):
            print("❌ Некорректный отправитель!")
            return

        sender_address = str(sender.pubkey())
        addresses, invalid = self._validate_pubkeys(addresses)
        if invalid:
            print(f"⚠️ Пропущено некорректных адресов: {len(invalid)}")
        if sender_address in addresses:
            addresses.remove(sender_address)
        if not addresses:
            print("❌ Нет получателей!")
            return
        try:
            amount = decimal_to_raw(Decimal(input("Сумма USDC для каждого адреса: ").strip()), 6)
        except (InvalidOperation, ValueError):
            print("❌ Неверная сумма USDC!")
            return

        usdc = self.config_mgr.get_all_tokens()['USDC']
        token_balance = self.rpc.get_token_balance(sender_address, usdc['mint'])
        if not token_balance or token_balance['spendableAmount'] < amount * len(addresses):
            print("❌ Недостаточно USDC на associated token account!")
            return
        rent = self.rpc.get_token_account_rent_exemption()
        sender_system_rent = self.rpc.get_minimum_balance_for_rent_exemption(0)
        if rent is None or sender_system_rent is None:
            print("❌ Не удалось рассчитать rent для token account")
            return

        recipient_state = []
        for address in addresses:
            ata = self.rpc._associated_token_address(address, usdc['mint'], TOKEN_PROGRAM_ID)
            exists = self.rpc.account_exists(str(ata))
            if exists is None:
                print("❌ Не удалось проверить token account получателя")
                return
            recipient_state.append((address, exists))
        blockhash_info = self.rpc.get_latest_blockhash()
        if blockhash_info is None:
            print("❌ Не удалось получить blockhash")
            return
        first_address, first_ata_exists = recipient_state[0]
        preview = self._build_usdc_transfer_message(
            sender, Pubkey.from_string(first_address), amount,
            blockhash_info.blockhash, first_ata_exists,
        )
        fee = self.rpc.get_fee_for_message(preview)
        if fee is None:
            print("❌ Не удалось рассчитать комиссию")
            return
        total_rent = rent * sum(1 for _, exists in recipient_state if not exists)
        total_fee = fee * len(addresses)
        sol_balance = self.rpc.get_balance_lamports(sender_address)
        required_sol = total_fee + total_rent + sender_system_rent
        if sol_balance is None or sol_balance < required_sol:
            print("❌ Недостаточно SOL на комиссии и создание USDC-аккаунтов!")
            return
        print(f"\n📤 USDC: по {format_raw_amount(amount, 6)} на {len(addresses)} адресов")
        print(f"⚙️ Комиссии: {format_raw_amount(total_fee, 9)} SOL")
        print(f"🏦 Rent новых ATA: {format_raw_amount(total_rent, 9)} SOL")
        print(f"🛡️ Минимальный остаток SOL отправителя: {format_raw_amount(sender_system_rent, 9)} SOL")
        for index, (address, exists) in enumerate(recipient_state, 1):
            print(f"   {index}. {address} {'(ATA уже есть)' if exists else '(будет создан ATA)'}")
        if input("⚠️ Продолжить? (yes/no): ").strip().lower() != 'yes':
            print("❌ Операция отменена")
            return

        counts = {'confirmed': 0, 'pending': 0, 'expired': 0, 'failed': 0}
        for index, (address, ata_exists) in enumerate(recipient_state, 1):
            info = self.rpc.get_latest_blockhash()
            if info is None:
                counts['failed'] += 1
                continue
            message = self._build_usdc_transfer_message(
                sender, Pubkey.from_string(address), amount, info.blockhash, ata_exists,
            )
            tx = Transaction([sender], message, Hash.from_string(info.blockhash))
            state, signature = self._submit_and_confirm(tx, info)
            counts[state] = counts.get(state, 0) + 1
            print(f"{'✓' if state == 'confirmed' else '⚠️'} {index}. {state}: {signature or '-'}")
            if signature:
                self.file_mgr.append_history(f"Multisender USDC {address}: {state}, tx={signature}")
        print(f"✅ Подтверждено: {counts['confirmed']}/{len(recipient_state)}")
    
    def _send_one_to_many(self):
        """Отправка с одного кошелька на несколько"""
        keys = self.file_mgr.read_lines('keys')
        addresses = self.file_mgr.read_lines('wallets')
        
        if not keys:
            print("❌ Файл keys.txt пуст!")
            return
        
        if not addresses:
            addresses = self._keys_to_addresses_list(keys)
            if not addresses:
                print("❌ Не удалось получить адреса!")
                return

        try:
            sender_idx = int(input(f"Выберите отправителя (1-{len(keys)}): ")) - 1
            if sender_idx < 0 or sender_idx >= len(keys):
                print("❌ Неверный номер!")
                return
        except ValueError:
            print("❌ Неверное число!")
            return

        try:
            sender_kp = self.parse_private_key(keys[sender_idx])
            sender_address = str(sender_kp.pubkey())
        except Exception as e:
            print(f"❌ Ошибка восстановления ключа: {str(e)[:50]}")
            return

        original_count = len(addresses)
        addresses, invalid_addresses = self._validate_pubkeys(addresses)
        if invalid_addresses:
            print(f"⚠️  Пропущено некорректных адресов: {len(invalid_addresses)}")
        duplicate_count = original_count - len(invalid_addresses) - len(addresses)
        if duplicate_count:
            print(f"⚠️  Удалено дубликатов адресов: {duplicate_count}")
        if sender_address in addresses:
            addresses.remove(sender_address)
            print("⚠️  Адрес отправителя исключён из получателей")
        if not addresses:
            print("❌ После валидации не осталось получателей!")
            return

        try:
            amount_lamports = decimal_to_raw(
                Decimal(input("Сумма SOL для каждого адреса: ").strip()),
                9,
            )
        except (InvalidOperation, ValueError):
            print("❌ Неверная сумма или больше 9 знаков после запятой!")
            return

        new_system_accounts = []
        for address in addresses:
            exists = self.rpc.account_exists(address)
            if exists is None:
                print("❌ Не удалось проверить существование адреса получателя")
                return
            if not exists:
                new_system_accounts.append(address)
        if new_system_accounts:
            system_rent = self.rpc.get_minimum_balance_for_rent_exemption(0)
            if system_rent is None:
                print("❌ Не удалось рассчитать минимум для нового SOL-аккаунта")
                return
            if amount_lamports < system_rent:
                print(
                    "❌ Для нового адреса нужно отправить минимум "
                    f"{format_raw_amount(system_rent, 9)} SOL; "
                    f"сейчас указано {format_raw_amount(amount_lamports, 9)} SOL"
                )
                return
            print(f"ℹ️  Будет создано новых SOL-аккаунтов: {len(new_system_accounts)}")

        blockhash_info = self.rpc.get_latest_blockhash()
        if blockhash_info is None:
            print("❌ Не удалось получить blockhash для расчёта комиссии")
            return
        preview_message = self._build_transfer_message(
            sender_kp,
            Pubkey.from_string(addresses[0]),
            amount_lamports,
            blockhash_info.blockhash,
        )
        fee_per_tx = self.rpc.get_fee_for_message(preview_message)
        if fee_per_tx is None:
            print("❌ Не удалось рассчитать комиссию; операция отменена")
            return

        balance_lamports = self.rpc.get_balance_lamports(sender_address)
        if balance_lamports is None:
            print("❌ Не удалось получить баланс; отправка заблокирована")
            return
        total_fee = fee_per_tx * len(addresses)
        total_transfer = amount_lamports * len(addresses)
        total_needed = total_transfer + total_fee

        print(f"\n📤 Режим: Один → Много ({len(addresses)} адресов)")
        print(f"💰 Баланс отправителя: {format_raw_amount(balance_lamports, 9)} SOL")
        print(f"💸 Требуется отправить: {format_raw_amount(total_transfer, 9)} SOL")
        print(f"⚙️  Комиссии: {format_raw_amount(total_fee, 9)} SOL")
        print(f"📊 ИТОГО: {format_raw_amount(total_needed, 9)} SOL")
        print("📋 Финальные получатели:")
        for index, recipient in enumerate(addresses, 1):
            print(f"   {index}. {recipient}")
        if balance_lamports < total_needed:
            print("❌ Недостаточно средств!")
            return

        confirm = input("\n⚠️  Вы уверены? (yes/no): ").strip().lower()
        if confirm != 'yes':
            print("❌ Операция отменена")
            return

        print(f"\n⏳ Отправляем {format_raw_amount(amount_lamports, 9)} SOL на {len(addresses)} адресов...")
        print("-" * 60)

        counts = {'confirmed': 0, 'pending': 0, 'expired': 0, 'failed': 0}
        for i, recipient in enumerate(addresses, 1):
            try:
                blockhash_info = self.rpc.get_latest_blockhash()
                if not blockhash_info:
                    print(f"❌ {i}/{len(addresses)}. Ошибка получения blockhash")
                    counts['failed'] += 1
                    continue

                message = self._build_transfer_message(
                    sender_kp,
                    Pubkey.from_string(recipient),
                    amount_lamports,
                    blockhash_info.blockhash,
                )
                tx = Transaction([sender_kp], message, Hash.from_string(blockhash_info.blockhash))
                state, signature = self._submit_and_confirm(tx, blockhash_info)
                counts[state] = counts.get(state, 0) + 1
                if state == 'confirmed':
                    print(f"✓ {i}/{len(addresses)}. Подтверждено | TX: {signature[:30]}...")
                elif state == 'pending':
                    print(f"⚠️  {i}/{len(addresses)}. Статус неизвестен | TX: {signature[:30]}...")
                else:
                    print(f"❌ {i}/{len(addresses)}. {state}")
                if signature:
                    self.file_mgr.append_history(f"Multisender 1→many {recipient}: {state}, tx={signature}")
            except Exception as e:
                print(f"❌ {i}/{len(addresses)}. Ошибка: {str(e)[:60]}")
                counts['failed'] += 1

        print("-" * 60)
        print(f"\n✅ Подтверждено: {counts['confirmed']}/{len(addresses)}")
        print(f"⚠️  Неизвестно/истекло: {counts['pending'] + counts['expired']}; ошибок: {counts['failed']}")
        self.file_mgr.append_history(f"Multisender (1→many): {counts}")
    
    def _send_many_to_one(self):
        """Отправка с нескольких кошельков на один"""
        keys = self.file_mgr.read_lines('keys')
        addresses = self.file_mgr.read_lines('wallets')
        
        if not keys:
            print("❌ Файл keys.txt пуст!")
            return
        
        if not addresses:
            addresses = self._keys_to_addresses_list(keys)
            if not addresses:
                print("❌ Не удалось получить адреса!")
                return
        
        addresses, invalid_addresses = self._validate_pubkeys(addresses)
        if invalid_addresses:
            print(f"⚠️  Пропущено некорректных адресов: {len(invalid_addresses)}")
        if not addresses:
            print("❌ Нет корректных адресов получателей!")
            return

        print(f"\n📤 Режим: Много → Один ({len(keys)} строк ключей)")
        print("\nВыберите адрес получателя:")
        
        for i, addr in enumerate(addresses, 1):
            print(f"{i}. {addr}")
        
        try:
            recipient_idx = int(input(f"\nВыбор (1-{len(addresses)}): ")) - 1
            if recipient_idx < 0 or recipient_idx >= len(addresses):
                print("❌ Неверный номер!")
                return
        except ValueError:
            print("❌ Неверное число!")
            return
        
        recipient = addresses[recipient_idx]
        recipient_pubkey = Pubkey.from_string(recipient)

        senders: Dict[str, Keypair] = {}
        invalid_keys = 0
        for key in keys:
            try:
                keypair = self.parse_private_key(key)
                senders.setdefault(str(keypair.pubkey()), keypair)
            except Exception:
                invalid_keys += 1
        if invalid_keys:
            print(f"⚠️  Пропущено некорректных ключей: {invalid_keys}")
        if recipient in senders:
            del senders[recipient]
            print("⚠️  Кошелёк-получатель исключён из отправителей")
        if not senders:
            print("❌ Нет корректных уникальных отправителей!")
            return

        plan: List[Tuple[Keypair, int, int]] = []
        total_to_send = 0
        print(f"\n⏳ Проверяем балансы {len(keys)} кошельков...")

        for i, (address, keypair) in enumerate(senders.items(), 1):
            try:
                balance = self.rpc.get_balance_lamports(address)
                if balance is None:
                    print(f"❌ {i}. {address[:20]}... → ошибка RPC")
                    continue
                blockhash_info = self.rpc.get_latest_blockhash()
                if blockhash_info is None:
                    print(f"❌ {i}. {address[:20]}... → нет blockhash")
                    continue
                message = self._build_transfer_message(
                    keypair, recipient_pubkey, 1, blockhash_info.blockhash,
                )
                fee = self.rpc.get_fee_for_message(message)
                if fee is None:
                    print(f"❌ {i}. {address[:20]}... → не удалось рассчитать комиссию")
                    continue
                if balance > fee:
                    net_amount = balance - fee
                    plan.append((keypair, balance, fee))
                    total_to_send += net_amount
                    print(f"✓ {i}. {address[:20]}... → {format_raw_amount(net_amount, 9)} SOL")
                else:
                    print(f"• {i}. {address[:20]}... → Недостаточно")
            except Exception as e:
                print(f"❌ {i}. Ошибка: {str(e)[:40]}")
        
        if not plan:
            print("❌ Нет кошельков с достаточным балансом!")
            return

        print(f"\n💰 Всего к отправке: {format_raw_amount(total_to_send, 9)} SOL")
        confirm = input("⚠️  Продолжить? (yes/no): ").strip().lower()
        if confirm != 'yes':
            print("❌ Операция отменена")
            return
        
        print(f"\n⏳ Отправляем с {len(plan)} кошельков...")
        print("-" * 60)

        counts = {'confirmed': 0, 'pending': 0, 'expired': 0, 'failed': 0}
        for sender_idx, (sender_kp, _, _) in enumerate(plan, 1):
            try:
                sender_address = str(sender_kp.pubkey())
                current_balance = self.rpc.get_balance_lamports(sender_address)
                blockhash_info = self.rpc.get_latest_blockhash()
                if current_balance is None or blockhash_info is None:
                    counts['failed'] += 1
                    continue

                fee_message = self._build_transfer_message(
                    sender_kp, recipient_pubkey, 1, blockhash_info.blockhash,
                )
                fee = self.rpc.get_fee_for_message(fee_message)
                if fee is None or current_balance <= fee:
                    print(f"❌ {sender_idx}. Баланс изменился или нет комиссии")
                    counts['failed'] += 1
                    continue
                amount_lamports = current_balance - fee
                message = self._build_transfer_message(
                    sender_kp, recipient_pubkey, amount_lamports, blockhash_info.blockhash,
                )
                tx = Transaction([sender_kp], message, Hash.from_string(blockhash_info.blockhash))
                state, signature = self._submit_and_confirm(tx, blockhash_info)
                counts[state] = counts.get(state, 0) + 1
                if state == 'confirmed':
                    print(f"✓ {sender_idx}. Подтверждено {format_raw_amount(amount_lamports, 9)} SOL")
                else:
                    print(f"⚠️  {sender_idx}. Статус: {state}")
                if signature:
                    self.file_mgr.append_history(f"Multisender many→1 {sender_address}: {state}, tx={signature}")
            except Exception as e:
                print(f"❌ {sender_idx}. Ошибка: {str(e)[:60]}")
                counts['failed'] += 1

        print("-" * 60)
        print(f"\n✅ Подтверждено: {counts['confirmed']}/{len(plan)}")
        print(f"⚠️  Неизвестно/истекло: {counts['pending'] + counts['expired']}; ошибок: {counts['failed']}")
        self.file_mgr.append_history(f"Multisender (many→1): {counts}")
    
    # =========== 5. SWAP TOKENS ===========
    
    async def swap_tokens(self):
        """💱 Обмен токенов через Jupiter API"""
        print("\n" + "="*60)
        print("💱 SWAP TOKENS - Обмен через Jupiter")
        print("="*60)
        
        # Получаем ключи
        keys = self.file_mgr.read_lines('keys')
        if not keys:
            print("❌ Файл keys.txt пуст!")
            return
        
        # Получаем токены из конфига
        tokens = self.config_mgr.get_all_tokens()
        if not tokens:
            print("❌ Токены не настроены в config.json!")
            return
        
        # Показываем доступные токены
        print("\n📋 Доступные токены:")
        token_list = list(tokens.items())
        for i, (symbol, info) in enumerate(token_list, 1):
            print(f"  {i}. {symbol} ({info['mint'][:20]}...)")
        print("  0. Ввести mint адрес вручную")
        
        # Выбор входящего токена
        print("\n📥 Выберите ВХОДЯЩИЙ токен (что отдаём):")
        try:
            input_choice = input("Номер или символ: ").strip()
            
            if input_choice == '0':
                input_mint = input("   Mint адрес: ").strip()
                input_symbol = input("   Символ токена: ").strip().upper()
                input_decimals = int(input("   Decimals (0-255): ").strip() or "6")
                input_token = TokenInfo(mint=input_mint, decimals=input_decimals, symbol=input_symbol)
            elif input_choice.isdigit():
                input_idx = int(input_choice) - 1
                if input_idx < 0 or input_idx >= len(token_list):
                    print("❌ Неверный номер!")
                    return
                input_symbol, input_info = token_list[input_idx]
                input_token = TokenInfo(
                    mint=input_info['mint'],
                    decimals=input_info['decimals'],
                    symbol=input_symbol
                )
            else:
                input_symbol = input_choice.upper()
                if input_symbol not in tokens:
                    print("❌ Токен не найден!")
                    return
                input_info = tokens[input_symbol]
                input_token = TokenInfo(
                    mint=input_info['mint'],
                    decimals=input_info['decimals'],
                    symbol=input_symbol
                )
            
            print(f"   ✅ Выбран: {input_token.symbol}")
        except ValueError:
            print("❌ Неверный ввод!")
            return
        
        # Выбор исходящего токена
        print("\n📤 Выберите ВЫХОДНОЙ токен (что получаем):")
        available = [(s, i) for s, i in token_list if s != input_token.symbol]
        for i, (symbol, _) in enumerate(available, 1):
            print(f"  {i}. {symbol}")
        print("  0. Ввести mint адрес вручную")
        
        try:
            output_choice = input("Номер или символ: ").strip()
            
            if output_choice == '0':
                output_mint = input("   Mint адрес: ").strip()
                output_symbol = input("   Символ токена: ").strip().upper()
                output_decimals = int(input("   Decimals (0-255): ").strip() or "6")
                output_token = TokenInfo(mint=output_mint, decimals=output_decimals, symbol=output_symbol)
            elif output_choice.isdigit():
                output_idx = int(output_choice) - 1
                if output_idx < 0 or output_idx >= len(available):
                    print("❌ Неверный номер!")
                    return
                output_symbol, output_info = available[output_idx]
                output_token = TokenInfo(
                    mint=output_info['mint'],
                    decimals=output_info['decimals'],
                    symbol=output_symbol
                )
            else:
                output_symbol = output_choice.upper()
                if output_symbol not in tokens or output_symbol == input_token.symbol:
                    print("❌ Токен не найден или совпадает с входящим!")
                    return
                output_info = tokens[output_symbol]
                output_token = TokenInfo(
                    mint=output_info['mint'],
                    decimals=output_info['decimals'],
                    symbol=output_symbol
                )
            
            print(f"   ✅ Выбран: {output_token.symbol}")
        except ValueError:
            print("❌ Неверный ввод!")
            return

        try:
            Pubkey.from_string(input_token.mint)
            Pubkey.from_string(output_token.mint)
        except Exception:
            print("❌ Некорректный mint адрес!")
            return
        if not input_token.symbol or not output_token.symbol:
            print("❌ Символ токена не может быть пустым!")
            return
        if input_token.mint == output_token.mint:
            print("❌ Входной и выходной mint совпадают!")
            return

        for token in (input_token, output_token):
            actual_decimals = self.rpc.get_token_decimals(token.mint)
            if actual_decimals is None:
                print(f"❌ Не удалось проверить mint {token.symbol} в сети")
                return
            if token.decimals != actual_decimals:
                print(f"⚠️  {token.symbol}: decimals исправлено {token.decimals} → {actual_decimals} по on-chain данным")
                token.decimals = actual_decimals
        
        # Выбор режима (один или все кошельки)
        print("\n👛 Выберите режим:")
        print("  1. Один кошелёк")
        print("  2. Все кошельки из keys.txt")
        
        mode_choice = input("Выбор (1 или 2): ").strip()
        
        if mode_choice == '1':
            print(f"\nДоступно кошельков: {len(keys)}")
            for index, key in enumerate(keys, 1):
                try:
                    print(f"   {index}. {self.parse_private_key(key).pubkey()}")
                except Exception:
                    print(f"   {index}. <некорректный ключ>")
            try:
                wallet_idx = int(input(f"Выберите кошелёк (1-{len(keys)}): ")) - 1
                if wallet_idx < 0 or wallet_idx >= len(keys):
                    print("❌ Неверный номер!")
                    return
                selected_keys = [keys[wallet_idx]]
            except ValueError:
                print("❌ Неверное число!")
                return
        elif mode_choice == '2':
            selected_keys = keys
        else:
            print("❌ Неверный выбор!")
            return

        selected_keypairs: Dict[str, Keypair] = {}
        invalid_key_count = 0
        for key in selected_keys:
            try:
                keypair = self.parse_private_key(key)
                selected_keypairs.setdefault(str(keypair.pubkey()), keypair)
            except Exception:
                invalid_key_count += 1
        if invalid_key_count:
            print(f"⚠️  Пропущено некорректных ключей: {invalid_key_count}")
        if not selected_keypairs:
            print("❌ Нет корректных ключей для swap!")
            return
        
        # Сумма
        swap_all_choice = input("\n💰 Свапнуть весь баланс? (y/n): ").strip().lower()
        swap_all = swap_all_choice == 'y'
        
        amount = 0
        if not swap_all:
            try:
                amount_str = input(f"Сумма {input_token.symbol}: ").strip()
                amount = decimal_to_raw(Decimal(amount_str), input_token.decimals)
            except (InvalidOperation, ValueError):
                print("❌ Неверная сумма или слишком много знаков после запятой!")
                return
        
        # Slippage
        default_slippage = self.config_mgr.get_default_slippage()
        try:
            slippage_str = input(f"\n📊 Slippage % (Enter = {default_slippage/100}%): ").strip()
            if slippage_str:
                slippage_percent = Decimal(slippage_str)
                if not slippage_percent.is_finite() or slippage_percent < Decimal('0.01') or slippage_percent > Decimal('50'):
                    print("❌ Slippage должен быть от 0.01% до 50%!")
                    return
                slippage_bps = int(slippage_percent * 100)
            else:
                slippage_bps = default_slippage
        except (InvalidOperation, ValueError):
            print("❌ Неверный slippage!")
            return
        
        print(f"   ✅ Slippage: {slippage_bps/100}%")
        
        # Настройки приоритета
        priority = self.config_mgr.get_priority_settings()
        priority_cap = int(priority['max_lamports']) if priority['max_lamports'] else 0
        reserve_lamports = max(
            self.config_mgr.get_swap_fee_reserve_lamports(),
            priority_cap + 3_000_000,
        )
        
        # Подтверждение
        print(f"\n{'='*50}")
        print("📋 ПАРАМЕТРЫ СВОПА:")
        print(f"   📥 Отдаём: {'ВСЁ' if swap_all else format_raw_amount(amount, input_token.decimals)} {input_token.symbol}")
        print(f"   📤 Получаем: {output_token.symbol}")
        print(f"   📊 Slippage: {slippage_bps/100}%")
        print(f"   💸 Priority: {priority['level']} ({priority['max_lamports']} lamports)")
        if input_token.mint == WRAPPED_SOL_MINT:
            print(f"   🛡️  Резерв SOL на fee/ATA: {format_raw_amount(reserve_lamports, 9)} SOL")
        else:
            print("   🧮 Комиссия SOL будет проверена по готовой swap-транзакции")
        print(f"   👛 Кошельков: {len(selected_keypairs)}")
        for index, address in enumerate(selected_keypairs, 1):
            print(f"      {index}. {address}")
        print("="*50)
        
        confirm = input("\n⚠️  Начать? (yes/no): ").strip().lower()
        if confirm != 'yes':
            print("❌ Отменено")
            return
        
        # Создаём Jupiter клиент
        jupiter = JupiterSwapClient(
            api_url=self.config_mgr.get_jupiter_api_url(),
            api_key=self.config_mgr.get_jupiter_api_key(),
            priority_level=priority['level'],
            max_priority_lamports=priority['max_lamports']
        )
        try:
            jupiter.get_priority_fee_payload()
        except ValueError as exc:
            print(f"❌ {exc}")
            return
        
        counts = {'confirmed': 0, 'pending': 0, 'expired': 0, 'failed': 0, 'skipped': 0}
        delay = self.config_mgr.get_delay_between_wallets()
        
        try:
            for i, (pubkey, keypair) in enumerate(selected_keypairs.items(), 1):
                print(f"\n[{i}/{len(selected_keypairs)}]")
                outcome_recorded = False
                
                try:
                    print(f"🔄 Кошелёк: {pubkey[:20]}...{pubkey[-8:]}")
                    
                    # Получаем балансы
                    sol_balance_lamports = self.rpc.get_balance_lamports(pubkey)
                    if sol_balance_lamports is None:
                        print("💰 SOL: ошибка RPC")
                        counts['failed'] += 1
                        continue
                    print(f"💰 SOL: {format_raw_amount(sol_balance_lamports, 9)}")
                    
                    # Определяем сумму для свопа
                    swap_amount = amount
                    
                    if input_token.mint == WRAPPED_SOL_MINT:
                        if swap_all:
                            if sol_balance_lamports <= reserve_lamports:
                                print("   ⚠️ Недостаточно SOL")
                                continue
                            swap_amount = sol_balance_lamports - reserve_lamports
                            print(f"   📊 Свапаем: {format_raw_amount(swap_amount, 9)} SOL")
                        else:
                            if sol_balance_lamports < amount + reserve_lamports:
                                print("   ⚠️ Недостаточно SOL")
                                continue
                    else:
                        # Получаем баланс токена
                        token_info = self.rpc.get_token_balance(pubkey, input_token.mint)
                        
                        if token_info:
                            ui_balance = token_info['uiAmount']
                            raw_balance = token_info['spendableAmount']
                            print(f"💰 {input_token.symbol}: {ui_balance}")
                            if token_info['auxiliaryAmount'] > 0:
                                print(
                                    f"   ⚠️ {format_raw_amount(token_info['auxiliaryAmount'], input_token.decimals)} "
                                    "на auxiliary token accounts недоступно Jupiter"
                                )
                        else:
                            print(f"   ⚠️ Нет {input_token.symbol} на балансе")
                            continue
                        
                        if swap_all:
                            if raw_balance <= 0:
                                print(f"   ⚠️ Нет {input_token.symbol} на associated token account для swap")
                                continue
                            swap_amount = raw_balance
                            print(f"   📊 Свапаем: {format_raw_amount(raw_balance, input_token.decimals)} {input_token.symbol}")
                        else:
                            if raw_balance < amount:
                                print(f"   ⚠️ Недостаточно {input_token.symbol}")
                                continue
                        
                    
                    # Получаем котировку
                    print("📊 Получение котировки...")
                    quote = await jupiter.get_quote(
                        input_token.mint,
                        output_token.mint,
                        swap_amount,
                        slippage_bps
                    )
                    
                    if not quote:
                        print("   ❌ Не удалось получить котировку")
                        continue
                    
                    in_amount = int(quote.get("inAmount", 0))
                    out_amount = int(quote.get("outAmount", 0))
                    
                    print(f"   📥 Отдаём: {format_raw_amount(in_amount, input_token.decimals)} {input_token.symbol}")
                    print(f"   📤 Получаем: {format_raw_amount(out_amount, output_token.decimals)} {output_token.symbol}")
                    
                    # Получаем транзакцию
                    print("📝 Создание транзакции...")
                    swap_data = await jupiter.get_swap_transaction(quote, pubkey)
                    
                    if not swap_data:
                        print("   ❌ Не удалось создать транзакцию")
                        continue
                    
                    # Подписываем транзакцию
                    try:
                        transaction = VersionedTransaction.from_bytes(swap_data.transaction_bytes)
                        signed_tx = VersionedTransaction(transaction.message, [keypair])
                    except Exception as e:
                        print(f"   ❌ Ошибка подписи: {str(e)[:50]}")
                        continue

                    if input_token.mint != WRAPPED_SOL_MINT:
                        current_sol_balance = self.rpc.get_balance_lamports(pubkey)
                        network_fee = self.rpc.get_fee_for_message(signed_tx.message)
                        if current_sol_balance is None:
                            print("   ❌ Не удалось получить SOL-баланс для комиссии")
                            continue
                        if network_fee is None:
                            required_sol = (
                                priority_cap + self.MIN_SPL_SWAP_FEE_RESERVE_LAMPORTS
                            )
                            print(
                                "   ⚙️ RPC не оценил fee versioned-транзакции; "
                                f"проверяем безопасный минимум "
                                f"{format_raw_amount(required_sol, 9)} SOL"
                            )
                        else:
                            required_sol = network_fee + priority_cap
                            print(
                                "   ⚙️ Комиссия сети: "
                                f"{format_raw_amount(network_fee, 9)} SOL"
                            )
                        if current_sol_balance < required_sol:
                            print(
                                "   ⚠️ Недостаточно SOL для комиссии "
                                f"(нужно {format_raw_amount(required_sol, 9)} SOL)"
                            )
                            continue
                    
                    # Отправляем транзакцию
                    print("📤 Отправка транзакции...")
                    state, signature = await asyncio.to_thread(
                        self._submit_with_last_valid_height,
                        signed_tx,
                        swap_data.last_valid_block_height,
                    )
                    counts[state] = counts.get(state, 0) + 1
                    outcome_recorded = True

                    if signature:
                        print(f"✅ TX: {signature[:40]}...")
                        print(f"🔗 https://solscan.io/tx/{signature}")
                    else:
                        print("   ❌ Ошибка отправки транзакции")
                    if state == 'confirmed':
                        print("   ✅ Подтверждено!")
                    elif state == 'pending':
                        print("   ⚠️ Таймаут: статус неизвестен, не повторяйте swap вслепую")
                    else:
                        print(f"   ❌ Статус: {state}")
                    if signature:
                        self.file_mgr.append_history(
                            f"Swap {input_token.symbol}→{output_token.symbol} {pubkey}: {state}, tx={signature}"
                        )
                    
                    # Задержка между кошельками
                    if i < len(selected_keypairs):
                        print(f"⏳ Ждём {delay} сек...")
                        await asyncio.sleep(delay)
                    
                except Exception as e:
                    print(f"   ❌ Ошибка: {str(e)[:80]}")
                    counts['failed'] += 1
                    outcome_recorded = True
                finally:
                    if not outcome_recorded:
                        counts['skipped'] += 1
        
        finally:
            await jupiter.close()
        
        print(f"\n{'='*50}")
        print(f"✅ Подтверждено: {counts['confirmed']}/{len(selected_keypairs)}")
        print(f"• Пропущено до отправки: {counts['skipped']}")
        print(f"⚠️  Неизвестно/истекло: {counts['pending'] + counts['expired']}; ошибок: {counts['failed']}")
        self.file_mgr.append_history(f"Swap {input_token.symbol}→{output_token.symbol}: {counts}")
    
    # =========== 6. REFUND ===========
    
    def refund(self):
        """💰 Закрытие пустых токен-аккаунтов и возврат SOL"""
        print("\n" + "="*60)
        print("💰 REFUND - Закрытие пустых токен-аккаунтов")
        print("="*60)
        print("📋 Закрывает SPL Token аккаунты с нулевым балансом")
        print("   и возвращает замороженный SOL (~0.002 за аккаунт)")
        print("="*60)
        
        # Загружаем приватные ключи
        keys = self.file_mgr.read_lines('keys')
        if not keys:
            print("❌ Файл keys.txt пуст!")
            return
        
        print(f"\n🔍 СКАНИРОВАНИЕ {len(keys)} КОШЕЛЬКОВ")
        print("-" * 60)
        
        # Статистика
        stats = {
            'wallets_checked': 0,
            'wallets_with_tokens': 0,
            'total_token_accounts': 0,
            'empty_token_accounts': 0,
            'skipped_accounts': 0,
            'closed_accounts': 0,
            'pending': 0,
            'expired': 0,
            'failed': 0,
            'total_refunded_lamports': 0,
        }
        
        # Собираем информацию о всех кошельках
        wallets_to_process = []
        
        for i, key in enumerate(keys, 1):
            try:
                keypair = self.parse_private_key(key)
                pubkey = str(keypair.pubkey())
                
                # Получаем баланс SOL
                sol_balance_lamports = self.rpc.get_balance_lamports(pubkey)
                if sol_balance_lamports is None:
                    raise RpcError("Не удалось получить SOL-баланс")
                
                # Получаем пустые токен-аккаунты
                empty_accounts, total_accounts, empty_count, skipped_count = self.rpc.get_empty_token_accounts(pubkey)
                
                stats['wallets_checked'] += 1
                stats['total_token_accounts'] += total_accounts
                stats['empty_token_accounts'] += empty_count
                stats['skipped_accounts'] += skipped_count
                
                print(f"\n[{i}/{len(keys)}] {pubkey[:20]}...{pubkey[-8:]}")
                print(f"    💰 SOL: {format_raw_amount(sol_balance_lamports, 9)}")
                print(f"    📦 Токен-аккаунтов: {total_accounts} (пустых: {empty_count}, недоступных: {skipped_count})")
                
                if empty_accounts:
                    stats['wallets_with_tokens'] += 1
                    potential_refund_lamports = sum(account.rent_lamports for account in empty_accounts)
                    print(f"    💵 Можно вернуть: ~{format_raw_amount(potential_refund_lamports, 9)} SOL")
                    
                    wallets_to_process.append({
                        'keypair': keypair,
                        'pubkey': pubkey,
                        'sol_balance_lamports': sol_balance_lamports,
                        'empty_accounts': empty_accounts
                    })
                    
            except Exception as e:
                print(f"\n[{i}/{len(keys)}] ❌ Ошибка: {str(e)[:50]}")
        
        print("\n" + "-" * 60)
        print("\n📊 РЕЗУЛЬТАТ СКАНИРОВАНИЯ:")
        print(f"   👛 Проверено кошельков: {stats['wallets_checked']}")
        print(f"   📦 Всего токен-аккаунтов: {stats['total_token_accounts']}")
        print(f"   🗑️  Пустых аккаунтов: {stats['empty_token_accounts']}")
        print(f"   ⚠️  Недоступно для закрытия: {stats['skipped_accounts']}")
        print(f"   ✅ Кошельков с пустыми аккаунтами: {len(wallets_to_process)}")
        
        if not wallets_to_process:
            print("\n✅ Нет пустых токен-аккаунтов для закрытия!")
            self.file_mgr.append_history(f"Refund: scanned {stats['wallets_checked']} wallets, no empty accounts")
            return
        
        # Подсчитываем потенциальный возврат
        total_potential_lamports = sum(
            sum(account.rent_lamports for account in w['empty_accounts'])
            for w in wallets_to_process
        )
        total_accounts_to_close = sum(len(w['empty_accounts']) for w in wallets_to_process)
        
        print(f"\n💰 ПОТЕНЦИАЛЬНЫЙ ВОЗВРАТ: ~{format_raw_amount(total_potential_lamports, 9)} SOL")
        print(f"   (за закрытие {total_accounts_to_close} аккаунтов)")
        
        # Подтверждение
        confirm = input("\n⚠️  Закрыть пустые аккаунты и вернуть SOL? (yes/no): ").strip().lower()
        if confirm != 'yes':
            print("❌ Операция отменена")
            return
        
        print("\n" + "="*60)
        print("🔄 ЗАКРЫТИЕ ПУСТЫХ АККАУНТОВ")
        print("="*60)
        
        # Обрабатываем каждый кошелёк
        for wallet_info in wallets_to_process:
            keypair = wallet_info['keypair']
            pubkey = wallet_info['pubkey']
            empty_accounts = wallet_info['empty_accounts']
            sol_balance_lamports = wallet_info['sol_balance_lamports']
            
            print(f"\n👛 {pubkey[:20]}...{pubkey[-8:]}")
            print(f"   Закрываем {len(empty_accounts)} аккаунтов...")
            
            # Проверяем минимальный баланс для комиссии
            if sol_balance_lamports <= 0:
                print("   ⚠️  Нет SOL для комиссии")
                continue
            
            for token_account in empty_accounts:
                try:
                    # Получаем blockhash
                    blockhash_info = self.rpc.get_latest_blockhash()
                    if not blockhash_info:
                        print("   ❌ Ошибка получения blockhash")
                        stats['failed'] += 1
                        continue
                    
                    # Создаём инструкцию CloseAccount (opcode = 9)
                    close_data = bytes([9])
                    
                    token_acc_pubkey = Pubkey.from_string(token_account.pubkey)
                    
                    close_instruction = Instruction(
                        program_id=token_account.program_id,
                        accounts=[
                            AccountMeta(pubkey=token_acc_pubkey, is_signer=False, is_writable=True),
                            AccountMeta(pubkey=keypair.pubkey(), is_signer=False, is_writable=True),
                            AccountMeta(pubkey=keypair.pubkey(), is_signer=True, is_writable=False),
                        ],
                        data=close_data
                    )
                    
                    # Создаём и подписываем транзакцию
                    message = Message.new_with_blockhash(
                        [close_instruction],
                        keypair.pubkey(),
                        Hash.from_string(blockhash_info.blockhash),
                    )
                    fee = self.rpc.get_fee_for_message(message)
                    current_balance = self.rpc.get_balance_lamports(pubkey)
                    if fee is None or current_balance is None or current_balance < fee:
                        print(f"   ❌ {token_account.pubkey[:20]}... (нет комиссии)")
                        stats['failed'] += 1
                        continue
                    tx = Transaction([keypair], message, Hash.from_string(blockhash_info.blockhash))

                    state, signature = self._submit_and_confirm(tx, blockhash_info)
                    if state == 'confirmed':
                        print(f"   ✅ {token_account.pubkey[:20]}... (+{format_raw_amount(token_account.rent_lamports, 9)} SOL)")
                        stats['closed_accounts'] += 1
                        stats['total_refunded_lamports'] += token_account.rent_lamports
                    elif state in {'pending', 'expired'}:
                        print(f"   ⚠️  {token_account.pubkey[:20]}... (статус: {state})")
                        stats[state] += 1
                    else:
                        print(f"   ❌ {token_account.pubkey[:20]}... (ошибка)")
                        stats['failed'] += 1
                    if signature:
                        self.file_mgr.append_history(
                            f"Refund {token_account.pubkey}: {state}, tx={signature}"
                        )
                    
                    # Небольшая задержка между транзакциями
                    time.sleep(0.3)
                    
                except Exception as e:
                    print(f"   ❌ Ошибка: {str(e)[:50]}")
                    stats['failed'] += 1
        
        # Итоговая статистика
        print("\n" + "="*60)
        print("📊 ИТОГОВАЯ СТАТИСТИКА")
        print("="*60)
        print(f"   👛 Проверено кошельков: {stats['wallets_checked']}")
        print(f"   📦 Всего токен-аккаунтов: {stats['total_token_accounts']}")
        print(f"   🗑️  Пустых найдено: {stats['empty_token_accounts']}")
        print(f"   ✅ Успешно закрыто: {stats['closed_accounts']}")
        print(f"   ⚠️  Неизвестно/истекло: {stats['pending'] + stats['expired']}")
        print(f"   ❌ Ошибок: {stats['failed']}")
        print(f"   💰 Подтверждённый возврат SOL: ~{format_raw_amount(stats['total_refunded_lamports'], 9)}")
        print("="*60)
        
        self.file_mgr.append_history(
            f"Refund: closed {stats['closed_accounts']} accounts, "
            f"pending={stats['pending']}, expired={stats['expired']}, failed={stats['failed']}, "
            f"refunded={stats['total_refunded_lamports']} lamports"
        )


# ==================== UI ====================

class SolanaBotUI:
    """Интерфейс пользователя"""
    
    def __init__(self):
        self.config_mgr = ConfigManager()
        self.file_mgr = FileManager(self.config_mgr.config)
        self.wallet_mgr = WalletManager(self.config_mgr, self.file_mgr)
    
    def print_banner(self):
        """Вывести баннер"""
        banner = """
🚀 Solana Bot - полнофункциональный бот для Solana
        """
        print(banner)
    
    def print_menu(self):
        """Вывести главное меню"""
        menu = """
Главное меню

1. 🔐 Create wallets — Создание кошельков
2. 🔄 Converter — Конвертация ключей
3. 📊 Balance Checker — Проверка баланса
4. 📤 Multisender — Множественная отправка
5. 💱 Swap — Обмен токенов
6. 💰 Refund — Закрытие пустых аккаунтов
0. 🚪 Exit — Выход
        """
        print(menu)
    
    async def run(self):
        """Основной цикл приложения"""
        self.print_banner()
        
        while True:
            self.print_menu()
            choice = input("Выбор (0-6): ").strip()
            
            if choice == '0':
                print("\n👋 До встречи!")
                break
            elif choice == '1':
                self.wallet_mgr.create_wallets()
            elif choice == '2':
                self.wallet_mgr.convert_keys()
            elif choice == '3':
                self.wallet_mgr.check_balance()
            elif choice == '4':
                self.wallet_mgr.multisender()
            elif choice == '5':
                await self.wallet_mgr.swap_tokens()
            elif choice == '6':
                self.wallet_mgr.refund()
            else:
                print("❌ Неверный выбор! Попробуйте снова.")
            
            input("\nНажмите Enter для продолжения...")


# ==================== ENTRY POINT ====================

async def main():
    """Точка входа"""
    try:
        app = SolanaBotUI()
        await app.run()
    except KeyboardInterrupt:
        print("\n\n⚠️ Приложение прервано пользователем")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Критическая ошибка: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
