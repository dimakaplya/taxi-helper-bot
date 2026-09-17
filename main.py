#!/usr/bin/env python3
import logging
import asyncio
import sqlite3
import json
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
import os

import fetch_yandex_data  # логика похода в Yandex Rasp API, запускается фоново прямо на Railway

BOT_TOKEN = os.getenv('TELEGRAM_TOKEN', '8968196261:AAGjxaTy_evirnWDAO124vmkbbDFy03kekY')

# Файл с реальными данными. Раньше генерировался локальным скриптом на Маке,
# теперь фоновая задача внутри самого бота (см. flights_data_updater ниже)
# обновляет его прямо на Railway каждые FLIGHTS_UPDATE_INTERVAL_HOURS часов.
FLIGHTS_DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'flights_data.json')
FLIGHTS_DATA_MAX_AGE_HOURS = 26  # если данные старше - считаем их устаревшими
FLIGHTS_UPDATE_INTERVAL_HOURS = 2  # см. расчёт квоты 500 запросов/сутки в fetch_yandex_data.py

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==================== ПРОПУСКНАЯ СПОСОБНОСТЬ ====================
AIRPORT_CAPACITY = {
    'UUWW': 4966, 'UUDD': 1586, 'UUWL': 1838, 'UULP': 2373,
    'UNNT': 1084, 'USSS': 947, 'UWKD': 616, 'UUCC': 251,
    'UNOO': 183, 'UWWW': 411, 'URRP': 171, 'UWUU': 559,
    'URKK': 525, 'URSS': 1427,
}

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
        {'name': 'RND (Ростов-на-Дону)', 'emoji': '✈️', 'icao': 'URRP', 'iata': 'RND'},
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
user_state = {}

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
                flights.append({
                    'time': flight_data['time'],
                    'callsign': f"{flight_data['airline']} {flight_data['flight']}",
                    'destination': flight_data.get('point', ''),
                    'aircraft': flight_data.get('aircraft', ''),
                    'firstSeen': int(flight_time.timestamp()),
                    'passengers': total_pax,
                    'passengers_economy': economy_pax,
                    'passengers_business': business_pax,
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
                flights.append({
                    'time': flight_data['time'],
                    'callsign': f"{flight_data['airline']}{flight_data['flight']}",
                    'destination': flight_data.get('dest') or flight_data.get('origin'),
                    'aircraft': '',
                    'firstSeen': int(flight_time.timestamp()),
                    'passengers': total_pax,
                    'passengers_economy': economy_pax,
                    'passengers_business': business_pax,
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

def init_db():
    conn = sqlite3.connect(DB_FILE)
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
    conn.commit()
    conn.close()

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

@router.message(Command("start"))
async def start(message: types.Message):
    init_db()
    text = "🚕 *Taxi Helper*\n\nВыбери город 👇"
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
        [KeyboardButton(text="🏛️ Москва"), KeyboardButton(text="🕯️ СПб")],
        [KeyboardButton(text="🌲 Новосибирск"), KeyboardButton(text="🏔️ Екатеринбург")],
        [KeyboardButton(text="🎓 Казань"), KeyboardButton(text="❄️ Челябинск")],
        [KeyboardButton(text="🌾 Омск"), KeyboardButton(text="🏭 Самара")],
        [KeyboardButton(text="🌊 Ростов"), KeyboardButton(text="⛰️ Уфа")],
        [KeyboardButton(text="🌴 Краснодар"), KeyboardButton(text="🏖️ Сочи")]
    ])
    await message.answer(text, reply_markup=keyboard, parse_mode='Markdown')

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
    keyboard_buttons = [[KeyboardButton(text=f"{cat_data['name']}")] for cat_data in CATEGORIES.values()]
    keyboard_buttons.append([KeyboardButton(text="← Назад")])
    await message.answer(text, reply_markup=ReplyKeyboardMarkup(resize_keyboard=True, keyboard=keyboard_buttons))

@router.message(lambda message: any(cat_data['name'] in message.text for cat_data in CATEGORIES.values()))
async def select_category(message: types.Message):
    user_id = message.from_user.id
    if user_id not in user_state:
        await message.answer("Сначала выбери город!")
        return
    # Сортируем по длине названия по убыванию - иначе "ТАКСИ" (подстрока
    # "ТАКСИ ULTIMA") матчится раньше и категория Ultima никогда не выбирается
    for cat_key, cat_data in sorted(CATEGORIES.items(), key=lambda kv: -len(kv[1]['name'])):
        if cat_data['name'] in message.text:
            user_state[user_id]['category'] = cat_key
            break
    text = "Выбери услугу 👇"
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
        [KeyboardButton(text="Куда поехать")],
        [KeyboardButton(text="Аэропорты")],
        [KeyboardButton(text="Повышенный спрос")],
        [KeyboardButton(text="Дорожные события")],
        [KeyboardButton(text="← Назад")]
    ])
    await message.answer(text, reply_markup=keyboard)

@router.message(lambda message: message.text == "Аэропорты")
async def show_airport_menu(message: types.Message):
    user_id = message.from_user.id
    if user_id not in user_state:
        await message.answer("Сначала выбери город!")
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
        flights = get_airport_flights(airport['icao'], flight_type)
        capacity = AIRPORT_CAPACITY.get(airport['icao'], 1000)
        relevant_cap = {'economy': capacity * ECONOMY_SHARE, 'business': capacity * BUSINESS_SHARE, 'total': capacity}[relevant_class]
        if flights:
            key = {'economy': 'passengers_economy', 'business': 'passengers_business', 'total': 'passengers'}[relevant_class]
            total_passengers = sum(f.get(key, 0) for f in flights)
            avg_load = (total_passengers / len(flights) / relevant_cap) * 100
        else:
            avg_load = 0
        emoji = get_load_emoji(avg_load)
        button_text = f"{airport['emoji']} {airport['name']} {emoji} {avg_load:.0f}%"
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
    capacity = AIRPORT_CAPACITY.get(airport['icao'], 1000)
    economy_capacity = capacity * ECONOMY_SHARE
    business_capacity = capacity * BUSINESS_SHARE

    user_id = callback_query.from_user.id
    category = user_state.get(user_id, {}).get('category', 'taxi')
    relevant_class = CATEGORY_TO_CLASS.get(category, 'total')

    msg = await callback_query.message.edit_text(f"⏳ Загружаю расписание {airport['name']}...")
    flights = get_airport_flights(airport['icao'], flight_type)
    flight_label = '📥 Прилеты' if flight_type == 'arrivals' else '📤 Вылеты'
    class_label = {'economy': 'Эконом-класс', 'business': 'Бизнес-класс', 'total': 'Все классы'}[relevant_class]
    text = f"*{airport['emoji']} {airport['name']} - {flight_label}*\n"
    text += f"_Обновлено: {datetime.now().strftime('%H:%M:%S')}_\n"
    text += f"_Пропускная способность: {capacity} пас/час (эконом {economy_capacity:.0f} / бизнес {business_capacity:.0f})_\n"
    text += f"_Рекомендации рассчитаны для: {class_label}_\n\n"
    text += "*📊 ПРОГНОЗ ЗАГРУЖЕННОСТИ (текущее время +8 часов):*\n\n"
    now = datetime.now()
    current_hour = now.hour
    for hour_offset in range(8):
        hour_of_day = (current_hour + hour_offset) % 24
        hour_time = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=hour_offset)
        hour_str = hour_time.strftime('%H:00')
        hour_display = f"{hour_str} (+1д)" if hour_of_day < current_hour else hour_str
        flights_in_hour = 0
        economy_in_hour = 0
        business_in_hour = 0
        for flight in flights:
            flight_time = datetime.fromtimestamp(flight.get('firstSeen', 0))
            if flight_time.hour == hour_of_day:
                flights_in_hour += 1
                economy_in_hour += flight.get('passengers_economy', 0)
                business_in_hour += flight.get('passengers_business', 0)
        total_in_hour = economy_in_hour + business_in_hour

        relevant_pax = {'economy': economy_in_hour, 'business': business_in_hour, 'total': total_in_hour}[relevant_class]
        relevant_cap = {'economy': economy_capacity, 'business': business_capacity, 'total': capacity}[relevant_class]
        load = (relevant_pax / relevant_cap) * 100 if relevant_pax > 0 else 0
        emoji = get_load_emoji(load)
        action = get_load_recommendation(load)
        text += f"{emoji} *{hour_display}* | Нагрузка ({class_label.lower()}): *{load:.0f}%*\n"
        text += f"   Рекомендация: *{action}*\n"
        text += f"   🛬 Рейсов: {flights_in_hour}  |  ✈️ Пассажиры: {total_in_hour} (эконом {economy_in_hour} / бизнес {business_in_hour})\n\n"
    text += "_🔴0-50% НЕ ЕХАТЬ | 🟡51-70% ОЧЕРЕДЬ | 🟢71-100% ЕХАТЬ | 🟣>100% СРОЧНО_"
    await msg.edit_text(text, parse_mode='Markdown')
    await callback_query.answer()

@router.callback_query(lambda c: c.data == "airport_availability")
async def show_airport_availability(callback_query: types.CallbackQuery):
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
    class_label = {'economy': 'эконом', 'business': 'бизнес', 'total': 'все классы'}[relevant_class]
    msg = await callback_query.message.edit_text("⏳ Загружаю доступность...")
    text = f"*🔄 Доступность Аэропортов*\n_Обновлено: {datetime.now().strftime('%H:%M:%S')} | Класс: {class_label}_\n\n"
    for airport in airports:
        arrivals = get_airport_flights(airport['icao'], 'arrivals')
        departures = get_airport_flights(airport['icao'], 'departures')
        capacity = AIRPORT_CAPACITY.get(airport['icao'], 1000)
        relevant_cap = {'economy': capacity * ECONOMY_SHARE, 'business': capacity * BUSINESS_SHARE, 'total': capacity}[relevant_class]
        key = {'economy': 'passengers_economy', 'business': 'passengers_business', 'total': 'passengers'}[relevant_class]
        total_passengers = sum(f.get(key, 0) for f in arrivals + departures)
        current_load = (total_passengers / relevant_cap) * 100 if total_passengers > 0 else 0
        if current_load < 50:
            status = "✅ Свободен"
            load_emoji = "🟢"
        elif current_load < 70:
            status = "⚠️ Средняя нагрузка"
            load_emoji = "🟡"
        elif current_load < 100:
            status = "🟠 Высокая нагрузка"
            load_emoji = "🟠"
        else:
            status = "🔴 Перегруженный"
            load_emoji = "🔴"
        text += f"{airport['emoji']} *{airport['name']}*\n"
        text += f"  {load_emoji} {status}\n"
        text += f"  📊 Загруженность: {current_load:.0f}%\n"
        text += f"  ✈️ Рейсов: {len(arrivals + departures)}\n\n"
    await msg.edit_text(text, parse_mode='Markdown')
    await callback_query.answer()

@router.callback_query(lambda c: c.data == "airport_queue")
async def show_queue_menu(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    if user_id not in user_state:
        await callback_query.answer("Ошибка!", show_alert=True)
        return
    city = user_state[user_id]['city']
    airports = AIRPORTS_INFO.get(city, [])
    if not airports:
        await callback_query.answer("Не найдены", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{airport['emoji']} {airport['name']}", callback_data=f"queue_airport_{city}_{i}")] for i, airport in enumerate(airports)])
    await callback_query.message.edit_text("Выбери аэропорт 👇", reply_markup=keyboard)
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
    await callback_query.message.edit_text(f"*{airport['emoji']} {airport['name']}*", reply_markup=keyboard, parse_mode='Markdown')
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

async def main():
    global bot
    if not await initialize_bot():
        return
    dp.include_router(router)
    if os.getenv('YANDEX_RASP_API_KEY'):
        asyncio.create_task(flights_data_updater())
    else:
        logger.warning("⚠️ YANDEX_RASP_API_KEY не задан в переменных окружения Railway - flights_data.json не будет обновляться автоматически")
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
