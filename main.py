#!/usr/bin/env python3
import logging
import asyncio
import sqlite3
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
import os

import fetch_yandex_data  # логика похода в Yandex Rasp API, запускается фоново прямо на Railway
import fetch_trains_data  # поезда дальнего следования (Казанский, Ленинградский) - тот же ключ и квота
import fetch_favt_notices  # логика сбора уведомлений Росавиации (@favt_info), тоже фоново
import fetch_timepad_data  # афиша города (TimePad) для кнопки "🎭 События города" - используется
                            # только для TIMEPAD_CITY_MAP; timepad_data.json обновляется ЛОКАЛЬНО
                            # (см. fetch_timepad_data.py), Railway не может дотянуться до TimePad
                            # (Cloudflare блокирует датацентровые IP, см. комментарий в самом файле)

BOT_TOKEN = os.getenv('TELEGRAM_TOKEN', '8968196261:AAGjxaTy_evirnWDAO124vmkbbDFy03kekY')

# Файл с реальными данными. Раньше генерировался локальным скриптом на Маке,
# теперь фоновые задачи внутри самого бота (см. airports_data_updater и
# trains_data_updater ниже) обновляют его прямо на Railway - по отдельным,
# независимым друг от друга графикам (аэропорты и поезда).
# DATA_DIR - тот же путь, что и в fetch_yandex_data.py (если задан volume на
# Railway, оба файла - flights_data.json и api_usage_log.json - должны лежать
# в одном месте, иначе бот и фетчер будут работать с разными копиями).
DATA_DIR = os.getenv('DATA_DIR') or os.path.dirname(os.path.abspath(__file__))
FLIGHTS_DATA_FILE = os.path.join(DATA_DIR, 'flights_data.json')
FLIGHTS_DATA_MAX_AGE_HOURS = 26  # если данные старше - считаем их устаревшими
TRAINS_DATA_FILE = os.path.join(DATA_DIR, 'trains_data.json')
# Вокзалы теперь не только в Москве - у каждой станции (см. STATIONS в
# fetch_trains_data.py) есть свой город бота. Кнопка "🚆 Вокзалы" видна в
# городе, только если для него есть хотя бы одна станция здесь.
STATION_CITY = {
    's2000003': 'moscow',     # Казанский вокзал
    's2006004': 'moscow',     # Ленинградский вокзал
    's9602494': 'spb',        # Московский вокзал (Санкт-Петербург)
    's9613602': 'krasnodar',  # Краснодар-1
    's9613054': 'sochi',      # Адлер (часть Большого Сочи - см. решение пользователя)
    's9612089': 'nnovgorod',  # Московский вокзал (Нижний Новгород)
    's9623141': 'kazan',      # Казань-Пасс.
}
TRAIN_CITIES = set(STATION_CITY.values())

# Ориентировочная "пропускная способность" вокзала (пас/час) - используется
# как база для % загрузки, ТОЧНО ПО ТОЙ ЖЕ ЛОГИКЕ, что и AIRPORT_CAPACITY для
# аэропортов. У РЖД нет открытой ПОЧАСОВОЙ статистики по вокзалам, а
# по-станционной разбивки именно ДАЛЬНЕГО следования (без пригородных
# электричек) свежее 2014 года найти не удалось несмотря на поиск - поэтому
# базой по-прежнему служат помесячные цифры 2014 года (РЖД, через tutu.ru) -
# Казанский ~1.2 млн/мес, Ленинградский ~751 тыс/мес, Московский (СПб) ~1
# млн/мес - но ОТМАСШТАБИРОВАНЫ на +25% под текущий уровень пассажиропотока
# дальнего следования по сети РЖД. Множитель взят из открытой статистики
# роста: сеть РЖД в дальнем следовании выросла на 12,7% в 2023 г. к 2022 г.
# (Интерфакс), и ещё раньше отмечался рост на 16% в первой половине 2023 к
# 2022 (Ведомости) - к 2024-2025 рост уже почти остановился (+0,3% по данным
# Ведомостей за май-август 2025 к 2024) - то есть основной прирост пришёлся на
# 2022-2023 год. +25% - консервативная оценка суммарного роста с 2014 по
# 2023-2025 (а не точный станционный расчёт, которого просто нет в открытых
# источниках). Для остальных 4 станций (Краснодар-1, Адлер, Нижний Новгород,
# Казань-Пасс.) станционных данных нет вообще ни за один год - цифры это
# ГРУБАЯ прикидка по размеру вокзала/города относительно откалиброванных
# московских, в тех же пропорциях что и раньше (Адлер - с поправкой на резкий
# рост в курортный сезон). Как и у аэропортов, это ориентир для "выше/ниже
# обычного", а не точная цифра - поправить, если найдутся более свежие
# станционные данные.
STATION_CAPACITY = {
    's2000003': 2100,  # Казанский вокзал (1,2 млн/мес, 2014, +25%)
    's2006004': 1300,  # Ленинградский вокзал (751 тыс/мес, 2014, +25%)
    's9602494': 1750,  # Московский вокзал (СПб) (1 млн/мес, 2014, +25%)
    's9613602': 700,   # Краснодар-1 - оценка (пропорция к Казанскому)
    's9613054': 950,   # Адлер - оценка (пропорция к Казанскому, курортный сезон)
    's9612089': 850,   # Московский вокзал (Нижний Новгород) - оценка
    's9623141': 1100,  # Казань-Пасс. - оценка
}

# Аэропорты: ночью (00:00-06:00 МСК) рейсов мало - вообще НЕ обновляем в этом
# окне (0 запусков), а не просто реже, как было раньше. Днём (06:00-24:00) -
# каждые FLIGHTS_DAY_INTERVAL_HOURS часов.
FLIGHTS_NIGHT_START_HOUR = 0
FLIGHTS_NIGHT_END_HOUR = 6  # [0, 6) - ночь (обновлений нет), [6, 24) - день
FLIGHTS_DAY_INTERVAL_HOURS = 2  # днём - каждые 2 часа (06,08,...,22 = 9 запусков/сутки)

# Поезда (7 вокзалов по 6 городам - см. STATION_CITY): отдельный, не
# завязанный на день/ночь график - раз в TRAINS_UPDATE_INTERVAL_HOURS часов,
# круглосуточно (Сапсаны и дальние поезда ходят и вечером/рано утром, а объём
# запросов по 7 вокзалам всё ещё небольшой - не жалко гонять и ночью).
TRAINS_UPDATE_INTERVAL_HOURS = 12  # 2 запуска/сутки

# Прогноз загруженности вокзала показывает на TRAIN_FORECAST_HOURS часов
# вперёд (было 8, увеличено по просьбе пользователя). У аэропортов свой,
# отдельный 8-часовой прогноз (см. show_airport_details) - не путать.
TRAIN_FORECAST_HOURS = 12
# Гранулярность прогноза - блоками по TRAIN_FORECAST_PERIOD_MINUTES минут
# (было по часу целиком, уменьшено до получаса по просьбе пользователя).
# Число периодов на весь прогноз = TRAIN_FORECAST_HOURS*60/TRAIN_FORECAST_PERIOD_MINUTES.
TRAIN_FORECAST_PERIOD_MINUTES = 30

# Расчёт по квоте (500 запросов/сутки на ключ, общий для fetch_yandex_data.py
# и fetch_trains_data.py - см. их докстринги; доступ к общему счётчику
# сериализован через _yandex_api_lock ниже, чтобы независимые графики не
# читали устаревший остаток друг у друга одновременно):
# Аэропорты: 9 дневных запусков x ~45 (13 активных x 2 направления +
# пагинация для крупных, худший случай) = 405.
# Поезда: только прибытия (см. fetch_trains_data.py), 2 запуска x ~4
# (2 вокзала x 1 направление, пагинация маловероятна, но берём с запасом) = 8.
# Итого худший случай: 405+8=413 из 500 (порог безопасности - 450) - запас
# ~37 запросов. Это именно ХУДШИЙ случай (по факту обычно сильно меньше, см.
# докстринг fetch_yandex_data.py: "~28-45" - это верхняя граница, а не
# типичный расход), а DAILY_SAFETY_LIMIT в обоих скриптах в любом случае не
# даст ключ заблокировать - просто пропустит лишний запуск ближе к концу
# суток, если расход неожиданно окажется выше обычного.

# Уведомления Росавиации об ограничениях в аэропортах (@favt_info) - публичная
# веб-страница, лимита запросов нет, поэтому обновляем чаще, чем расписание рейсов.
FAVT_NOTICES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'favt_notices.json')
FAVT_UPDATE_INTERVAL_MINUTES = 15

# Афиша города (TimePad, см. fetch_timepad_data.py) - события меняются
# медленно (не по минутам, как рейсы/статусы). Обновляется ЛОКАЛЬНО (см.
# fetch_timepad_data.py - Railway не может дотянуться до TimePad, Cloudflare
# блокирует датацентровые IP), файл коммитится/пушится вручную. Пока
# покрывает только Москву.
TIMEPAD_DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'timepad_data.json')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==================== ПРОПУСКНАЯ СПОСОБНОСТЬ ====================
# UWUU (Уфа) заменён на UWGG (Стригино, Нижний Новгород) - Уфа убрана из
# бота (см. STATIONS/nnovgorod выше). Значение для UWGG - оценка по годовому
# пассажиропотоку (~1.48 млн пасс/год, Коммерсантъ, рекордный год) в той же
# пропорции к остальным аэропортам этого списка, что и у них; поправить, если
# найдётся более точный источник или другая методика калибровки исходного
# словаря.
AIRPORT_CAPACITY = {
    'UUWW': 4966, 'UUDD': 1586, 'UUWL': 1838, 'UULP': 2373,
    'UNNT': 1084, 'USSS': 947, 'UWKD': 616, 'UUCC': 251,
    'UNOO': 183, 'UWWW': 411, 'URRP': 171, 'UWGG': 400,
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
    'URKK': 'Europe/Moscow', 'URSS': 'Europe/Moscow', 'UWGG': 'Europe/Moscow',
    'UNNT': 'Asia/Novosibirsk',
    'USSS': 'Asia/Yekaterinburg', 'UUCC': 'Asia/Yekaterinburg',
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
    'nnovgorod': [
        # Заменяет Уфу (по просьбе пользователя - Уфа полностью убрана из бота,
        # Нижний Новгород занял её место со 100% той же структурой).
        {'name': 'GOJ (Нижний Новгород)', 'emoji': '✈️', 'icao': 'UWGG', 'iata': 'GOJ'},
    ],
    'krasnodar': [
        # Вновь открыт с 11.09.2025 (был закрыт с 2022) - в отличие от RND,
        # данные собираем как обычно.
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

# FALLBACK_DEPARTURES_SVO убран вместе с самой функцией вылетов (см. ниже -
# теперь собираем и показываем только прилёты, для экономии квоты Yandex Rasp).

DB_FILE = 'taxi_queue.db'
# Если водитель встал в очередь и не появлялся дольше этого времени - считаем,
# что он уже уехал (забрал пассажира) или просто забыл нажать "Покинуть
# очередь", и убираем его из очереди автоматически.
QUEUE_ENTRY_TTL_MINUTES = 120

# "Отдать заказ" - водитель транслирует другим водителям СВОЕГО ГОРОДА заказ,
# который сам не может/не хочет выполнить (по просьбе пользователя). Только
# Такси и Ultima - у формы заказа есть "класс автомобиля"/"кол-во пассажиров",
# это про пассажирские поездки, Курьеру/Грузовому такси не подходит (решение
# пользователя). Рассылка идёт ТЕМ ЖЕ водителям, что видят саму кнопку - тот
# же город, та же пара категорий - через user_state, как и остальные пуши в
# боте (см. push_airport_status_change). Первый принявший и отправитель видят
# контакт друг друга (решение пользователя) и дальше связываются напрямую в
# Telegram - бот в самой сделке не участвует, только сводит.
SHARED_ORDER_CATEGORIES = {'taxi', 'ultima'}
SHARED_ORDER_EXPIRY_HOURS = 1  # предложение считается неактуальным через час (решение пользователя)

# Человекочитаемые названия городов (ключ city - тот же, что в city_map ниже
# и в AIRPORTS_INFO) - нужны для текста рассылки заказов и подтверждений.
CITY_DISPLAY_NAMES = {
    'moscow': 'Москва', 'spb': 'Санкт-Петербург', 'novosibirsk': 'Новосибирск',
    'ekb': 'Екатеринбург', 'kazan': 'Казань', 'chelyabinsk': 'Челябинск',
    'omsk': 'Омск', 'samara': 'Самара', 'rostov': 'Ростов-на-Дону',
    'nnovgorod': 'Нижний Новгород', 'krasnodar': 'Краснодар', 'sochi': 'Сочи',
}

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

_timepad_data_cache = None
_timepad_data_mtime = None

def load_timepad_data():
    """Загружает timepad_data.json (афиша города, см. fetch_timepad_data.py)."""
    global _timepad_data_cache, _timepad_data_mtime
    try:
        mtime = os.path.getmtime(TIMEPAD_DATA_FILE)
        if _timepad_data_cache is not None and mtime == _timepad_data_mtime:
            return _timepad_data_cache
        with open(TIMEPAD_DATA_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        _timepad_data_cache = data
        _timepad_data_mtime = mtime
        return data
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка чтения timepad_data.json: {e}")
        return None

_trains_data_cache = None
_trains_data_mtime = None

def load_trains_data():
    """Загружает trains_data.json (поезда дальнего следования по вокзалам из
    STATION_CITY - Москва, СПб, Краснодар, Сочи/Адлер, Нижний Новгород,
    Казань, только прибытия - см. fetch_trains_data.py)."""
    global _trains_data_cache, _trains_data_mtime
    try:
        mtime = os.path.getmtime(TRAINS_DATA_FILE)
        if _trains_data_cache is not None and mtime == _trains_data_mtime:
            return _trains_data_cache
        with open(TRAINS_DATA_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        _trains_data_cache = data
        _trains_data_mtime = mtime
        return data
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка чтения trains_data.json: {e}")
        return None

# По просьбе пользователя: и Такси, и Ultima теперь видят ВСЕ прибывающие
# поезда дальнего следования (пригородные электрички не собираются вообще -
# см. fetch_trains_data.py, transport_types=train). Раньше Ultima видела
# только Сапсаны - теперь вместо фильтрации по категории Ultima просто
# ПОДСВЕЧИВАЕТСЯ акцент на Сапсанах и фирменных/премиальных поездах (см.
# is_sapsan/is_firmenny в fetch_trains_data.py и сортировку/пометки в
# show_train_station_arrivals ниже), а не скрывает остальные поезда.
def get_trains_for_station(station_code, category):
    data = load_trains_data()
    if not data:
        return None, None
    station = data.get('stations', {}).get(station_code)
    if not station:
        return None, None
    return station['name'], station.get('arrivals', [])

def _minutes_in_period(train_minutes, period_start_minutes, period_length, day_minutes=24 * 60):
    """train_minutes/period_start_minutes - минуты от полуночи (0-1439).
    Обрабатывает переход через полночь (период может начинаться в 23:45 и
    заканчиваться в 00:15 следующих суток) - без этого последние периоды
    прогноза перед полуночью теряли бы поезда, приходящиеся на начало
    следующих суток."""
    period_end_minutes = period_start_minutes + period_length
    if period_end_minutes <= day_minutes:
        return period_start_minutes <= train_minutes < period_end_minutes
    return train_minutes >= period_start_minutes or train_minutes < (period_end_minutes - day_minutes)

def compute_current_train_period_load(station_code, category, period_offset=0):
    """Загрузка вокзала на ЗАДАННЫЙ TRAIN_FORECAST_PERIOD_MINUTES-минутный
    период (по умолчанию текущий получас, округлённый вниз, МСК) - по той же
    логике, что compute_current_hour_load для аэропортов: сумма оценки
    пассажиров прибывающих в этот период поездов / (часовая ёмкость,
    пропорционально урезанная под длину периода) * 100. Раньше период был
    целый час (см. историю) - уменьшен до получаса по просьбе пользователя,
    поэтому емкость STATION_CAPACITY (задана как пас/ЧАС) тоже делим
    пропорционально - иначе один и тот же поезд в получасовом окне давал бы
    вдвое заниженный % по сравнению со старой часовой версией. Пассажиры
    считаются по СЕРЕДИНЕ диапазона оценки (passengers_min/max), как
    единственно разумный способ свести диапазон к одному числу."""
    _, arrivals = get_trains_for_station(station_code, category)
    arrivals = arrivals or []
    now = datetime.now(ZoneInfo('Europe/Moscow'))
    now_minutes = now.hour * 60 + (now.minute // TRAIN_FORECAST_PERIOD_MINUTES) * TRAIN_FORECAST_PERIOD_MINUTES
    period_start_minutes = (now_minutes + TRAIN_FORECAST_PERIOD_MINUTES * period_offset) % (24 * 60)

    def _train_minutes(t):
        h, m = t['time'].split(':')
        return int(h) * 60 + int(m)

    trains_in_period = [
        t for t in arrivals
        if _minutes_in_period(_train_minutes(t), period_start_minutes, TRAIN_FORECAST_PERIOD_MINUTES)
    ]
    total_passengers = sum((t['passengers_min'] + t['passengers_max']) / 2 for t in trains_in_period)
    hourly_capacity = STATION_CAPACITY.get(station_code, 1000)
    period_capacity = hourly_capacity * (TRAIN_FORECAST_PERIOD_MINUTES / 60)
    load = (total_passengers / period_capacity) * 100 if total_passengers > 0 else 0
    return load, trains_in_period, period_start_minutes

# Такси эконом/комфорт и Ultima видят одну и ту же афишу TimePad без разбора
# по классу - событие уже прошло фильтр по масштабу (tickets_total>=200) на
# этапе сбора данных, см. fetch_timepad_data.py. Курьер/Грузовое такси эту
# кнопку вообще не видят (см. CATEGORIES_WITHOUT_EVENTS ниже).
CATEGORIES_WITHOUT_EVENTS = {'courier', 'cargo'}

def get_events_for_user(city, category, limit=10):
    """Ближайшие события города под класс водителя. Источник - ТОЛЬКО TimePad
    (см. fetch_timepad_data.py) - KudaGo убран по просьбе пользователя (кнопка
    "Поехали" по координатам открывала именно приложение Яндекс.Навигатор
    вместо карт/браузера на части телефонов, и события были не нужны).
    Возвращает (events, city_supported) - city_supported=False значит
    TimePad вообще не покрывает этот город (нужно отдельное сообщение "нет
    данных", а не пустой список - это разные вещи)."""
    if city not in fetch_timepad_data.TIMEPAD_CITY_MAP:
        return [], False

    now_ts = datetime.now(ZoneInfo('UTC')).timestamp()
    data = load_timepad_data()
    city_events = (data or {}).get('cities', {}).get(city, [])
    # fetch_timepad_data.py уже отфильтровал по категориям и
    # tickets_total>=200 на стороне сбора данных - здесь только актуальность
    # по времени (адрес и масштаб уже гарантированы).
    upcoming = [e for e in city_events if e.get('start', 0) >= now_ts]
    upcoming.sort(key=lambda e: e['start'])
    return upcoming[:limit], True

# Часовой пояс городов афиши (используется только для событий - не путать с
# AIRPORT_TIMEZONE, который привязан к конкретным аэропортам). Пока только
# Москва (TimePad), остальные оставлены на будущее расширение покрытия.
EVENT_CITY_TIMEZONE = {
    'moscow': 'Europe/Moscow',
    'spb': 'Europe/Moscow',
    'ekb': 'Asia/Yekaterinburg',
    'kazan': 'Europe/Moscow',
}

def format_event_datetime(event, city):
    """Диапазон начала-конца мероприятия в часовом поясе города - водителю
    важно понимать не только когда началось, но и когда примерно закончится
    (именно момент разъезда даёт всплеск спроса у площадки). Если TimePad не
    знает точную длительность - end==start, и показываем только начало, без
    диапазона."""
    tz = ZoneInfo(EVENT_CITY_TIMEZONE.get(city, 'Europe/Moscow'))
    start_dt = datetime.fromtimestamp(event['start'], tz)
    end_ts = event.get('end') or event['start']
    if end_ts <= event['start']:
        return start_dt.strftime('%d.%m, %H:%M')
    end_dt = datetime.fromtimestamp(end_ts, tz)
    if start_dt.date() == end_dt.date():
        return f"{start_dt.strftime('%d.%m, %H:%M')}–{end_dt.strftime('%H:%M')}"
    return f"{start_dt.strftime('%d.%m %H:%M')} – {end_dt.strftime('%d.%m %H:%M')}"

def estimate_attendance(event):
    """Возвращает (мин, макс) числа посетителей. TimePad-события всегда
    несут РЕАЛЬНОЕ число билетов (tickets_total) - оно уже прошло порог
    TIMEPAD_MIN_TICKETS на этапе сбора данных (см. fetch_timepad_data.py),
    так что отдельная догадка по ключевым словам в названии площадки не
    нужна. Диапазон 0.7x-1.0x от tickets_total проще подать водителю, чем
    точную цифру, и не выглядит как гарантия явки именно этого числа людей."""
    tickets_total = event.get('tickets_total') or 0
    return int(tickets_total * 0.7), int(tickets_total)

def build_event_message(event, city):
    """Текст + инлайн-кнопки для ОДНОГО события TimePad. "🚗 Поехали": у
    TimePad нет координат площадки вообще (только текстовый адрес) - кнопка
    ведёт на ссылку-ПОИСК по адресу https://yandex.ru/maps/?text=<адрес>
    (Яндекс.Карты сами геокодируют текст), а НЕ на схему yandexnavi://
    build_route_on_map: у неё нет веб-фолбэка вообще (если у водителя не
    установлен именно Яндекс.Навигатор - кнопка молча ничего не сделает), а
    обычная https-ссылка на Яндекс.Карты открывается всегда - в приложении
    Карт/Навигатора, если оно установлено и ассоциировано с доменом, и в
    браузере в любом случае, если нет. "🔗 Подробнее" - страница события на TimePad."""
    date_str = format_event_datetime(event, city)
    lo, hi = estimate_attendance(event)
    lines = [f"🎫 *{event['title']}*"]
    address = event.get('place_address')
    if address:
        lines.append(f"📍 {address}")
    lines.append(f"👥 ~{lo}–{hi} чел.")
    lines.append(f"🗓 {date_str}")
    # Цену билета не показываем - водителям она не нужна.
    text = '\n'.join(lines)

    buttons = []
    if address:
        from urllib.parse import quote
        buttons.append(InlineKeyboardButton(text="🚗 Поехали", url=f"https://yandex.ru/maps/?text={quote(address)}"))
    if event.get('url'):
        buttons.append(InlineKeyboardButton(text="🔗 Подробнее", url=event['url']))
    keyboard = InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None
    return text, keyboard

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

def get_airport_flights(airport_icao):
    """Получить ПРИЛЁТЫ аэропорта из реальных данных (flights_data.json).
    Вылеты больше не собираются и не показываются (см. fetch_yandex_data.py -
    убраны ради экономии дневной квоты Yandex Rasp API). Если файла нет - для
    SVO отдаём запасной хардкод, для остальных пусто."""
    try:
        now = datetime.now()
        flights = []
        data = load_flights_data()

        if data and airport_icao in data.get('airports', {}):
            raw_flights = data['airports'][airport_icao].get('arrivals', [])
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
            logger.info(f"✅ Загружено {len(flights)} реальных прилётов {airport_icao}")
            return flights

        # Запасной вариант - только для SVO, пока нет свежего flights_data.json
        if airport_icao == 'UUWW':
            for flight_data in FALLBACK_ARRIVALS_SVO:
                time_parts = flight_data['time'].split(':')
                hour, minute = int(time_parts[0]), int(time_parts[1])
                flight_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                total_pax = flight_data['passengers']
                economy_pax = round(total_pax * ECONOMY_SHARE)
                business_pax = total_pax - economy_pax
                destination = flight_data.get('origin')
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

# Для вокзалов - ПО ПРОСЬБЕ ПОЛЬЗОВАТЕЛЯ упрощённая БИНАРНАЯ индикация вместо
# 4-уровневой шкалы аэропортов выше (get_load_emoji/get_load_recommendation):
# всего 2 состояния, без отдельной кнопки - просто символ+короткая подпись в
# тексте прогноза. До 50% включительно - не ехать, выше 50% - ехать.
def get_train_load_symbol(load_percent):
    return '🟢' if load_percent > 50 else '🔴'

def get_train_load_label(load_percent):
    return 'ЕХАТЬ' if load_percent > 50 else 'НЕ ЕХАТЬ'

# DEPARTURE_LEAD_TIME_HOURS и get_departure_recommendation() убраны вместе с
# вылетами (см. get_airport_flights) - вылеты больше не собираются и не
# показываются, ради экономии дневной квоты Yandex Rasp API.

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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS shared_orders (
            order_id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL,
            sender_contact TEXT NOT NULL,
            city TEXT NOT NULL,
            category TEXT NOT NULL,
            pickup TEXT NOT NULL,
            dropoff TEXT NOT NULL,
            price TEXT NOT NULL,
            car_class TEXT NOT NULL,
            passengers TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            accepted_by INTEGER,
            accepted_by_contact TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            accepted_at DATETIME
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_shared_orders_status ON shared_orders (status, created_at)')
    # Миграция: номер телефона клиента добавлен позже, чем сама таблица -
    # CREATE TABLE IF NOT EXISTS не трогает уже существующую (на Railway)
    # таблицу, колонку нужно добавлять отдельно. SQLite не умеет "ADD COLUMN
    # IF NOT EXISTS", поэтому сначала проверяем через PRAGMA.
    cursor.execute('PRAGMA table_info(shared_orders)')
    existing_columns = {row[1] for row in cursor.fetchall()}
    if 'client_phone' not in existing_columns:
        cursor.execute('ALTER TABLE shared_orders ADD COLUMN client_phone TEXT')
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

def format_user_contact(user):
    """Контакт для связи между водителями - ник через @ (как просил
    пользователь). У части аккаунтов Telegram ник не задан вообще -
    запасной вариант: имя + числовой ID (по нему тоже можно найти человека
    через пересылку сообщения, хоть и не так напрямую, как по нику)."""
    if user.username:
        return f"@{user.username}"
    name = escape_md(user.full_name or 'без имени')
    return f"{name} (ник не задан, ID: {user.id})"

def create_shared_order(sender_id, sender_contact, city, category, pickup, dropoff, price, car_class, passengers, client_phone=None):
    init_db()
    conn = get_db_connection()
    cursor = conn.execute(
        'INSERT INTO shared_orders (sender_id, sender_contact, city, category, pickup, dropoff, price, car_class, passengers, client_phone) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (sender_id, sender_contact, city, category, pickup, dropoff, price, car_class, passengers, client_phone)
    )
    order_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return order_id

_SHARED_ORDER_FIELDS = ['order_id', 'sender_id', 'sender_contact', 'city', 'category', 'pickup', 'dropoff',
                         'price', 'car_class', 'passengers', 'status', 'accepted_by', 'accepted_by_contact',
                         'created_at', 'accepted_at', 'client_phone']

def get_shared_order(order_id):
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(f'SELECT {", ".join(_SHARED_ORDER_FIELDS)} FROM shared_orders WHERE order_id = ?', (order_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return dict(zip(_SHARED_ORDER_FIELDS, row))

def is_shared_order_expired(order):
    """SHARED_ORDER_EXPIRY_HOURS (решение пользователя - 1 час) с момента
    создания. created_at хранится в UTC (SQLite CURRENT_TIMESTAMP), поэтому
    сравниваем с datetime.now(UTC), а не с местным временем сервера."""
    try:
        created = datetime.strptime(order['created_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=ZoneInfo('UTC'))
    except Exception:
        return False  # не смогли распарсить - не считаем протухшим, чтобы не терять заказ на пустом месте
    return datetime.now(ZoneInfo('UTC')) - created > timedelta(hours=SHARED_ORDER_EXPIRY_HOURS)

def try_accept_shared_order(order_id, accepted_by, accepted_by_contact):
    """Атомарно "забирает" заказ - условие status='open' прямо в WHERE
    гарантирует, что при одновременном нажатии "Принять" двумя водителями
    выиграет только тот, чей UPDATE применится первым (SQLite сериализует
    запись на уровне файла БД) - остальные получат rowcount=0 и поймут, что
    опоздали, без отдельных блокировок в коде бота."""
    init_db()
    conn = get_db_connection()
    cursor = conn.execute(
        "UPDATE shared_orders SET status='accepted', accepted_by=?, accepted_by_contact=?, accepted_at=? "
        "WHERE order_id=? AND status='open'",
        (accepted_by, accepted_by_contact, datetime.now(ZoneInfo('UTC')).strftime('%Y-%m-%d %H:%M:%S'), order_id)
    )
    conn.commit()
    won = cursor.rowcount == 1
    conn.close()
    return won

def expire_shared_order(order_id):
    init_db()
    conn = get_db_connection()
    conn.execute("UPDATE shared_orders SET status='expired' WHERE order_id=? AND status='open'", (order_id,))
    conn.commit()
    conn.close()

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
        [KeyboardButton(text="🌊 Ростов"), KeyboardButton(text="🏰 Нижний Новгород")],
        [KeyboardButton(text="🌴 Краснодар"), KeyboardButton(text="🏖️ Сочи")]
    ])

def category_keyboard():
    keyboard_buttons = [[KeyboardButton(text=f"{cat_data['name']}")] for cat_data in CATEGORIES.values()]
    keyboard_buttons.append([KeyboardButton(text="← Назад"), KeyboardButton(text="🏙 Выбор города")])
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

def services_keyboard(category=None, city=None):
    # Итоговый набор кнопок меню услуг (по заданному порядку). "Заказы
    # города" (было "Повышенный спрос") убрана по просьбе пользователя - была
    # заглушкой без своей логики. "Дорожные события" тоже пока без
    # обработчика - как было. "🎭 События города" (афиша TimePad) - только
    # у Такси/Ultima, курьеру и грузовому такси не актуальна (см.
    # CATEGORIES_WITHOUT_EVENTS). "🚆 Вокзалы" - только в городах из
    # TRAIN_CITIES (см. STATION_CITY), той же категории, что и аэропорты.
    # "🔄 Отдать заказ" - только Такси/Ultima (см. SHARED_ORDER_CATEGORIES).
    # "🧰 Инструменты водителя" - отдельный модуль (см.
    # COURIER_MODULE_CATEGORIES) с финансовым калькулятором смены +
    # заглушки под карту точек; изначально делался под курьеров, но по
    # просьбе пользователя открыт всем категориям (калькулятор дохода/км/
    # топлива/часов одинаково полезен и такси, и грузовому такси).
    buttons = []
    if category in SHARED_ORDER_CATEGORIES:
        buttons.append([KeyboardButton(text="🔄 Отдать заказ")])
    if category in COURIER_MODULE_CATEGORIES:
        buttons.append([KeyboardButton(text="🧰 Инструменты водителя")])
    if category not in CATEGORIES_WITHOUT_AIRPORTS:
        buttons.append([KeyboardButton(text="✈️ Аэропорты")])
    if city in TRAIN_CITIES and category not in CATEGORIES_WITHOUT_AIRPORTS:
        buttons.append([KeyboardButton(text="🚆 Вокзалы")])
    buttons.append([KeyboardButton(text="⛽ Где бензин")])
    if category not in CATEGORIES_WITHOUT_EVENTS:
        buttons.append([KeyboardButton(text="🎭 События города")])
    buttons.append([KeyboardButton(text="Дорожные события")])
    buttons.append([KeyboardButton(text="← Назад"), KeyboardButton(text="🏙 Выбор города")])
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=buttons)

# ==================== МОДУЛЬ "ИНСТРУМЕНТЫ ВОДИТЕЛЯ" (бывш. "Курьеру") ====================
# Изначально прототип (courier-bot-package) делался под курьеров/доставку
# (Яндекс.Еда, Купер, СДЭК, WB/Ozon-логистика), но пользователь попросил
# открыть его всем категориям - формулы (доход/км/топливо/часы -> чистая
# прибыль) одинаково применимы к такси и грузовому такси, специфики именно
# под курьера в них нет. Встраивается тем же паттерном, что и остальной бот:
# JSON/state, никакой БД (в отличие от исходной спеки прототипа, где
# предполагался Postgres+PostGIS - на этом этапе не нужен, все "точечные"
# разделы ниже пока заглушки). Рабочий сейчас - только 💰 Финансы (см.
# COURIER_FINANCE_* ниже). Остальные 4 пункта меню - "в разработке" (текст
# как в самом прототипе, экран data-view="soon").
COURIER_MODULE_CATEGORIES = set(CATEGORIES.keys())  # все категории

COURIER_STUB_SECTIONS = {
    "📈 Спрос сейчас",
    "🚻 Туалеты рядом",
    "🅿️ Парковка / остановка",
    "🛠 ТО транспорта",
}

def courier_module_keyboard():
    buttons = [
        [KeyboardButton(text="💰 Финансы")],
        [KeyboardButton(text="📈 Спрос сейчас")],
        [KeyboardButton(text="🚻 Туалеты рядом")],
        [KeyboardButton(text="🅿️ Парковка / остановка")],
        [KeyboardButton(text="🛠 ТО транспорта")],
        [KeyboardButton(text="← Назад"), KeyboardButton(text="🏙 Выбор города")],
    ]
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=buttons)

def courier_finance_cancel_keyboard():
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[[KeyboardButton(text="❌ Отмена")]])

# Резерв на износ/ремонт - фиксированный % от валового дохода (см. README
# прототипа: "доход минус топливо минус резерв на износ"). Пользователь явно
# попросил 10% - в отличие от топлива это не физическая величина, а
# отложенная "подушка" на будущий ремонт/амортизацию, поэтому считается от
# дохода, а не от километража.
COURIER_WEAR_RESERVE_RATE = 0.10

COURIER_FINANCE_STEP_PROMPTS = {
    'income': "💰 Валовый доход за смену, ₽ (только число):",
    'km': "🚗 Километраж за день, км:",
    'consumption': "⛽ Расход топлива на 100 км (л или кВтч - как в профиле):",
    'fuel_price': "💵 Стоимость топлива за литр/кВтч, ₽:",
    'expenses': "📦 Доп. расходы за смену, ₽ (шины, штрафы и т.п. - если нет, пришли 0):",
    'hours': "🕐 Сколько часов длилась смена (можно дробно, например 5.5):",
}

def parse_decimal(text):
    """Число с точкой или запятой из свободного текста пользователя -
    Telegram-клавиатуры на телефоне часто подставляют запятую вместо точки."""
    cleaned = (text or '').strip().replace(',', '.')
    match = re.search(r'-?\d+(\.\d+)?', cleaned)
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None

async def send_start_screen(message: types.Message):
    """Общий код /start и кнопки "🏙 Выбор города" - сбрасывает весь user_state
    (включая любой незавершённый черновик заказа/расчёта) и возвращает на
    экран выбора города. Кнопка добавлена, чтобы не заставлять пользователя
    искать команду /start в интерфейсе Telegram - она есть на всех
    клавиатурах ниже корневого экрана (см. category_keyboard,
    services_keyboard, courier_module_keyboard)."""
    init_db()
    user_state.pop(message.from_user.id, None)
    text = "🚕 *Taxi Helper*\n\nВыбери город 👇"
    await message.answer(text, reply_markup=city_keyboard(), parse_mode='Markdown')

@router.message(Command("start"))
async def start(message: types.Message):
    await send_start_screen(message)

@router.message(lambda message: message.text == "🏙 Выбор города")
async def start_button(message: types.Message):
    await send_start_screen(message)

@router.message(lambda message: message.text == "← Назад")
async def go_back(message: types.Message):
    user_id = message.from_user.id
    state = user_state.get(user_id)

    if not state or 'city' not in state:
        # Некуда возвращаться дальше - показываем выбор города
        await message.answer("Выбери город 👇", reply_markup=city_keyboard())
        return

    if state.pop('in_courier_module', None):
        # Были в подменю "🧰 Инструменты водителя" -> возвращаемся на экран услуг (категория и город остаются)
        await message.answer("Выбери услугу 👇", reply_markup=services_keyboard(state.get('category'), state.get('city')))
        return

    if 'category' in state:
        # Были на экране услуг -> возвращаемся к выбору категории (город остаётся)
        state.pop('category', None)
        await message.answer("Выбери категорию 👇", reply_markup=category_keyboard())
    else:
        # Были на экране категорий -> возвращаемся к выбору города
        user_state.pop(user_id, None)
        await message.answer("Выбери город 👇", reply_markup=city_keyboard())

# ==================== "ОТДАТЬ ЗАКАЗ" (водитель -> водителям своего города) ====================
# Пошаговый сбор заказа через user_state[user_id]['order_draft'] = {'step':
# ..., 'data': {...}} - в боте нет отдельной FSM-библиотеки, весь остальной
# код тоже держит "текущий шаг" прямо в user_state, здесь та же схема.
# Шаги: pickup -> dropoff -> price -> car_class -> passengers -> confirm
# (на confirm ждём нажатия инлайн-кнопок, а не текста).

def shared_order_cancel_keyboard():
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[[KeyboardButton(text="❌ Отмена")]])

def shared_order_car_class_keyboard(category):
    tariffs = CATEGORIES.get(category, {}).get('tariffs', [])
    buttons = [[KeyboardButton(text=t)] for t in tariffs]
    buttons.append([KeyboardButton(text="❌ Отмена")])
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=buttons)

def shared_order_passengers_keyboard():
    buttons = [
        [KeyboardButton(text="1"), KeyboardButton(text="2"), KeyboardButton(text="3")],
        [KeyboardButton(text="4"), KeyboardButton(text="5"), KeyboardButton(text="6")],
        [KeyboardButton(text="❌ Отмена")],
    ]
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=buttons)

SHARED_ORDER_STEP_PROMPTS = {
    'pickup': "📍 Введи адрес *подачи* (точка А):",
    'dropoff': "🏁 Введи адрес *прибытия* (точка Б):",
    'price': "💰 Введи стоимость поездки в рублях (только число):",
    'client_phone': "📱 Введи номер телефона клиента (его передадим водителю, который примет заказ). Если номера нет - пришли *-*:",
}

async def show_shared_order_confirmation(message, data, category, city):
    city_name = CITY_DISPLAY_NAMES.get(city, city)
    text = (
        "*Проверь заказ перед отправкой:*\n\n"
        f"📍 Подача: {escape_md(data['pickup'])}\n"
        f"🏁 Прибытие: {escape_md(data['dropoff'])}\n"
        f"💰 Стоимость: {data['price']} ₽\n"
        f"🚘 Класс: {data['car_class']}\n"
        f"👥 Пассажиров: {data['passengers']}\n"
        f"📱 Телефон клиента: {escape_md(data['client_phone']) if data.get('client_phone') else 'не указан'}\n\n"
        f"_Разошлём водителям Такси/Ultima города {city_name}. Предложение будет "
        f"действовать {SHARED_ORDER_EXPIRY_HOURS} час, пока кто-то не примет._"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Отправить заказ", callback_data="order_confirm_send")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="order_confirm_cancel")]
    ])
    await message.answer(text, reply_markup=keyboard, parse_mode='Markdown')

@router.message(lambda message: message.text == "🔄 Отдать заказ")
async def start_shared_order(message: types.Message):
    user_id = message.from_user.id
    state = user_state.get(user_id)
    if not state or 'category' not in state:
        await message.answer("Сначала выбери город и категорию!")
        return
    if state.get('category') not in SHARED_ORDER_CATEGORIES:
        await message.answer(
            "Отдавать заказы могут только категории Такси и Ultima.",
            reply_markup=services_keyboard(state.get('category'), state.get('city')),
        )
        return
    state['order_draft'] = {'step': 'pickup', 'data': {}}
    await message.answer(SHARED_ORDER_STEP_PROMPTS['pickup'], reply_markup=shared_order_cancel_keyboard(), parse_mode='Markdown')

@router.message(lambda message: user_state.get(message.from_user.id, {}).get('order_draft') is not None)
async def shared_order_flow(message: types.Message):
    """Ловит ЛЮБОЙ текст, пока у пользователя активен черновик заказа - должен
    стоять РАНЬШЕ остальных текстовых хендлеров (город/категория и т.п.),
    иначе, например, адрес "Москва, ул. Ленина 1" перехватит select_city."""
    user_id = message.from_user.id
    state = user_state[user_id]
    draft = state['order_draft']
    text = (message.text or '').strip()
    category = state.get('category')
    city = state.get('city')

    if text == "❌ Отмена":
        state.pop('order_draft', None)
        await message.answer("Черновик заказа отменён.", reply_markup=services_keyboard(category, city))
        return

    step = draft['step']

    if step == 'pickup':
        if not text:
            await message.answer("Адрес не может быть пустым, попробуй ещё раз:")
            return
        draft['data']['pickup'] = text
        draft['step'] = 'dropoff'
        state['order_draft'] = draft
        await message.answer(SHARED_ORDER_STEP_PROMPTS['dropoff'], reply_markup=shared_order_cancel_keyboard(), parse_mode='Markdown')
        return

    if step == 'dropoff':
        if not text:
            await message.answer("Адрес не может быть пустым, попробуй ещё раз:")
            return
        draft['data']['dropoff'] = text
        draft['step'] = 'price'
        state['order_draft'] = draft
        await message.answer(SHARED_ORDER_STEP_PROMPTS['price'], reply_markup=shared_order_cancel_keyboard(), parse_mode='Markdown')
        return

    if step == 'price':
        price_digits = re.sub(r'[^\d]', '', text)
        if not price_digits:
            await message.answer("Не понял сумму - введи просто число, например 1500:")
            return
        draft['data']['price'] = price_digits
        draft['step'] = 'car_class'
        state['order_draft'] = draft
        await message.answer("🚘 Выбери класс автомобиля 👇", reply_markup=shared_order_car_class_keyboard(category))
        return

    if step == 'car_class':
        tariffs = CATEGORIES.get(category, {}).get('tariffs', [])
        if text not in tariffs:
            await message.answer("Выбери класс кнопкой на клавиатуре 👇", reply_markup=shared_order_car_class_keyboard(category))
            return
        draft['data']['car_class'] = text
        draft['step'] = 'passengers'
        state['order_draft'] = draft
        await message.answer("👥 Сколько пассажиров?", reply_markup=shared_order_passengers_keyboard())
        return

    if step == 'passengers':
        digits = re.sub(r'[^\d]', '', text)
        if not digits or int(digits) <= 0:
            await message.answer("Введи число пассажиров (например 2) или выбери кнопкой 👇", reply_markup=shared_order_passengers_keyboard())
            return
        draft['data']['passengers'] = digits
        draft['step'] = 'client_phone'
        state['order_draft'] = draft
        await message.answer(SHARED_ORDER_STEP_PROMPTS['client_phone'], reply_markup=shared_order_cancel_keyboard(), parse_mode='Markdown')
        return

    if step == 'client_phone':
        if not text:
            await message.answer("Пришли номер телефона клиента или *-*, если его нет:", parse_mode='Markdown')
            return
        draft['data']['client_phone'] = None if text == '-' else text
        draft['step'] = 'confirm'
        state['order_draft'] = draft
        await show_shared_order_confirmation(message, draft['data'], category, city)
        return

    # step == 'confirm' - здесь ждём нажатия инлайн-кнопок на сообщении выше,
    # а не текста; "❌ Отмена" обработана в самом начале функции.
    await message.answer("Нажми «📤 Отправить заказ» или «❌ Отменить» на сообщении выше 👆")

async def broadcast_shared_order(order_id, data, city, category, sender_id):
    """Рассылает объявление о заказе ТЕМ ЖЕ водителям, что видят саму кнопку
    "Отдать заказ" - тот же город, категории из SHARED_ORDER_CATEGORIES, кроме
    самого отправителя. Как и push_airport_status_change - берём СРЕЗ
    user_state (рассылка не мгновенная, список не должен "плыть" по ходу)."""
    if not bot:
        return 0
    recipients = [
        uid for uid, s in list(user_state.items())
        if isinstance(s, dict) and uid != sender_id and s.get('city') == city and s.get('category') in SHARED_ORDER_CATEGORIES
    ]
    if not recipients:
        return 0

    text = (
        f"🔄 *Заказ от другого водителя* (#{order_id})\n\n"
        f"📍 Подача: {escape_md(data['pickup'])}\n"
        f"🏁 Прибытие: {escape_md(data['dropoff'])}\n"
        f"💰 Стоимость: {data['price']} ₽\n"
        f"🚘 Класс: {data['car_class']}\n"
        f"👥 Пассажиров: {data['passengers']}\n\n"
        f"_Предложение действует {SHARED_ORDER_EXPIRY_HOURS} час. Кто первый примет - получит контакт отправителя._"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять заказ", callback_data=f"order_accept_{order_id}")],
        [InlineKeyboardButton(text="❌ Отказаться", callback_data=f"order_decline_{order_id}")]
    ])
    sent = 0
    for uid in recipients:
        try:
            await bot.send_message(uid, text, reply_markup=keyboard, parse_mode='Markdown')
            sent += 1
        except Exception as e:
            logger.warning(f"⚠️ Не удалось отправить заказ #{order_id} водителю {uid}: {e}")
        await asyncio.sleep(0.05)  # Telegram допускает ~30 сообщений/сек в разные чаты - берём с запасом
    logger.info(f"🔄 Заказ #{order_id} разослан {sent}/{len(recipients)} водителям города {city}")
    return sent

@router.callback_query(lambda c: c.data == "order_confirm_cancel")
async def cancel_shared_order_draft(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    state = user_state.get(user_id, {})
    state.pop('order_draft', None)
    await callback_query.message.edit_text("Черновик заказа отменён.")
    await callback_query.answer()
    await callback_query.message.answer("Выбери действие 👇", reply_markup=services_keyboard(state.get('category'), state.get('city')))

@router.callback_query(lambda c: c.data == "order_confirm_send")
async def confirm_send_shared_order(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    state = user_state.get(user_id)
    if not state or state.get('order_draft', {}).get('step') != 'confirm':
        await callback_query.answer("Черновик не найден - начни заново", show_alert=True)
        return
    data = state['order_draft']['data']
    category = state.get('category')
    city = state.get('city')
    city_name = CITY_DISPLAY_NAMES.get(city, city)

    sender_contact = format_user_contact(callback_query.from_user)
    order_id = create_shared_order(user_id, sender_contact, city, category, data['pickup'], data['dropoff'], data['price'], data['car_class'], data['passengers'], data.get('client_phone'))
    state.pop('order_draft', None)

    await callback_query.message.edit_text(f"⏳ Отправляю заказ #{order_id} водителям города {city_name}...")
    await callback_query.answer()

    sent = await broadcast_shared_order(order_id, data, city, category, user_id)
    if sent > 0:
        await callback_query.message.edit_text(f"✅ Заказ #{order_id} отправлен {sent} водителям города {city_name}. Ждём отклика - предложение действует {SHARED_ORDER_EXPIRY_HOURS} час.")
    else:
        await callback_query.message.edit_text(
            f"✅ Заказ #{order_id} создан, но сейчас в городе {city_name} нет других известных водителей Такси/Ultima. "
            f"Как только кто-то из них напишет боту, увидит твой заказ, пока он не истёк."
        )
    await callback_query.message.answer("Выбери действие 👇", reply_markup=services_keyboard(category, city))

@router.callback_query(lambda c: c.data.startswith('order_accept_'))
async def accept_shared_order(callback_query: types.CallbackQuery):
    order_id = int(callback_query.data[len('order_accept_'):])
    order = get_shared_order(order_id)
    if not order:
        await callback_query.answer("Заказ не найден", show_alert=True)
        return
    if order['status'] != 'open' or is_shared_order_expired(order):
        if order['status'] == 'open':
            expire_shared_order(order_id)
        await callback_query.message.edit_text("😔 Этот заказ уже занят другим водителем или срок предложения истёк.")
        await callback_query.answer("Заказ уже недоступен", show_alert=True)
        return

    accepted_by_contact = format_user_contact(callback_query.from_user)
    won = try_accept_shared_order(order_id, callback_query.from_user.id, accepted_by_contact)
    if not won:
        # Кто-то другой принял на долю секунды раньше - атомарный UPDATE (см.
        # try_accept_shared_order) сам разрулил гонку, здесь просто сообщаем.
        await callback_query.message.edit_text("😔 Этот заказ уже занят другим водителем - вы опоздали буквально на секунды.")
        await callback_query.answer("Заказ уже занят", show_alert=True)
        return

    text = (
        f"✅ *Вы приняли заказ #{order_id}*\n\n"
        f"📍 Подача: {escape_md(order['pickup'])}\n"
        f"🏁 Прибытие: {escape_md(order['dropoff'])}\n"
        f"💰 Стоимость: {order['price']} ₽\n"
        f"🚘 Класс: {order['car_class']}\n"
        f"👥 Пассажиров: {order['passengers']}\n"
    )
    if order.get('client_phone'):
        # Телефон клиента - не в общей рассылке (см. broadcast_shared_order),
        # виден только тому, кто реально принял заказ.
        text += f"📱 Телефон клиента: {escape_md(order['client_phone'])}\n"
    text += f"\n📞 Свяжитесь с отправителем: {order['sender_contact']}"
    await callback_query.message.edit_text(text, parse_mode='Markdown')
    await callback_query.answer("Заказ принят!")

    if bot:
        try:
            await bot.send_message(
                order['sender_id'],
                f"🎉 *Ваш заказ #{order_id} принят!*\n\n📞 Свяжитесь с водителем: {accepted_by_contact}",
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.warning(f"⚠️ Не удалось уведомить отправителя {order['sender_id']} о принятии заказа #{order_id}: {e}")

@router.callback_query(lambda c: c.data.startswith('order_decline_'))
async def decline_shared_order(callback_query: types.CallbackQuery):
    await callback_query.message.edit_text("Вы отказались от этого заказа.")
    await callback_query.answer()

# ==================== МОДУЛЬ "ИНСТРУМЕНТЫ ВОДИТЕЛЯ" - хендлеры ====================

@router.message(lambda message: message.text == "🧰 Инструменты водителя")
async def open_courier_module(message: types.Message):
    user_id = message.from_user.id
    state = user_state.get(user_id)
    if not state or 'category' not in state:
        await message.answer("Сначала выбери город и категорию!")
        return
    if state.get('category') not in COURIER_MODULE_CATEGORIES:
        # COURIER_MODULE_CATEGORIES сейчас = все категории, но проверку
        # оставляем на случай, если позже какую-то категорию снова исключат.
        await message.answer(
            "Этот раздел пока недоступен для твоей категории.",
            reply_markup=services_keyboard(state.get('category'), state.get('city')),
        )
        return
    state['in_courier_module'] = True
    await message.answer("🧰 *Инструменты водителя*\n\nВыбери раздел 👇", reply_markup=courier_module_keyboard(), parse_mode='Markdown')

@router.message(lambda message: message.text == "💰 Финансы" and user_state.get(message.from_user.id, {}).get('in_courier_module'))
async def start_courier_finance(message: types.Message):
    user_id = message.from_user.id
    state = user_state[user_id]
    state['courier_finance_draft'] = {'step': 'income', 'data': {}}
    await message.answer(COURIER_FINANCE_STEP_PROMPTS['income'], reply_markup=courier_finance_cancel_keyboard())

@router.message(lambda message: message.text in COURIER_STUB_SECTIONS and user_state.get(message.from_user.id, {}).get('in_courier_module'))
async def courier_stub_section(message: types.Message):
    # Спрос/туалеты/парковка/ТО - пока без реальных точек (нужна карта +
    # источники данных, см. README прототипа), тот же текст, что в самом
    # HTML-прототипе на экране-заглушке (data-view="soon").
    await message.answer("Этот раздел в разработке 🚧 — скоро будет", reply_markup=courier_module_keyboard())

@router.message(lambda message: user_state.get(message.from_user.id, {}).get('courier_finance_draft') is not None)
async def courier_finance_flow(message: types.Message):
    """Пошаговый сбор данных для финансового калькулятора - та же схема, что
    у shared_order_flow (черновик в state, один вопрос за раз). Должен стоять
    РАНЬШЕ общих текстовых хендлеров по той же причине (см. shared_order_flow)."""
    user_id = message.from_user.id
    state = user_state[user_id]
    draft = state['courier_finance_draft']
    text = (message.text or '').strip()

    if text == "❌ Отмена":
        state.pop('courier_finance_draft', None)
        await message.answer("Расчёт отменён.", reply_markup=courier_module_keyboard())
        return

    step = draft['step']

    if step == 'income':
        value = parse_decimal(text)
        if value is None or value < 0:
            await message.answer("Не понял сумму - введи просто число, например 2340:")
            return
        draft['data']['income'] = value
        draft['step'] = 'km'
        state['courier_finance_draft'] = draft
        await message.answer(COURIER_FINANCE_STEP_PROMPTS['km'], reply_markup=courier_finance_cancel_keyboard())
        return

    if step == 'km':
        value = parse_decimal(text)
        if value is None or value < 0:
            await message.answer("Не понял километраж - введи число, например 87:")
            return
        draft['data']['km'] = value
        draft['step'] = 'consumption'
        state['courier_finance_draft'] = draft
        await message.answer(COURIER_FINANCE_STEP_PROMPTS['consumption'], reply_markup=courier_finance_cancel_keyboard())
        return

    if step == 'consumption':
        value = parse_decimal(text)
        if value is None or value < 0:
            await message.answer("Не понял расход - введи число, например 6.2:")
            return
        draft['data']['consumption'] = value
        draft['step'] = 'fuel_price'
        state['courier_finance_draft'] = draft
        await message.answer(COURIER_FINANCE_STEP_PROMPTS['fuel_price'], reply_markup=courier_finance_cancel_keyboard())
        return

    if step == 'fuel_price':
        value = parse_decimal(text)
        if value is None or value < 0:
            await message.answer("Не понял цену топлива - введи число, например 61.5:")
            return
        draft['data']['fuel_price'] = value
        draft['step'] = 'expenses'
        state['courier_finance_draft'] = draft
        await message.answer(COURIER_FINANCE_STEP_PROMPTS['expenses'], reply_markup=courier_finance_cancel_keyboard())
        return

    if step == 'expenses':
        value = parse_decimal(text)
        if value is None or value < 0:
            await message.answer("Не понял сумму - введи число (или 0, если доп. расходов не было):")
            return
        draft['data']['expenses'] = value
        draft['step'] = 'hours'
        state['courier_finance_draft'] = draft
        await message.answer(COURIER_FINANCE_STEP_PROMPTS['hours'], reply_markup=courier_finance_cancel_keyboard())
        return

    if step == 'hours':
        value = parse_decimal(text)
        if value is None or value <= 0:
            await message.answer("Не понял часы - введи число больше нуля, например 5.5:")
            return
        draft['data']['hours'] = value
        state.pop('courier_finance_draft', None)
        await send_courier_finance_result(message, draft['data'])
        return

async def send_courier_finance_result(message: types.Message, data):
    """Считает и показывает итог смены - формула и порядок строк повторяют
    карточку "📊 СМЕНА - ИТОГ" из прототипа (income - топливо - резерв на
    износ - доп.расходы = чистыми; отдельной строкой ₽/час)."""
    income = data['income']
    km = data['km']
    consumption = data['consumption']
    fuel_price = data['fuel_price']
    expenses = data['expenses']
    hours = data['hours']

    fuel_cost = (km / 100) * consumption * fuel_price
    wear_reserve = income * COURIER_WEAR_RESERVE_RATE
    net_profit = income - fuel_cost - wear_reserve - expenses
    per_hour = net_profit / hours

    def fmt(n):
        return f"{n:,.0f}".replace(',', ' ')

    lines = [
        "📊 *СМЕНА — ИТОГ*",
        "",
        f"Валовый доход: {fmt(income)} ₽",
        f"⛽ Топливо ({fmt(km)} км × {consumption:g} на 100): −{fmt(fuel_cost)} ₽",
        f"🔧 Резерв на износ/ремонт (10%): −{fmt(wear_reserve)} ₽",
    ]
    if expenses > 0:
        lines.append(f"📦 Доп. расходы: −{fmt(expenses)} ₽")
    lines.append("")
    lines.append(f"✅ *Чистыми за смену: {fmt(net_profit)} ₽*")
    lines.append(f"за {hours:g} ч ≈ {fmt(per_hour)} ₽/ч")

    await message.answer('\n'.join(lines), reply_markup=courier_module_keyboard(), parse_mode='Markdown')

@router.message(lambda message: any(city in message.text for city in ["Москва", "СПб", "Новосибирск", "Екатеринбург", "Казань", "Челябинск", "Омск", "Самара", "Ростов", "Нижний Новгород", "Краснодар", "Сочи"]))
async def select_city(message: types.Message):
    city_map = {
        "🏛️ Москва": "moscow", "🕯️ СПб": "spb", "🌲 Новосибирск": "novosibirsk",
        "🏔️ Екатеринбург": "ekb", "🎓 Казань": "kazan", "❄️ Челябинск": "chelyabinsk",
        "🌾 Омск": "omsk", "🏭 Самара": "samara", "🌊 Ростов": "rostov",
        "🏰 Нижний Новгород": "nnovgorod", "🌴 Краснодар": "krasnodar", "🏖️ Сочи": "sochi"
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
    await message.answer(text, reply_markup=services_keyboard(selected_category, user_state[user_id].get('city')))

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

ROAD_EVENTS_CHANNELS = {
    'moscow': ('https://t.me/dtp777', 'Москва'),
    'spb': ('https://t.me/dtp_spb78', 'Санкт-Петербург'),
}

@router.message(lambda message: message.text == "Дорожные события")
async def show_road_events(message: types.Message):
    """ДТП и дорожные происшествия по городам - вместо собственной ленты
    в боте просто отдаём кнопку-ссылку на публичный Telegram-канал с живыми
    сводками ДТП для этого города (Москва -> @dtp777, СПб -> @dtp_spb78).
    Для городов без канала в ROAD_EVENTS_CHANNELS остаётся текст-заглушка."""
    state = user_state.get(message.from_user.id, {})
    city = state.get('city')
    channel = ROAD_EVENTS_CHANNELS.get(city)
    if channel:
        url, city_name = channel
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚨 Открыть канал ДТП", url=url)]
        ])
        await message.answer(
            f"🚧 *Дорожные события — {city_name}*\n\nАктуальные ДТП и происшествия — в Telegram-канале 👇",
            reply_markup=keyboard,
            parse_mode='Markdown',
        )
    else:
        await message.answer(
            "🚧 *Дорожные события*\n\nДля этого города канал с ДТП пока не подключен.",
            reply_markup=services_keyboard(state.get('category'), city),
            parse_mode='Markdown',
        )

@router.message(lambda message: message.text == "🎭 События города")
async def show_city_events(message: types.Message):
    """Афиша - источник ТОЛЬКО TimePad (см. fetch_timepad_data.py; KudaGo
    убран по просьбе пользователя - кнопка "Поехали" по координатам
    открывала именно приложение Яндекс.Навигатор вместо карт/браузера,
    события были не нужны). Показываем топ-10 ближайших крупных событий
    (см. get_events_for_user, limit=10). Для городов вне покрытия TimePad
    явно говорим "нет данных", а не показываем пустой экран."""
    user_id = message.from_user.id
    if user_id not in user_state or 'city' not in user_state[user_id]:
        await message.answer("Сначала выбери город!")
        return
    city = user_state[user_id]['city']
    category = user_state[user_id].get('category', 'taxi')

    events, city_supported = get_events_for_user(city, category, limit=10)

    if not city_supported:
        text = (
            "🎭 *События города*\n\n"
            "Для этого города пока нет данных об афише - источник событий "
            "(TimePad) пока покрывает только Москву. Будем искать источник "
            "и для остальных городов."
        )
        await message.answer(text, reply_markup=services_keyboard(category, city), parse_mode='Markdown')
        return

    if not events:
        text = (
            "🎭 *События города*\n\n"
            "На ближайшее время подходящих событий не нашлось. Загляни позже - "
            "афиша обновляется каждые несколько часов."
        )
        await message.answer(text, reply_markup=services_keyboard(category, city), parse_mode='Markdown')
        return

    class_label = CATEGORIES.get(category, {}).get('name', '')
    header = f"🎭 *События города* ({class_label.title() if class_label else 'все'})"
    # Заголовок - обычным сообщением с прикреплённой нижней клавиатурой услуг
    # (она остаётся видна и дальше, повторно прикреплять на каждое сообщение
    # не нужно). Каждое событие - ОТДЕЛЬНЫМ сообщением со СВОЕЙ инлайн-кнопкой
    # "Поехали", чтобы кнопка однозначно вела именно к этому месту, а не к
    # первому/последнему в общем списке. Небольшая пауза между отправками -
    # чтобы Telegram не сворачивал быстро идущие подряд сообщения от одного
    # бота визуально в одну группу у пользователя.
    await message.answer(header, reply_markup=services_keyboard(category, city), parse_mode='Markdown')
    for event in events:
        text, keyboard = build_event_message(event, city)
        await message.answer(text, reply_markup=keyboard, parse_mode='Markdown', disable_web_page_preview=True)
        await asyncio.sleep(0.1)

@router.message(lambda message: message.text == "✈️ Аэропорты")
async def show_airport_menu(message: types.Message):
    user_id = message.from_user.id
    if user_id not in user_state:
        await message.answer("Сначала выбери город!")
        return
    if user_state[user_id].get('category') in CATEGORIES_WITHOUT_AIRPORTS:
        await message.answer("Для этой категории аэропорты недоступны.", reply_markup=services_keyboard(user_state[user_id].get('category'), user_state[user_id].get('city')))
        return
    text = "Выбери действие 👇"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Прилеты", callback_data="airport_arrivals")],
        [InlineKeyboardButton(text="🔄 Доступность", callback_data="airport_availability")],
        [InlineKeyboardButton(text="📋 Очередь", callback_data="airport_queue")]
    ])
    await message.answer(text, reply_markup=keyboard)

def build_train_stations_keyboard(category, city):
    """Список вокзалов ДАННОГО ГОРОДА (фильтр по STATION_CITY) - у каждого
    символ+% загрузки ТЕКУЩЕГО получаса (бинарная индикация вокзалов - см.
    get_train_load_symbol, отличается от 4-уровневой шкалы аэропортов)."""
    data = load_trains_data()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    if not data or not data.get('stations'):
        return keyboard, False
    city_stations = {code: st for code, st in data['stations'].items() if STATION_CITY.get(code) == city}
    if not city_stations:
        return keyboard, False
    for code, station in city_stations.items():
        load, trains_in_period, _ = compute_current_train_period_load(code, category)
        symbol = get_train_load_symbol(load)
        button_text = f"🚆 {station['name']} {symbol} {load:.0f}%"
        keyboard.inline_keyboard.append([InlineKeyboardButton(text=button_text, callback_data=f"train_station_{code}")])
    return keyboard, True

@router.message(lambda message: message.text == "🚆 Вокзалы")
async def show_train_stations_menu(message: types.Message):
    """Список вокзалов ВЫБРАННОГО ГОРОДА (см. STATION_CITY и
    fetch_trains_data.py). Кнопка и так видна только в городах из TRAIN_CITIES
    (см. services_keyboard), но проверяем город и здесь на случай, если
    пользователь сменил город, не обновив клавиатуру."""
    user_id = message.from_user.id
    if user_id not in user_state or 'city' not in user_state[user_id]:
        await message.answer("Сначала выбери город!")
        return
    category = user_state[user_id].get('category')
    city = user_state[user_id]['city']
    if city not in TRAIN_CITIES:
        await message.answer(
            "🚆 Вокзалы пока недоступны в этом городе.",
            reply_markup=services_keyboard(category, city),
        )
        return
    keyboard, has_data = build_train_stations_keyboard(category, city)
    if not has_data:
        await message.answer(
            "🚆 Данные по вокзалам ещё не загружены - обновляются раз в 12 часов, загляни чуть позже.",
            reply_markup=services_keyboard(category, city),
        )
        return
    await message.answer("Выбери вокзал 👇", reply_markup=keyboard)

@router.callback_query(lambda c: c.data.startswith('train_station_'))
async def show_train_station_arrivals(callback_query: types.CallbackQuery):
    """Прогноз загруженности вокзала - TRAIN_FORECAST_HOURS часов вперёд (12),
    блоками по TRAIN_FORECAST_PERIOD_MINUTES минут (30 - уменьшено с целого
    часа по просьбе пользователя), у каждого блока символ+%/бинарная
    рекомендация (см. get_train_load_symbol - у вокзалов, в отличие от
    аэропортов, только 2 состояния "ехать"/"не ехать"), а внутри блока - сами
    поезда (время, откуда, статус - без числа пассажиров в отображении,
    убрано по просьбе пользователя, хотя сама оценка всё ещё считается под
    капотом для расчёта Загрузки). И Такси, и Ultima видят ОДИНАКОВЫЙ список -
    ВСЕ поезда дальнего следования, кроме пригородных электричек (их вообще
    не собираем - см. fetch_trains_data.py). Разница только в подаче: у
    Ultima Сапсаны и фирменные/премиальные поезда идут ПЕРВЫМИ в списке блока
    (акцент на премиальном сегменте), у Такси порядок - просто по времени.
    Только прибытия - вылеты не собираются."""
    code = callback_query.data[len('train_station_'):]
    user_id = callback_query.from_user.id
    category = user_state.get(user_id, {}).get('category', 'taxi')

    station_name, arrivals = get_trains_for_station(code, category)
    if station_name is None:
        await callback_query.answer("Данные пока недоступны", show_alert=True)
        return

    now = datetime.now(ZoneInfo('Europe/Moscow'))
    class_label = 'акцент на Сапсан/фирменные' if category == 'ultima' else 'все поезда'
    capacity = STATION_CAPACITY.get(code, 1000)
    msg = await callback_query.message.edit_text(f"⏳ Загружаю прогноз по вокзалу {station_name}...")

    text = f"*🚆 {station_name} - прогноз загруженности* ({class_label})\n"
    text += f"_Обновлено: {now.strftime('%H:%M:%S')} (МСК)_\n"
    text += f"_Ориентировочная пропускная способность: ~{capacity} пас/час (оценка)_\n\n"

    # Текущее время округляем вниз до получаса - первый блок прогноза должен
    # начинаться с него же, а не с произвольной минуты нажатия кнопки.
    period_base = now.replace(minute=(now.minute // TRAIN_FORECAST_PERIOD_MINUTES) * TRAIN_FORECAST_PERIOD_MINUTES, second=0, microsecond=0)
    total_periods = (TRAIN_FORECAST_HOURS * 60) // TRAIN_FORECAST_PERIOD_MINUTES

    any_trains = False
    for period_offset in range(total_periods):
        load, trains_in_period, _ = compute_current_train_period_load(code, category, period_offset)
        block_start = period_base + timedelta(minutes=TRAIN_FORECAST_PERIOD_MINUTES * period_offset)
        block_end = block_start + timedelta(minutes=TRAIN_FORECAST_PERIOD_MINUTES - 1)
        block_display = f"{block_start.strftime('%H:%M')}–{block_end.strftime('%H:%M')}"
        symbol = get_train_load_symbol(load)
        label = get_train_load_label(load)
        text += f"{symbol} *{block_display}* | Загрузка: *{load:.0f}%* - *{label}*\n"
        if trains_in_period:
            any_trains = True
            if category == 'ultima':
                # Сапсан/фирменные - первыми в списке (акцент для Ultima)
                trains_in_period = sorted(trains_in_period, key=lambda t: (not t.get('is_sapsan'), not t.get('is_firmenny'), t['time']))
            for t in trains_in_period:
                if t.get('is_sapsan'):
                    status = "🚄 Сапсан"
                elif t.get('is_firmenny'):
                    status = "⭐ Фирменный"
                else:
                    status = "🚆 обычный"
                # Количество пассажиров убрано из отображения по просьбе
                # пользователя - сама оценка передаётся под капотом всё равно
                # используется для расчёта Загрузки/символа выше.
                text += f"   • {t['time']} из {t['point']} (№{t['number']}) - {status}\n"
        else:
            text += "   Прибытий не ожидается\n"
        text += "\n"

    if not any_trains:
        text += f"_На ближайшие {TRAIN_FORECAST_HOURS} часов прибытий не найдено._\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="train_stations_back")]])
    await msg.edit_text(text, reply_markup=keyboard, parse_mode='Markdown')
    await callback_query.answer()

@router.callback_query(lambda c: c.data == "train_stations_back")
async def train_stations_back(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    category = user_state.get(user_id, {}).get('category', 'taxi')
    city = user_state.get(user_id, {}).get('city')
    keyboard, has_data = build_train_stations_keyboard(category, city)
    if not has_data:
        await callback_query.message.edit_text("🚆 Данные по вокзалам сейчас недоступны.")
        await callback_query.answer()
        return
    await callback_query.message.edit_text("Выбери вокзал 👇", reply_markup=keyboard)
    await callback_query.answer()

@router.callback_query(lambda c: c.data == "airport_arrivals")
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
    category = user_state.get(user_id, {}).get('category', 'taxi')
    relevant_class = CATEGORY_TO_CLASS.get(category, 'total')
    msg = await callback_query.message.edit_text("⏳ Загружаю прилеты...")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    for i, airport in enumerate(airports):
        if airport.get('closed'):
            button_text = f"{airport['emoji']} {airport['name']} 🔴 ЗАКРЫТ"
        else:
            current_load, _, _ = compute_current_hour_load(airport['icao'], relevant_class)
            emoji = get_load_emoji(current_load)
            button_text = f"{airport['emoji']} {airport['name']} {emoji} {current_load:.0f}%"
        keyboard.inline_keyboard.append([InlineKeyboardButton(text=button_text, callback_data=f"airport_details_{city}_{i}")])
    await msg.edit_text("✅ Аэропорты (прилеты):", reply_markup=keyboard)
    await callback_query.answer()

@router.callback_query(lambda c: c.data.startswith('airport_details_'))
async def show_airport_details(callback_query: types.CallbackQuery):
    data_parts = callback_query.data.split('_')
    city = data_parts[2]
    airport_idx = int(data_parts[3])
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

    msg = await callback_query.message.edit_text(f"⏳ Загружаю расписание {airport['name']}...")
    flights = get_airport_flights(airport['icao'])
    class_label = {'economy': 'Эконом-класс', 'business': 'Бизнес-класс', 'total': 'Все классы'}[relevant_class]
    now = get_airport_now(airport['icao'])  # местное время АЭРОПОРТА, не сервера
    text = f"*{airport['emoji']} {airport['name']} - 📥 Прилеты*\n"
    text += f"_Обновлено: {now.strftime('%H:%M:%S')} (местное время аэропорта)_\n"
    text += f"_Пропускная способность: {capacity} пас/час (эконом {economy_capacity:.0f} / бизнес {business_capacity:.0f})_\n"
    text += f"_Рекомендации рассчитаны для: {class_label}_\n"
    text += "\n*📊 ПРОГНОЗ ЗАГРУЖЕННОСТИ АЭРОПОРТА (текущее время +8 часов):*\n\n"
    current_hour = now.hour
    for hour_offset in range(8):
        order_hour_of_day = (current_hour + hour_offset) % 24

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
            if flight_time.hour == order_hour_of_day:
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
        action = get_load_recommendation(load)
        text += f"{emoji} *{hour_display}* | Нагрузка ({class_label.lower()}): *{load:.0f}%*\n"
        text += f"   Рекомендация: *{action}*\n"
        text += f"   🛬 Рейсов: {flights_in_hour} (🇷🇺 внутр. {domestic_in_hour} / 🌍 межд. {international_in_hour})  |  ✈️ Пассажиры: {total_in_hour} (эконом {economy_in_hour} / бизнес {business_in_hour})\n\n"

    text += "_🔴0-50% НЕ ЕХАТЬ | 🟡51-70% ОЧЕРЕДЬ | 🟢71-100% ЕХАТЬ | 🟣>100% СРОЧНО_"
    await msg.edit_text(text, parse_mode='Markdown')
    await callback_query.answer()

def compute_current_hour_load(airport_icao, relevant_class, hour_offset=0):
    """Загрузка на ТЕКУЩИЙ час по ПРИЛЁТАМ - используется в списке аэропортов
    (show_airport_info) и в пуше о повышенном спросе (high_demand_alert_checker).
    hour_offset сдвигает "текущий" час вперёд - для заблаговременных пушей."""
    now = get_airport_now(airport_icao)
    target_hour = (now.hour + hour_offset) % 24
    flights = get_airport_flights(airport_icao)
    capacity = AIRPORT_CAPACITY.get(airport_icao, 1000)
    relevant_cap = {'economy': capacity * ECONOMY_SHARE, 'business': capacity * BUSINESS_SHARE, 'total': capacity}[relevant_class]
    key = {'economy': 'passengers_economy', 'business': 'passengers_business', 'total': 'passengers'}[relevant_class]
    flights_now = [f for f in flights if datetime.fromtimestamp(f.get('firstSeen', 0)).hour == target_hour]
    total_passengers = sum(f.get(key, 0) for f in flights_now)
    load = (total_passengers / relevant_cap) * 100 if total_passengers > 0 else 0
    return load, len(flights_now), target_hour

def compute_current_availability(airport_icao, relevant_class):
    """Загруженность аэропорта ПРЯМО СЕЙЧАС для конкретного класса (эконом/бизнес/все):
    только прилёты в текущий час (эти пассажиры выходят из терминала прямо сейчас) -
    вылеты больше не собираются (убраны ради экономии квоты, см. get_airport_flights).
    Сравнивается с ЧАСОВОЙ пропускной способностью - раньше тут по ошибке складывались
    пассажиры ВСЕХ рейсов за весь день, что давало 1000-2000%+."""
    now = get_airport_now(airport_icao)
    current_hour = now.hour
    arrivals = get_airport_flights(airport_icao)
    capacity = AIRPORT_CAPACITY.get(airport_icao, 1000)
    relevant_cap = {'economy': capacity * ECONOMY_SHARE, 'business': capacity * BUSINESS_SHARE, 'total': capacity}[relevant_class]
    key = {'economy': 'passengers_economy', 'business': 'passengers_business', 'total': 'passengers'}[relevant_class]

    arrivals_now = [f for f in arrivals if datetime.fromtimestamp(f.get('firstSeen', 0)).hour == current_hour]

    total_passengers = sum(f.get(key, 0) for f in arrivals_now)
    load = (total_passengers / relevant_cap) * 100 if total_passengers > 0 else 0
    return {
        'load': load,
        'capacity': capacity,
        'relevant_cap': relevant_cap,
        'arrivals_now': arrivals_now,
        'total_passengers': total_passengers,
        'now': now,
        'current_hour': current_hour,
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
    прилёты текущего часа и все уведомления Росавиации за 12ч."""
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
        text += f"🛬 Прилетает в {info['current_hour']:02d}:00-{(info['current_hour']+1)%24:02d}:00: {len(info['arrivals_now'])} рейсов\n\n"

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

# Сериализует доступ к общему счётчику квоты (api_usage_log.json, один ключ
# на аэропорты и поезда) между двумя НЕЗАВИСИМЫМИ фоновыми циклами ниже. Без
# этого, если графики случайно совпадут по времени, оба могут одновременно
# прочитать один и тот же "остаток на сегодня" и вместе превысить квоту -
# именно так уже один раз заблокировали ключ.
_yandex_api_lock = asyncio.Lock()


def seconds_until_hour(target_hour, tz='Europe/Moscow'):
    """Сколько секунд осталось до ближайшего наступления target_hour:00 по
    заданной таймзоне (сегодня, если ещё не наступил, иначе завтра)."""
    now = datetime.now(ZoneInfo(tz))
    target = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def airports_data_updater():
    """Фоновая задача для flights_data.json. С 00:00 до 06:00 по Москве
    (FLIGHTS_NIGHT_START_HOUR-FLIGHTS_NIGHT_END_HOUR) обновлений НЕТ ВООБЩЕ -
    рейсов ночью мало, ждём до 06:00. С 06:00 до 24:00 - каждые
    FLIGHTS_DAY_INTERVAL_HOURS часов. Запускается сразу при старте бота (если
    он поднялся не ночью), чтобы данные были свежими с первого деплоя."""
    while True:
        hour = datetime.now(ZoneInfo('Europe/Moscow')).hour
        if FLIGHTS_NIGHT_START_HOUR <= hour < FLIGHTS_NIGHT_END_HOUR:
            wait_s = seconds_until_hour(FLIGHTS_NIGHT_END_HOUR)
            logger.info(f"🌙 Ночь (00:00-06:00 МСК) - аэропорты не обновляем, жду до 06:00 ({wait_s/3600:.1f}ч)")
            await asyncio.sleep(wait_s)
            continue
        try:
            logger.info("🔄 Обновляю flights_data.json из Yandex Rasp API...")
            async with _yandex_api_lock:
                await asyncio.to_thread(fetch_yandex_data.main)
            logger.info("✅ flights_data.json обновлён")
        except Exception as e:
            logger.error(f"❌ Ошибка фонового обновления flights_data.json: {e}")
        await asyncio.sleep(FLIGHTS_DAY_INTERVAL_HOURS * 3600)


async def trains_data_updater():
    """Фоновая задача для trains_data.json (7 вокзалов по 6 городам, включая
    Сапсан - см. STATION_CITY и fetch_trains_data.py). Независимый от
    аэропортов график - раз в TRAINS_UPDATE_INTERVAL_HOURS часов,
    круглосуточно, без привязки к дню/ночи. Запускается сразу при старте
    бота."""
    while True:
        try:
            logger.info("🔄 Обновляю trains_data.json (Казанский/Ленинградский) из Yandex Rasp API...")
            async with _yandex_api_lock:
                await asyncio.to_thread(fetch_trains_data.main)
            logger.info("✅ trains_data.json обновлён")
        except Exception as e:
            logger.error(f"❌ Ошибка фонового обновления trains_data.json: {e}")
        await asyncio.sleep(TRAINS_UPDATE_INTERVAL_HOURS * 3600)

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
                load, _, target_hour = compute_current_hour_load(icao, relevant_class, hour_offset=HIGH_DEMAND_LEAD_HOURS)
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
        asyncio.create_task(airports_data_updater())
        asyncio.create_task(trains_data_updater())
    else:
        logger.warning("⚠️ YANDEX_RASP_API_KEY не задан в переменных окружения Railway - flights_data.json и trains_data.json не будут обновляться автоматически")
    asyncio.create_task(favt_notices_updater())
    asyncio.create_task(high_demand_alert_checker())
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
