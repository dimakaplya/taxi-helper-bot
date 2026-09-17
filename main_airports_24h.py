#!/usr/bin/env python3
import logging
import asyncio
import sqlite3
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
import os

import fetch_yandex_data  # логика похода в Yandex Rasp API, запускается фоново прямо на Railway
import fetch_favt_notices  # логика сбора уведомлений Росавиации (@favt_info), тоже фоново

BOT_TOKEN = os.getenv('TELEGRAM_TOKEN', '8968196261:AAGjxaTy_evirnWDAO124vmkbbDFy03kekY')

# Файл с реальными данными. Раньше генерировался локальным скриптом на Маке,
# теперь фоновая задача внутри самого бота (см. flights_data_updater ниже)
# обновляет его прямо на Railway каждые FLIGHTS_UPDATE_INTERVAL_HOURS часов.
FLIGHTS_DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'flights_data.json')
FLIGHTS_DATA_MAX_AGE_HOURS = 26  # если данные старше - считаем их устаревшими
FLIGHTS_UPDATE_INTERVAL_HOURS = 2  # см. расчёт квоты 500 запросов/сутки в fetch_yandex_data.py

# Уведомления Росавиации об ограничениях в аэропортах (@favt_info) - публичная
# веб-страница, лимита запросов нет, поэтому обновляем чаще, чем расписание рейсов.
FAVT_NOTICES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'favt_notices.json')
FAVT_UPDATE_INTERVAL_MINUTES = 15

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==================== ПРОПУСКНАЯ СПОСОБНОСТЬ ====================
AIRPORT_CAPACITY = {
    'UUWW': 4966, 'UUDD': 1586, 'UUWL': 1838, 'UULP': 2373,
    'UNNT': 1084, 'USSS': 947, 'UWKD': 616, 'UUCC': 251,
    'UNOO': 183, 'UWWW': 411, 'URRP': 171, 'UWUU': 559,
    'URKK': 525, 'URSS': 1427,
}

# Часовой пояс каждого аэропорта (расписание Yandex Rasp API приходит в
# МЕСТНОМ времени аэропорта, а не в UTC/московском). Сервер Railway обычно
# работает по UTC, поэтому "текущий час" нужно считать именно в поясе
# конкретного аэропорта, а не по системному времени сервера - иначе все
# расчёты "сейчас"/"через 2 часа" будут сдвинуты на несколько часов.
AIRPORT_TIMEZONE = {
    'UUWW': 'Europe/Moscow', 'UUDD': 'Europe/Moscow', 'UUWL': 'Europe/Moscow',
    'UULP': 'Europe/Moscow', 'UWKD': 'Europe/Moscow', 'URRP': 'Europe/Moscow',
    'URKK': 'Europe/Moscow', 'URSS': 'Europe/Moscow',
    'UNNT': 'Asia/Novosibirsk',
    'USSS': 'Asia/Yekaterinburg', 'UUCC': 'Asia/Yekaterinburg', 'UWUU': 'Asia/Yekaterinburg',
    'UNOO': 'Asia/Omsk',
    'UWWW': 'Europe/Samara',
}

def get_airport_now(airport_icao):
    """Текущее время в часовом поясе конкретного аэропорта."""
    tz_name = AIRPORT_TIMEZONE.get(airport_icao, 'Europe/Moscow')
    try:
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        return datetime.now()  # fallback, если tzdata почему-то недоступна

# Разбивка пассажиропотока по классам обслуживания (совпадает с fetch_yandex_data.py)
ECONOMY_SHARE = 0.85
BUSINESS_SHARE = 0.15

# Категории водителей -> какой класс пассажиров для них релевантен
CATEGORY_TO_CLASS = {
    'taxi': 'economy',
    'ultima': 'business',
    'courier': 'total',
    'cargo': 'total',
}

AIRPORTS_INFO = {
    'moscow': [
        {'name': 'SVO (Шереметьево)', 'emoji': '✈️', 'icao': 'UUWW', 'iata': 'SVO'},
        {'name': 'DME (Домодедово)', 'emoji': '✈️', 'icao': 'UUDD', 'iata': 'DME'},
        {'name': 'VKO (Внуково)', 'emoji': '✈️', 'icao': 'UUWL', 'iata': 'VKO'},
    ],
    'spb': [
        {'name': 'LED (Пулково)', 'emoji': '✈️', 'icao': 'UULP', 'iata': 'LED'},
    ],
    'novosibirsk': [
        {'name': 'OVB (Толмачёво)', 'emoji': '✈️', 'icao': 'UNNT', 'iata': 'OVB'},
    ],
    'ekb': [
        {'name': 'SVX (Кольцово)', 'emoji': '✈️', 'icao': 'USSS', 'iata': 'SVX'},
    ],
    'kazan': [
        {'name': 'KZN (Казань)', 'emoji': '✈️', 'icao': 'UWKD', 'iata': 'KZN'},
    ],
    'chelyabinsk': [
        {'name': 'CEK (Баландино)', 'emoji': '✈️', 'icao': 'UUCC', 'iata': 'CEK'},
    ],
    'omsk': [
        {'name': 'OMS (Омск)', 'emoji': '✈️', 'icao': 'UNOO', 'iata': 'OMS'},
    ],
    'samara': [
        {'name': 'KUF (Курумоч)', 'emoji': '✈️', 'icao': 'UWWW', 'iata': 'KUF'},
    ],
    'rostov': [
        # ⚠️ Платов закрыт для гражданских полётов - данные по нему не собираем
        # (fetch_yandex_data.py пропускает его без единого запроса к API,
        # экономим квоту), бот показывает статичную заглушку "закрыт".
        {'name': 'RND (Ростов-на-Дону)', 'emoji': '✈️', 'icao': 'URRP', 'iata': 'RND', 'closed': True},
    ],
    'ufa': [
        {'name': 'UFA (Уфа)', 'emoji': '✈️', 'icao': 'UWUU', 'iata': 'UFA'},
    ],
    'krasnodar': [
        {'name': 'KRR (Краснодар)', 'emoji': '✈️', 'icao': 'URKK', 'iata': 'KRR'},
    ],
    'sochi': [
        {'name': 'AER (Адлер)', 'emoji': '✈️', 'icao': 'URSS', 'iata': 'AER'},
    ]
}

# Обратный индекс ICAO -> город/данные аэропорта - нужен для пушей об
# изменении статуса аэропорта: по коду аэропорта нужно быстро понять, каким
# водителям (по выбранному городу) это разослать.
ICAO_TO_CITY = {}
ICAO_TO_AIRPORT = {}
for _city_key, _airports_list in AIRPORTS_INFO.items():
    for _airport in _airports_list:
        ICAO_TO_CITY[_airport['icao']] = _city_key
        ICAO_TO_AIRPORT[_airport['icao']] = _airport

CATEGORIES = {
    'taxi': {'name': 'ТАКСИ', 'tariffs': ['Эконом', 'Комфорт', 'Комфорт+', 'Минивэн']},
    'ultima': {'name': 'ТАКСИ ULTIMA', 'tariffs': ['Business', 'Premier', 'Elite', 'Cruise']},
    'courier': {'name': 'КУРЬЕР', 'tariffs': ['Пеший', 'Авто']},
    'cargo': {'name': 'ГРУЗОВОЕ ТАКСИ', 'tariffs': []}
}

# ==================== ЗАПАСНЫЕ ДАННЫЕ SVO (fallback, если flights_data.json ещё не сгенерирован) ====================
FALLBACK_ARRIVALS_SVO = [
    {'time': '00:30', 'origin': 'Стамбул', 'airline': 'Turkish', 'flight': '1502', 'passengers': 180},
    {'time': '01:15', 'origin': 'Дубай', 'airline': 'Emirates', 'flight': '502', 'passengers': 200},
    {'time': '02:45', 'origin': 'Барселона', 'airline': 'Lufthansa', 'flight': '782', 'passengers': 190},
    {'time': '03:30', 'origin': 'Берлин', 'airline': 'Аэрофлот', 'flight': '1870', 'passengers': 175},
    {'time': '04:45', 'origin': 'Пекин', 'airline': 'Air China', 'flight': '812', 'passengers': 220},
    {'time': '06:00', 'origin': 'Паттайя', 'airline': 'Thai', 'flight': '2202', 'passengers': 190},
    {'time': '06:45', 'origin': 'Бангкок', 'airline': 'S7', 'flight': '4201', 'passengers': 200},
    {'time': '07:15', 'origin': 'Шарм-эль-Шейх', 'airline': 'Аэрофлот', 'flight': '430', 'passengers': 210},
    {'time': '10:00', 'origin': 'Стамбул', 'airline': 'Turkish', 'flight': '1505', 'passengers': 220},
    {'time': '10:45', 'origin': 'Барселона', 'airline': 'Iberia', 'flight': '1125', 'passengers': 210},
    {'time': '11:15', 'origin': 'Дубай', 'airline': 'Emirates', 'flight': '505', 'passengers': 230},
    {'time': '11:50', 'origin': 'Милан', 'airline': 'Lufthansa', 'flight': '785', 'passengers': 215},
    {'time': '12:20', 'origin': 'Париж', 'airline': 'Air France', 'flight': '1602', 'passengers': 225},
    {'time': '12:55', 'origin': 'Франкфурт', 'airline': 'Lufthansa', 'flight': '787', 'passengers': 220},
    {'time': '13:30', 'origin': 'Рим', 'airline': 'Alitalia', 'flight': '1402', 'passengers': 210},
    {'time': '14:00', 'origin': 'Лондон', 'airline': 'British Airways', 'flight': '2502', 'passengers': 235},
    {'time': '14:45', 'origin': 'Вена', 'airline': 'Austrian', 'flight': '602', 'passengers': 200},
    {'time': '15:15', 'origin': 'Праг', 'airline': 'Czech Airlines', 'flight': '1302', 'passengers': 195},
    {'time': '15:50', 'origin': 'Амстердам', 'airline': 'KLM', 'flight': '803', 'passengers': 230},
    {'time': '16:20', 'origin': 'Женева', 'airline': 'SWISS', 'flight': '502', 'passengers': 210},
    {'time': '17:00', 'origin': 'Стокгольм', 'airline': 'SAS', 'flight': '1402', 'passengers': 205},
    {'time': '18:00', 'origin': 'Копенгаген', 'airline': 'SAS', 'flight': '1403', 'passengers': 210},
    {'time': '18:45', 'origin': 'Хельсинки', 'airline': 'Finnair', 'flight': '802', 'passengers': 195},
    {'time': '19:15', 'origin': 'Осло', 'airline': 'SAS', 'flight': '1404', 'passengers': 205},
    {'time': '19:50', 'origin': 'Цюрих', 'airline': 'SWISS', 'flight': '503', 'passengers': 200},
    {'time': '20:20', 'origin': 'Брюссель', 'airline': 'Brussels Airlines', 'flight': '502', 'passengers': 215},
    {'time': '20:55', 'origin': 'Таллин', 'airline': 'Lufthansa', 'flight': '1302', 'passengers': 190},
    {'time': '22:00', 'origin': 'Рига', 'airline': 'airBaltic', 'flight': '302', 'passengers': 185},
    {'time': '22:45', 'origin': 'Вильнюс', 'airline': 'Lufthansa', 'flight': '1303', 'passengers': 180},
    {'time': '23:30', 'origin': 'Минск', 'airline': 'Аэрофлот', 'flight': '1850', 'passengers': 195},
]

FALLBACK_DEPARTURES_SVO = [
    {'time': '00:45', 'dest': 'Ташкент', 'airline': 'Uzbekistan', 'flight': '602', 'passengers': 185},
    {'time': '01:30', 'dest': 'Баку', 'airline': 'AZAL', 'flight': '4110', 'passengers': 200},
    {'time': '02:15', 'dest': 'Тбилиси', 'airline': 'Georgian', 'flight': '501', 'passengers': 175},
    {'time': '03:45', 'dest': 'Ереван', 'airline': 'Armavia', 'flight': '301', 'passengers': 160},
    {'time': '04:30', 'dest': 'Алма-Ата', 'airline': 'Air Astana', 'flight': '301', 'passengers': 210},
    {'time': '06:15', 'dest': 'Санкт-Петербург', 'airline': 'Россия', 'flight': '6230', 'passengers': 185},
    {'time': '06:50', 'dest': 'Казань', 'airline': 'Победа', 'flight': '6730', 'passengers': 200},
    {'time': '07:20', 'dest': 'Екатеринбург', 'airline': 'Аэрофлот', 'flight': '1450', 'passengers': 210},
    {'time': '07:55', 'dest': 'Новосибирск', 'airline': 'S7', 'flight': '4160', 'passengers': 220},
    {'time': '10:00', 'dest': 'Стамбул', 'airline': 'Turkish', 'flight': '1515', 'passengers': 225},
    {'time': '10:45', 'dest': 'Берлин', 'airline': 'Lufthansa', 'flight': '790', 'passengers': 215},
    {'time': '11:15', 'dest': 'Дубай', 'airline': 'Emirates', 'flight': '510', 'passengers': 240},
    {'time': '11:50', 'dest': 'Париж', 'airline': 'Air France', 'flight': '1610', 'passengers': 230},
    {'time': '12:20', 'dest': 'Лондон', 'airline': 'British Airways', 'flight': '2510', 'passengers': 235},
    {'time': '12:55', 'dest': 'Милан', 'airline': 'Alitalia', 'flight': '1410', 'passengers': 220},
    {'time': '13:30', 'dest': 'Рим', 'airline': 'Alitalia', 'flight': '1412', 'passengers': 215},
    {'time': '14:00', 'dest': 'Вена', 'airline': 'Austrian', 'flight': '612', 'passengers': 205},
    {'time': '14:45', 'dest': 'Прага', 'airline': 'Czech Airlines', 'flight': '1312', 'passengers': 200},
    {'time': '15:15', 'dest': 'Амстердам', 'airline': 'KLM', 'flight': '812', 'passengers': 235},
    {'time': '15:50', 'dest': 'Женева', 'airline': 'SWISS', 'flight': '512', 'passengers': 220},
    {'time': '16:20', 'dest': 'Цюрих', 'airline': 'SWISS', 'flight': '514', 'passengers': 215},
    {'time': '17:00', 'dest': 'Мюнхен', 'airline': 'Lufthansa', 'flight': '791', 'passengers': 210},
    {'time': '18:00', 'dest': 'Копенгаген', 'airline': 'SAS', 'flight': '1412', 'passengers': 215},
    {'time': '18:45', 'dest': 'Хельсинки', 'airline': 'Finnair', 'flight': '812', 'passengers': 205},
    {'time': '19:15', 'dest': 'Осло', 'airline': 'SAS', 'flight': '1413', 'passengers': 210},
    {'time': '19:50', 'dest': 'Стокгольм', 'airline': 'SAS', 'flight': '1414', 'passengers': 220},
    {'time': '20:20', 'dest': 'Бельфаст', 'airline': 'British Airways', 'flight': '2515', 'passengers': 210},
    {'time': '20:55', 'dest': 'Эдинбург', 'airline': 'British Airways', 'flight': '2516', 'passengers': 205},
    {'time': '22:00', 'dest': 'Дублин', 'airline': 'Aer Lingus', 'flight': '503', 'passengers': 200},
    {'time': '22:45', 'dest': 'Мадрид', 'airline': 'Iberia', 'flight': '1135', 'passengers': 210},
    {'time': '23:30', 'dest': 'Барселона', 'airline': 'Iberia', 'flight': '1137', 'passengers': 215},
]

DB_FILE = 'taxi_queue.db'
# Если водитель встал в очередь и не появлялся дольше этого времени - считаем,
# что он уже уехал (забрал пассажира) или просто забыл нажать "Покинуть
# очередь", и убираем его из очереди автоматически.
QUEUE_ENTRY_TTL_MINUTES = 120

# user_state раньше жил только в памяти процесса - при каждом рестарте/редеплое
# (то есть при каждом git push) состояние ВСЕХ водителей обнулялось, и им
# приходилось заново жать /start. При росте числа водителей (сотни-тысяча)
# это уже реальная проблема, а не мелочь. Решение: user_state[user_id] и
# вложенные в него ключи автоматически сохраняются в SQLite при любом
# изменении - а при старте бота состояние подгружается обратно. Весь
# остальной код бота работает с user_state как с обычным dict, ничего в нём
# менять не пришлось - персистентность спрятана внутри этих двух классов.

class PersistentUserDict(dict):
    """Состояние ОДНОГО пользователя. Любое изменение ключа (set/del/pop)
    сразу сохраняет весь словарь целиком в БД."""
    def __init__(self, user_id, *args, **kwargs):
        self._user_id = user_id
        super().__init__(*args, **kwargs)

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        save_user_state(self._user_id, dict(self))

    def __delitem__(self, key):
        super().__delitem__(key)
        save_user_state(self._user_id, dict(self))

    def pop(self, key, *default):
        result = super().pop(key, *default)
        save_user_state(self._user_id, dict(self))
        return result

class PersistentUserStateStore(dict):
    """user_state целиком. user_state[user_id] = {...} оборачивает значение в
    PersistentUserDict и сохраняет его; user_state.pop(user_id) удаляет
    запись и из БД тоже."""
    def __setitem__(self, user_id, value):
        if not isinstance(value, PersistentUserDict):
            value = PersistentUserDict(user_id, value)
        super().__setitem__(user_id, value)
        save_user_state(user_id, dict(value))

    def pop(self, user_id, *default):
        result = super().pop(user_id, *default)
        delete_user_state(user_id)
        return result

def save_user_state(user_id, state_dict):
    try:
        init_db()
        conn = get_db_connection()
        conn.execute(
            'INSERT INTO user_states (user_id, state_json, updated_at) VALUES (?, ?, ?) '
            'ON CONFLICT(user_id) DO UPDATE SET state_json = excluded.state_json, updated_at = excluded.updated_at',
            (user_id, json.dumps(state_dict, ensure_ascii=False), datetime.now(ZoneInfo('UTC')).strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"❌ Не удалось сохранить состояние пользователя {user_id}: {e}")

def delete_user_state(user_id):
    try:
        init_db()
        conn = get_db_connection()
        conn.execute('DELETE FROM user_states WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"❌ Не удалось удалить состояние пользователя {user_id}: {e}")

def load_all_user_states():
    """Восстанавливает user_state из БД при старте бота - без этого все
    водители слетали бы на выбор города после каждого редеплоя."""
    try:
        init_db()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT user_id, state_json FROM user_states')
        rows = cursor.fetchall()
        conn.close()
        restored = 0
        for user_id, state_json in rows:
            try:
                data = json.loads(state_json)
                # dict.__setitem__ напрямую - в обход персистентности, иначе
                # мы бы тут же переписали в БД то, что только что из неё прочитали
                dict.__setitem__(user_state, user_id, PersistentUserDict(user_id, data))
                restored += 1
            except Exception:
                continue
        logger.info(f"✅ Восстановлено состояние {restored} пользователей из БД")
    except Exception as e:
        logger.error(f"❌ Не удалось восстановить состояния пользователей: {e}")

user_state = PersistentUserStateStore()

_flights_data_cache = None
_flights_data_mtime = None

def load_flights_data():
    """Загружает flights_data.json (генерируется fetch_yandex_data.py локально).
    Кэширует в памяти, перечитывает только если файл изменился на диске."""
    global _flights_data_cache, _flights_data_mtime
    try:
        mtime = os.path.getmtime(FLIGHTS_DATA_FILE)
        if _flights_data_cache is not None and mtime == _flights_data_mtime:
            return _flights_data_cache

        with open(FLIGHTS_DATA_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)

        generated_at = datetime.fromisoformat(data['generated_at'])
        age_hours = (datetime.now() - generated_at).total_seconds() / 3600
        if age_hours > FLIGHTS_DATA_MAX_AGE_HOURS:
            logger.warning(f"⚠️ flights_data.json устарел ({age_hours:.1f}ч), но всё равно используем")

        _flights_data_cache = data
        _flights_data_mtime = mtime
        return data
    except FileNotFoundError:
        logger.warning("⚠️ flights_data.json не найден - запусти fetch_yandex_data.py локально. Использую запасные данные.")
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка чтения flights_data.json: {e}")
        return None

_favt_notices_cache = None
_favt_notices_mtime = None

def load_favt_notices():
    """Загружает favt_notices.json (уведомления Росавиации об ограничениях)."""
    global _favt_notices_cache, _favt_notices_mtime
    try:
        mtime = os.path.getmtime(FAVT_NOTICES_FILE)
        if _favt_notices_cache is not None and mtime == _favt_notices_mtime:
            return _favt_notices_cache
        with open(FAVT_NOTICES_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        _favt_notices_cache = data
        _favt_notices_mtime = mtime
        return data
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка чтения favt_notices.json: {e}")
        return None

def get_notices_for_airport(icao):
    """Уведомления Росавиации за последние 12ч, касающиеся конкретного аэропорта."""
    data = load_favt_notices()
    if not data:
        return []
    return [n for n in data.get('notices', []) if icao in n.get('airports', [])]

# Аэропорты, закрытые для гражданских полётов постоянно (не зависит от
# уведомлений Росавиации, которые могут вообще не упоминать их) - Платов
# (Ростов). Статус для них всегда "закрыт", данные по ним не собираются
# (см. fetch_yandex_data.py) и не запрашиваются.
PERMANENTLY_CLOSED_AIRPORTS = {'URRP'}

def get_airport_status(icao):
    """Статус аэропорта по последнему уведомлению Росавиации за 12ч:
    'closed' (ВВЕДЕНЫ ограничения), 'coordinated' (работает по согласованию),
    'open' (СНЯТЫ ограничения), или 'open' по умолчанию, если уведомлений нет
    вообще (не значит 100% гарантию - просто нет свежих данных об ограничениях)."""
    if icao in PERMANENTLY_CLOSED_AIRPORTS:
        return 'closed', None
    notices = get_notices_for_airport(icao)
    if not notices:
        return 'open', None
    latest = notices[0]  # notices уже отсортированы по времени, свежие первые
    text_upper = latest['text'].upper()
    if 'ПО СОГЛАСОВАНИЮ' in text_upper:
        return 'coordinated', latest
    if 'СНЯТ' in text_upper:
        return 'open', latest
    if 'ВВЕДЕН' in text_upper:
        return 'closed', latest
    return 'open', latest

AIRPORT_STATUS_DISPLAY = {
    'open': ('🟢', 'ОТКРЫТ'),
    'coordinated': ('🟡', 'РАБОТАЕТ ПО СОГЛАСОВАНИЮ'),
    'closed': ('🔴', 'ЗАКРЫТ (ограничения)'),
}

def escape_md(text):
    """Экранирует спецсимволы legacy Markdown (parse_mode='Markdown'), чтобы
    непредсказуемый внешний текст (уведомления Росавиации и т.п.) не ломал
    разметку сообщения - иначе Telegram отклоняет весь месседж целиком."""
    if not text:
        return text
    for ch in ('_', '*', '`', '['):
        text = text.replace(ch, '\\' + ch)
    return text

def get_airport_flights(airport_icao, flight_type='departures'):
    """Получить рейсы аэропорта из реальных данных (flights_data.json).
    Если файла нет - для SVO отдаём запасной хардкод, для остальных пусто."""
    try:
        now = datetime.now()
        flights = []
        data = load_flights_data()

        if data and airport_icao in data.get('airports', {}):
            raw_flights = data['airports'][airport_icao].get(flight_type, [])
            for flight_data in raw_flights:
                time_parts = flight_data['time'].split(':')
                hour, minute = int(time_parts[0]), int(time_parts[1])
                flight_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                total_pax = flight_data.get('passengers', 170)
                # economy/business берутся из fetch_yandex_data.py (уже посчитаны по типу ВС);
                # если их нет в файле (старые данные) - считаем на лету по стандартной разбивке
                economy_pax = flight_data.get('passengers_economy', round(total_pax * ECONOMY_SHARE))
                business_pax = flight_data.get('passengers_business', total_pax - economy_pax)
                # 'domestic' есть в свежих данных (посчитан в fetch_yandex_data.py);
                # если его нет (старый flights_data.json, ещё без этого поля) -
                # считаем на лету тем же классификатором, чтобы не хранить два разных.
                destination = flight_data.get('point', '')
                domestic = flight_data.get('domestic')
                if domestic is None:
                    domestic = fetch_yandex_data.is_domestic_flight(destination)
                flights.append({
                    'time': flight_data['time'],
                    'callsign': f"{flight_data['airline']} {flight_data['flight']}",
                    'destination': destination,
                    'aircraft': flight_data.get('aircraft', ''),
                    'firstSeen': int(flight_time.timestamp()),
                    'passengers': total_pax,
                    'passengers_economy': economy_pax,
                    'passengers_business': business_pax,
                    'domestic': domestic,
                })
            logger.info(f"✅ Загружено {len(flights)} реальных рейсов {airport_icao} ({flight_type})")
            return flights

        # Запасной вариант - только для SVO, пока нет свежего flights_data.json
        if airport_icao == 'UUWW':
            data_source = FALLBACK_ARRIVALS_SVO if flight_type == 'arrivals' else FALLBACK_DEPARTURES_SVO
            for flight_data in data_source:
                time_parts = flight_data['time'].split(':')
                hour, minute = int(time_parts[0]), int(time_parts[1])
                flight_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                total_pax = flight_data['passengers']
                economy_pax = round(total_pax * ECONOMY_SHARE)
                business_pax = total_pax - economy_pax
                destination = flight_data.get('dest') or flight_data.get('origin')
                flights.append({
                    'time': flight_data['time'],
                    'callsign': f"{flight_data['airline']}{flight_data['flight']}",
                    'destination': destination,
                    'aircraft': '',
                    'firstSeen': int(flight_time.timestamp()),
                    'passengers': total_pax,
                    'passengers_economy': economy_pax,
                    'passengers_business': business_pax,
                    'domestic': fetch_yandex_data.is_domestic_flight(destination),
                })
            logger.info(f"⚠️ Использую запасные данные SVO ({len(flights)} рейсов)")
            return flights

        return []
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        return []

def get_load_emoji(load_percent):
    if load_percent <= 50: return '🔴'
    elif load_percent <= 70: return '🟡'
    elif load_percent <= 100: return '🟢'
    else: return '🟣'

def get_load_recommendation(load_percent):
    if load_percent <= 50: return 'НЕ ЕХАТЬ'
    elif load_percent <= 70: return '📍 ЗАНЯТЬ ОЧЕРЕДЬ'
    elif load_percent <= 100: return '✅ ЕХАТЬ'
    else: return '🚨 СРОЧНО'

# Среднее время до вылета, за которое пассажир заказывает такси из города в
# аэропорт (внутренние рейсы ~2ч до вылета, международные обычно больше, но
# берём усреднённо). Используется только для вкладки "Вылеты" - в отличие от
# прилётов, где пассажир уже в аэропорту и водителю нужно ехать туда, при
# вылете пассажир ещё в городе, и заказ появляется заранее, а не в момент
# вылета - логика "ехать в аэропорт/в очередь" тут не подходит вообще.
DEPARTURE_LEAD_TIME_HOURS = 2

def get_departure_recommendation(demand_percent):
    if demand_percent <= 50: return '😴 Заказов мало'
    elif demand_percent <= 70: return '📱 Будь на связи в городе'
    elif demand_percent <= 100: return '🏙️ Активно бери заказы в аэропорт'
    else: return '🔥 Пиковый спрос на заказы'

def get_db_connection():
    """Единая точка подключения к SQLite. При росте числа водителей (сотни
    одновременных отметок в час пик) SQLite по умолчанию сериализует запись -
    один писатель блокирует остальных и можно словить "database is locked".
    WAL-режим позволяет читать во время записи и сильно снижает конфликты,
    а busy_timeout заставляет соединение подождать и повторить попытку вместо
    того чтобы сразу падать с ошибкой."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=10000')
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            city TEXT,
            airport TEXT,
            tariff TEXT,
            position_range TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_queue_lookup ON queue (city, airport, tariff, timestamp)')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_states (
            user_id INTEGER PRIMARY KEY,
            state_json TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS airport_statuses (
            icao TEXT PRIMARY KEY,
            status TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS high_demand_alerts_sent (
            icao TEXT,
            relevant_class TEXT,
            target_date TEXT,
            target_hour INTEGER,
            sent_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (icao, relevant_class, target_date, target_hour)
        )
    ''')
    conn.commit()
    conn.close()

def save_airport_status(icao, status):
    try:
        init_db()
        conn = get_db_connection()
        conn.execute(
            'INSERT INTO airport_statuses (icao, status, updated_at) VALUES (?, ?, ?) '
            'ON CONFLICT(icao) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at',
            (icao, status, datetime.now(ZoneInfo('UTC')).strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"❌ Не удалось сохранить статус аэропорта {icao}: {e}")

def load_all_airport_statuses():
    """Последний известный статус каждого аэропорта (из прошлого прогона) -
    хранится в БД, а не в памяти, чтобы рестарт/редеплой бота не считался
    "изменением статуса" и не рассылал ложные пуши всем водителям."""
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT icao, status FROM airport_statuses')
    rows = cursor.fetchall()
    conn.close()
    return {icao: status for icao, status in rows}

def was_high_demand_alert_sent(icao, relevant_class, target_date, target_hour):
    """Проверяет, уже отправляли ли пуш про повышенный спрос именно для этого
    конкретного часового слота (аэропорт+класс+дата+час). Фоновая задача
    проверяет прогноз каждые 15 минут, а окно "через 2 часа" остаётся тем же
    целый час - без этой проверки водитель получил бы 3-4 одинаковых пуша
    подряд про один и тот же будущий час."""
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT 1 FROM high_demand_alerts_sent WHERE icao=? AND relevant_class=? AND target_date=? AND target_hour=?',
        (icao, relevant_class, target_date, target_hour)
    )
    row = cursor.fetchone()
    conn.close()
    return row is not None

def mark_high_demand_alert_sent(icao, relevant_class, target_date, target_hour):
    init_db()
    conn = get_db_connection()
    conn.execute(
        'INSERT OR IGNORE INTO high_demand_alerts_sent (icao, relevant_class, target_date, target_hour, sent_at) VALUES (?, ?, ?, ?, ?)',
        (icao, relevant_class, target_date, target_hour, datetime.now(ZoneInfo('UTC')).strftime('%Y-%m-%d %H:%M:%S'))
    )
    conn.commit()
    conn.close()

def cleanup_old_high_demand_alerts():
    """Чистим отметки об отправленных пушах старше 2 дней, чтобы таблица не
    росла бесконечно - для дедупликации важны только сегодняшние/вчерашние."""
    try:
        init_db()
        conn = get_db_connection()
        cutoff = (datetime.now(ZoneInfo('UTC')) - timedelta(days=2)).strftime('%Y-%m-%d')
        conn.execute('DELETE FROM high_demand_alerts_sent WHERE target_date < ?', (cutoff,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"❌ Не удалось почистить high_demand_alerts_sent: {e}")

# Водители сами отмечают, сколько машин видят в очереди на аэропорту - выбором
# диапазона, а не точного числа (точно посчитать чужие машины в моменте
# нереально). Диапазоны по 5: 1-5, 6-10, ... 96-100.
QUEUE_RANGES = [(i, i + 4) for i in range(1, 101, 5)]

def queue_range_label(idx):
    lo, hi = QUEUE_RANGES[idx]
    return f"{lo}-{hi}"

def format_airport_local_time(utc_timestamp_str, airport_icao):
    """Время отметки в БД хранится как datetime.now() сервера (Railway работает
    в UTC), поэтому для показа пользователю его нужно перевести в местное
    время КОНКРЕТНОГО аэропорта - иначе увидим UTC вместо реального часа, тот
    же баг, что уже правили с расписанием рейсов."""
    try:
        naive = datetime.strptime(utc_timestamp_str, '%Y-%m-%d %H:%M:%S')
        aware_utc = naive.replace(tzinfo=ZoneInfo('UTC'))
        tz_name = AIRPORT_TIMEZONE.get(airport_icao, 'Europe/Moscow')
        local = aware_utc.astimezone(ZoneInfo(tz_name))
        return local.strftime('%H:%M')
    except Exception:
        return utc_timestamp_str

def queue_submit_report(user_id, city, airport_icao, category, range_str):
    """Сохраняет отметку водителя о длине очереди. Это не "встать в очередь" -
    просто разовый отчёт "я сейчас вижу вот столько машин", поэтому старые
    отметки не удаляются при новой - копится история, из которой потом берём
    последние отметки для оценки."""
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO queue (user_id, city, airport, tariff, position_range, timestamp) VALUES (?, ?, ?, ?, ?, ?)',
        (user_id, city, airport_icao, category, range_str, datetime.now(ZoneInfo('UTC')).strftime('%Y-%m-%d %H:%M:%S'))
    )
    conn.commit()
    conn.close()

def queue_latest_report(city, airport_icao, category):
    """Последняя свежая отметка водителя (за QUEUE_ENTRY_TTL_MINUTES) - без
    усреднения, просто тот диапазон, который отметил последний водитель.
    Возвращает (range_str, timestamp) или (None, None), если свежих отметок нет."""
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cutoff = (datetime.now(ZoneInfo('UTC')) - timedelta(minutes=QUEUE_ENTRY_TTL_MINUTES)).strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute(
        'SELECT position_range, timestamp FROM queue WHERE city = ? AND airport = ? AND tariff = ? AND timestamp >= ? '
        'ORDER BY timestamp DESC LIMIT 1',
        (city, airport_icao, category, cutoff)
    )
    row = cursor.fetchone()
    conn.close()
    return row if row else (None, None)

bot = None
dp = Dispatcher()
router = Router()

async def initialize_bot():
    global bot
    try:
        logger.info("📡 Инициализирую бота...")
        bot = Bot(token=BOT_TOKEN)
        me = await bot.get_me()
        logger.info(f"✅ Бот: @{me.username}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        return False

def city_keyboard():
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
        [KeyboardButton(text="🏛️ Москва"), KeyboardButton(text="🕯️ СПб")],
        [KeyboardButton(text="🌲 Новосибирск"), KeyboardButton(text="🏔️ Екатеринбург")],
        [KeyboardButton(text="🎓 Казань"), KeyboardButton(text="❄️ Челябинск")],
        [KeyboardButton(text="🌾 Омск"), KeyboardButton(text="🏭 Самара")],
        [KeyboardButton(text="🌊 Ростов"), KeyboardButton(text="⛰️ Уфа")],
        [KeyboardButton(text="🌴 Краснодар"), KeyboardButton(text="🏖️ Сочи")]
    ])

def category_keyboard():
    keyboard_buttons = [[KeyboardButton(text=f"{cat_data['name']}")] for cat_data in CATEGORIES.values()]
    keyboard_buttons.append([KeyboardButton(text="← Назад")])
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=keyboard_buttons)

# Курьеру и Грузовому такси аэропорты не нужны (это не про перевозку
# пассажиров с рейсов) - кнопка "Аэропорты" им не показывается вообще.
CATEGORIES_WITHOUT_AIRPORTS = {'courier', 'cargo'}

# Внешний бот "Где бензин" (@gde_benzin_rubot) - народная карта наличия
# топлива на АЗС по России. Это ОТДЕЛЬНЫЙ бот, а не канал - в отличие от
# @favt_info у него нет публичной веб-версии переписки, поэтому его данные
# нельзя "стянуть" скрапингом как уведомления Росавиации. Интеграция - кнопка
# со ссылкой, открывающая чат с этим ботом напрямую.
FUEL_BOT_URL = "https://t.me/gde_benzin_rubot"

def services_keyboard(category=None):
    # "Куда поехать", "Аэропорты" и "Где бензин" - навигационный блок сверху
    # (куда сейчас ехать/что с топливом), "Повышенный спрос" и "Дорожные
    # события" - блок прогнозов/уведомлений снизу.
    buttons = [[KeyboardButton(text="Куда поехать")]]
    if category not in CATEGORIES_WITHOUT_AIRPORTS:
        buttons.append([KeyboardButton(text="Аэропорты")])
    buttons.append([KeyboardButton(text="⛽ Где бензин")])
    buttons.append([KeyboardButton(text="Повышенный спрос")])
    buttons.append([KeyboardButton(text="Дорожные события")])
    buttons.append([KeyboardButton(text="← Назад")])
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=buttons)

@router.message(Command("start"))
async def start(message: types.Message):
    init_db()
    user_state.pop(message.from_user.id, None)
    text = "🚕 *Taxi Helper*\n\nВыбери город 👇"
    await message.answer(text, reply_markup=city_keyboard(), parse_mode='Markdown')

@router.message(lambda message: message.text == "← Назад")
async def go_back(message: types.Message):
    user_id = message.from_user.id
    state = user_state.get(user_id)

    if not state or 'city' not in state:
        # Некуда возвращаться дальше - показываем выбор города
        await message.answer("Выбери город 👇", reply_markup=city_keyboard())
        return

    if 'category' in state:
        # Были на экране услуг -> возвращаемся к выбору категории (город остаётся)
        state.pop('category', None)
        await message.answer("Выбери категорию 👇", reply_markup=category_keyboard())
    else:
        # Были на экране категорий -> возвращаемся к выбору города
        user_state.pop(user_id, None)
        await message.answer("Выбери город 👇", reply_markup=city_keyboard())

@router.message(lambda message: any(city in message.text for city in ["Москва", "СПб", "Новосибирск", "Екатеринбург", "Казань", "Челябинск", "Омск", "Самара", "Ростов", "Уфа", "Краснодар", "Сочи"]))
async def select_city(message: types.Message):
    city_map = {
        "🏛️ Москва": "moscow", "🕯️ СПб": "spb", "🌲 Новосибирск": "novosibirsk",
        "🏔️ Екатеринбург": "ekb", "🎓 Казань": "kazan", "❄️ Челябинск": "chelyabinsk",
        "🌾 Омск": "omsk", "🏭 Самара": "samara", "🌊 Ростов": "rostov",
        "⛰️ Уфа": "ufa", "🌴 Краснодар": "krasnodar", "🏖️ Сочи": "sochi"
    }
    user_state[message.from_user.id] = {'city': city_map.get(message.text, "moscow")}
    text = f"Вы выбрали {message.text}\n\nВыбери категорию 👇"
    await message.answer(text, reply_markup=category_keyboard())

@router.message(lambda message: any(cat_data['name'] in message.text for cat_data in CATEGORIES.values()))
async def select_category(message: types.Message):
    user_id = message.from_user.id
    if user_id not in user_state:
        await message.answer("Сначала выбери город!")
        return
    # Сортируем по длине названия по убыванию - иначе "ТАКСИ" (подстрока
    # "ТАКСИ ULTIMA") матчится раньше и категория Ultima никогда не выбирается
    selected_category = None
    for cat_key, cat_data in sorted(CATEGORIES.items(), key=lambda kv: -len(kv[1]['name'])):
        if cat_data['name'] in message.text:
            user_state[user_id]['category'] = cat_key
            selected_category = cat_key
            break
    text = "Выбери услугу 👇"
    await message.answer(text, reply_markup=services_keyboard(selected_category))

@router.message(lambda message: message.text == "⛽ Где бензин")
async def show_fuel_bot(message: types.Message):
    """Ссылка на отдельного стороннего бота @gde_benzin_rubot (народная карта
    наличия топлива на АЗС по России) - не встроенные в Taxi Helper данные,
    а прямой переход в его собственный чат."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⛽ Открыть «Где бензин»", url=FUEL_BOT_URL)]
    ])
    text = (
        "⛽ *Где бензин*\n\n"
        "Народная карта наличия топлива на АЗС по России - отдельный бот. "
        "Нажми кнопку ниже, чтобы открыть его."
    )
    await message.answer(text, reply_markup=keyboard, parse_mode='Markdown')

@router.message(lambda message: message.text == "Аэропорты")
async def show_airport_menu(message: types.Message):
    user_id = message.from_user.id
    if user_id not in user_state:
        await message.answer("Сначала выбери город!")
        return
    if user_state[user_id].get('category') in CATEGORIES_WITHOUT_AIRPORTS:
        await message.answer("Для этой категории аэропорты недоступны.", reply_markup=services_keyboard(user_state[user_id].get('category')))
        return
    text = "Выбери действие 👇"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Прилеты", callback_data="airport_arrivals")],
        [InlineKeyboardButton(text="📤 Вылеты", callback_data="airport_departures")],
        [InlineKeyboardButton(text="🔄 Доступность", callback_data="airport_availability")],
        [InlineKeyboardButton(text="📋 Очередь", callback_data="airport_queue")]
    ])
    await message.answer(text, reply_markup=keyboard)

@router.callback_query(lambda c: c.data in ["airport_arrivals", "airport_departures"])
async def show_airport_info(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    if user_id not in user_state:
        await callback_query.answer("Ошибка!", show_alert=True)
        return
    city = user_state[user_id]['city']
    airports = AIRPORTS_INFO.get(city, [])
    if not airports:
        await callback_query.answer("Не найдены", show_alert=True)
        return
    flight_type = 'arrivals' if callback_query.data == 'airport_arrivals' else 'departures'
    flight_label = '📥 Прилеты' if flight_type == 'arrivals' else '📤 Вылеты'
    category = user_state.get(user_id, {}).get('category', 'taxi')
    relevant_class = CATEGORY_TO_CLASS.get(category, 'total')
    msg = await callback_query.message.edit_text(f"⏳ Загружаю {flight_label.lower()}...")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    for i, airport in enumerate(airports):
        if airport.get('closed'):
            button_text = f"{airport['emoji']} {airport['name']} 🔴 ЗАКРЫТ"
        else:
            current_load, _, _ = compute_current_hour_load(airport['icao'], flight_type, relevant_class)
            emoji = get_load_emoji(current_load)
            button_text = f"{airport['emoji']} {airport['name']} {emoji} {current_load:.0f}%"
        keyboard.inline_keyboard.append([InlineKeyboardButton(text=button_text, callback_data=f"airport_details_{city}_{i}_{flight_type}")])
    await msg.edit_text(f"✅ Аэропорты ({flight_label.lower()}):", reply_markup=keyboard)
    await callback_query.answer()

@router.callback_query(lambda c: c.data.startswith('airport_details_'))
async def show_airport_details(callback_query: types.CallbackQuery):
    data_parts = callback_query.data.split('_')
    city = data_parts[2]
    airport_idx = int(data_parts[3])
    flight_type = data_parts[4]
    airport = AIRPORTS_INFO[city][airport_idx]

    if airport.get('closed'):
        text = (
            f"*{airport['emoji']} {airport['name']}*\n\n"
            f"🔴 *АЭРОПОРТ ЗАКРЫТ*\n\n"
            f"Гражданские полёты не выполняются. Расписание не собирается и не показывается."
        )
        await callback_query.message.edit_text(text, parse_mode='Markdown')
        await callback_query.answer()
        return

    capacity = AIRPORT_CAPACITY.get(airport['icao'], 1000)
    economy_capacity = capacity * ECONOMY_SHARE
    business_capacity = capacity * BUSINESS_SHARE

    user_id = callback_query.from_user.id
    category = user_state.get(user_id, {}).get('category', 'taxi')
    relevant_class = CATEGORY_TO_CLASS.get(category, 'total')

    is_departure = flight_type == 'departures'
    msg = await callback_query.message.edit_text(f"⏳ Загружаю расписание {airport['name']}...")
    flights = get_airport_flights(airport['icao'], flight_type)
    flight_label = '📥 Прилеты' if flight_type == 'arrivals' else '📤 Вылеты'
    class_label = {'economy': 'Эконом-класс', 'business': 'Бизнес-класс', 'total': 'Все классы'}[relevant_class]
    now = get_airport_now(airport['icao'])  # местное время АЭРОПОРТА, не сервера
    text = f"*{airport['emoji']} {airport['name']} - {flight_label}*\n"
    text += f"_Обновлено: {now.strftime('%H:%M:%S')} (местное время аэропорта)_\n"
    text += f"_Пропускная способность: {capacity} пас/час (эконом {economy_capacity:.0f} / бизнес {business_capacity:.0f})_\n"
    text += f"_Рекомендации рассчитаны для: {class_label}_\n"
    if is_departure:
        text += f"_Заказы считаются за ~{DEPARTURE_LEAD_TIME_HOURS}ч до вылета - именно тогда пассажир вызывает такси из города_\n\n"
        text += "*🏙️ ПРОГНОЗ СПРОСА НА ЗАКАЗЫ ПО ГОРОДУ (текущее время +8 часов):*\n\n"
    else:
        text += "\n*📊 ПРОГНОЗ ЗАГРУЖЕННОСТИ АЭРОПОРТА (текущее время +8 часов):*\n\n"
    current_hour = now.hour
    for hour_offset in range(8):
        # Для вылетов "час на табло" - это час, КОГДА ВОДИТЕЛЮ ИСКАТЬ ЗАКАЗ В ГОРОДЕ,
        # а сами рейсы, которые порождают этот спрос, вылетают позже, на DEPARTURE_LEAD_TIME_HOURS.
        # Для прилётов - как раньше, час фактического прилёта = час, когда ехать в аэропорт.
        order_hour_of_day = (current_hour + hour_offset) % 24
        relevant_flight_hour = (order_hour_of_day + DEPARTURE_LEAD_TIME_HOURS) % 24 if is_departure else order_hour_of_day

        hour_time = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=hour_offset)
        hour_str = hour_time.strftime('%H:00')
        hour_display = f"{hour_str} (+1д)" if order_hour_of_day < current_hour else hour_str
        flights_in_hour = 0
        economy_in_hour = 0
        business_in_hour = 0
        domestic_in_hour = 0
        international_in_hour = 0
        for flight in flights:
            flight_time = datetime.fromtimestamp(flight.get('firstSeen', 0))
            if flight_time.hour == relevant_flight_hour:
                flights_in_hour += 1
                economy_in_hour += flight.get('passengers_economy', 0)
                business_in_hour += flight.get('passengers_business', 0)
                if flight.get('domestic', True):
                    domestic_in_hour += 1
                else:
                    international_in_hour += 1
        total_in_hour = economy_in_hour + business_in_hour

        relevant_pax = {'economy': economy_in_hour, 'business': business_in_hour, 'total': total_in_hour}[relevant_class]
        relevant_cap = {'economy': economy_capacity, 'business': business_capacity, 'total': capacity}[relevant_class]
        load = (relevant_pax / relevant_cap) * 100 if relevant_pax > 0 else 0
        emoji = get_load_emoji(load)

        if is_departure:
            action = get_departure_recommendation(load)
            flight_hour_str = f"{relevant_flight_hour:02d}:00"
            text += f"{emoji} *{hour_display}* | Спрос на заказы ({class_label.lower()}): *{load:.0f}%*\n"
            text += f"   Рекомендация: *{action}*\n"
            text += f"   🛫 Вылетов в ~{flight_hour_str}: {flights_in_hour} (🇷🇺 внутр. {domestic_in_hour} / 🌍 межд. {international_in_hour})  |  ✈️ Пассажиров с заказом: {total_in_hour} (эконом {economy_in_hour} / бизнес {business_in_hour})\n\n"
        else:
            action = get_load_recommendation(load)
            text += f"{emoji} *{hour_display}* | Нагрузка ({class_label.lower()}): *{load:.0f}%*\n"
            text += f"   Рекомендация: *{action}*\n"
            text += f"   🛬 Рейсов: {flights_in_hour} (🇷🇺 внутр. {domestic_in_hour} / 🌍 межд. {international_in_hour})  |  ✈️ Пассажиры: {total_in_hour} (эконом {economy_in_hour} / бизнес {business_in_hour})\n\n"

    if is_departure:
        text += "_🔴0-50% заказов мало | 🟡51-70% будь на связи | 🟢71-100% активно бери заказы | 🟣>100% пиковый спрос_"
    else:
        text += "_🔴0-50% НЕ ЕХАТЬ | 🟡51-70% ОЧЕРЕДЬ | 🟢71-100% ЕХАТЬ | 🟣>100% СРОЧНО_"
    await msg.edit_text(text, parse_mode='Markdown')
    await callback_query.answer()

def compute_current_hour_load(airport_icao, flight_type, relevant_class, hour_offset=0):
    """Загрузка на ТЕКУЩИЙ час для ОДНОГО направления (только прилёты или только
    вылеты) - используется в списке аэропортов (show_airport_info). Для вылетов
    берётся час +DEPARTURE_LEAD_TIME_HOURS: спрос на заказы в городе сейчас
    соответствует рейсам, которые улетят примерно через 2 часа, а не рейсам,
    улетающим прямо в этот час (пассажир уже давно уехал бы в аэропорт).
    hour_offset сдвигает "текущий" час вперёд - используется для заблаговременных
    пуш-уведомлений о повышенном спросе (см. high_demand_alert_checker)."""
    now = get_airport_now(airport_icao)
    current_hour = (now.hour + hour_offset) % 24
    target_hour = (current_hour + DEPARTURE_LEAD_TIME_HOURS) % 24 if flight_type == 'departures' else current_hour
    flights = get_airport_flights(airport_icao, flight_type)
    capacity = AIRPORT_CAPACITY.get(airport_icao, 1000)
    relevant_cap = {'economy': capacity * ECONOMY_SHARE, 'business': capacity * BUSINESS_SHARE, 'total': capacity}[relevant_class]
    key = {'economy': 'passengers_economy', 'business': 'passengers_business', 'total': 'passengers'}[relevant_class]
    flights_now = [f for f in flights if datetime.fromtimestamp(f.get('firstSeen', 0)).hour == target_hour]
    total_passengers = sum(f.get(key, 0) for f in flights_now)
    load = (total_passengers / relevant_cap) * 100 if total_passengers > 0 else 0
    return load, len(flights_now), target_hour

def compute_current_availability(airport_icao, relevant_class):
    """Загруженность аэропорта ПРЯМО СЕЙЧАС для конкретного класса (эконом/бизнес/все):
    прилёты в текущий час (эти пассажиры выходят из терминала прямо сейчас) +
    вылеты через DEPARTURE_LEAD_TIME_HOURS (эти пассажиры заказывают такси в городе
    прямо сейчас). Сравнивается с ЧАСОВОЙ пропускной способностью - раньше тут по
    ошибке складывались пассажиры ВСЕХ рейсов за весь день, что давало 1000-2000%+."""
    now = get_airport_now(airport_icao)
    current_hour = now.hour
    arrivals = get_airport_flights(airport_icao, 'arrivals')
    departures = get_airport_flights(airport_icao, 'departures')
    capacity = AIRPORT_CAPACITY.get(airport_icao, 1000)
    relevant_cap = {'economy': capacity * ECONOMY_SHARE, 'business': capacity * BUSINESS_SHARE, 'total': capacity}[relevant_class]
    key = {'economy': 'passengers_economy', 'business': 'passengers_business', 'total': 'passengers'}[relevant_class]

    departure_order_hour = (current_hour + DEPARTURE_LEAD_TIME_HOURS) % 24
    arrivals_now = [f for f in arrivals if datetime.fromtimestamp(f.get('firstSeen', 0)).hour == current_hour]
    departures_soon = [f for f in departures if datetime.fromtimestamp(f.get('firstSeen', 0)).hour == departure_order_hour]

    total_passengers = sum(f.get(key, 0) for f in arrivals_now) + sum(f.get(key, 0) for f in departures_soon)
    load = (total_passengers / relevant_cap) * 100 if total_passengers > 0 else 0
    return {
        'load': load,
        'capacity': capacity,
        'relevant_cap': relevant_cap,
        'arrivals_now': arrivals_now,
        'departures_soon': departures_soon,
        'total_passengers': total_passengers,
        'now': now,
        'current_hour': current_hour,
        'departure_order_hour': departure_order_hour,
    }

@router.callback_query(lambda c: c.data == "airport_availability")
async def show_airport_availability(callback_query: types.CallbackQuery):
    """Список аэропортов города с быстрым индикатором загрузки - выбери конкретный
    для подробностей (см. show_availability_details)."""
    user_id = callback_query.from_user.id
    if user_id not in user_state:
        await callback_query.answer("Ошибка!", show_alert=True)
        return
    city = user_state[user_id]['city']
    airports = AIRPORTS_INFO.get(city, [])
    if not airports:
        await callback_query.answer("Не найдены", show_alert=True)
        return
    category = user_state.get(user_id, {}).get('category', 'taxi')
    relevant_class = CATEGORY_TO_CLASS.get(category, 'total')
    msg = await callback_query.message.edit_text("⏳ Загружаю доступность...")

    try:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[])
        for i, airport in enumerate(airports):
            if airport.get('closed'):
                button_text = f"🔴 {airport['emoji']} {airport['name']} ЗАКРЫТ"
            else:
                info = compute_current_availability(airport['icao'], relevant_class)
                emoji = get_load_emoji(info['load'])
                airport_status, _ = get_airport_status(airport['icao'])
                status_icon, _ = AIRPORT_STATUS_DISPLAY[airport_status]
                # Если аэропорт закрыт/по согласованию - это важнее, чем % загрузки, ставим первым
                button_text = f"{status_icon} {airport['emoji']} {airport['name']} {emoji} {info['load']:.0f}%"
            keyboard.inline_keyboard.append([InlineKeyboardButton(text=button_text, callback_data=f"availability_details_{city}_{i}")])
        await msg.edit_text("🔄 *Доступность аэропортов*\nВыбери аэропорт для подробностей 👇", reply_markup=keyboard, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"❌ Ошибка в show_airport_availability: {e}")
        try:
            await msg.edit_text("⚠️ Не удалось загрузить доступность аэропортов. Попробуй ещё раз через минуту.")
        except Exception:
            pass

    await callback_query.answer()

@router.callback_query(lambda c: c.data.startswith('availability_details_'))
async def show_availability_details(callback_query: types.CallbackQuery):
    """Подробная доступность ОДНОГО выбранного аэропорта: загрузка сейчас,
    прилёты/вылеты текущего часа и все уведомления Росавиации за 12ч."""
    data_parts = callback_query.data.split('_')
    city = data_parts[2]
    airport_idx = int(data_parts[3])
    airport = AIRPORTS_INFO[city][airport_idx]

    if airport.get('closed'):
        text = (
            f"*{airport['emoji']} {airport['name']} - Доступность*\n\n"
            f"🔴 *АЭРОПОРТ ЗАКРЫТ*\n\n"
            f"Гражданские полёты не выполняются. Данные не собираются."
        )
        await callback_query.message.edit_text(text, parse_mode='Markdown')
        await callback_query.answer()
        return

    user_id = callback_query.from_user.id
    category = user_state.get(user_id, {}).get('category', 'taxi')
    relevant_class = CATEGORY_TO_CLASS.get(category, 'total')
    class_label = {'economy': 'Эконом-класс', 'business': 'Бизнес-класс', 'total': 'Все классы'}[relevant_class]

    msg = await callback_query.message.edit_text(f"⏳ Загружаю {airport['name']}...")

    try:
        info = compute_current_availability(airport['icao'], relevant_class)
        load = info['load']
        if load < 50:
            status, load_emoji = "✅ Свободен", "🟢"
        elif load < 70:
            status, load_emoji = "⚠️ Средняя нагрузка", "🟡"
        elif load < 100:
            status, load_emoji = "🟠 Высокая нагрузка", "🟠"
        else:
            status, load_emoji = "🔴 Перегруженный", "🔴"

        airport_status, status_notice = get_airport_status(airport['icao'])
        status_icon, status_text = AIRPORT_STATUS_DISPLAY[airport_status]

        text = f"*{airport['emoji']} {airport['name']} - Доступность*\n"
        text += f"_Обновлено: {info['now'].strftime('%H:%M:%S')} (местное время) | Класс: {class_label}_\n\n"
        text += f"{status_icon} *АЭРОПОРТ {status_text}*\n"
        if status_notice:
            try:
                snt = datetime.fromisoformat(status_notice['time']).astimezone().strftime('%H:%M')
            except Exception:
                snt = '??:??'
            text += f"_по данным Росавиации на {snt}_\n"
        text += "\n"
        text += f"{load_emoji} *{status}*\n"
        text += f"📊 Загруженность сейчас: *{load:.0f}%* (от {info['relevant_cap']:.0f} пас/час)\n\n"
        text += f"🛬 Прилетает в {info['current_hour']:02d}:00-{(info['current_hour']+1)%24:02d}:00: {len(info['arrivals_now'])} рейсов\n"
        text += f"🛫 Вылетает в {info['departure_order_hour']:02d}:00-{(info['departure_order_hour']+1)%24:02d}:00 (заказы в городе сейчас): {len(info['departures_soon'])} рейсов\n\n"

        notices = get_notices_for_airport(airport['icao'])
        if notices:
            text += "*📢 Уведомления Росавиации (последние 12ч):*\n"
            for n in notices[:5]:
                try:
                    ntime = datetime.fromisoformat(n['time']).astimezone().strftime('%H:%M')
                except Exception:
                    ntime = '??:??'
                restricted = 'ВВЕДЕНЫ' in n['text'].upper() and 'СНЯТ' not in n['text'].upper()
                notice_emoji = '🚫' if restricted else 'ℹ️'
                text += f"{notice_emoji} *{ntime}:* {escape_md(n['text'][:200])}\n\n"
            if len(notices) > 5:
                text += f"_(+{len(notices) - 5} ещё за 12ч)_\n"
        else:
            text += "_Уведомлений Росавиации по этому аэропорту за последние 12ч нет_\n"

        try:
            await msg.edit_text(text, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"❌ Не удалось отправить доступность с Markdown-разметкой: {e}")
            plain_text = text.replace('*', '').replace('_', '')
            await msg.edit_text(plain_text)
    except Exception as e:
        logger.error(f"❌ Непредвиденная ошибка в show_availability_details: {e}")
        try:
            await msg.edit_text("⚠️ Не удалось загрузить доступность. Попробуй ещё раз через минуту.")
        except Exception:
            pass

    await callback_query.answer()

def queue_class_key(user_id):
    """Ключ для группировки очереди в БД: категория + конкретный тариф
    (если он выбран), например "taxi:Комфорт+" или "ultima:Business". Так
    очередь у Эконома и у Минивэна на одном и том же аэропорту не смешивается."""
    state = user_state.get(user_id, {})
    category = state.get('category', '')
    tariff = state.get('queue_tariff')
    return f"{category}:{tariff}" if tariff else category

def queue_class_display(user_id):
    state = user_state.get(user_id, {})
    category = state.get('category', '')
    tariff = state.get('queue_tariff')
    cat_name = CATEGORIES.get(category, {}).get('name', category)
    return f"{cat_name} ({tariff})" if tariff else cat_name

async def show_queue_airport_picker(message, user_id):
    city = user_state[user_id]['city']
    airports = AIRPORTS_INFO.get(city, [])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{airport['emoji']} {airport['name']}", callback_data=f"queue_airport_{city}_{i}")] for i, airport in enumerate(airports)])
    await message.edit_text("Выбери аэропорт 👇", reply_markup=keyboard)

@router.callback_query(lambda c: c.data == "airport_queue")
async def show_queue_menu(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    if user_id not in user_state or 'category' not in user_state[user_id]:
        await callback_query.answer("Ошибка!", show_alert=True)
        return
    city = user_state[user_id]['city']
    airports = AIRPORTS_INFO.get(city, [])
    if not airports:
        await callback_query.answer("Не найдены", show_alert=True)
        return

    category = user_state[user_id]['category']
    tariffs = CATEGORIES.get(category, {}).get('tariffs', [])
    if tariffs:
        # Сначала спрашиваем конкретный класс - у ТАКСИ и ТАКСИ ULTIMA свои
        # варианты (эконом/комфорт/... у такси, business/premier/... у ultima),
        # поэтому кнопки берутся из тарифов уже выбранной категории.
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=f"queue_tariff_{i}")] for i, t in enumerate(tariffs)
        ])
        await callback_query.message.edit_text("Выбери класс 👇", reply_markup=keyboard)
    else:
        user_state[user_id]['queue_tariff'] = None
        await show_queue_airport_picker(callback_query.message, user_id)
    await callback_query.answer()

@router.callback_query(lambda c: c.data.startswith('queue_tariff_'))
async def select_queue_tariff(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    if user_id not in user_state or 'category' not in user_state[user_id]:
        await callback_query.answer("Начни заново с /start", show_alert=True)
        return
    category = user_state[user_id]['category']
    tariffs = CATEGORIES.get(category, {}).get('tariffs', [])
    idx = int(callback_query.data.split('_')[-1])
    if idx < 0 or idx >= len(tariffs):
        await callback_query.answer("Ошибка!", show_alert=True)
        return
    user_state[user_id]['queue_tariff'] = tariffs[idx]
    await show_queue_airport_picker(callback_query.message, user_id)
    await callback_query.answer()

@router.callback_query(lambda c: c.data.startswith('queue_airport_'))
async def show_queue_options(callback_query: types.CallbackQuery):
    data_parts = callback_query.data.split('_')
    city = data_parts[2]
    airport_idx = int(data_parts[3])
    user_id = callback_query.from_user.id
    user_state[user_id]['queue_airport'] = (city, airport_idx)
    airport = AIRPORTS_INFO[city][airport_idx]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Текущая очередь", callback_data=f"view_queue_{city}_{airport_idx}")],
        [InlineKeyboardButton(text="🚗 Занять очередь", callback_data=f"join_queue_{city}_{airport_idx}")]
    ])
    text = f"*{airport['emoji']} {airport['name']}*\nКласс: {queue_class_display(user_id)}"
    await callback_query.message.edit_text(text, reply_markup=keyboard, parse_mode='Markdown')
    await callback_query.answer()

@router.callback_query(lambda c: c.data.startswith('join_queue_'))
async def show_range_picker(callback_query: types.CallbackQuery):
    """Кнопка "Занять очередь" на самом деле не ставит водителя в реальную
    очередь, а открывает выбор диапазона - сколько машин водитель сейчас видит
    в очереди на аэропорту (1-5, 6-10, ... 96-100). Точное число никто не
    посчитает на глаз, а диапазон - реально."""
    user_id = callback_query.from_user.id
    if user_id not in user_state or 'category' not in user_state[user_id]:
        await callback_query.answer("Начни заново с /start", show_alert=True)
        return
    _, _, city, airport_idx_str = callback_query.data.split('_')
    airport_idx = int(airport_idx_str)
    airport = AIRPORTS_INFO[city][airport_idx]

    buttons = []
    row = []
    for i in range(len(QUEUE_RANGES)):
        row.append(InlineKeyboardButton(text=queue_range_label(i), callback_data=f"qsub_{city}_{airport_idx}_{i}"))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="airport_queue")])

    text = f"*{airport['emoji']} {airport['name']}*\nКласс: {queue_class_display(user_id)}\n\nСколько машин сейчас в очереди? Выбери диапазон 👇"
    await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode='Markdown')
    await callback_query.answer()

@router.callback_query(lambda c: c.data.startswith('qsub_'))
async def submit_range(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    if user_id not in user_state or 'category' not in user_state[user_id]:
        await callback_query.answer("Начни заново с /start", show_alert=True)
        return
    _, city, airport_idx_str, range_idx_str = callback_query.data.split('_')
    airport_idx = int(airport_idx_str)
    range_idx = int(range_idx_str)
    airport = AIRPORTS_INFO[city][airport_idx]
    range_str = queue_range_label(range_idx)

    queue_submit_report(user_id, city, airport['icao'], queue_class_key(user_id), range_str)
    local_time = get_airport_now(airport['icao']).strftime('%H:%M')

    text = (
        f"✅ Спасибо! Отметка *{range_str}* сохранена в *{local_time}*\n\n"
        f"{airport['emoji']} {airport['name']}\n"
        f"Класс: {queue_class_display(user_id)}"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚗 Отметить ещё раз", callback_data=f"join_queue_{city}_{airport_idx}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="airport_queue")]
    ])
    await callback_query.message.edit_text(text, reply_markup=keyboard, parse_mode='Markdown')
    await callback_query.answer("Отметка сохранена")

@router.callback_query(lambda c: c.data.startswith('view_queue_'))
async def view_queue(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    if user_id not in user_state or 'category' not in user_state[user_id]:
        await callback_query.answer("Начни заново с /start", show_alert=True)
        return
    _, _, city, airport_idx_str = callback_query.data.split('_')
    airport_idx = int(airport_idx_str)
    airport = AIRPORTS_INFO[city][airport_idx]

    range_str, ts = queue_latest_report(city, airport['icao'], queue_class_key(user_id))

    text = (
        f"📋 *Очередь*\n\n"
        f"{airport['emoji']} {airport['name']}\n"
        f"Класс: {queue_class_display(user_id)}\n\n"
    )
    if range_str is not None:
        local_time = format_airport_local_time(ts, airport['icao'])
        text += f"🚗 В очереди: *{range_str}* машин\n_Последняя отметка: {local_time} (местное время аэропорта)_"
    else:
        text += f"Пока нет свежих отметок от водителей за последние {QUEUE_ENTRY_TTL_MINUTES // 60}ч.\nОтметь сам, сколько видишь машин 👇"

    buttons = [
        [InlineKeyboardButton(text="🚗 Отметить очередь", callback_data=f"join_queue_{city}_{airport_idx}")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"view_queue_{city}_{airport_idx}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="airport_queue")]
    ]

    await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode='Markdown')
    await callback_query.answer()

async def flights_data_updater():
    """Фоновая задача: раз в FLIGHTS_UPDATE_INTERVAL_HOURS часов дёргает Yandex Rasp API
    и перезаписывает flights_data.json прямо на Railway. Запускается сразу при старте
    бота (не ждёт первого интервала), чтобы данные были свежими с первого деплоя.
    fetch_yandex_data.main() - синхронная (requests), поэтому уносим её в отдельный
    поток через asyncio.to_thread, чтобы не блокировать обработку сообщений бота.
    Сам fetch_yandex_data.py следит за дневной квотой (500 запросов) и просто
    пропустит запуск, если лимит почти исчерпан - падать бот не будет."""
    while True:
        try:
            logger.info("🔄 Обновляю flights_data.json из Yandex Rasp API...")
            await asyncio.to_thread(fetch_yandex_data.main)
            logger.info("✅ flights_data.json обновлён")
        except Exception as e:
            logger.error(f"❌ Ошибка фонового обновления flights_data.json: {e}")
        await asyncio.sleep(FLIGHTS_UPDATE_INTERVAL_HOURS * 3600)

def format_queue_breakdown(city, icao, category):
    """Блок с текущей очередью ДЛЯ КОНКРЕТНОЙ категории водителя - только его
    собственные тарифы: Такси видит Эконом/Комфорт/Комфорт+/Минивэн, Ultima -
    свои Business/Premier/Elite/Cruise. Категории без тарифов (Курьер/Грузовое
    такси) сюда вообще не попадают - у них нет доступа к аэропортам."""
    tariffs = CATEGORIES.get(category, {}).get('tariffs', [])
    if not tariffs:
        return ''
    lines = [f"\n\n📋 *Очередь ({CATEGORIES[category]['name']}):*"]
    for tariff in tariffs:
        range_str, ts = queue_latest_report(city, icao, f"{category}:{tariff}")
        if range_str:
            local_time = format_airport_local_time(ts, icao)
            lines.append(f"   • {tariff}: *{range_str}* машин _(отметка {local_time})_")
        else:
            lines.append(f"   • {tariff}: нет свежих отметок")
    return '\n'.join(lines)

async def push_airport_status_change(icao, airport, old_status, new_status, notice):
    """Рассылает пуш всем водителям, у кого выбран город этого аэропорта, о
    смене статуса (например ОТКРЫТ -> ЗАКРЫТ). Бот может писать первым только
    тем, кто хотя бы раз ему написал - это все, кто есть в user_state. Курьер
    и Грузовое такси этот пуш не получают - у них нет доступа к аэропортам
    вообще. Каждому водителю ДОБАВЛЯЕТСЯ персональный блок очереди - только по
    тарифам его собственной категории (Такси не видит очередь Ultima и наоборот)."""
    city = ICAO_TO_CITY.get(icao)
    if not city or not bot:
        return
    status_icon, status_text = AIRPORT_STATUS_DISPLAY[new_status]
    base_text = f"{status_icon} *{airport['emoji']} {airport['name']}*\n\nСтатус изменился: *{status_text}*"
    if notice and notice.get('text'):
        snippet = notice['text']
        if len(snippet) > 300:
            snippet = snippet[:300] + '…'
        base_text += f"\n\n_По данным Росавиации:_\n{escape_md(snippet)}"

    # Копия списка - рассылка идёт не одну секунду, а user_state тем временем
    # может меняться (кто-то жмёт кнопки), нельзя итерировать "живой" словарь
    recipients = [
        (uid, state) for uid, state in list(user_state.items())
        if isinstance(state, dict) and state.get('city') == city and state.get('category') not in CATEGORIES_WITHOUT_AIRPORTS
    ]
    if not recipients:
        logger.info(f"📢 Статус {icao} изменился ({old_status} -> {new_status}), но в городе {city} сейчас нет известных водителей с доступом к аэропортам")
        return

    logger.info(f"📢 Статус {icao} изменился ({old_status} -> {new_status}) - рассылаю {len(recipients)} водителям города {city}")
    sent, failed = 0, 0
    for user_id, state in recipients:
        category = state.get('category')
        text = base_text + format_queue_breakdown(city, icao, category)
        text += "\n\n_Подробности во вкладке «Доступность»._"
        try:
            await bot.send_message(user_id, text, parse_mode='Markdown')
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning(f"⚠️ Не удалось отправить пуш пользователю {user_id}: {e}")
        # Telegram допускает ~30 сообщений/сек в разные чаты - берём с запасом
        await asyncio.sleep(0.05)
    logger.info(f"📢 Пуш по {icao} разослан: {sent} успешно, {failed} ошибок")

async def notify_airport_status_changes():
    """Сравнивает текущий статус каждого аэропорта (по свежим уведомлениям
    Росавиации) с последним сохранённым в БД. Пушит только РЕАЛЬНОЕ изменение,
    а не каждый прогон - и не пушит вообще на первом прогоне после деплоя
    (когда сохранённого статуса ещё нет), иначе все водители получили бы пуш
    просто от того что бот только что узнал текущий статус."""
    previous = load_all_airport_statuses()
    for icao, airport in ICAO_TO_AIRPORT.items():
        if icao in PERMANENTLY_CLOSED_AIRPORTS:
            continue  # статус константа, реальных "изменений" тут не бывает
        try:
            new_status, notice = get_airport_status(icao)
        except Exception as e:
            logger.error(f"❌ Не удалось получить статус {icao}: {e}")
            continue

        old_status = previous.get(icao)
        if old_status != new_status:
            save_airport_status(icao, new_status)
            if old_status is not None:
                await push_airport_status_change(icao, airport, old_status, new_status, notice)

HIGH_DEMAND_LEAD_HOURS = 2
HIGH_DEMAND_THRESHOLD = 100
HIGH_DEMAND_CHECK_INTERVAL_MINUTES = 15
# Обратное к CATEGORY_TO_CLASS, но только категории с доступом к аэропортам -
# Курьер/Грузовое такси используют relevant_class='total' и в этот пуш не попадают.
RELEVANT_CLASS_TO_CATEGORY = {'economy': 'taxi', 'business': 'ultima'}

async def push_high_demand_alert(icao, airport, relevant_class, target_hour, target_date, load):
    """Рассылает заблаговременный пуш о повышенном спросе: прогноз загрузки
    прилётов через HIGH_DEMAND_LEAD_HOURS часа даёт фиолетовый уровень (>100%,
    "СРОЧНО"). Получают только водители категории, которой соответствует
    relevant_class (эконом -> Такси, бизнес -> Ultima) - как и в пуше о смене
    статуса, каждому добавляется персональный блок текущей очереди по его
    собственным тарифам."""
    city = ICAO_TO_CITY.get(icao)
    category = RELEVANT_CLASS_TO_CATEGORY.get(relevant_class)
    if not city or not bot or not category:
        return
    class_name = CATEGORIES[category]['name']
    base_text = (
        f"🟣 *{airport['emoji']} {airport['name']}*\n\n"
        f"Через {HIGH_DEMAND_LEAD_HOURS} часа (~{target_hour:02d}:00) ожидается "
        f"*повышенный спрос* на прилёты ({class_name}) - прогноз загрузки выше 100%."
    )

    # Копия списка - рассылка идёт не одну секунду, а user_state тем временем
    # может меняться (кто-то жмёт кнопки), нельзя итерировать "живой" словарь
    recipients = [
        (uid, state) for uid, state in list(user_state.items())
        if isinstance(state, dict) and state.get('city') == city and state.get('category') == category
    ]
    if not recipients:
        logger.info(f"📢 Прогноз повышенного спроса {icao} ({relevant_class}, {target_hour:02d}:00), но в городе {city} нет известных водителей категории {category}")
        return

    logger.info(f"📢 Прогноз повышенного спроса {icao} ({relevant_class}, {target_hour:02d}:00, загрузка {load:.0f}%) - рассылаю {len(recipients)} водителям категории {category}")
    sent, failed = 0, 0
    for user_id, state in recipients:
        driver_category = state.get('category')
        text = base_text + format_queue_breakdown(city, icao, driver_category)
        text += "\n\n_Подробности во вкладке «Доступность»._"
        try:
            await bot.send_message(user_id, text, parse_mode='Markdown')
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning(f"⚠️ Не удалось отправить пуш о спросе пользователю {user_id}: {e}")
        # Telegram допускает ~30 сообщений/сек в разные чаты - берём с запасом
        await asyncio.sleep(0.05)
    logger.info(f"📢 Пуш о спросе по {icao} разослан: {sent} успешно, {failed} ошибок")

async def check_high_demand_alerts():
    """Проверяет прогноз загрузки прилётов через HIGH_DEMAND_LEAD_HOURS часа для
    каждого (аэропорт, релевантный класс). При прогнозе >100% (фиолетовый
    уровень) шлёт заблаговременный пуш - но не чаще одного раза на конкретный
    (аэропорт, класс, дата, час) слот: фоновая проверка идёт каждые
    HIGH_DEMAND_CHECK_INTERVAL_MINUTES минут, а окно "через 2 часа" весь этот
    час указывает на один и тот же будущий час, так что без дедупликации
    водитель получил бы несколько одинаковых пушей подряд."""
    cleanup_old_high_demand_alerts()
    for icao, airport in ICAO_TO_AIRPORT.items():
        if icao in PERMANENTLY_CLOSED_AIRPORTS or airport.get('closed'):
            continue
        for relevant_class in ('economy', 'business'):
            try:
                load, _, target_hour = compute_current_hour_load(icao, 'arrivals', relevant_class, hour_offset=HIGH_DEMAND_LEAD_HOURS)
            except Exception as e:
                logger.error(f"❌ Не удалось посчитать прогноз спроса {icao}/{relevant_class}: {e}")
                continue
            if load <= HIGH_DEMAND_THRESHOLD:
                continue
            target_date = (get_airport_now(icao) + timedelta(hours=HIGH_DEMAND_LEAD_HOURS)).strftime('%Y-%m-%d')
            if was_high_demand_alert_sent(icao, relevant_class, target_date, target_hour):
                continue
            await push_high_demand_alert(icao, airport, relevant_class, target_hour, target_date, load)
            mark_high_demand_alert_sent(icao, relevant_class, target_date, target_hour)

async def high_demand_alert_checker():
    """Фоновая задача: раз в HIGH_DEMAND_CHECK_INTERVAL_MINUTES минут проверяет
    прогноз спроса на прилёты и заранее (за HIGH_DEMAND_LEAD_HOURS часа) шлёт
    пуш водителям, если ожидается фиолетовый уровень (>100%)."""
    while True:
        try:
            await check_high_demand_alerts()
        except Exception as e:
            logger.error(f"❌ Ошибка фоновой проверки повышенного спроса: {e}")
        await asyncio.sleep(HIGH_DEMAND_CHECK_INTERVAL_MINUTES * 60)

async def favt_notices_updater():
    """Фоновая задача: раз в FAVT_UPDATE_INTERVAL_MINUTES минут читает публичную
    веб-версию канала @favt_info (Росавиация) и обновляет favt_notices.json.
    Это не API с лимитом запросов - просто HTML-страница, поэтому можно обновлять
    часто. Уведомления об ограничениях в аэропортах критичны по времени. После
    каждого обновления проверяет, не изменился ли статус какого-то аэропорта, и
    при реальном изменении рассылает пуш водителям соответствующего города."""
    while True:
        try:
            logger.info("🔄 Обновляю favt_notices.json из канала Росавиации...")
            await asyncio.to_thread(fetch_favt_notices.main)
            logger.info("✅ favt_notices.json обновлён")
            await notify_airport_status_changes()
        except Exception as e:
            logger.error(f"❌ Ошибка фонового обновления favt_notices.json: {e}")
        await asyncio.sleep(FAVT_UPDATE_INTERVAL_MINUTES * 60)

async def main():
    global bot
    if not await initialize_bot():
        return
    load_all_user_states()
    dp.include_router(router)
    if os.getenv('YANDEX_RASP_API_KEY'):
        asyncio.create_task(flights_data_updater())
    else:
        logger.warning("⚠️ YANDEX_RASP_API_KEY не задан в переменных окружения Railway - flights_data.json не будет обновляться автоматически")
    asyncio.create_task(favt_notices_updater())
    asyncio.create_task(high_demand_alert_checker())
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
