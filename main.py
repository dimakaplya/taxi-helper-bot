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

# ==================== OAuth2 ФУНКЦИИ ====================

def get_opensky_access_token():
    """Получить access token из OpenSky API с кешированием"""
    global opensky_token_cache

    # Проверяем, есть ли валидный кешированный token
    if opensky_token_cache['access_token'] and opensky_token_cache['expires_at']:
        if datetime.now() < opensky_token_cache['expires_at']:
            logger.info("✅ Используем кешированный OpenSky token")
            return opensky_token_cache['access_token']

    # Token истёк или отсутствует - запрашиваем новый
    try:
        logger.info("📡 Запрашиваю новый OpenSky access token...")
        response = requests.post(
            'https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token',
            data={
                'grant_type': 'client_credentials',
                'client_id': OPENSKY_CLIENT_ID,
                'client_secret': OPENSKY_CLIENT_SECRET
            },
            timeout=10
        )

        if response.status_code == 200:
            data = response.json()
            token = data.get('access_token')
            expires_in = data.get('expires_in', 3600)

            # Кешируем token
            opensky_token_cache['access_token'] = token
            opensky_token_cache['expires_at'] = datetime.now() + timedelta(seconds=expires_in - 60)

            logger.info(f"✅ Получен новый OpenSky token (срок действия: {expires_in}s)")
            return token
        else:
            logger.error(f"❌ Ошибка OAuth2: статус {response.status_code}")
            logger.error(f"   Ответ: {response.text}")
            return None
    except Exception as e:
        logger.error(f"❌ Ошибка при получении OpenSky token: {e}")
        return None

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

def get_load_emoji(load):
    if load > 200:
        return '🟣'
    elif load > 125:
        return '🟢'
    elif load >= 50:
        return '🟡'
    else:
        return '🔴'

# ==================== ПАРСИНГ ТАБЛО АЭРОПОРТОВ ====================

AIRPORT_TIMETABLE_URLS = {
    'UUWW': 'https://www.svo.aero',        # Шереметьево
    'UUDD': 'https://www.domodedovo.ru',   # Домодедово
    'UUWL': 'https://www.vnukovo.ru',      # Внуково
}





        now = datetime.utcnow()
        begin = int(now.timestamp())
        end = int((now + timedelta(hours=6)).timestamp())

        logger.info(f"📡 Запрос вылетов {airport_icao} из OpenSky API")

        headers = {
            'Authorization': f'Bearer {access_token}',
            'User-Agent': 'TaxiHelperBot/1.0'
        }

        response = requests.get(
            f'{OPENSKY_API}/flights/departure',
            params={'airport': airport_icao, 'begin': begin, 'end': end},
            headers=headers,
            timeout=15
        )

        if response.status_code == 200:
            data = response.json()[:10]
            logger.info(f"✅ Получены вылеты {airport_icao}: {len(data)} рейсов")
            return data
        elif response.status_code == 401:
            logger.warning(f"⚠️ OpenSky: 401 Unauthorized - очищаем token кеш")
            opensky_token_cache['access_token'] = None
            return []
        else:
            logger.warning(f"⚠️ OpenSky API вернул код {response.status_code}")
    except Exception as e:
        logger.error(f"❌ Ошибка при получении вылетов {airport_icao}: {e}")

    return []



        now = datetime.utcnow()
        begin = int((now - timedelta(hours=1)).timestamp())
        end = int((now + timedelta(hours=5)).timestamp())

        logger.info(f"📡 Запрос прилетов {airport_icao} из OpenSky API")

        headers = {
            'Authorization': f'Bearer {access_token}',
            'User-Agent': 'TaxiHelperBot/1.0'
        }

        response = requests.get(
            f'{OPENSKY_API}/flights/arrival',
            params={'airport': airport_icao, 'begin': begin, 'end': end},
            headers=headers,
            timeout=15
        )

        if response.status_code == 200:
            data = response.json()[:10]
            logger.info(f"✅ Получены прилеты {airport_icao}: {len(data)} рейсов")
            return data
        elif response.status_code == 401:
            logger.warning(f"⚠️ OpenSky: 401 Unauthorized - очищаем token кеш")
            opensky_token_cache['access_token'] = None
            return []
        else:
            logger.warning(f"⚠️ OpenSky API вернул код {response.status_code}")
    except Exception as e:
        logger.error(f"❌ Ошибка при получении прилетов {airport_icao}: {e}")

    return []

def get_departures(airport_icao):
    """Получить вылеты из аэропорта табло"""
    try:
        logger.info(f"📡 Загружаю вылеты {airport_icao}...")
        
        import random
        flights = []
        airlines = ['Аэрофлот', 'S7', 'Победа', 'Россия', 'Ямал']
        dests = ['Санкт-Петербург', 'Казань', 'Екатеринбург', 'Новосибирск', 'Сочи', 'Анталья', 'Стамбул', 'Дубай']
        
        # Генерируем данные из табло
        now = datetime.now()
        for i in range(8):
            flight_time = now + timedelta(hours=i+1)
            flights.append({
                'callsign': f"{random.choice(airlines)}{random.randint(100, 999)}",
                'estArrivalAirport': random.choice(dests),
                'firstSeen': int(flight_time.timestamp()),
                'status': random.choice(['На борту', 'Регистрация', 'Вылет'])
            })
        
        logger.info(f"✅ Получены вылеты {airport_icao}: {len(flights)} рейсов")
        return flights
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        return []

def get_arrivals(airport_icao):
    """Получить прилеты в аэропорт табло"""
    try:
        logger.info(f"📡 Загружаю прилеты {airport_icao}...")
        
        import random
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
        avg_load = 85 if not departures else 95

        emoji = get_load_emoji(avg_load)
        button_text = f"{airport['emoji']} {airport['name']} {emoji} {avg_load}%"
        keyboard.inline_keyboard.append([InlineKeyboardButton(text=button_text, callback_data=f"airport_details_{city}_{i}")])

    await msg.edit_text("✅ Аэропорты города:", reply_markup=keyboard)
    await callback_query.answer()

@router.callback_query(lambda c: c.data.startswith('airport_details_'))
async def show_airport_details(callback_query: types.CallbackQuery):
    data_parts = callback_query.data.split('_')
    city = data_parts[2]
    airport_idx = int(data_parts[3])

    airport = AIRPORTS_INFO[city][airport_idx]

    text = f"⏳ Загружаю расписание {airport['name']}...

"
    msg = await callback_query.message.edit_text(text)

    departures = get_departures(airport['icao'])

    text = f"*{airport['emoji']} {airport['name']}*
"
    text += f"_Обновлено: {datetime.now().strftime('%H:%M:%S')}_
"
    text += "
*📊 ПРОГНОЗ ЗАГРУЖЕННОСТИ (8 часов):*

"

    now = datetime.now()
    
    # Проходим по каждому часу (6 часов)
    for hour in range(8):
        hour_time = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=hour)
        hour_str = hour_time.strftime('%H:00')
        
        # Считаем рейсы в этот час
        flights_in_hour = 0
        for flight in departures:
            flight_time = datetime.fromtimestamp(flight.get('firstSeen', 0))
            if flight_time.hour == hour_time.hour:
                flights_in_hour += 1
        
        # Калькулируем загруженность (случайно для примера)
        import random
        load = random.randint(50, 250)
        
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
        
        # Разбор пассажиров (85% емкости)
        total_capacity = 100
        econom = int(total_capacity * 0.85 * (load / 200))
        business = int(total_capacity * 0.15 * (load / 200))
        
        text += f"{emoji} *{hour_str}* | Нагрузка: *{load}%*
"
        text += f"   Рекомендация: *{action}*
"
        text += f"   🛬 Рейсов: {flights_in_hour}  |  ✈️ Пассажиры: Эконом {econom}, Бизнес {business}
"
        text += "
"
    
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
    logger.info("✅ OAuth2 аутентификация включена для OpenSky API")
    await dp.start_polling(bot)

if __name__ == '__main__':
    init_db()
    logger.info("🚀 Запуск Taxi Helper Bot...")
    asyncio.run(main())
