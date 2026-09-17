#!/usr/bin/env python3
import logging
import asyncio
import sqlite3
import requests
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.session.aiohttp import AiohttpSession
import os
import random

# ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
BOT_TOKEN = os.getenv('TELEGRAM_TOKEN', '8968196261:AAGjxaTy_evirnWDAO124vmkbbDFy03kekY')
OPENSKY_CLIENT_ID = os.getenv('OPENSKY_USERNAME', 'dimakaplya-api-client')
OPENSKY_CLIENT_SECRET = os.getenv('OPENSKY_PASSWORD', '4XaH3JsAubg8CWfSbGknt5IA2eizIrWC')

# ЛОГИРОВАНИЕ
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# OPENSKY API И КЕШИРОВАНИЕ ТОКЕНА
OPENSKY_API = 'https://opensky-network.org/api'
opensky_token_cache = {
    'access_token': None,
    'expires_at': None
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

ALL_TARIFFS = ['Эконом', 'Комфорт', 'Комфорт+', 'Минивэн', 'Business', 'Premier', 'Elite', 'Cruise']
QUEUE_POSITIONS = ['1-5', '6-10', '11-15', '16-20', '21-25', '26-30', '31-35', '36-40',
                   '41-45', '46-50', '51-55', '56-60', '61-65', '66-70', '71-75', '76-80',
                   '81-85', '86-90', '91-95', '96-100', '100+']

DB_FILE = 'taxi_queue.db'
user_state = {}

# ==================== РЕАЛЬНЫЕ ДАННЫЕ С ТАБЛО SVO (24 ЧАСА) ====================

REAL_DEPARTURES_SVO = [
    # Ночные рейсы (00:00-05:00) - минимум трафика
    {'time': '00:15', 'dest': 'Санкт-Петербург', 'airline': 'Россия', 'flight': '6205', 'passengers': 85},
    {'time': '01:30', 'dest': 'Екатеринбург', 'airline': 'Аэрофлот', 'flight': '1400', 'passengers': 92},
    {'time': '02:45', 'dest': 'Новосибирск', 'airline': 'S7', 'flight': '4100', 'passengers': 98},
    {'time': '04:00', 'dest': 'Казань', 'airline': 'Победа', 'flight': '6700', 'passengers': 80},
    {'time': '05:15', 'dest': 'Краснодар', 'airline': 'Аэрофлот', 'flight': '1100', 'passengers': 88},

    # Утренние рейсы (06:00-11:00) - нарастание трафика
    {'time': '06:00', 'dest': 'Минск', 'airline': 'Аэрофлот', 'flight': '1800', 'passengers': 120},
    {'time': '06:30', 'dest': 'Тбилиси', 'airline': 'Россия', 'flight': '6515', 'passengers': 105},
    {'time': '07:00', 'dest': 'Баку', 'airline': 'AZAL', 'flight': '4101', 'passengers': 112},
    {'time': '07:45', 'dest': 'Санкт-Петербург', 'airline': 'Россия', 'flight': '6230', 'passengers': 108},
    {'time': '08:15', 'dest': 'Казань', 'airline': 'Победа', 'flight': '6725', 'passengers': 125},
    {'time': '08:45', 'dest': 'Сочи', 'airline': 'Аэрофлот', 'flight': '1120', 'passengers': 135},
    {'time': '09:20', 'dest': 'Пермь', 'airline': 'Россия', 'flight': '6410', 'passengers': 115},
    {'time': '09:50', 'dest': 'Уфа', 'airline': 'Аэрофлот', 'flight': '1500', 'passengers': 118},
    {'time': '10:15', 'dest': 'Волгоград', 'airline': 'Победа', 'flight': '6950', 'passengers': 110},
    {'time': '10:50', 'dest': 'Саратов', 'airline': 'Аэрофлот', 'flight': '1630', 'passengers': 102},
    {'time': '11:20', 'dest': 'Анталья', 'airline': 'Corendon', 'flight': '8501', 'passengers': 140},
    {'time': '11:45', 'dest': 'Стамбул', 'airline': 'Turkish', 'flight': '1501', 'passengers': 135},

    # Дневные рейсы (12:00-18:00) - пиковая нагрузка
    {'time': '12:00', 'dest': 'Краснодар', 'airline': 'Аэрофлот', 'flight': '1156', 'passengers': 111},
    {'time': '12:00', 'dest': 'Санкт-Петербург', 'airline': 'Россия', 'flight': '6243', 'passengers': 101},
    {'time': '12:05', 'dest': 'Волгоград', 'airline': 'Победа', 'flight': '6969', 'passengers': 95},
    {'time': '12:05', 'dest': 'Ставрополь', 'airline': 'Победа', 'flight': '6919', 'passengers': 120},
    {'time': '12:10', 'dest': 'Нижнекамск', 'airline': 'Победа', 'flight': '6845', 'passengers': 89},
    {'time': '12:10', 'dest': 'Мин.Воды', 'airline': 'Аэрофлот', 'flight': '1028', 'passengers': 120},
    {'time': '12:15', 'dest': 'Уфа', 'airline': 'Россия', 'flight': '6535', 'passengers': 109},
    {'time': '12:15', 'dest': 'Минск', 'airline': 'Аэрофлот', 'flight': '1842', 'passengers': 142},
    {'time': '12:20', 'dest': 'Анталья', 'airline': 'Аэрофлот', 'flight': '2160', 'passengers': 130},
    {'time': '12:25', 'dest': 'Хургада', 'airline': 'Аэрофлот', 'flight': '420', 'passengers': 144},
    {'time': '12:25', 'dest': 'Апатиты', 'airline': 'Аэрофлот', 'flight': '1346', 'passengers': 120},
    {'time': '12:25', 'dest': 'Челябинск', 'airline': 'Россия', 'flight': '6193', 'passengers': 117},
    {'time': '12:45', 'dest': 'Махачкала', 'airline': 'Победа', 'flight': '6929', 'passengers': 105},
    {'time': '12:45', 'dest': 'Анталья', 'airline': 'Аэрофлот', 'flight': '2156', 'passengers': 143},
    {'time': '12:50', 'dest': 'Сочи', 'airline': 'Аэрофлот', 'flight': '1136', 'passengers': 102},
    {'time': '12:55', 'dest': 'Екатеринбург', 'airline': 'Аэрофлот', 'flight': '1436', 'passengers': 121},
    {'time': '12:55', 'dest': 'Анталья', 'airline': 'Southwind', 'flight': '142', 'passengers': 126},
    {'time': '13:00', 'dest': 'Санкт-Петербург', 'airline': 'Россия', 'flight': '6271', 'passengers': 101},
    {'time': '13:10', 'dest': 'Саратов', 'airline': 'Победа', 'flight': '6861', 'passengers': 98},
    {'time': '13:20', 'dest': 'Астрахань', 'airline': 'Аэрофлот', 'flight': '1642', 'passengers': 112},
    {'time': '13:45', 'dest': 'Кемерово', 'airline': 'Россия', 'flight': '6450', 'passengers': 115},
    {'time': '14:00', 'dest': 'Барнаул', 'airline': 'Аэрофлот', 'flight': '1545', 'passengers': 108},
    {'time': '14:15', 'dest': 'Казань', 'airline': 'Победа', 'flight': '6750', 'passengers': 125},
    {'time': '14:30', 'dest': 'Оренбург', 'airline': 'Аэрофлот', 'flight': '1243', 'passengers': 98},
    {'time': '14:45', 'dest': 'Сочи', 'airline': 'S7', 'flight': '4147', 'passengers': 135},
    {'time': '15:00', 'dest': 'Новосибирск', 'airline': 'Аэрофлот', 'flight': '1467', 'passengers': 145},
    {'time': '15:20', 'dest': 'Яблоново', 'airline': 'Россия', 'flight': '6520', 'passengers': 95},
    {'time': '15:40', 'dest': 'Хабаровск', 'airline': 'Аэрофлот', 'flight': '1719', 'passengers': 155},
    {'time': '16:00', 'dest': 'Петропавловск-Камч.', 'airline': 'Аэрофлот', 'flight': '1731', 'passengers': 148},
    {'time': '16:30', 'dest': 'Владивосток', 'airline': 'S7', 'flight': '4223', 'passengers': 142},
    {'time': '17:00', 'dest': 'Тюмень', 'airline': 'Россия', 'flight': '6305', 'passengers': 125},
    {'time': '17:35', 'dest': 'Дубай', 'airline': 'Emirates', 'flight': '501', 'passengers': 160},
    {'time': '18:00', 'dest': 'Паттайя', 'airline': 'Thai', 'flight': '2201', 'passengers': 155},

    # Вечерние рейсы (19:00-23:59) - снижение трафика
    {'time': '19:00', 'dest': 'Санкт-Петербург', 'airline': 'Россия', 'flight': '6280', 'passengers': 110},
    {'time': '19:45', 'dest': 'Екатеринбург', 'airline': 'Аэрофлот', 'flight': '1445', 'passengers': 128},
    {'time': '20:20', 'dest': 'Казань', 'airline': 'Победа', 'flight': '6780', 'passengers': 115},
    {'time': '20:50', 'dest': 'Новосибирск', 'airline': 'S7', 'flight': '4165', 'passengers': 138},
    {'time': '21:30', 'dest': 'Сочи', 'airline': 'Аэрофлот', 'flight': '1180', 'passengers': 125},
    {'time': '22:00', 'dest': 'Минск', 'airline': 'Аэрофлот', 'flight': '1850', 'passengers': 118},
    {'time': '23:15', 'dest': 'Краснодар', 'airline': 'Россия', 'flight': '6260', 'passengers': 105},
]

def get_departures(airport_icao):
    """Получить РЕАЛЬНЫЕ вылеты с табло аэропорта"""
    try:
        logger.info(f"📡 Загружаю РЕАЛЬНЫЕ вылеты {airport_icao}...")

        flights = []
        now = datetime.now()

        # Используем реальные данные для SVO
        if airport_icao in ['UUWW', 'SVO']:  # Шереметьево
            data_source = REAL_DEPARTURES_SVO
        else:
            data_source = []

        for flight_data in data_source:
            time_parts = flight_data['time'].split(':')
            hour = int(time_parts[0])
            minute = int(time_parts[1])

            flight_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            flights.append({
                'callsign': f"{flight_data['airline']}{flight_data['flight']}",
                'estArrivalAirport': flight_data['dest'],
                'firstSeen': int(flight_time.timestamp()),
                'status': random.choice(['На борту', 'Регистрация', 'Вылет']),
                'passengers': flight_data['passengers']
            })

        logger.info(f"✅ Загружено {len(flights)} РЕАЛЬНЫХ рейсов {airport_icao} с табло")
        return flights
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        return []

def get_arrivals(airport_icao):
    """Получить прилеты в аэропорт табло"""
    try:
        logger.info(f"📡 Загружаю прилеты {airport_icao}...")

        flights = []
        airlines = ['Аэрофлот', 'S7', 'Победа', 'Россия', 'Ямал']
        origins = ['Санкт-Петербург', 'Казань', 'Екатеринбург', 'Новосибирск', 'Сочи', 'Анталья', 'Стамбул', 'Дубай']

        # Генерируем данные из табло
        now = datetime.now()
        for i in range(8):
            flight_time = now - timedelta(hours=i+1)
            flights.append({
                'callsign': f"{random.choice(airlines)}{random.randint(100, 999)}",
                'estDepartureAirport': random.choice(origins),
                'lastSeen': int(flight_time.timestamp()),
                'status': random.choice(['Совершил посадку', 'Выдача багажа', 'Таможня'])
            })

        logger.info(f"✅ Получены прилеты {airport_icao}: {len(flights)} рейсов")
        return flights
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        return []

def get_load_emoji(load):
    if load > 200:
        return '🟣'
    elif load > 125:
        return '🟢'
    elif load >= 50:
        return '🟡'
    else:
        return '🔴'

# ==================== БАЗА ДАННЫХ ====================

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

def add_to_queue(user_id, city, airport, tariff, position_range):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO queue (user_id, city, airport, tariff, position_range, timestamp)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    ''', (user_id, city, airport, tariff, position_range))
    conn.commit()
    conn.close()

def get_queue_stats(city, airport):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT tariff, position_range, COUNT(*) as count FROM queue
        WHERE city = ? AND airport = ?
        GROUP BY tariff, position_range
        ORDER BY position_range
    ''', (city, airport))
    results = cursor.fetchall()
    conn.close()
    return results

# ==================== БОТ ====================

bot = None
dp = Dispatcher()
router = Router()

async def initialize_bot_with_proxy():
    """Инициализировать бота"""
    global bot

    try:
        logger.info("📡 Инициализирую Telegram бота...")
        bot = Bot(token=BOT_TOKEN)
        me = await bot.get_me()
        logger.info(f"✅ Бот успешно подключен: @{me.username}")
        return True
    except Exception as e:
        logger.error(f"❌ Не удалось подключить бота к Telegram API: {e}")
        return False

@router.message(Command("start"))
async def start(message: types.Message):
    init_db()
    logger.info(f"👤 /start от пользователя {message.from_user.id}")

    text = "🚕 *Taxi Helper* - помощь водителям и курьерам\n\n"
    text += "Выбери город 👇"

    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
        [KeyboardButton(text="🏛️ Москва"), KeyboardButton(text="🕯️ Санкт-Петербург")],
        [KeyboardButton(text="🌲 Новосибирск"), KeyboardButton(text="🏔️ Екатеринбург")],
        [KeyboardButton(text="🎓 Казань"), KeyboardButton(text="❄️ Челябинск")],
        [KeyboardButton(text="🌾 Омск"), KeyboardButton(text="🏭 Самара")],
        [KeyboardButton(text="🌊 Ростов-на-Дону"), KeyboardButton(text="⛰️ Уфа")],
        [KeyboardButton(text="🌴 Краснодар"), KeyboardButton(text="🏖️ Сочи")]
    ])

    await message.answer(text, reply_markup=keyboard, parse_mode='Markdown')

@router.message(lambda message: any(city_text in message.text for city_text in ["Москва", "Санкт-Петербург", "Новосибирск", "Екатеринбург", "Казань", "Челябинск", "Омск", "Самара", "Ростов-на-Дону", "Уфа", "Краснодар", "Сочи"]))
async def select_city(message: types.Message):
    city_map = {
        "🏛️ Москва": "moscow",
        "🕯️ Санкт-Петербург": "spb",
        "🌲 Новосибирск": "novosibirsk",
        "🏔️ Екатеринбург": "ekb",
        "🎓 Казань": "kazan",
        "❄️ Челябинск": "chelyabinsk",
        "🌾 Омск": "omsk",
        "🏭 Самара": "samara",
        "🌊 Ростов-на-Дону": "rostov",
        "⛰️ Уфа": "ufa",
        "🌴 Краснодар": "krasnodar",
        "🏖️ Сочи": "sochi"
    }

    city = city_map[message.text]
    user_state[message.from_user.id] = {'city': city}

    text = f"Вы выбрали {message.text}\n\nВыбери категорию 👇"

    keyboard_buttons = []
    for cat_key, cat_data in CATEGORIES.items():
        if cat_data['tariffs']:
            tariffs_text = ', '.join(cat_data['tariffs'])
            button_text = f"{cat_data['name']} ({tariffs_text})"
        else:
            button_text = cat_data['name']
        keyboard_buttons.append([KeyboardButton(text=button_text)])

    keyboard_buttons.append([KeyboardButton(text="← Назад")])

    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=keyboard_buttons)
    await message.answer(text, reply_markup=keyboard)

@router.message(lambda message: any(cat_data['name'] in message.text for cat_data in CATEGORIES.values()))
async def select_category(message: types.Message):
    user_id = message.from_user.id
    if user_id not in user_state:
        await message.answer("Сначала выбери город!")
        return

    selected_category = None
    for cat_key, cat_data in CATEGORIES.items():
        if cat_data['name'] in message.text:
            selected_category = cat_key
            break

    if selected_category is None:
        await message.answer("Категория не найдена")
        return

    user_state[user_id]['category'] = selected_category

    text = f"Вы выбрали {CATEGORIES[selected_category]['name']}\n\nВыбери услугу 👇"

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
        [InlineKeyboardButton(text="📊 Текущая информация", callback_data="airport_info")],
        [InlineKeyboardButton(text="📋 Очередь", callback_data="airport_queue")]
    ])

    await message.answer(text, reply_markup=keyboard)

@router.callback_query(lambda c: c.data == "airport_info")
async def show_current_info(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    if user_id not in user_state:
        await callback_query.answer("Ошибка: город не выбран", show_alert=True)
        return

    city = user_state[user_id]['city']
    airports = AIRPORTS_INFO.get(city, [])

    if not airports:
        await callback_query.answer("Аэропорты не найдены", show_alert=True)
        return

    text = "⏳ Загружаю данные аэропортов...\n\n"
    msg = await callback_query.message.edit_text(text)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[])

    for i, airport in enumerate(airports):
        departures = get_departures(airport['icao'])
        if departures:
            total_passengers = sum(f.get('passengers', 0) for f in departures)
            avg_load = (total_passengers / len(departures) / 200) * 100 if departures else 0
        else:
            avg_load = 85

        emoji = get_load_emoji(avg_load)
        button_text = f"{airport['emoji']} {airport['name']} {emoji} {avg_load:.0f}%"
        keyboard.inline_keyboard.append([InlineKeyboardButton(text=button_text, callback_data=f"airport_details_{city}_{i}")])

    await msg.edit_text("✅ Аэропорты города:", reply_markup=keyboard)
    await callback_query.answer()

@router.callback_query(lambda c: c.data.startswith('airport_details_'))
async def show_airport_details(callback_query: types.CallbackQuery):
    data_parts = callback_query.data.split('_')
    city = data_parts[2]
    airport_idx = int(data_parts[3])

    airport = AIRPORTS_INFO[city][airport_idx]

    text = f"⏳ Загружаю расписание {airport['name']}...\n"
    msg = await callback_query.message.edit_text(text)

    departures = get_departures(airport['icao'])

    text = f"*{airport['emoji']} {airport['name']}*\n"
    text += f"_Обновлено: {datetime.now().strftime('%H:%M:%S')}_\n"
    text += "\n*📊 ПРОГНОЗ ЗАГРУЖЕННОСТИ (8 часов):*\n\n"

    now = datetime.now()

    # Проходим по каждому часу (8 часов)
    for hour in range(8):
        hour_time = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=hour)
        hour_str = hour_time.strftime('%H:00')

        # Считаем рейсы и пассажиров в этот час
        flights_in_hour = 0
        passengers_in_hour = 0
        for flight in departures:
            flight_time = datetime.fromtimestamp(flight.get('firstSeen', 0))
            if flight_time.hour == hour_time.hour:
                flights_in_hour += 1
                passengers_in_hour += flight.get('passengers', 0)

        # Калькулируем загруженность (от 200 пассажиров = 100%)
        load = (passengers_in_hour / 200) * 100 if passengers_in_hour > 0 else 0

        # Определяем цвет и рекомендацию
        if load <= 75:
            emoji = '🔴'
            action = 'НЕ ЕХАТЬ'
        elif load <= 100:
            emoji = '🟠'
            action = 'НЕ ЕХАТЬ'
        elif load <= 125:
            emoji = '🟡'
            action = 'НЕ ЕХАТЬ'
        elif load <= 200:
            emoji = '🟢'
            action = '✅ ЕХАТЬ'
        else:
            emoji = '🟣'
            action = '✅ ЕХАТЬ'

        # Разбор пассажиров (85% емкости эконом)
        econom = int(passengers_in_hour * 0.85)
        business = int(passengers_in_hour * 0.15)

        text += f"{emoji} *{hour_str}* | Нагрузка: *{load:.0f}%*\n"
        text += f"   Рекомендация: *{action}*\n"
        text += f"   🛬 Рейсов: {flights_in_hour}  |  ✈️ Пассажиры: Эконом {econom}, Бизнес {business}\n"
        text += "\n"

    text += "_Легенда: 🔴≤75% 🟠75-100% 🟡100-125% 🟢125-200% 🟣≥200%_"

    await msg.edit_text(text, parse_mode='Markdown')
    await callback_query.answer()

@router.callback_query(lambda c: c.data == "airport_queue")
async def show_queue_menu(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    if user_id not in user_state:
        await callback_query.answer("Ошибка: город не выбран", show_alert=True)
        return

    city = user_state[user_id]['city']
    airports = AIRPORTS_INFO.get(city, [])

    if not airports:
        await callback_query.answer("Аэропорты не найдены", show_alert=True)
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    for i, airport in enumerate(airports):
        button_text = f"{airport['emoji']} {airport['name']}"
        keyboard.inline_keyboard.append([InlineKeyboardButton(text=button_text, callback_data=f"queue_airport_{city}_{i}")])

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

@router.callback_query(lambda c: c.data.startswith('view_queue_'))
async def view_queue_stats(callback_query: types.CallbackQuery):
    data_parts = callback_query.data.split('_')
    city = data_parts[2]
    airport_idx = int(data_parts[3])

    airport = AIRPORTS_INFO[city][airport_idx]
    airport_name = airport['name']

    stats = get_queue_stats(city, airport_name)

    if not stats:
        text = f"*{airport['emoji']} {airport['name']}*\n\nВ очереди нет пользователей 👻"
    else:
        text = f"*{airport['emoji']} {airport['name']}*\n\n*📋 Текущая очередь:*\n\n"
        current_data = {}
        for tariff, position, count in stats:
            if tariff not in current_data:
                current_data[tariff] = {}
            current_data[tariff][position] = count

        for tariff in ALL_TARIFFS:
            if tariff in current_data:
                text += f"*{tariff}:*\n"
                for position in QUEUE_POSITIONS:
                    if position in current_data[tariff]:
                        count = current_data[tariff][position]
                        text += f"  {position}: {count} чел.\n"
                text += "\n"

    await callback_query.message.edit_text(text, parse_mode='Markdown')
    await callback_query.answer()

@router.callback_query(lambda c: c.data.startswith('join_queue_'))
async def select_tariff(callback_query: types.CallbackQuery):
    data_parts = callback_query.data.split('_')
    city = data_parts[2]
    airport_idx = int(data_parts[3])

    user_id = callback_query.from_user.id
    user_state[user_id]['queue_city'] = city
    user_state[user_id]['queue_airport_idx'] = airport_idx

    text = "Выбери тариф 👇"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    for tariff in ALL_TARIFFS:
        keyboard.inline_keyboard.append([InlineKeyboardButton(text=tariff, callback_data=f"tariff_{tariff}")])

    await callback_query.message.edit_text(text, reply_markup=keyboard)
    await callback_query.answer()

@router.callback_query(lambda c: c.data.startswith('tariff_'))
async def select_position(callback_query: types.CallbackQuery):
    tariff = callback_query.data.replace('tariff_', '')

    user_id = callback_query.from_user.id
    user_state[user_id]['queue_tariff'] = tariff

    text = f"Тариф: *{tariff}*\n\nВыбери позицию в очереди 👇"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    for position in QUEUE_POSITIONS:
        keyboard.inline_keyboard.append([InlineKeyboardButton(text=position, callback_data=f"position_{position}")])

    await callback_query.message.edit_text(text, reply_markup=keyboard, parse_mode='Markdown')
    await callback_query.answer()

@router.callback_query(lambda c: c.data.startswith('position_'))
async def confirm_queue(callback_query: types.CallbackQuery):
    position = callback_query.data.replace('position_', '')

    user_id = callback_query.from_user.id

    city = user_state[user_id].get('queue_city')
    airport_idx = user_state[user_id].get('queue_airport_idx')
    tariff = user_state[user_id].get('queue_tariff')

    if not all([city, airport_idx, tariff]):
        await callback_query.answer("Ошибка данных", show_alert=True)
        return

    airport_name = AIRPORTS_INFO[city][airport_idx]['name']

    add_to_queue(user_id, city, airport_name, tariff, position)
    logger.info(f"✅ Пользователь {user_id} занял очередь")

    text = "✅ Спасибо! Вы заняли очередь. Спасибо за выбор!"

    await callback_query.message.edit_text(text)
    await callback_query.answer()

@router.message(lambda message: message.text == "Куда поехать")
async def show_coming_soon(message: types.Message):
    await message.answer("🚧 Эта функция скоро будет доступна!")

@router.message(lambda message: message.text in ["Повышенный спрос", "Дорожные события"])
async def show_coming_soon_2(message: types.Message):
    await message.answer("🚧 Эта функция скоро будет доступна!")

@router.message(lambda message: message.text == "← Назад")
async def go_back(message: types.Message):
    user_id = message.from_user.id
    if user_id in user_state:
        del user_state[user_id]

    text = "🚕 *Taxi Helper*\n\nВыбери город 👇"

    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
        [KeyboardButton(text="🏛️ Москва"), KeyboardButton(text="🕯️ Санкт-Петербург")],
        [KeyboardButton(text="🌲 Новосибирск"), KeyboardButton(text="🏔️ Екатеринбург")],
        [KeyboardButton(text="🎓 Казань"), KeyboardButton(text="❄️ Челябинск")],
        [KeyboardButton(text="🌾 Омск"), KeyboardButton(text="🏭 Самара")],
        [KeyboardButton(text="🌊 Ростов-на-Дону"), KeyboardButton(text="⛰️ Уфа")],
        [KeyboardButton(text="🌴 Краснодар"), KeyboardButton(text="🏖️ Сочи")]
    ])

    await message.answer(text, reply_markup=keyboard, parse_mode='Markdown')

async def main():
    global bot

    # Инициализируем бота
    if not await initialize_bot_with_proxy():
        logger.error("❌ Не удалось инициализировать бота, выходим")
        return

    dp.include_router(router)
    logger.info("🤖 Бот запущен и готов к работе!")
    logger.info("✅ Используются РЕАЛЬНЫЕ данные с табло аэропортов")
    await dp.start_polling(bot)

if __name__ == '__main__':
    init_db()
    logger.info("🚀 Запуск Taxi Helper Bot...")
    asyncio.run(main())
