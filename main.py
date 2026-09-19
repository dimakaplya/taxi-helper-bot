#!/usr/bin/env python3
import logging
import asyncio
import sqlite3
import json
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from math import radians, sin, cos, asin, sqrt
from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
import os
import aiohttp  # прямой запрос к Open-Meteo (публичный API без ключа) - см. блок "ДОЖДЬ" ниже

import fetch_yandex_data  # логика похода в Yandex Rasp API, запускается фоново прямо на Railway
import fetch_trains_data  # поезда дальнего следования (Казанский, Ленинградский) - тот же ключ и квота
import fetch_favt_notices  # логика сбора уведомлений Росавиации (@favt_info), тоже фоново
import fetch_road_events   # ДТП по городам (Москва: @dtp777+@DtOperativno слиты в одну ленту, СПб: @dtp_spb78) - тем же способом, фоново
import fetch_concert_events  # афиша концертов из Telegram-каналов (Москва: @concerts_moscow, СПб: @spb_conc) - второй источник для "🎭 События города", тем же способом, фоново
import fetch_mos_road_data  # официальный API data.mos.ru (доп. источник для Москвы) - см. MOS_DATA_API_KEY ниже
import fetch_timepad_data  # афиша города (TimePad) для кнопки "🎭 События города" - используется
                            # только для TIMEPAD_CITY_MAP; timepad_data.json обновляется ЛОКАЛЬНО
                            # (см. fetch_timepad_data.py), Railway не может дотянуться до TimePad
                            # (Cloudflare блокирует датацентровые IP, см. комментарий в самом файле)
from config_loader import get_cities as _get_config_cities  # единый справочник аэропортов/вокзалов - см. config.json и config_loader.py

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
# Вокзалы теперь не только в Москве - у каждой станции (см. config.json,
# читается через config_loader.py) есть свой город бота. Кнопка "🚆 Вокзалы"
# видна в городе, только если для него есть хотя бы одна станция здесь.
# STATION_CITY/STATION_CAPACITY раньше были захардкожены вручную (и
# дублировались в fetch_trains_data.py) - рассинхрон таких копий уже
# приводил к реальному багу с аэропортами, поэтому теперь оба словаря
# строятся из ЕДИНОГО источника (config.json) при импорте модуля.
STATION_CITY = {}
STATION_CAPACITY = {}
for _city_key, _city_data in _get_config_cities().items():
    for _station in _city_data.get('stations', []):
        STATION_CITY[_station['code']] = _city_key
        if 'capacity' in _station:
            STATION_CAPACITY[_station['code']] = _station['capacity']
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
# станционные данные. Сами цифры - в config.json (см. STATION_CAPACITY выше,
# строится оттуда через config_loader.py).

# Аэропорты: ночью (00:00-06:00 МСК) рейсов мало - вообще НЕ обновляем в этом
# окне (0 запусков), а не просто реже, как было раньше. Днём (06:00-24:00) -
# каждые FLIGHTS_DAY_INTERVAL_HOURS часов.
FLIGHTS_NIGHT_START_HOUR = 0
FLIGHTS_NIGHT_END_HOUR = 6  # [0, 6) - ночь (реже), [6, 24) - день
FLIGHTS_DAY_INTERVAL_HOURS = 1  # днём - каждый час (06,07,...,23 = 18 запусков/сутки).
# ИЗМЕНЕНО 19.09.2026 с 2ч по просьбе пользователя (было 9 запусков/сутки).
FLIGHTS_NIGHT_INTERVAL_HOURS = 2  # ночью (00:00-06:00) - каждые 2ч (00,02,04 = 3 запуска/сутки).
# ДОБАВЛЕНО 19.09.2026 по просьбе пользователя - раньше ночью обновлений не
# было ВООБЩЕ (см. историю airports_data_updater), теперь собираем реже, но
# не пропускаем совсем.
# Итого аэропорты: 18 (день) + 3 (ночь) = 21 запуск/сутки x 13 аэропортов =
# 273 запроса/сутки только на рейсы. Вместе с поездами (52/сутки) - 325/500
# (65%) - ещё в пределах DAILY_SAFETY_LIMIT (см. fetch_yandex_data.py, 70% =
# 350), но запас уже небольшой. Стоит помнить при новой блокировке ключа -
# см. инцидент 19.09.2026.

# Поезда (7 вокзалов по 6 городам - см. STATION_CITY): отдельный, не
# завязанный на день/ночь график - раз в TRAINS_UPDATE_INTERVAL_HOURS часов,
# круглосуточно (Сапсаны и дальние поезда ходят и вечером/рано утром, а объём
# запросов по 7 вокзалам всё ещё небольшой - не жалко гонять и ночью).
TRAINS_UPDATE_INTERVAL_HOURS = 6  # 4 запуска/сутки (было 12ч/2 запуска - ИЗМЕНЕНО 19.09.2026)

# Прогноз загруженности вокзала показывает на TRAIN_FORECAST_HOURS часов
# вперёд (было 8, увеличено по просьбе пользователя). У аэропортов свой,
# отдельный 8-часовой прогноз (см. show_airport_details) - не путать.
TRAIN_FORECAST_HOURS = 12
# Гранулярность прогноза - блоками по TRAIN_FORECAST_PERIOD_MINUTES минут.
# Было по часу целиком -> уменьшено до получаса -> ВОЗВРАЩЕНО обратно к часу
# по просьбе пользователя (19.09.2026).
# Число периодов на весь прогноз = TRAIN_FORECAST_HOURS*60/TRAIN_FORECAST_PERIOD_MINUTES.
TRAIN_FORECAST_PERIOD_MINUTES = 60

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

# Дорожные события (ДТП) по городам - те же публичные веб-версии Telegram-
# каналов (@dtp777 Москва, @dtp_spb78 СПб), тот же способ сбора, что и у
# @favt_info выше (см. fetch_road_events.py). Часто не блокируется/не
# требует ключа, поэтому обновляем каждые 10 минут.
ROAD_EVENTS_DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'road_events_data.json')
ROAD_EVENTS_UPDATE_INTERVAL_MINUTES = 10

# Афиша концертов из Telegram-каналов (Москва: @concerts_moscow, СПб:
# @spb_conc) - второй источник для "🎭 События города" вместе с TimePad, тот
# же способ сбора, что и ROAD_EVENTS_* выше (см. fetch_concert_events.py).
# Афиша меняется медленнее, чем сводки ДТП - обновляем реже (раз в 3 часа,
# как FAVT/TimePad-подобные источники, а не каждые 10 минут).
CONCERT_EVENTS_DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'concert_events_data.json')
CONCERT_EVENTS_UPDATE_INTERVAL_MINUTES = 180

# Официальный API data.mos.ru (apidata.mos.ru) - дополнительный источник для
# Москвы, доп. к @dtp777/@DtOperativno выше (см. fetch_mos_road_data.py).
# Ключ пользователь зарегистрировал и прислал сам (не хранится в коде) -
# передаётся через переменную окружения MOS_DATA_API_KEY на Railway. Если
# переменная не задана, источник просто молча не участвует (как и
# YANDEX_RASP_API_KEY выше по файлу).
MOS_ROAD_DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mos_road_data.json')
MOS_ROAD_DATA_UPDATE_INTERVAL_MINUTES = 10

# Афиша города (TimePad, см. fetch_timepad_data.py) - события меняются
# медленно (не по минутам, как рейсы/статусы). Обновляется ЛОКАЛЬНО (см.
# fetch_timepad_data.py - Railway не может дотянуться до TimePad, Cloudflare
# блокирует датацентровые IP), файл коммитится/пушится вручную. Пока
# покрывает только Москву.
TIMEPAD_DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'timepad_data.json')

# Погода/осадки - Open-Meteo (open-meteo.com), публичный API без ключа и
# лимита на наш объём запросов (бесплатный тариф - до 10 000 запросов/сутки,
# нам хватит 12 городов раз в RAIN_CHECK_INTERVAL_MINUTES с большим запасом).
# В отличие от Overpass (см. fetch_toilets_data.py и остальные
# fetch_*_data.py) Open-Meteo НЕ блокирует датацентровые IP - работает
# напрямую с Railway, как и fetch_favt_notices.py/fetch_road_events.py, без
# браузерного обхода.
# Координаты - центр bbox каждого города (см. CITY_BBOX в fetch_toilets_data.py
# и остальных fetch_*_data.py - тот же набор из 12 городов).
RAIN_CITY_COORDS = {
    'moscow': (55.725, 37.645),
    'spb': (59.925, 30.300),
    'novosibirsk': (55.000, 82.975),
    'ekb': (56.825, 60.600),
    'kazan': (55.775, 49.150),
    'chelyabinsk': (55.175, 61.425),
    'omsk': (54.975, 73.325),
    'samara': (53.200, 50.150),
    'rostov': (47.225, 39.700),
    'nnovgorod': (56.300, 43.950),
    'krasnodar': (45.025, 38.975),
    'sochi': (43.540, 39.800),
}
OPEN_METEO_URL = 'https://api.open-meteo.com/v1/forecast'
# Как часто опрашивать Open-Meteo по каждому городу и пересчитывать
# упреждающий пуш (см. rain_checker ниже) - тот же интервал, что и у
# остальных фоновых проверок (favt_notices/road_events).
RAIN_CHECK_INTERVAL_MINUTES = 20
# За сколько минут до начала (или усиления) осадков слать пуш - по просьбе
# пользователя: не "уже идёт", а заблаговременное предупреждение.
RAIN_LEAD_MINUTES = 30
# Прогноз на сколько часов вперёд показываем в ручном режиме (кнопка
# "🌤 Погода") - почасовая разбивка: осадки/вид осадка + температура.
RAIN_FORECAST_HOURS = 12

# Коды погоды Open-Meteo (WMO weathercode) -> (человеческое название, вес
# "силы" для сравнения при усилении, эмодзи). Вес используется в
# check_rain_transitions, чтобы решить, считать ли переход "усилением"
# (дождь -> ливень) и стоит ли из-за этого слать ДОПОЛНИТЕЛЬНЫЙ пуш, даже
# если пуш о начале осадков уже был отправлен. 0 и коды без активных осадков
# (туман, облачность) не считаются осадками вообще - PRECIP_WEATHERCODES ниже
# ограничивает список только теми кодами, что реально означают дождь/снег/град.
WEATHERCODE_INFO = {
    0: ('ясно', 0, '☀️'),
    1: ('малооблачно', 0, '🌤'),
    2: ('облачно с прояснениями', 0, '⛅'),
    3: ('пасмурно', 0, '☁️'),
    45: ('туман', 0, '🌫'),
    48: ('изморозь', 0, '🌫'),
    51: ('морось слабая', 1, '🌦'),
    53: ('морось', 2, '🌦'),
    55: ('морось сильная', 3, '🌧'),
    56: ('ледяная морось слабая', 2, '🌧'),
    57: ('ледяная морось сильная', 3, '🌧'),
    61: ('дождь слабый', 2, '🌧'),
    63: ('дождь', 3, '🌧'),
    65: ('сильный дождь', 5, '🌧'),
    66: ('ледяной дождь слабый', 3, '🌧'),
    67: ('ледяной дождь сильный', 5, '🌧'),
    71: ('снег слабый', 2, '🌨'),
    73: ('снег', 3, '🌨'),
    75: ('сильный снегопад', 5, '❄️'),
    77: ('снежная крупа', 2, '🌨'),
    80: ('ливень слабый', 3, '🌧'),
    81: ('ливень', 4, '🌧'),
    82: ('сильный ливень', 6, '⛈'),
    85: ('снежный заряд слабый', 3, '🌨'),
    86: ('снежный заряд сильный', 5, '❄️'),
    95: ('гроза', 6, '⛈'),
    96: ('гроза с градом слабая', 7, '⛈'),
    99: ('гроза с сильным градом', 8, '⛈'),
}
# Коды, которые считаем "осадками" для целей пуша/фонового мониторинга -
# всё, что не входит сюда (ясно/облачно/туман), не запускает предупреждение.
PRECIP_WEATHERCODES = {code for code, (_, weight, _) in WEATHERCODE_INFO.items() if weight > 0}

def describe_weathercode(code):
    return WEATHERCODE_INFO.get(code, ('осадки', 1, '🌧'))

# ==================== НАСТРОЙКИ ПУШЕЙ ====================
# По просьбе пользователя - каждый тип автопуша можно включить/выключить
# отдельно (кнопка "🔔 Уведомления" в "Инструменты водителя", см.
# notification_settings_keyboard/show_notification_settings ниже). Настройка
# хранится в user_state[uid]['notif_prefs'][key] - словарь key->bool, читается
# через notifications_enabled(). Отсутствие ключа = включено (все типы по
# умолчанию ON, чтобы не заставлять существующих пользователей заново всё
# включать после обновления бота).
NOTIFICATION_TYPES = {
    'weather': {'label': 'Погода/осадки', 'emoji': '🌤'},
    'airport_status': {'label': 'Статус аэропорта', 'emoji': '✈️'},
    'high_demand': {'label': 'Повышенный спрос', 'emoji': '📈'},
    'holidays': {'label': 'Праздники', 'emoji': '🎉'},
    'peak_hours': {'label': 'Часы пика', 'emoji': '📅'},
}

def notifications_enabled(state, notif_key):
    """True, если пользователь не выключал явно этот тип пуша - отсутствие
    записи в notif_prefs (новый пользователь или пуш добавлен позже, чем
    пользователь в последний раз открывал настройки) трактуется как ON."""
    if not isinstance(state, dict):
        return True
    prefs = state.get('notif_prefs') or {}
    return prefs.get(notif_key, True)

# ==================== ОЧЕРЕДЬ У АЭРОПОРТА (гео) ====================
# Идея пользователя: водитель/курьер включает в Telegram трансляцию живой
# геопозиции, бот сам считает расстояние до ближайшего аэропорта
# (nearest_airport(), см. haversine_km) и присылает 4 пуша за поездку:
# вход в AIRPORT_QUEUE_RADIUS_OUTER_KM, вход в AIRPORT_QUEUE_RADIUS_INNER_KM,
# и дальше "уже N минут рядом" на каждой отметке из AIRPORT_QUEUE_TIME_PUSHES_MIN
# - помогает не терять счёт времени в очереди на посадку у аэропорта.
# Включается/выключается кнопкой "📍 Очередь у аэропорта" в Инструментах
# водителя (обычный toggle) - НЕ добавлена в NOTIFICATION_TYPES/notif_prefs,
# потому что сам факт включения трансляции уже и есть согласие на эти пуши;
# отдельная on/off настройка была бы избыточной.
# Состояние - НЕ отдельная таблица в БД, а user_state[uid]['airport_queue']
# (dict) и user_state[uid]['airport_queue_active'] (bool) - у user_state уже
# есть персистентность (см. PersistentUserDict выше), дублировать её в SQL
# нет смысла. См. process_airport_queue_ping/check_airport_queue_timers ниже
# (рядом с push_airport_status_change).
AIRPORT_QUEUE_RADIUS_OUTER_KM = 3.0
AIRPORT_QUEUE_RADIUS_INNER_KM = 1.5
AIRPORT_QUEUE_TIME_PUSHES_MIN = (30, 60)  # "уже 30 минут рядом" / "уже 1 час рядом"
# Как часто (минуты) фоновый чекер досылает пуши по времени - основные 2
# пуша (3км/1.5км) шлются сразу по факту нового пинга геопозиции, а эти два
# идут по прошедшему времени, поэтому нужен отдельный фоновый прогон (см.
# airport_queue_checker) - иначе они бы не пришли, если юзер просто стоит на
# месте и новых пингов долго нет.
AIRPORT_QUEUE_CHECK_INTERVAL_MINUTES = 5
# Инструкция рекомендует делиться геопозицией «Пока не отключу» (бессрочно) -
# так трансляция не обрывается сама, водителю не нужно вспоминать её включить
# заново каждые несколько часов. У такого режима Telegram шлёт live_period =
# 0x7FFFFFFF (условная "бесконечность"), поэтому ориентироваться на live_period
# при определении "трансляция скорее всего закончилась" больше нельзя - вместо
# этого просто следим за тем, сколько времени НЕТ новых пингов геопозиции:
# дольше AIRPORT_QUEUE_STALE_TIMEOUT_MINUTES без единого обновления - считаем,
# что трансляция прервалась (юзер сам отключил, разрядился телефон и т.п.), и
# шлём напоминание включить её заново (см. send_airport_queue_expired_push).
AIRPORT_QUEUE_STALE_TIMEOUT_MINUTES = 30

# ==================== ПРАЗДНИКИ ====================
# Идея пользователя: праздники (особенно Новый год, 8 марта, 9 мая, День
# города) заметно поднимают спрос на такси/курьеров - люди едут в гости,
# на салюты, доставляют подарки. Два пуша на каждый праздник: один раз за
# HOLIDAY_LEAD_DAYS день(-я) до, и затем каждые HOLIDAY_DAY_CHECK_INTERVAL_
# MINUTES минут (по факту - раз в HOLIDAY_CHECK_INTERVAL_MINUTES минут, но
# фильтруется по дате) в САМ день праздника - см. holiday_checker ниже.
#
# Даты вручную, не вычисляются по формуле - потому что для Дней городов
# устойчивой формулы физически нет (администрации сами каждый год выбирают
# дату, иногда меняя даже сам принцип выбора - см. историю Екатеринбурга/
# Нижнего Новгорода). Федеральные праздники РФ фиксированы законом (кроме
# отдельных переносов выходных, которые тут не важны - переносят ВЫХОДНОЙ
# день, а не сам праздник). Список дат на 2026-2027:
# - 2026: даты Дня города подтверждены официально/СМИ на месте (см. историю
#   ресёрча) - Москва 5 сент, СПб 27 мая (фиксирован законом), Новосибирск
#   28 июня, Екатеринбург 1 авг, Казань 30 авг (фиксирован законом,
#   совпадает с Днём Татарстана), Челябинск 7 сент, Омск 1 авг, Самара
#   13 сент, Ростов-на-Дону 20 сент, Нижний Новгород 15 авг, Краснодар
#   26 сент, Сочи 30 мая.
# - 2027: федеральные даты фиксированы и надёжны. Дни городов на 2027 ещё
#   НЕ объявлены администрациями (ближе к дате скорректировать) - здесь
#   проставлена лучшая оценка по обычной традиции города (первая/последняя
#   суббота такого-то месяца и т.п.) - см. TODO у каждой такой записи.
# is_national=True - пушим ВО ВСЕХ городах; иначе - только city (ключ из
# CITY_DISPLAY_NAMES).
HOLIDAYS = [
    # --- Федеральные (2026, оставшаяся часть года) ---
    {'date': (2026, 12, 31), 'name': 'Новый год', 'emoji': '🎄', 'is_national': True},
    # --- Федеральные (2027) ---
    {'date': (2027, 1, 1), 'name': 'Новый год', 'emoji': '🎄', 'is_national': True},
    {'date': (2027, 2, 23), 'name': 'День защитника Отечества', 'emoji': '🎖', 'is_national': True},
    {'date': (2027, 3, 8), 'name': 'Международный женский день', 'emoji': '🌷', 'is_national': True},
    {'date': (2027, 5, 1), 'name': 'Праздник Весны и Труда', 'emoji': '🌱', 'is_national': True},
    {'date': (2027, 5, 9), 'name': 'День Победы', 'emoji': '🎗', 'is_national': True},
    {'date': (2027, 6, 12), 'name': 'День России', 'emoji': '🇷🇺', 'is_national': True},
    {'date': (2027, 11, 4), 'name': 'День народного единства', 'emoji': '🤝', 'is_national': True},
    {'date': (2027, 12, 31), 'name': 'Новый год', 'emoji': '🎄', 'is_national': True},
    # --- Дни городов (2026) ---
    {'date': (2026, 9, 5), 'name': 'День города', 'emoji': '🎉', 'city': 'moscow'},
    {'date': (2026, 9, 7), 'name': 'День города', 'emoji': '🎉', 'city': 'chelyabinsk'},
    {'date': (2026, 9, 13), 'name': 'День города', 'emoji': '🎉', 'city': 'samara'},
    {'date': (2026, 9, 20), 'name': 'День города', 'emoji': '🎉', 'city': 'rostov'},
    {'date': (2026, 9, 26), 'name': 'День города', 'emoji': '🎉', 'city': 'krasnodar'},
    # --- Дни городов (2027) - фиксированные законом (надёжно) ---
    {'date': (2027, 5, 27), 'name': 'День города', 'emoji': '🎉', 'city': 'spb'},
    {'date': (2027, 8, 30), 'name': 'День города (День Татарстана)', 'emoji': '🎉', 'city': 'kazan'},
    # --- Дни городов (2027) - оценка по традиции, TODO сверить ближе к дате ---
    {'date': (2027, 6, 27), 'name': 'День города', 'emoji': '🎉', 'city': 'novosibirsk'},  # TODO: последнее воскресенье июня, уточнить
    {'date': (2027, 8, 7), 'name': 'День города', 'emoji': '🎉', 'city': 'ekb'},  # TODO: первая суббота августа (правило менялось в 2026), уточнить
    {'date': (2027, 8, 7), 'name': 'День города', 'emoji': '🎉', 'city': 'omsk'},  # TODO: первая суббота августа, уточнить
    {'date': (2027, 8, 21), 'name': 'День города', 'emoji': '🎉', 'city': 'nnovgorod'},  # TODO: третья суббота августа, уточнить
    {'date': (2027, 9, 4), 'name': 'День города', 'emoji': '🎉', 'city': 'moscow'},  # TODO: первые/вторые выходные сентября, уточнить
    {'date': (2027, 9, 12), 'name': 'День города', 'emoji': '🎉', 'city': 'chelyabinsk'},  # TODO: обычно ближе к 13 сентября, уточнить
    {'date': (2027, 9, 12), 'name': 'День города', 'emoji': '🎉', 'city': 'samara'},  # TODO: вторая суббота/воскресенье сентября, уточнить
    {'date': (2027, 9, 19), 'name': 'День города', 'emoji': '🎉', 'city': 'rostov'},  # TODO: третье воскресенье сентября, уточнить
    {'date': (2027, 9, 25), 'name': 'День города', 'emoji': '🎉', 'city': 'krasnodar'},  # TODO: последняя суббота сентября, уточнить
    {'date': (2027, 5, 29), 'name': 'День города', 'emoji': '🎉', 'city': 'sochi'},  # TODO: дата плавает год от года без правила, уточнить
]
# За сколько дней ДО праздника слать разовый пуш-напоминание.
HOLIDAY_LEAD_DAYS = 1
# Как часто (в минутах) проверять праздничный календарь - раз в прогон
# смотрим "есть ли праздник завтра (ровно HOLIDAY_LEAD_DAYS дней) - если
# есть и ещё не пушили - шлём разовое напоминание" и "идёт ли сегодня
# праздник - если да, шлём" (дедуп внутри дня - см. holiday_pushes_sent).
HOLIDAY_CHECK_INTERVAL_MINUTES = 60

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==================== ПРОПУСКНАЯ СПОСОБНОСТЬ ====================
# UWUU (Уфа) заменён на UWGG (Стригино, Нижний Новгород) - Уфа убрана из
# бота (см. STATIONS/nnovgorod выше). Значение для UWGG - оценка по годовому
# пассажиропотоку (~1.48 млн пасс/год, Коммерсантъ, рекордный год) в той же
# пропорции к остальным аэропортам этого списка, что и у них; поправить, если
# найдётся более точный источник или другая методика калибровки исходного
# словаря.
# AIRPORT_CAPACITY / AIRPORT_TIMEZONE / AIRPORTS_INFO / AIRPORT_COORDS
# раньше были захардкожены вручную здесь (и AIRPORTS дублировался в
# fetch_yandex_data.py) - рассинхрон таких копий уже приводил к реальному
# багу (Шереметьево показывал "все нули"). Теперь единственный источник
# правды - config.json (см. config_loader.py), эти четыре словаря строятся
# из него при импорте модуля. AIRPORT_TERMINAL_ZONES/TERMINAL_LETTER_TO_ZONE
# (терминальные зоны Шереметьево) в config.json НЕ вынесены - слишком тесно
# завязаны на код (flight_terminal_zone/compute_zone_capacity_shares),
# остаются захардкожены ниже как раньше.
AIRPORT_CAPACITY = {}
AIRPORT_TIMEZONE = {}
AIRPORT_COORDS = {}
AIRPORTS_INFO = {}
for _city_key, _city_data in _get_config_cities().items():
    _city_airports = []
    for _a in _city_data.get('airports', []):
        AIRPORT_CAPACITY[_a['icao']] = _a['capacity']
        AIRPORT_TIMEZONE[_a['icao']] = _a['timezone']
        AIRPORT_COORDS[_a['icao']] = tuple(_a['coords'])
        _entry = {'name': _a['name'], 'emoji': _a['emoji'], 'icao': _a['icao'], 'iata': _a['iata']}
        if _a.get('zone_key'):
            _entry['zone_key'] = _a['zone_key']
        if _a.get('closed'):
            _entry['closed'] = True
        _city_airports.append(_entry)
    AIRPORTS_INFO[_city_key] = _city_airports

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

# Обратный индекс ICAO -> город/данные аэропорта - нужен для пушей об
# изменении статуса аэропорта: по коду аэропорта нужно быстро понять, каким
# водителям (по выбранному городу) это разослать. Статус/ограничения
# Росавиации ОБЩИЕ на весь аэропорт (см. комментарий у SVO в AIRPORTS_INFO
# выше - Росавиация не делит по терминалам), поэтому для аэропорта с
# несколькими зональными записями (одинаковый icao, разные zone_key) сюда
# должна попасть ОДНА нейтральная запись без привязки к конкретной зоне -
# иначе пуши о статусе называли бы "Шереметьево Терминал D" даже когда речь
# не про конкретный терминал, а про весь аэропорт. Раз zone-записи всегда
# идут ПОСЛЕ первой (см. порядок в AIRPORTS_INFO), достаточно не
# перезаписывать уже существующую запись без zone_key записью с zone_key.
ICAO_TO_CITY = {}
ICAO_TO_AIRPORT = {}
for _city_key, _airports_list in AIRPORTS_INFO.items():
    for _airport in _airports_list:
        ICAO_TO_CITY[_airport['icao']] = _city_key
        if _airport['icao'] not in ICAO_TO_AIRPORT or not _airport.get('zone_key'):
            ICAO_TO_AIRPORT[_airport['icao']] = _airport

# Координаты (широта, долгота) каждого аэропорта (AIRPORT_COORDS) - открытые
# авиационные данные, нужны для фичи "Очередь у аэропорта" (см. блок
# AIRPORT_QUEUE_* ниже): по живой геопозиции водителя считаем расстояние до
# ближайшего аэропорта (haversine_km, см. nearest_airport_zone() рядом с
# haversine_km). Сами координаты - в config.json (см. блок выше, где строится
# этот словарь). UUEE (Шереметьево) - средняя точка между терминальными
# зонами, см. AIRPORT_TERMINAL_ZONES ниже - используется как фолбэк, если
# AIRPORT_TERMINAL_ZONES почему-то не задан для этого icao (не должно
# происходить, но на всякий случай).

# Шереметьево (UUEE) - по просьбе пользователя разделён на ДВЕ точки: "B/C"
# (терминалы B и C вместе, одна общая точка) и отдельно "D" (обновлено
# 19.09.2026 - до этого была короткая попытка развести B и C по отдельности,
# пользователь передумал и попросил вернуть обратно вместе). Терминалы E и F
# НЕ включены ни в одну зону и не показываются отдельной точкой - по словам
# пользователя, у Яндекс.Go парковка/очередь для водителей есть только на
# B, C и D, так что E/F не имеют смысла как отдельная точка (рейсы туда
# просто не попадают ни в одну зону - это осознанно, не баг). Терминал A -
# отдельная площадка БИЗНЕС-авиации (частные джеты), к обычным пассажирским
# рейсам не относится и в расписании Yandex Rasp практически не встречается;
# "VIP" вообще не является буквой терминала в данных API.
# Координаты зоны "B/C" - СРЕДНЯЯ точка между терминалами B (55.981271,
# 37.414241) и C (55.980579, 37.409522), по официальным адресам (2ГИС), а не
# координаты одного из них - по просьбе пользователя ("возьми среднию").
# Терминал D - отдельная территория южнее, свой подъезд с Международного
# шоссе, добраться из B/C можно только в объезд или на подземном поезде.
AIRPORT_TERMINAL_ZONES = {
    'UUEE': {
        'bc': {'coords': (55.980925, 37.4118815), 'label': 'Терминалы B/C'},
        'd': {'coords': (55.962927, 37.406064), 'label': 'Терминал D'},
    },
}

# Буква терминала из данных Yandex Rasp API (flight['terminal'], см.
# fetch_yandex_data.py) -> ключ зоны в AIRPORT_TERMINAL_ZONES[icao]. Нужно
# только для аэропортов с несколькими зонами - сейчас только UUEE. Терминал A
# (бизнес-авиация) и терминалы E/F сюда намеренно не включены - см. комментарий
# выше: A к пассажирским рейсам не относится, а E/F пользователь попросил не
# делать отдельной точкой (нет там очереди Яндекс.Go) - рейсы в эти
# терминалы просто не попадают ни в одну зону, это осознанно.
TERMINAL_LETTER_TO_ZONE = {
    'UUEE': {'B': 'bc', 'C': 'bc', 'D': 'd'},
}

def flight_terminal_zone(icao, terminal_letter):
    """Ключ зоны (см. AIRPORT_TERMINAL_ZONES) для буквы терминала конкретного
    рейса - None, если у аэропорта нет деления на зоны, буква не пришла от
    Yandex Rasp API (terminal_letter пуст/None), либо буква не входит в
    TERMINAL_LETTER_TO_ZONE (новый/неизвестный терминал - лучше молча не
    отнести рейс ни к одной зоне, чем ошибиться)."""
    if not terminal_letter:
        return None
    return TERMINAL_LETTER_TO_ZONE.get(icao, {}).get(terminal_letter.strip().upper())

def compute_zone_capacity_shares(icao):
    """Доля общей пропускной способности аэропорта (AIRPORT_CAPACITY[icao]) на
    каждую терминальную зону - вычисляется по ФАКТИЧЕСКОМУ распределению
    сегодняшних рейсов между зонами (сколько рейсов сегодня реально прилетает
    в каждую зону), а не по захардкоженной оценке - официальных цифр по
    пропускной способности именно по терминалам B/C и D по отдельности нет
    (есть только по терминалу в целом), а прикидывать было бы менее точно,
    чем считать по реальному расписанию на сегодня.

    Возвращает (shares_dict, has_data). Если у аэропорта нет зон - ({}, True).
    Если СЕГОДНЯ ни у одного рейса нет распознанного терминала (Yandex Rasp
    не прислал поле terminal ни разу, или прислал буквы вне TERMINAL_LETTER_TO_ZONE) -
    has_data=False, а shares_dict всё равно 50/50 (может использоваться как
    честный фолбэк, но вызывающий код должен ИНТЕРПРЕТИРОВАТЬ has_data=False
    как сигнал НЕ фильтровать рейсы по зоне - иначе обе зоны одновременно
    показали бы "0 рейсов", даже если рейсы реально есть, просто без
    известного терминала (см. отчёт пользователя 19.09.2026 - B/C и D
    ОДНОВРЕМЕННО показали 0% на все 8 часов, хотя аэропорт не может быть
    пуст 8 часов подряд у крупного хаба - оказалось, что terminal не пришёл
    ни у одного рейса за весь день)."""
    zones = AIRPORT_TERMINAL_ZONES.get(icao)
    if not zones:
        return {}, True
    counts = {zk: 0 for zk in zones}
    for f in get_airport_flights(icao):
        zk = flight_terminal_zone(icao, f.get('terminal'))
        if zk in counts:
            counts[zk] += 1
    total_known = sum(counts.values())
    if total_known == 0:
        share = 1.0 / len(zones)
        return {zk: share for zk in zones}, False
    return {zk: counts[zk] / total_known for zk in zones}, True

CATEGORIES = {
    'taxi': {'name': '🚕 ТАКСИ', 'tariffs': ['Эконом', 'Комфорт', 'Комфорт+', 'Минивэн']},
    'ultima': {'name': '💎 ТАКСИ ULTIMA', 'tariffs': ['Business', 'Premier', 'Elite', 'Cruise']},
    'courier': {'name': '📦 КУРЬЕР', 'tariffs': ['Пеший', 'Авто']},
    'cargo': {'name': '🚚 ГРУЗОВОЕ ТАКСИ', 'tariffs': []}
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

_road_events_cache = None
_road_events_mtime = None

def load_road_events():
    """Загружает road_events_data.json (сообщения о ДТП из @dtp777/@dtp_spb78,
    см. fetch_road_events.py)."""
    global _road_events_cache, _road_events_mtime
    try:
        mtime = os.path.getmtime(ROAD_EVENTS_DATA_FILE)
        if _road_events_cache is not None and mtime == _road_events_mtime:
            return _road_events_cache
        with open(ROAD_EVENTS_DATA_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        _road_events_cache = data
        _road_events_mtime = mtime
        return data
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка чтения road_events_data.json: {e}")
        return None

def get_road_events_for_city(city):
    """Последние сообщения о ДТП для города бота ('moscow'/'spb') - пусто,
    если для города канал не настроен или файл ещё не собран."""
    data = load_road_events()
    if not data:
        return []
    return data.get('cities', {}).get(city, [])

_concert_events_cache = None
_concert_events_mtime = None

def load_concert_events():
    """Загружает concert_events_data.json (афиша концертов из
    @concerts_moscow/@spb_conc, см. fetch_concert_events.py)."""
    global _concert_events_cache, _concert_events_mtime
    try:
        mtime = os.path.getmtime(CONCERT_EVENTS_DATA_FILE)
        if _concert_events_cache is not None and mtime == _concert_events_mtime:
            return _concert_events_cache
        with open(CONCERT_EVENTS_DATA_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        _concert_events_cache = data
        _concert_events_mtime = mtime
        return data
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка чтения concert_events_data.json: {e}")
        return None

def get_concert_events_for_city(city):
    """ВСЕ последние посты афиши концертов для города бота ('moscow'/'spb') -
    пусто, если для города канал не настроен или файл ещё не собран. Без
    фильтра по категории/актуальности - для этого см.
    get_upcoming_concert_events_for_category ниже."""
    data = load_concert_events()
    if not data:
        return []
    return data.get('cities', {}).get(city, [])

def get_upcoming_concert_events_for_category(city, category, limit=10):
    """Предстоящие события афиши концертов для КОНКРЕТНОЙ категории водителя -
    по просьбе пользователя (22.09.2026): события с ценой >= PRICE_THRESHOLD_RUB
    (см. fetch_concert_events.py) показываются И такси, И Ultima
    (price_category='all'); дешевле/бесплатные/без указанной цены - ТОЛЬКО
    такси (price_category='taxi_only') - Ultima это не интересно. Курьер/
    Грузовое такси эту кнопку вообще не видят (см. CATEGORIES_WITHOUT_EVENTS),
    поэтому фильтр здесь рассчитан только на 'taxi'/'ultima'. Событие без
    распознанной даты (start=None - разметка поста не подошла под парсер)
    пропускается: без даты нельзя понять, актуально ли оно ещё."""
    posts = get_concert_events_for_city(city)
    now_ts = datetime.now(ZoneInfo('UTC')).timestamp()
    upcoming = []
    for post in posts:
        if not post.get('start'):
            continue
        try:
            start_dt = datetime.fromisoformat(post['start'])
        except Exception:
            continue
        if start_dt.timestamp() < now_ts:
            continue
        price_category = post.get('price_category', 'taxi_only')
        if category == 'ultima' and price_category != 'all':
            continue
        upcoming.append(post)
    upcoming.sort(key=lambda p: p['start'])
    return upcoming[:limit]

_mos_road_data_cache = None
_mos_road_data_mtime = None

def load_mos_road_data():
    """Загружает mos_road_data.json (см. fetch_mos_road_data.py) - сырой
    ответ официального API data.mos.ru. Формат полей датасета ещё не
    финализирован (см. докстринг fetch_mos_road_data.py) - используется
    только чтобы не падать, если файла ещё нет или API вернул ошибку."""
    global _mos_road_data_cache, _mos_road_data_mtime
    try:
        mtime = os.path.getmtime(MOS_ROAD_DATA_FILE)
        if _mos_road_data_cache is not None and mtime == _mos_road_data_mtime:
            return _mos_road_data_cache
        with open(MOS_ROAD_DATA_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        _mos_road_data_cache = data
        _mos_road_data_mtime = mtime
        return data
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка чтения mos_road_data.json: {e}")
        return None

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
                    'terminal': flight_data.get('terminal'),
                })
            logger.info(f"✅ Загружено {len(flights)} реальных прилётов {airport_icao}")
            return flights

        # Запасной вариант - только для SVO, пока нет свежего flights_data.json
        if airport_icao == 'UUEE':
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
    # Формулировки статусов - по просьбе пользователя (было
    # "НЕ ЕХАТЬ/ЗАНЯТЬ ОЧЕРЕДЬ/ЕХАТЬ/СРОЧНО"), пороги загрузки не менялись.
    if load_percent <= 50: return 'Не ехать'
    elif load_percent <= 70: return 'Уточни очередь'
    elif load_percent <= 100: return 'Занимай очередь'
    else: return 'Срочно ехать'

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
        CREATE TABLE IF NOT EXISTS rain_state (
            city TEXT PRIMARY KEY,
            last_event_start TEXT,
            last_weight INTEGER,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS holiday_pushes_sent (
            holiday_key TEXT,
            city TEXT,
            kind TEXT,
            sent_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (holiday_key, city, kind)
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
        CREATE TABLE IF NOT EXISTS peak_hour_alerts_sent (
            city TEXT,
            target_date TEXT,
            target_hour INTEGER,
            sent_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (city, target_date, target_hour)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS green_demand_alerts_sent (
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

def save_rain_state(city, event_start, weight):
    """event_start - ISO-строка начала того события осадков, о котором уже
    отправлен пуш (или None, если сейчас ничего не запушено). weight - "сила"
    осадков на момент пуша (см. WEATHERCODE_INFO) - нужна, чтобы отличить
    усиление (дождь -> ливень, weight вырос) от простого повтора того же
    прогноза на следующем прогоне."""
    try:
        init_db()
        conn = get_db_connection()
        conn.execute(
            'INSERT INTO rain_state (city, last_event_start, last_weight, updated_at) VALUES (?, ?, ?, ?) '
            'ON CONFLICT(city) DO UPDATE SET last_event_start = excluded.last_event_start, '
            'last_weight = excluded.last_weight, updated_at = excluded.updated_at',
            (city, event_start, weight, datetime.now(ZoneInfo('UTC')).strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"❌ Не удалось сохранить состояние осадков для {city}: {e}")

def load_all_rain_states():
    """Последнее запушенное событие осадков по каждому городу (время начала +
    сила) - хранится в БД по тому же принципу, что и load_all_airport_statuses():
    рестарт/редеплой бота не должен считаться поводом для нового пуша."""
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT city, last_event_start, last_weight FROM rain_state')
    rows = cursor.fetchall()
    conn.close()
    return {city: (event_start, weight) for city, event_start, weight in rows}

def was_holiday_push_sent(holiday_key, city, kind):
    """kind - 'lead' (за HOLIDAY_LEAD_DAYS до) или 'day' (в сам день, дедуп
    внутри ОДНОГО дня - см. mark_holiday_push_sent/holiday_checker). city -
    ключ города или 'ALL' для национального праздника (используем единую
    запись вместо 12 отдельных, т.к. рассылка национального праздника всё
    равно идёт одним проходом по всем городам сразу)."""
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT 1 FROM holiday_pushes_sent WHERE holiday_key = ? AND city = ? AND kind = ?',
        (holiday_key, city, kind)
    )
    row = cursor.fetchone()
    conn.close()
    return row is not None

def mark_holiday_push_sent(holiday_key, city, kind):
    try:
        init_db()
        conn = get_db_connection()
        conn.execute(
            'INSERT OR IGNORE INTO holiday_pushes_sent (holiday_key, city, kind) VALUES (?, ?, ?)',
            (holiday_key, city, kind)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"❌ Не удалось отметить пуш о празднике ({holiday_key}, {city}, {kind}): {e}")

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

def was_green_demand_alert_sent(icao, relevant_class, target_date, target_hour):
    """Тот же дедуп-паттерн, что was_high_demand_alert_sent, но отдельная
    таблица для зелёного уровня (71-100%, "Занимай очередь") - чтобы не
    конфликтовать с дедупом фиолетового уровня по тому же (icao,
    relevant_class, дата, час): это разные пуши с разными условиями и должны
    дедуплицироваться независимо."""
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT 1 FROM green_demand_alerts_sent WHERE icao=? AND relevant_class=? AND target_date=? AND target_hour=?',
        (icao, relevant_class, target_date, target_hour)
    )
    row = cursor.fetchone()
    conn.close()
    return row is not None

def mark_green_demand_alert_sent(icao, relevant_class, target_date, target_hour):
    init_db()
    conn = get_db_connection()
    conn.execute(
        'INSERT OR IGNORE INTO green_demand_alerts_sent (icao, relevant_class, target_date, target_hour, sent_at) VALUES (?, ?, ?, ?, ?)',
        (icao, relevant_class, target_date, target_hour, datetime.now(ZoneInfo('UTC')).strftime('%Y-%m-%d %H:%M:%S'))
    )
    conn.commit()
    conn.close()

def cleanup_old_green_demand_alerts():
    """Чистим отметки старше 2 дней - тот же принцип, что cleanup_old_high_demand_alerts."""
    try:
        init_db()
        conn = get_db_connection()
        cutoff = (datetime.now(ZoneInfo('UTC')) - timedelta(days=2)).strftime('%Y-%m-%d')
        conn.execute('DELETE FROM green_demand_alerts_sent WHERE target_date < ?', (cutoff,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"❌ Не удалось почистить green_demand_alerts_sent: {e}")

def was_peak_hour_alert_sent(city, target_date, target_hour):
    """Тот же дедуп-паттерн, что was_high_demand_alert_sent, но по (город,
    дата, час начала пика) - пик один на весь город, не привязан к
    конкретному аэропорту/классу, поэтому таблица/ключ проще."""
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT 1 FROM peak_hour_alerts_sent WHERE city=? AND target_date=? AND target_hour=?',
        (city, target_date, target_hour)
    )
    row = cursor.fetchone()
    conn.close()
    return row is not None

def mark_peak_hour_alert_sent(city, target_date, target_hour):
    init_db()
    conn = get_db_connection()
    conn.execute(
        'INSERT OR IGNORE INTO peak_hour_alerts_sent (city, target_date, target_hour, sent_at) VALUES (?, ?, ?, ?)',
        (city, target_date, target_hour, datetime.now(ZoneInfo('UTC')).strftime('%Y-%m-%d %H:%M:%S'))
    )
    conn.commit()
    conn.close()

def cleanup_old_peak_hour_alerts():
    """Чистим отметки старше 2 дней - тот же принцип, что cleanup_old_high_demand_alerts."""
    try:
        init_db()
        conn = get_db_connection()
        cutoff = (datetime.now(ZoneInfo('UTC')) - timedelta(days=2)).strftime('%Y-%m-%d')
        conn.execute('DELETE FROM peak_hour_alerts_sent WHERE target_date < ?', (cutoff,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"❌ Не удалось почистить peak_hour_alerts_sent: {e}")

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
        # На боте где-то раньше (вручную или другим запуском) был включён
        # webhook - Telegram не даёт одновременно webhook и getUpdates
        # (polling, см. dp.start_polling ниже), из-за чего в логах Railway
        # сыпался TelegramConflictError "can't use getUpdates method while
        # webhook is active". Снимаем webhook явно при каждом старте - если
        # его и не было, вызов просто ничего не делает (безопасно вызывать
        # всегда), а если был - синхронизирует бота обратно на polling.
        try:
            webhook_info = await bot.get_webhook_info()
            if webhook_info.url:
                logger.warning(f"⚠️ Обнаружен активный webhook ({webhook_info.url}) - удаляю, бот работает через polling")
                await bot.delete_webhook(drop_pending_updates=False)
                logger.info("✅ Webhook удалён")
        except Exception as e:
            logger.error(f"❌ Не удалось проверить/удалить webhook: {e}")
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

# Ещё одна внешняя ссылка того же типа (кнопка -> открывает чат/страницу
# стороннего сервиса напрямую, без интеграции с данными бота) - по просьбе
# пользователя, реферальная ссылка на бота VPN-сервиса.
VPN_BOT_URL = "https://t.me/Vpntaxihelper_bot?start=633742909"

# Ещё один сторонний бот того же типа (кнопка -> открывает чат напрямую,
# БЕЗ интеграции с данными) - @Yan_rus_bot показывает коэффициент
# повышенного спроса (surge/"кэф") по зонам города. По просьбе пользователя
# рассматривали встроить это напрямую в Taxi Helper, но технической
# возможности нет: сам сторонний бот получает эти данные через авторизацию
# в личном кабинете водителя Яндекс.Про (эмуляция внутреннего API Яндекс
# Такси) - это не публичный API, а обход системы Яндекса, чреватый банами
# аккаунтов и нарушением условий использования. Такое в Taxi Helper не
# делаем ни для одного, ни для нескольких аккаунтов - вместо этого просто
# кнопка-ссылка на сторонний бот, тем же паттерном, что FUEL_BOT_URL/
# VPN_BOT_URL выше - пользователь сам переходит в его чат.
KEF_BOT_URL = "https://t.me/Yan_rus_bot"

# ==================== ЧАСЫ ПИКА ПО ДНЯМ НЕДЕЛИ ====================
# Общероссийская модель спроса такси по дням недели/часам - ОДНА и та же
# для всех 12 городов бота (структура рабочего дня - офисы к 9:00, конец
# в 18:00 - общероссийский паттерн, не специфичный для города; открытых
# данных с разбивкой по конкретным городам не нашлось). Час считается по
# МЕСТНОМУ времени каждого города (см. get_city_now ниже), не по Москве -
# так же, как AIRPORT_TIMEZONE/get_airport_now для рейсов.
#
# Источники (подтверждено пользователем по опыту водителей):
# - https://taxi.yandex.ru/blog/kak-perekhitrit-chas-pik/ (блог Яндекс.Такси) -
#   утренний пик 8:30-8:45, вечерний 18:00-18:50, пятница/суббота ночью
#   22:00-03:00 с локальными всплесками в первые 10-20 минут после целого
#   часа (0:10, 1:10, 2:20).
# - https://vc.ru/transport/57190 (анализ заказов по России) - будни (пн-чт)
#   дают ~13% заказов в день каждый, пятница/суббота ~16%, воскресенье ~14%;
#   в будни пиковые часы 8, 9, 18 (по 6% от заказов дня), минимум 1-6 утра
#   (не более 2%); в выходные пик смещается на 22:00 (6.2%), утреннего пика
#   нет, рост начинается с 10:00, после 3 ночи спрос падает до 2%.
#
# WEEKDAY_HOUR_LOAD: для каждого дня недели (0=понедельник...6=воскресенье)
# список (час_начала, час_конца, уровень, подпись) - уровень: 'low'/'mid'/
# 'high'/'peak', используется и для эмодзи, и для цвета в тексте. Диапазоны
# НЕ обязаны покрывать все 24 часа - часы вне списка показываются как
# обычный/средний спрос без отдельной строки (не загромождаем вывод).
PEAK_LEVEL_EMOJI = {'low': '🟢', 'mid': '🔵', 'high': '🟡', 'peak': '🔴'}
PEAK_LEVEL_LABEL = {'low': 'низкий спрос', 'mid': 'обычный спрос', 'high': 'повышенный спрос', 'peak': 'час пик'}

_WEEKDAY_PATTERN_WORKDAY = [  # понедельник-четверг - стабильный паттерн будня
    (1, 6, 'low', 'ночной минимум'),
    (8, 9, 'peak', 'утренний час пик'),
    (9, 10, 'high', 'утро, спрос ещё повышен'),
    (18, 19, 'peak', 'вечерний час пик'),
    (19, 20, 'high', 'вечер, спрос ещё повышен'),
]
_WEEKDAY_PATTERN_FRIDAY = [  # пятница - паттерн буднего дня + ночной пик "перед выходными"
    # Час 00:00-01:00 намеренно НЕ включён сюда - технически это начало самой
    # пятницы (а не конец ночи с четверга), в будний паттерн он и так попадает
    # под общий ночной минимум ниже. Пик "ночь пятницы" 22:00-24:00 относится
    # к вечеру САМОЙ пятницы - его "продолжение" после полуночи (уже суббота)
    # см. в начале _WEEKDAY_PATTERN_SATURDAY.
    (1, 6, 'low', 'ночной минимум'),
    (8, 9, 'peak', 'утренний час пик'),
    (9, 10, 'high', 'утро, спрос ещё повышен'),
    (18, 19, 'peak', 'вечерний час пик'),
    (19, 20, 'high', 'вечер, спрос ещё повышен'),
    (22, 24, 'peak', 'ночь пятницы - высокий спрос'),
]
_WEEKDAY_PATTERN_SATURDAY = [  # суббота - нет утреннего пика, вечер/ночь смещены
    (0, 3, 'peak', 'ночь - высокий спрос (после пятницы)'),
    (3, 10, 'low', 'раннее утро - минимум'),
    (22, 24, 'peak', 'ночь субботы - высокий спрос'),
]
_WEEKDAY_PATTERN_SUNDAY = [  # воскресенье - самый спокойный день, без ночного разгула
    (0, 3, 'high', 'ночь - спрос ещё повышен (после субботы)'),
    (3, 10, 'low', 'раннее утро - минимум'),
    (18, 22, 'mid', 'вечер - спрос чуть выше обычного'),
]

WEEKDAY_HOUR_LOAD = {
    0: _WEEKDAY_PATTERN_WORKDAY,   # понедельник
    1: _WEEKDAY_PATTERN_WORKDAY,   # вторник
    2: _WEEKDAY_PATTERN_WORKDAY,   # среда
    3: _WEEKDAY_PATTERN_WORKDAY,   # четверг
    4: _WEEKDAY_PATTERN_FRIDAY,    # пятница
    5: _WEEKDAY_PATTERN_SATURDAY,  # суббота
    6: _WEEKDAY_PATTERN_SUNDAY,    # воскресенье
}
WEEKDAY_NAMES = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']

def get_city_now(city):
    """Текущее время в часовом поясе города - переиспользует AIRPORT_TIMEZONE
    через первый аэропорт города (все города бота однозонные по времени -
    даже там, где несколько аэропортов/зон Шереметьево, часовой пояс один
    и тот же на весь город), тем же паттерном, что get_airport_now."""
    airports = AIRPORTS_INFO.get(city) or []
    tz_name = AIRPORT_TIMEZONE.get(airports[0]['icao'], 'Europe/Moscow') if airports else 'Europe/Moscow'
    try:
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        return datetime.now()

def format_peak_hours_text(city, target_weekday=None):
    """Текст с часами пика для города. target_weekday=None - текущий день
    (по местному времени города); 0-6 - конкретный день недели (для кнопок
    "смотреть другой день"). Показывает ТОЛЬКО заданные в WEEKDAY_HOUR_LOAD
    диапазоны - часы вне списка не перечисляются (обычный/средний спрос,
    отдельная строка не нужна)."""
    now = get_city_now(city)
    weekday = target_weekday if target_weekday is not None else now.weekday()
    city_name = CITY_DISPLAY_NAMES.get(city, city)
    pattern = WEEKDAY_HOUR_LOAD[weekday]

    lines = [f"📅 *Часы пика — {city_name}, {WEEKDAY_NAMES[weekday]}*\n"]
    for start_h, end_h, level, label in pattern:
        emoji = PEAK_LEVEL_EMOJI[level]
        if start_h < end_h:
            time_range = f"{start_h:02d}:00–{end_h:02d}:00"
        else:  # диапазон через полночь (например, 22-24 + 0-1 показаны отдельными строками - не переносим через день)
            time_range = f"{start_h:02d}:00–{end_h:02d}:00"
        lines.append(f"{emoji} {time_range} — {label}")

    if target_weekday is None:
        cur_hour = now.hour
        cur_level = None
        for start_h, end_h, level, label in pattern:
            if start_h <= cur_hour < end_h:
                cur_level = level
                break
        if cur_level:
            lines.append(f"\n_Сейчас ({now.strftime('%H:%M')}): {PEAK_LEVEL_EMOJI[cur_level]} {PEAK_LEVEL_LABEL[cur_level]}_")
        else:
            lines.append(f"\n_Сейчас ({now.strftime('%H:%M')}): {PEAK_LEVEL_EMOJI['mid']} {PEAK_LEVEL_LABEL['mid']}_")

    lines.append(
        "\n_Общая модель по данным Яндекс.Такси и статистике заказов по России - "
        "ориентир, не точный прогноз для конкретной минуты. Погода (дождь/снег) "
        "может резко повысить спрос вне этих часов._"
    )
    return '\n'.join(lines)

def get_current_peak_level(city):
    """Уровень спроса ('low'/'mid'/'high'/'peak') ПРЯМО СЕЙЧАС по местному
    времени города, по той же модели WEEKDAY_HOUR_LOAD, что и
    format_peak_hours_text - переиспользуется в компоновке "Куда ехать"
    (see WHERE_TO_GO_* ниже), чтобы не дублировать поиск текущего диапазона."""
    now = get_city_now(city)
    pattern = WEEKDAY_HOUR_LOAD[now.weekday()]
    for start_h, end_h, level, _label in pattern:
        if start_h <= now.hour < end_h:
            return level
    return 'mid'

PEAK_HOUR_PUSH_LEAD_MINUTES = 30  # за сколько минут до начала уведомляем - см. peak_hour_alert_checker ниже

def find_upcoming_peak_start(city, lead_minutes=PEAK_HOUR_PUSH_LEAD_MINUTES):
    """Ищет ближайшее НАЧАЛО диапазона уровня 'peak' (час пик, не просто
    high/mid) в пределах lead_minutes от текущего момента - для пуша "через
    30 минут начинается час пик". Проверяет только СЕГОДНЯШНИЙ день недели
    и завтрашний (на случай, если пик начинается сразу после полуночи,
    например суббота 00:00 - конец пятницы) - lead_minutes всегда меньше
    часа, этого достаточно, дальше можно не смотреть.

    Возвращает dict {target_date, target_hour, label} или None. target_date/
    target_hour - для дедупа (see was_peak_hour_alert_sent), должны совпадать
    с калиндарной датой/часом НАЧАЛА диапазона (а не датой "сейчас")."""
    now = get_city_now(city)
    window_end = now + timedelta(minutes=lead_minutes)

    for check_date, weekday in ((now, now.weekday()), (window_end, window_end.weekday())):
        pattern = WEEKDAY_HOUR_LOAD[weekday]
        for start_h, end_h, level, label in pattern:
            if level != 'peak':
                continue
            # Момент начала диапазона в системе того же дня, что и check_date -
            # start_h может быть 0 (полночь) для диапазонов, начинающихся
            # сразу после смены дня (см. _WEEKDAY_PATTERN_SATURDAY).
            start_dt = check_date.replace(hour=start_h, minute=0, second=0, microsecond=0)
            if now < start_dt <= window_end:
                return {
                    'target_date': start_dt.strftime('%Y-%m-%d'),
                    'target_hour': start_h,
                    'label': label,
                    'start_dt': start_dt,
                }
    return None

def peak_hours_weekday_keyboard(current_weekday):
    """Инлайн-кнопки переключения дня недели - 7 кнопок, текущий день
    отмечен, остальные ведут на peak_day_{0-6}."""
    buttons = []
    row = []
    for i, name in enumerate(WEEKDAY_NAMES):
        label = f"• {name[:2]} •" if i == current_weekday else name[:2]
        row.append(InlineKeyboardButton(text=label, callback_data=f"peak_day_{i}"))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def services_keyboard(category=None, city=None):
    # Итоговый набор кнопок меню услуг (по заданному порядку). "Заказы
    # города" (было "Повышенный спрос") убрана по просьбе пользователя - была
    # заглушкой без своей логики. "Дорожные события" тоже пока без
    # обработчика - как было. "🎭 События города" (афиша TimePad) - только
    # у Такси/Ultima, курьеру и грузовому такси не актуальна (см.
    # CATEGORIES_WITHOUT_EVENTS). "✈️🚆 Транспорт" объединяет аэропорты и
    # вокзалы в одну кнопку главного меню (короче список) - при нажатии
    # show_transport_menu показывает инлайн-подменю с двумя вариантами;
    # "🚆 Вокзалы" внутри него виден, только если город в TRAIN_CITIES (см.
    # STATION_CITY) - иначе только "✈️ Аэропорты". "🔄 Отдать заказ" - только
    # Такси/Ultima (см. SHARED_ORDER_CATEGORIES).
    # "🧰 Инструменты водителя" - отдельный модуль (см.
    # COURIER_MODULE_CATEGORIES) с финансовым калькулятором смены +
    # заглушки под карту точек; изначально делался под курьеров, но по
    # просьбе пользователя открыт всем категориям (калькулятор дохода/км/
    # топлива/часов одинаково полезен и такси, и грузовому такси).
    # "🌤 Погода" - на верхнем уровне (не внутри "Инструменты водителя") по
    # просьбе пользователя - почасовой прогноз + автопуш за RAIN_LEAD_MINUTES
    # минут до начала осадков (см. блок "ПОГОДА / ОСАДКИ" выше по файлу).
    # Раскладка в 2 колонки (по просьбе пользователя, тот же приём, что и в
    # courier_module_keyboard) - пункты сначала собираются в плоский список
    # (с учётом всех условий по категории выше), а потом режутся по 2 в ряд,
    # так что раскладка остаётся 2-колоночной независимо от того, сколько
    # именно кнопок видно конкретной категории.
    # "🔓 Бесплатный VPN TAXI HELPER" - НЕ в общей 2-колоночной сетке ниже, а
    # отдельной строкой в самом низу (перед "← Назад"/"🏙 Выбор города") - по
    # просьбе пользователя.
    # "💰 КУДА ЕХАТЬ ➡️" - самая верхняя строка меню, отдельной строкой (по
    # просьбе пользователя) - раньше была внутри "Инструменты водителя",
    # перенесена сюда как самая важная кнопка (решает, куда именно ехать
    # прямо сейчас). Доступна только категориям с аэропортами (см.
    # CATEGORIES_WITHOUT_AIRPORTS) - сводка построена на аэропортах/вокзалах/
    # часах пика для ПАССАЖИРСКИХ поездок, для курьера/грузового такси не
    # актуальна (см. обсуждение с пользователем 19.09.2026).
    top_row = []
    if category not in CATEGORIES_WITHOUT_AIRPORTS:
        top_row.append(KeyboardButton(text="💰 КУДА ЕХАТЬ ➡️"))

    items = []
    if category in SHARED_ORDER_CATEGORIES:
        items.append("🔄 Отдать заказ")
    items.append("🌤 Погода")
    if category in COURIER_MODULE_CATEGORIES:
        items.append("🧰 Инструменты водителя")
    if category not in CATEGORIES_WITHOUT_AIRPORTS:
        items.append("✈️🚆 Транспорт")
    items.append("⛽ Где бензин")
    if category not in CATEGORIES_WITHOUT_EVENTS:
        items.append("🎭 События города")
    items.append("⛔ Дорожные события")

    buttons = []
    if top_row:
        buttons.append(top_row)
    buttons.extend(
        [KeyboardButton(text=t) for t in items[i:i + 2]]
        for i in range(0, len(items), 2)
    )
    buttons.append([KeyboardButton(text="🔓 Бесплатный VPN TAXI HELPER")])
    # "⚙️ Настройки" - самый низ меню, отдельной строкой, выше "← Назад"/
    # "🏙 Выбор города" (по просьбе пользователя, 19.09.2026). Пока внутри
    # только уведомления (см. notification_settings_keyboard); раньше кнопка
    # "🔔 Уведомления" была внутри "Инструменты водителя", теперь убрана
    # оттуда и доступна только отсюда.
    buttons.append([KeyboardButton(text="⚙️ Настройки")])
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
    "🛠 ТО транспорта",
}

# "🚻 Туалеты"/"🅿️ Парковка" переведены с заглушки на реальные точки
# (OpenStreetMap) + добавлены "🔧 Шиномонтаж"/"🚿 Мойки" - см.
# NEARBY_SERVICES/show_nearby_prompt/handle_nearby_location ниже. Кнопка
# запрашивает геолокацию, показывает ближайшие NEARBY_RESULTS_COUNT точек с
# расстоянием и часами работы (если есть в OSM) и кнопкой "Поехали" (открывает
# маршрут в Яндекс Навигаторе) на каждую.
# Раскладка в 2 колонки (по просьбе пользователя) - тексты кнопок сокращены,
# где были длинные (см. NEARBY_BUTTON_TO_KIND). "🔔 Уведомления" - отдельная
# настройка, какие типы автопушей получать (см. блок "НАСТРОЙКИ ПУШЕЙ" ниже).
def courier_module_keyboard(category=None):
    """category=None показывает "📍 Очередь у аэропорта" (совместимость со
    старыми вызовами) - по просьбе пользователя кнопка скрыта для
    courier/cargo (см. CATEGORIES_WITHOUT_AIRPORTS): эти категории не
    забирают пассажиров в аэропорту, аэропортовые пуши им не нужны - та же
    логика, что у "✈️🚆 Транспорт" в services_keyboard."""
    buttons = [
        [KeyboardButton(text="💰 Финансы"), KeyboardButton(text="📈 Спрос сейчас")],
        [KeyboardButton(text="📅 Часы пика"), KeyboardButton(text="🚻 Туалеты")],
        [KeyboardButton(text="🅿️ Парковка"), KeyboardButton(text="🔧 Шиномонтаж")],
        [KeyboardButton(text="🚿 Мойки"), KeyboardButton(text="🍷 Алкомаркеты 24ч")],
        [KeyboardButton(text="🛒 Магазины 24ч"), KeyboardButton(text="🔌 Электрозарядки")],
        [KeyboardButton(text="🛠 ТО транспорта")],
    ]
    # "💰 КУДА ЕХАТЬ ➡️" отсюда убрана - перенесена в services_keyboard как
    # верхняя строка главного меню (по просьбе пользователя, 19.09.2026).
    # "🔔 Уведомления" тоже отсюда убрана - теперь доступна через
    # "⚙️ Настройки" в главном меню (по просьбе пользователя, 19.09.2026).
    if category not in CATEGORIES_WITHOUT_AIRPORTS:
        buttons.append([KeyboardButton(text="📍 Очередь у аэропорта")])
    buttons.append([KeyboardButton(text="← Назад"), KeyboardButton(text="🏙 Выбор города")])
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=buttons)

def notification_settings_keyboard(state):
    """Инлайн-клавиатура с переключателями по каждому типу пуша (✅/☐) -
    нажатие на кнопку тоглит именно этот тип и перерисовывает клавиатуру на
    месте (см. toggle_notification_setting), без отправки нового сообщения."""
    buttons = []
    for key, info in NOTIFICATION_TYPES.items():
        mark = '✅' if notifications_enabled(state, key) else '☐'
        buttons.append([InlineKeyboardButton(
            text=f"{mark} {info['emoji']} {info['label']}",
            callback_data=f"notif_toggle_{key}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# Кнопка (текст меню) -> ключ в NEARBY_SERVICES. Тексты сокращены под
# раскладку в 2 колонки (см. courier_module_keyboard) - было "🅿️ Парковка /
# остановка", стало "🅿️ Парковка".
NEARBY_BUTTON_TO_KIND = {
    "🚻 Туалеты": 'toilets',
    "🅿️ Парковка": 'parking',
    "🔧 Шиномонтаж": 'tires',
    "🚿 Мойки": 'car_wash',
    "🍷 Алкомаркеты 24ч": 'alcohol',
    "🛒 Магазины 24ч": 'grocery24',
    "🔌 Электрозарядки": 'ev_charging',
}

NEARBY_SERVICES = {
    # 'noun' - винительный падеж мн. числа для фразы "найти ближайшие ___"
    # (show_nearby_prompt) - для этих слов совпадает с именительным (label).
    'toilets': {'file': 'toilets_data.json', 'label': 'Туалеты', 'emoji': '🚻', 'noun': 'туалеты'},
    'parking': {'file': 'parking_data.json', 'label': 'Бесплатные парковки', 'emoji': '🅿️', 'noun': 'бесплатные парковки'},
    'tires': {'file': 'tires_data.json', 'label': 'Шиномонтажи', 'emoji': '🔧', 'noun': 'шиномонтажи'},
    'car_wash': {'file': 'car_wash_data.json', 'label': 'Автомойки', 'emoji': '🚿', 'noun': 'автомойки'},
    # Алкомаркеты и магазины - по прямой просьбе пользователя показываем ТОЛЬКО
    # круглосуточные (фильтрация уже на этапе сбора, см. is_24h в
    # fetch_alcohol_data.py/fetch_grocery24_data.py) - label честно об этом говорит.
    'alcohol': {'file': 'alcohol_data.json', 'label': 'Алкомаркеты 24 часа', 'emoji': '🍷', 'noun': 'круглосуточные алкомаркеты'},
    'grocery24': {'file': 'grocery24_data.json', 'label': 'Магазины 24 часа', 'emoji': '🛒', 'noun': 'круглосуточные магазины'},
    'ev_charging': {'file': 'ev_charging_data.json', 'label': 'Электрозарядки', 'emoji': '🔌', 'noun': 'электрозарядки'},
}
NEARBY_RESULTS_COUNT = 5

# Только для kind='toilets': точки бывают не только явными туалетами
# (amenity=toilets), но и местами, где туалет почти наверняка есть, хотя явно
# не помечен в OSM - заправки/кафе/рестораны (по просьбе пользователя, см.
# fetch_toilets_data.py). ТЦ (mall) в список намеренно НЕ входит - убрано по
# прямой просьбе пользователя (туалет в ТЦ часто не быстро найти/дойти).
# Подписываем явным словом "ТУАЛЕТ" + категория заведения, чтобы сразу было
# видно, что это место, где искать туалет (а не просто список заведений) -
# по просьбе пользователя, пример: "ТУАЛЕТ Ресторан Брудер". Для явного
# amenity=toilets отдельная категория не нужна - там просто "ТУАЛЕТ".
# Старые данные (собранные до этого расширения) поля 'kind' не имеют -
# render_nearby_results ниже просто не покажет префикс, это нормально.
NEARBY_POINT_KIND_LABELS = {
    'toilet': 'ТУАЛЕТ',
    'fuel': 'ТУАЛЕТ АЗС',
    'cafe': 'ТУАЛЕТ Кафе',
    'restaurant': 'ТУАЛЕТ Ресторан',
}

_nearby_cache = {}
_nearby_mtime = {}

def load_nearby_data(kind):
    """Читает <kind>_data.json (см. fetch_toilets_data.py/fetch_parking_data.py/
    fetch_tires_data.py/fetch_car_wash_data.py) с тем же кэшем по mtime, что и
    load_road_events/load_flights_data - файлы обновляются НЕ фоновой задачей
    бота (Overpass недоступен с Railway/облака), а вручную/по расписанию с
    компьютера с доступом в обход блокировки (см. docstring фетчеров)."""
    global _nearby_cache, _nearby_mtime
    cfg = NEARBY_SERVICES[kind]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), cfg['file'])
    try:
        mtime = os.path.getmtime(path)
        if _nearby_cache.get(kind) is not None and _nearby_mtime.get(kind) == mtime:
            return _nearby_cache[kind]
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        _nearby_cache[kind] = data
        _nearby_mtime[kind] = mtime
        return data
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка чтения {cfg['file']}: {e}")
        return None

def haversine_km(lat1, lon1, lat2, lon2):
    """Расстояние по прямой между двумя точками (км) - этого достаточно для
    сортировки "ближайшие N", точный маршрут посчитает уже сам Яндекс
    Навигатор по кнопке "Поехали"."""
    r = 6371.0
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    return 2 * r * asin(sqrt(a))

def nearest_airport(lat, lon):
    """Ближайший аэропорт (из AIRPORT_COORDS) к точке (lat, lon) - основа
    фичи "Очередь у аэропорта" (см. блок AIRPORT_QUEUE_* и
    process_airport_queue_ping ниже, рядом с push_airport_status_change).
    Возвращает (icao, dist_km) или (None, None), если AIRPORT_COORDS пуст."""
    best_icao, best_dist = None, None
    for icao, (a_lat, a_lon) in AIRPORT_COORDS.items():
        dist = haversine_km(lat, lon, a_lat, a_lon)
        if best_dist is None or dist < best_dist:
            best_icao, best_dist = icao, dist
    return best_icao, best_dist

def nearest_airport_zone(lat, lon):
    """Как nearest_airport(), но для аэропортов с несколькими терминальными
    зонами (см. AIRPORT_TERMINAL_ZONES - сейчас только UUEE/Шереметьево:
    "b", "c" и "d") дополнительно определяет БЛИЖАЙШУЮ зону внутри этого
    аэропорта, а не просто центр аэропорта в целом. Логика в два шага:
    1) находим ближайший АЭРОПОРТ как раньше (nearest_airport) - это не
       меняется, у Шереметьево остаётся один ICAO-код UUEE, просто внутри
       него теперь есть под-деление;
    2) если у найденного аэропорта есть зоны в AIRPORT_TERMINAL_ZONES -
       среди НИХ отдельно ищем ближайшую и считаем расстояние уже до неё
       (точнее, чем до усреднённой точки всего аэропорта), иначе zone_key/
       zone_label = None и расстояние - как раньше, до AIRPORT_COORDS[icao].
    Возвращает (icao, dist_km, zone_key, zone_label)."""
    icao, dist_km = nearest_airport(lat, lon)
    if icao is None:
        return None, None, None, None
    zones = AIRPORT_TERMINAL_ZONES.get(icao)
    if not zones:
        return icao, dist_km, None, None
    best_zone_key, best_zone_label, best_zone_dist = None, None, None
    for zone_key, zone_data in zones.items():
        z_lat, z_lon = zone_data['coords']
        d = haversine_km(lat, lon, z_lat, z_lon)
        if best_zone_dist is None or d < best_zone_dist:
            best_zone_key, best_zone_label, best_zone_dist = zone_key, zone_data['label'], d
    return icao, best_zone_dist, best_zone_key, best_zone_label

def nearest_nearby_points(kind, city, lat, lon, count=NEARBY_RESULTS_COUNT):
    """Возвращает (расстояние_км, точка) для ближайших count точек в городе,
    отсортированные по расстоянию. None - данные вообще не собраны (файла
    нет/битый), [] - данные есть, но конкретно для этого города пока пусто
    (сбор пока покрывает не все города - см. docstring фетчеров)."""
    data = load_nearby_data(kind)
    if not data:
        return None
    points = data.get('cities', {}).get(city, [])
    if not points:
        return []
    scored = [(haversine_km(lat, lon, p['lat'], p['lon']), p) for p in points]
    scored.sort(key=lambda x: x[0])
    return scored[:count]

def yandex_navi_url(lat, lon):
    """Ссылка кнопки "Поехали" - маршрут до точки в Яндекс Навигаторе/Картах.
    ВАЖНО: раньше тут была кастомная схема "yandexnavi://build_route_on_map?..."
    - Telegram Bot API отклоняет такие ссылки в инлайн-кнопках как невалидные
    (BUTTON_URL_INVALID), из-за чего сообщение целиком не отправлялось (весь
    список ближайших точек пропадал - баг, найденный пользователем живьём:
    первое сообщение "Готово" доходило, второе с кнопками - нет). Универсальная
    ссылка yandex.ru/maps с rtext/rtt - обычный https:// URL (Telegram его
    принимает), на телефоне с установленным Яндекс Навигатором/Картами
    открывается сразу в приложении (Yandex зарегистрировал universal links на
    этот домен), иначе - в браузере как веб-версия Яндекс Карт с готовым
    маршрутом."""
    return f"https://yandex.ru/maps/?rtext=~{lat},{lon}&rtt=auto"

def nearby_location_keyboard():
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
        [KeyboardButton(text="📍 Отправить геолокацию", request_location=True)],
        [KeyboardButton(text="❌ Отмена")],
    ])

def format_nearby_distance(dist_km):
    return f"{dist_km * 1000:.0f} м" if dist_km < 1 else f"{dist_km:.1f} км"

def render_nearby_results(kind, scored_points):
    """Текст + инлайн-клавиатура с кнопкой "🚕 Поехали" на каждую из ближайших
    точек (открывает маршрут в Яндекс Навигаторе - см. yandex_navi_url).
    Цену НЕ показываем - в OSM её почти никогда нет ни для шиномонтажей, ни
    для моек (решение пользователя - показывать без неё, а не выдумывать)."""
    cfg = NEARBY_SERVICES[kind]
    lines = [f"{cfg['emoji']} *{cfg['label']} — ближайшие {len(scored_points)}*\n"]
    buttons = []
    for i, (dist_km, point) in enumerate(scored_points, start=1):
        # У немалой части точек в OSM нет названия - раньше тут стояла
        # заглушка "Без названия", по просьбе пользователя убрали: если
        # названия нет, просто не показываем его (не выдумываем и не
        # подписываем никак), только номер и расстояние. Адрес не собираем и
        # не показываем вообще - в исходных данных (OSM) его тоже нет.
        name = point.get('name')
        title = f"*{escape_md(name)}* — " if name else ""
        # Категория точки (только для "Туалеты" - см. NEARBY_POINT_KIND_LABELS):
        # ТЦ/АЗС/кафе/ресторан показываем значком ПЕРЕД названием, чтобы было
        # видно, что это не гарантированный туалет, а место, где он вероятно есть.
        kind_label = NEARBY_POINT_KIND_LABELS.get(point.get('kind'))
        prefix = f"{kind_label} " if kind_label else ""
        hours = point.get('hours')
        hours_line = f"   🕐 {escape_md(hours)}" if hours else "   🕐 часы работы не указаны"
        # Только для kind='ev_charging' (см. extract_specs в
        # fetch_ev_charging_data.py) - тип/количество/мощность разъёмов, по
        # прямой просьбе пользователя показываем сразу в списке, а не только
        # по клику. Если specs нет (не было socket:* тегов в OSM) - строку не
        # показываем вообще, как и с часами работы.
        specs = point.get('specs')
        specs_line = f"\n   🔌 {escape_md(specs)}" if specs else ""
        lines.append(f"{i}. {prefix}{title}{format_nearby_distance(dist_km)}\n{hours_line}{specs_line}")
        buttons.append([InlineKeyboardButton(
            text=f"{i}. 🚕 Поехали",
            url=yandex_navi_url(point['lat'], point['lon']),
        )])
    text = '\n\n'.join(lines)
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)

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
        state.pop('nearby_pending', None)  # на случай если "Назад" пришёл, пока ждали геолокацию
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
    await message.answer("🧰 *Инструменты водителя*\n\nВыбери раздел 👇", reply_markup=courier_module_keyboard(state.get('category')), parse_mode='Markdown')

@router.message(lambda message: message.text == "💰 Финансы" and user_state.get(message.from_user.id, {}).get('in_courier_module'))
async def start_courier_finance(message: types.Message):
    user_id = message.from_user.id
    state = user_state[user_id]
    state['courier_finance_draft'] = {'step': 'income', 'data': {}}
    await message.answer(COURIER_FINANCE_STEP_PROMPTS['income'], reply_markup=courier_finance_cancel_keyboard())

@router.message(lambda message: message.text in COURIER_STUB_SECTIONS and user_state.get(message.from_user.id, {}).get('in_courier_module'))
async def courier_stub_section(message: types.Message):
    # ТО - пока без реальных точек (нужна карта + источники данных, см.
    # README прототипа), тот же текст, что в самом HTML-прототипе на
    # экране-заглушке (data-view="soon"). Туалеты/парковка/шиномонтаж/мойки
    # больше НЕ заглушки - см. show_nearby_prompt/handle_nearby_location ниже.
    # "Спрос сейчас" тоже больше не заглушка - см. show_kef_bot ниже.
    category = user_state.get(message.from_user.id, {}).get('category')
    await message.answer("Этот раздел в разработке 🚧 — скоро будет", reply_markup=courier_module_keyboard(category))

@router.message(lambda message: message.text == "📈 Спрос сейчас" and user_state.get(message.from_user.id, {}).get('in_courier_module'))
async def show_kef_bot(message: types.Message):
    """Ссылка на стороннего бота @Yan_rus_bot (коэффициент повышенного
    спроса/"кэф" по зонам города) - тот же паттерн, что show_fuel_bot/
    show_vpn_bot: кнопка просто открывает чужой чат напрямую, без какой-либо
    интеграции с данными самого Taxi Helper (см. комментарий у KEF_BOT_URL -
    прямая интеграция потребовала бы обхода Яндекса, этого не делаем)."""
    category = user_state.get(message.from_user.id, {}).get('category')
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📈 Открыть бота с кэфом", url=KEF_BOT_URL)]
    ])
    text = (
        "📈 *Спрос сейчас*\n\n"
        "Коэффициент повышенного спроса по районам - отдельный бот. "
        "Нажми кнопку ниже, чтобы открыть его."
    )
    await message.answer(text, reply_markup=keyboard, parse_mode='Markdown')

@router.message(lambda message: message.text == "📅 Часы пика" and user_state.get(message.from_user.id, {}).get('in_courier_module'))
async def show_peak_hours(message: types.Message):
    """Часы пика по дням недели (см. блок "ЧАСЫ ПИКА ПО ДНЯМ НЕДЕЛИ" выше
    по файлу) - показывает ТЕКУЩИЙ день недели по местному времени города
    (get_city_now), с инлайн-кнопками переключения на любой другой день
    (peak_hours_weekday_keyboard -> peak_day_{0-6})."""
    user_id = message.from_user.id
    state = user_state.get(user_id, {})
    city = state.get('city')
    if not city:
        await message.answer("Сначала выбери город 🏙")
        return
    now = get_city_now(city)
    text = format_peak_hours_text(city, target_weekday=None)
    keyboard = peak_hours_weekday_keyboard(now.weekday())
    await message.answer(text, reply_markup=keyboard, parse_mode='Markdown')

@router.callback_query(lambda c: c.data.startswith('peak_day_'))
async def switch_peak_hours_day(callback_query: types.CallbackQuery):
    """Переключение дня недели на экране "Часы пика" - перерисовывает то же
    сообщение (edit_text), без "Сейчас: ..." строки (она осмысленна только
    для текущего реального дня, см. format_peak_hours_text)."""
    user_id = callback_query.from_user.id
    state = user_state.get(user_id, {})
    city = state.get('city')
    if not city:
        await callback_query.answer("Начни заново с /start", show_alert=True)
        return
    weekday = int(callback_query.data.split('_')[-1])
    if weekday < 0 or weekday > 6:
        await callback_query.answer("Ошибка!", show_alert=True)
        return
    text = format_peak_hours_text(city, target_weekday=weekday)
    keyboard = peak_hours_weekday_keyboard(weekday)
    await callback_query.message.edit_text(text, reply_markup=keyboard, parse_mode='Markdown')
    await callback_query.answer()

# ==================== "КУДА ЕХАТЬ" (сводная рекомендация) ====================
# Пока ТОЛЬКО для такси/Ultima (см. courier_module_keyboard - кнопка скрыта
# для CATEGORIES_WITHOUT_AIRPORTS). Сравнивает несколько "кандидатов" -
# каждый аэропорт города и обобщённый "Город/центр" - по условному баллу
# спроса, собранному из уже существующих источников данных бота:
#   - аэропорт: загрузка прилётов ПРЯМО СЕЙЧАС (compute_current_availability),
#     статус Росавиации (get_airport_status - штраф, если закрыт/по
#     согласованию) и живая очередь у аэропорта (queue_latest_report по ЛЮБОМУ
#     тарифу - штраф, если очередь уже большая, даже при высокой загрузке)
#   - "Город/центр": уровень часа пика (get_current_peak_level) + бонус за
#     дождь/снег (weathercode из fetch_rain_forecast, тот же источник, что
#     уже используется для пуш-уведомлений о дожде)
# Разные единицы измерения (% загрузки аэропорта vs уровень пика города) -
# ЗНАЧИТ, это не физически точный расчёт, а понятный водителю ориентир с
# объяснением "почему" - как и WEEKDAY_HOUR_LOAD выше, ориентир, не прогноз.
# "Длинная" очередь для целей этого скоринга - от 21 машины (5-й диапазон
# QUEUE_RANGES и дальше). Строится из самого QUEUE_RANGES, а не захардкожена
# отдельным списком строк - чтобы не разъехаться, если шаг/границы диапазонов
# когда-нибудь изменятся (см. QUEUE_RANGES выше по файлу).
WHERE_TO_GO_QUEUE_LONG_RANGES = {f'{lo}-{hi}' for lo, hi in QUEUE_RANGES if lo >= 21}

def pluralize_ru(n, one, few, many):
    """Русское склонение по числу: 1 рейс / 2 рейса / 5 рейсов. Стандартное
    правило падежей (11-14 - всегда "many", иначе по последней цифре)."""
    n_abs = abs(n)
    if 11 <= n_abs % 100 <= 14:
        return many
    last = n_abs % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many

def score_station_candidate(city, code, station, category):
    """Балл и обоснование для одного вокзала города - тот же принцип, что
    score_airport_candidate, но на бинарной шкале вокзалов (см.
    get_train_load_symbol/get_train_load_label) вместо 4-уровневой шкалы
    аэропортов. По просьбе пользователя (21.09.2026) вокзалы тоже участвуют
    в "Куда ехать", а не только аэропорты. score - тот же % загрузки
    текущего получаса, что показывается на кнопке вокзала в разделе
    "✈️🚆 Транспорт" - шкалы сопоставимы (обе - % от часовой ёмкости),
    прямое сравнение с score аэропорта корректно."""
    load, trains_in_period, _ = compute_current_train_period_load(code, category)
    reasons = []
    if trains_in_period > 0:
        train_word = pluralize_ru(trains_in_period, "поезд", "поезда", "поездов")
        reasons.append(f"{trains_in_period} {train_word} в этот получас")
    else:
        reasons.append("прибытий в этот получас нет")
    label = get_train_load_label(load)
    if label == 'ЕХАТЬ':
        reasons.append("стоит подъехать")
    return {'label': f"🚆 {station['name']}", 'score': load, 'reasons': reasons, 'closed': False, 'advice': None}

async def score_airport_candidate(city, airport, category):
    """Считает балл и обоснование для одного аэропорта города. Возвращает
    dict {label, score, reasons: [str, ...], closed: bool}. relevant_class -
    та же логика, что CATEGORY_TO_CLASS в остальном боте (эконом/бизнес/все)."""
    icao = airport['icao']
    zone_key = airport.get('zone_key')
    relevant_class = CATEGORY_TO_CLASS.get(category, 'total')
    reasons = []

    status, _notice = get_airport_status(icao)
    if airport.get('closed') or status == 'closed':
        return {'label': airport['name'], 'score': -1000, 'reasons': ['аэропорт закрыт'], 'closed': True, 'advice': None}

    avail = compute_current_availability(icao, relevant_class, zone_key=zone_key)
    score = avail['load']  # базовый балл - % загрузки прилётов на текущий час
    n_flights = len(avail['arrivals_now'])
    if n_flights > 0:
        flight_word = pluralize_ru(n_flights, "рейс", "рейса", "рейсов")
        reasons.append(f"{n_flights} {flight_word} в этот час")
    else:
        reasons.append("прилётов в этот час нет")

    if status == 'coordinated':
        score *= 0.7
        reasons.append("работает по согласованию")

    # Очередь - берём самую свежую отметку СРЕДИ ВСЕХ тарифов этой категории
    # на этом аэропорту (а не только тарифа, который сейчас выбран у
    # пользователя) - для сводки "куда ехать" важна общая картина, не
    # конкретный класс. Ключ в БД - НЕ просто название тарифа, а
    # "{category}:{tariff}" (см. queue_class_key) - без category без
    # тарифа тоже проверяем (вдруг отмечали без выбора конкретного тарифа).
    tariffs = CATEGORIES.get(category, {}).get('tariffs') or []
    class_keys = {category} | {f"{category}:{t}" for t in tariffs}
    worst_range = None
    for class_key in class_keys:
        range_str, _ts = queue_latest_report(city, icao, class_key)
        if range_str in WHERE_TO_GO_QUEUE_LONG_RANGES:
            worst_range = range_str
            break
    if worst_range:
        score *= 0.6
        reasons.append(f"уже большая очередь ({worst_range} машин)")

    return {'label': airport['name'], 'score': score, 'reasons': reasons, 'closed': False, 'advice': None}

# Развёрнутая рекомендация по типам заведений для "Город/центр" - по
# просьбе пользователя (21.09.2026): просто "повышенный спрос" мало что
# говорит водителю, нужен конкретный совет КУДА именно в центре ехать/
# держаться, в зависимости от дня недели и текущего уровня спроса. Пятница/
# суббота вечер-ночь - ночная жизнь (клубы/бары/рестораны/стриптиз-клубы),
# будни вечер - рестораны/бары после работы, будни утро/день - деловой
# центр/офисы/ТЦ. Формулировки водитель дал сам (пятница-суббота = ночная
# жизнь, будни = обычный деловой трафик) - остальное подобрано по аналогии,
# не строгий факт, а ориентир ("держись центра и этих заведений").
_CITY_ADVICE_WEEKEND_NIGHT = (
    "ночь клубов, баров и ресторанов - держись центра и заведений ночной "
    "жизни (клубы, бары, рестораны, стриптиз-клубы): люди разъезжаются "
    "поздно и часто берут такси именно от входа"
)
_CITY_ADVICE_WEEKEND_EVENING = (
    "вечер пятницы/субботы - люди едут в центр в рестораны, бары и клубы: "
    "держись районов с заведениями, спрос будет расти к ночи"
)
_CITY_ADVICE_WORKDAY_EVENING = (
    "вечер буднего дня - спрос из бизнес-центров/офисов и ресторанов/баров "
    "после работы, держись делового центра"
)
_CITY_ADVICE_WORKDAY_PEAK = (
    "час пик буднего дня - основной поток из жилых районов в центр/офисы "
    "(или обратно вечером), держись крупных транспортных направлений"
)
_CITY_ADVICE_DEFAULT = "держись центра города и оживлённых районов"

def get_city_advice(city, level):
    """Развёрнутый совет ПО ТИПАМ ЗАВЕДЕНИЙ для "Город/центр", в зависимости
    от дня недели (местное время города) и текущего уровня спроса. См.
    комментарий у _CITY_ADVICE_* выше про логику. weekday 4=пятница,
    5=суббота, 6=воскресенье (как в get_city_now().weekday())."""
    now = get_city_now(city)
    weekday = now.weekday()
    hour = now.hour
    # Ночная зона пятницы/субботы переходит через полночь на следующий день
    # недели (пятница ночью -> уже суббота, суббота ночью -> уже
    # воскресенье) - поэтому проверяем ЧАСЫ ПОСЛЕ полуночи (hour < 4) у
    # СЛЕДУЮЩЕГО дня (5=суббота, 6=воскресенье), а не у самого (4,5).
    is_weekend_night_zone = (
        (weekday in (4, 5) and hour >= 22) or
        (weekday in (5, 6) and hour < 4)
    )
    is_weekend_evening = weekday in (4, 5) and 18 <= hour < 22
    if is_weekend_night_zone:
        return _CITY_ADVICE_WEEKEND_NIGHT
    if is_weekend_evening:
        return _CITY_ADVICE_WEEKEND_EVENING
    if weekday <= 3:  # будни (пн-чт)
        if level == 'peak':
            return _CITY_ADVICE_WORKDAY_PEAK
        if hour >= 18:
            return _CITY_ADVICE_WORKDAY_EVENING
    return _CITY_ADVICE_DEFAULT

async def score_city_candidate(city):
    """Балл для обобщённого "Город/центр" - на основе часа пика + погоды.
    Единицы условные (не %, как у аэропортов) - подобраны так, чтобы часы
    пика были заметно приоритетнее аэропорта со средней загрузкой, а низкий
    спрос - явно ниже почти любого аэропорта с прилётами."""
    level = get_current_peak_level(city)
    level_score = {'low': 10, 'mid': 40, 'high': 70, 'peak': 100}[level]
    reasons = [PEAK_LEVEL_LABEL[level]]

    score = level_score
    forecast = await fetch_rain_forecast(city)
    if forecast:
        current = forecast.get('current', {})
        code = current.get('weathercode')
        if code in PRECIP_WEATHERCODES:
            _name, weight, emoji = describe_weathercode(code)
            bonus = weight * 8  # вес 1-8 -> бонус 8-64 баллов
            score += bonus
            reasons.append(f"{emoji} осадки сейчас - спрос выше обычного")

    advice = get_city_advice(city, level)
    return {'label': 'Город / центр', 'score': score, 'reasons': reasons, 'closed': False, 'advice': advice}

# Концертное событие начинает давать всплеск спроса ЗА CONCERT_EVENT_LEAD_HOURS
# часов до начала (люди подъезжают заранее) и ОСТАЁТСЯ актуальным ещё
# CONCERT_EVENT_TAIL_HOURS часов после заявленного окончания (расходятся не
# все разом) - за пределами этого окна событие не показываем кандидатом в
# "Куда ехать" вообще (см. score_concert_event_candidates ниже).
CONCERT_EVENT_LEAD_HOURS = 1
CONCERT_EVENT_TAIL_HOURS = 1

def score_concert_event_candidates(city, category, limit=3):
    """Кандидаты "Куда ехать" на основе афиши концертов (см.
    get_upcoming_concert_events_for_category) - по просьбе пользователя
    (22.09.2026). Показываем только события, которые СЕЙЧАС релевантны по
    времени (см. CONCERT_EVENT_LEAD_HOURS/TAIL_HOURS выше), не всю афишу -
    иначе список "Куда ехать" был бы забит мероприятиями через неделю.
    Балл выше для события, которое вот-вот начнётся/только что началось
    (момент максимального наплыва - и подвоза, и разъезда), чем для того,
    что скоро закончится. Единицы условные, той же шкалы, что у
    score_city_candidate (peak=100), чтобы конкретное крупное событие могло
    обгонять общий уровень "Город/центр", но не обгоняло автоматически
    любой аэропорт с реальными прилётами."""
    events = get_upcoming_concert_events_for_category(city, category, limit=20)
    now_ts = datetime.now(ZoneInfo('UTC')).timestamp()
    candidates = []
    for post in events:
        try:
            start_ts = datetime.fromisoformat(post['start']).timestamp()
            end_ts = datetime.fromisoformat(post['end']).timestamp() if post.get('end') else start_ts
        except Exception:
            continue
        window_start = start_ts - CONCERT_EVENT_LEAD_HOURS * 3600
        window_end = end_ts + CONCERT_EVENT_TAIL_HOURS * 3600
        if not (window_start <= now_ts <= window_end):
            continue  # ещё рано или уже неактуально - не показываем кандидатом
        if now_ts < start_ts:
            score = 90  # скоро начнётся - подвоз гостей
        elif now_ts <= end_ts:
            score = 70  # мероприятие идёт - спрос ровнее, чем на входе/выходе
        else:
            score = 95  # уже закончилось (в пределах "хвоста") - самый пик разъезда
        reasons = ["мероприятие по афише"]
        if post.get('place'):
            reasons.append(post['place'])
        label = f"🎤 {post.get('title') or 'Мероприятие'}"
        candidates.append({'label': label, 'score': score, 'reasons': reasons, 'closed': False, 'advice': None})
    candidates.sort(key=lambda c: c['score'], reverse=True)
    return candidates[:limit]

async def compute_where_to_go(city, category):
    """Считает и сортирует всех кандидатов (аэропорты + вокзалы + актуальные
    события афиши + "Город/центр") по баллу - возвращает список dict от
    score_airport_candidate/score_station_candidate/score_concert_event_candidates/
    score_city_candidate, отсортированный по убыванию score, закрытые
    аэропорты (score=-1000) уходят в конец списка. Вокзалы добавлены по
    просьбе пользователя (21.09.2026) - только для городов из TRAIN_CITIES
    (см. STATION_CITY). Афиша концертов добавлена по просьбе пользователя
    (22.09.2026) - только события, актуальные ПРЯМО СЕЙЧАС (см.
    score_concert_event_candidates), не более 3, чтобы не забивать список."""
    candidates = []
    seen_icao = set()
    for airport in AIRPORTS_INFO.get(city, []):
        # Как и в ICAO_TO_AIRPORT - у Шереметьево несколько зональных записей
        # с одним icao (B/C и D), каждая - самостоятельный кандидат (разная
        # загрузка по зоне), дедуп не нужен, в отличие от ICAO_TO_AIRPORT.
        candidates.append(await score_airport_candidate(city, airport, category))
    if city in TRAIN_CITIES:
        trains_data = load_trains_data()
        if trains_data and trains_data.get('stations'):
            city_stations = {code: st for code, st in trains_data['stations'].items() if STATION_CITY.get(code) == city}
            for code, station in city_stations.items():
                candidates.append(score_station_candidate(city, code, station, category))
    candidates.extend(score_concert_event_candidates(city, category))
    candidates.append(await score_city_candidate(city))
    candidates.sort(key=lambda c: c['score'], reverse=True)
    return candidates

# Ранговые эмодзи для мест в списке "Остальные варианты" - по просьбе
# пользователя сделать сводку "красочнее" (21.09.2026): медаль для топ-3,
# дальше нейтральная точка. WHERE_TO_GO_RANK_EMOJI - медали 2 и 3 места
# (1 место уже показано отдельным блоком "Сейчас лучше всего" с 🏆).
WHERE_TO_GO_RANK_EMOJI = ['🥈', '🥉']

def _where_to_go_score_bar(score):
    """Условная визуальная шкала загрузки под score (0-100%+) - просто
    заполненность кружками, чисто декоративно (не привязана к 4-уровневой
    шкале аэропортов, т.к. у "Города" и вокзалов другие единицы/диапазоны).
    5 сегментов, каждый ~20 очков, максимум забивается на 5-м."""
    filled = min(5, max(0, round(score / 20)))
    return '●' * filled + '○' * (5 - filled)

def format_where_to_go_text(city, category, candidates):
    city_name = CITY_DISPLAY_NAMES.get(city, city)
    now = get_city_now(city)
    lines = [
        f"🧭✨ *КУДА ЕХАТЬ — {city_name.upper()}*",
        f"_{now.strftime('%H:%M')}, {WEEKDAY_NAMES[now.weekday()]}_",
        "━━━━━━━━━━━━━━━━━━",
    ]

    open_candidates = [c for c in candidates if not c['closed']]
    if not open_candidates:
        lines.append("\n⛔ Все аэропорты города сейчас закрыты - ориентируйся на центр города и часы пика (см. «📅 Часы пика»).")
        return '\n'.join(lines)

    best = open_candidates[0]
    reasons_str = ', '.join(best['reasons'])
    lines.append(f"\n🏆 *{best['label']}*")
    lines.append(f"{_where_to_go_score_bar(best['score'])}  _{reasons_str}_")
    if best.get('advice'):
        lines.append(f"\n💡 {best['advice'][0].upper()}{best['advice'][1:]}.")

    if len(open_candidates) > 1:
        lines.append("\n━━━━━━━━━━━━━━━━━━")
        lines.append("*Остальные варианты:*\n")
        for i, c in enumerate(open_candidates[1:]):
            rank_emoji = WHERE_TO_GO_RANK_EMOJI[i] if i < len(WHERE_TO_GO_RANK_EMOJI) else '▫️'
            lines.append(f"{rank_emoji} *{c['label']}*")
            lines.append(f"{_where_to_go_score_bar(c['score'])}  _{', '.join(c['reasons'])}_")
            # "Город/центр" не всегда занимает 1 место, но развёрнутый совет
            # по заведениям (см. get_city_advice) важен водителю в любом
            # случае - показываем его и здесь, а не только когда "Город"
            # лучший вариант (по просьбе пользователя, 21.09.2026: совет
            # пропадал, если аэропорт набирал больше баллов).
            if c.get('advice'):
                lines.append(f"💡 _{c['advice'][0].upper()}{c['advice'][1:]}._")
            lines.append("")

    closed = [c for c in candidates if c['closed']]
    if closed:
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append("⛔ Закрыто сейчас: " + ', '.join(c['label'] for c in closed))

    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append(
        "_Ориентир на основе прилётов, статуса аэропортов, очереди и часов "
        "пика - не гарантия заработка, реальный спрос может отличаться._"
    )
    return '\n'.join(lines)

@router.message(lambda message: message.text == "💰 КУДА ЕХАТЬ ➡️")
async def show_where_to_go(message: types.Message):
    user_id = message.from_user.id
    state = user_state.get(user_id, {})
    city = state.get('city')
    category = state.get('category')
    if not city:
        await message.answer("Сначала выбери город 🏙")
        return
    if category in CATEGORIES_WITHOUT_AIRPORTS:
        # Кнопка и так скрыта для этих категорий (courier_module_keyboard),
        # но хендлер матчится по тексту - на случай, если сообщение пришло
        # откуда-то ещё (например, старая клавиатура в чате).
        await message.answer("Этот раздел пока доступен только для Такси и Ultima.")
        return
    status_msg = await message.answer("🧭 Считаю варианты…")
    candidates = await compute_where_to_go(city, category)
    text = format_where_to_go_text(city, category, candidates)
    await status_msg.edit_text(text, parse_mode='Markdown')

@router.message(lambda message: message.text == "⚙️ Настройки")
async def show_notification_settings(message: types.Message):
    """"⚙️ Настройки" в главном меню (по просьбе пользователя, 19.09.2026,
    перенесена сюда из "Инструменты водителя", где была кнопкой
    "🔔 Уведомления"). Пока внутри только один раздел - настройка автопушей
    по типам (см. блок "НАСТРОЙКИ ПУШЕЙ" выше по файлу): каждый тип
    переключается отдельной инлайн-кнопкой (✅/☐), нажатие тоглит и
    перерисовывает клавиатуру на месте. Если в будущем добавятся другие
    настройки - здесь будет промежуточное меню."""
    user_id = message.from_user.id
    state = user_state.get(user_id, {})
    await message.answer(
        "🔔 *Уведомления*\n\nВыбери, какие пуши получать - нажми, чтобы включить/выключить:",
        reply_markup=notification_settings_keyboard(state),
        parse_mode='Markdown',
    )

@router.callback_query(lambda c: c.data.startswith("notif_toggle_"))
async def toggle_notification_setting(callback_query: types.CallbackQuery):
    await callback_query.answer()
    user_id = callback_query.from_user.id
    notif_key = callback_query.data[len("notif_toggle_"):]
    if notif_key not in NOTIFICATION_TYPES:
        return
    state = user_state[user_id]
    prefs = dict(state.get('notif_prefs') or {})
    prefs[notif_key] = not notifications_enabled(state, notif_key)
    state['notif_prefs'] = prefs
    await callback_query.message.edit_reply_markup(reply_markup=notification_settings_keyboard(state))

def airport_queue_enable_text():
    """Общий текст-инструкция - используется и в toggle_airport_queue_tracking
    (кнопка в Инструментах водителя), и в suggest_airport_queue_tracking (пуш
    сразу после выбора категории, см. select_category) - чтобы не разъезжались
    две копии текста. Рекомендуем «Пока не отключу» (а не «8 часов», как было
    раньше) - при таком выборе трансляция не обрывается сама вообще, водителю
    не нужно ничего вспоминать (если он всё же сам остановит её или она
    прервётся по другой причине - see send_airport_queue_expired_push)."""
    return (
        "📍 *Очередь в аэропорт для Тарифов Яндекс.Go*\n\n"
        "Как включить: скрепка 📎 → Геопозиция → *«Транслировать геопозицию»* → "
        "выбирай *«Пока не отключу»* - трансляция не оборвётся сама, и ты не "
        "пропустишь уведомления. Если сама прервётся (например разрядится "
        "телефон) - напомню включить заново.\n\n"
        "Дальше всё автоматически: как только окажешься в 3 км от аэропорта - пришлю пуш, "
        "затем на 1.5 км, и потом ещё два - через 30 минут и через 1 час, если всё ещё рядом. "
        "Помогает не терять счёт времени в очереди на получение заказа.\n\n"
        "Чтобы остановить очередь - нажми «📍 Очередь у аэропорта» в Инструментах водителя ещё раз."
    )

def enable_airport_queue_tracking(user_id):
    state = user_state[user_id]
    state['airport_queue_active'] = True
    state['airport_queue'] = {}

@router.message(lambda message: message.text == "📍 Очередь у аэропорта" and user_state.get(message.from_user.id, {}).get('in_courier_module') and user_state.get(message.from_user.id, {}).get('category') not in CATEGORIES_WITHOUT_AIRPORTS)
async def toggle_airport_queue_tracking(message: types.Message):
    """Кнопка-переключатель (toggle, без отдельного экрана): первое нажатие
    включает отслеживание живой геопозиции и объясняет, как её включить в
    самом Telegram (бот не может запросить живую геопозицию сам - только
    обычный request_location=True, разовую точку, см. handle_nearby_location
    выше - для живой трансляции юзер обязательно жмёт 📎 сам). Повторное
    нажатие выключает и сбрасывает накопленное состояние (см.
    process_airport_queue_ping/check_airport_queue_timers рядом с
    push_airport_status_change). Скрыта для courier/cargo в самой клавиатуре
    (courier_module_keyboard) - фильтр по CATEGORIES_WITHOUT_AIRPORTS в
    декораторе тут просто защита на случай, если кнопка всё же придёт
    текстом (например с уже открытой у пользователя старой клавиатуры)."""
    user_id = message.from_user.id
    state = user_state[user_id]
    category = state.get('category')
    if state.get('airport_queue_active'):
        state['airport_queue_active'] = False
        state['airport_queue'] = {}
        await message.answer("⏹ Отслеживание очереди у аэропорта остановлено.", reply_markup=courier_module_keyboard(category))
        return
    enable_airport_queue_tracking(user_id)
    await message.answer(airport_queue_enable_text(), reply_markup=courier_module_keyboard(category), parse_mode='Markdown')

def airport_queue_bonus_line(user_id, icao):
    """Необязательная строка-бонус в пуше - последняя САМООТЧЁТНАЯ отметка
    длины очереди от других водителей (см. queue_latest_report/"🚗 Занять
    очередь" - уже существующая, отдельная от геолокации фича). Если свежей
    отметки нет или класс не совпал - просто не добавляем строку, ничего не
    ломается."""
    city = ICAO_TO_CITY.get(icao)
    if not city:
        return ""
    range_str, ts = queue_latest_report(city, icao, queue_class_key(user_id))
    if not range_str:
        return ""
    local_time = format_airport_local_time(ts, icao)
    return f"\n\n🚗 Последняя отметка водителей: *{range_str}* машин в {local_time}"

def format_airport_queue_push(kind, airport, dist_km, zone_label=None):
    """zone_label - для Шереметьево (см. AIRPORT_TERMINAL_ZONES) уточняет,
    к какому именно терминальному комплексу ближе водитель ("Терминалы B/C"
    или "Терминал D") - у остальных аэропортов всегда None, текст не
    меняется."""
    name = f"{airport['emoji']} {airport['name']}"
    if zone_label:
        name += f" ({zone_label})"
    if kind == 'enter_outer':
        return f"📍 Вы примерно в {dist_km:.1f} км от {name}.\n\nОтслеживаю время рядом - напомню на 1.5 км, а дальше через 30 минут и через час, если всё ещё будете рядом."
    if kind == 'enter_inner':
        return f"📍 Вы уже в {dist_km:.1f} км от {name} - почти на месте."
    if kind == 30:
        return f"⏱ Вы уже 30 минут рядом с {name}."
    if kind == 60:
        return (
            f"⏱ Вы уже 1 час рядом с {name}.\n\n"
            f"Если уже уехали - нажми «📍 Очередь у аэропорта» в Инструментах водителя ещё раз, "
            f"чтобы остановить отслеживание и не получать лишних пушей."
        )
    return ""

async def send_airport_queue_push(user_id, icao, kind, dist_km=None, zone_label=None):
    if not bot:
        return
    airport = ICAO_TO_AIRPORT.get(icao)
    if not airport:
        return
    text = format_airport_queue_push(kind, airport, dist_km if dist_km is not None else 0, zone_label)
    text += airport_queue_bonus_line(user_id, icao)
    try:
        await bot.send_message(user_id, text, parse_mode='Markdown')
    except Exception as e:
        logger.warning(f"⚠️ Не удалось отправить пуш об очереди у аэропорта пользователю {user_id}: {e}")

async def send_airport_queue_expired_push(user_id, icao):
    """Пуш-напоминание на случай, когда трансляция геопозиции, судя по
    всему, закончилась (см. check_airport_queue_timers) - по просьбе
    пользователя, чтобы водитель не забывал включить её заново, если он
    всё ещё работает. icao может быть None (трансляция началась, но юзер
    ни разу не оказывался рядом ни с одним аэропортом) - тогда просто без
    упоминания конкретного аэропорта."""
    if not bot:
        return
    airport = ICAO_TO_AIRPORT.get(icao) if icao else None
    where = f" у {airport['emoji']} {airport['name']}" if airport else ""
    text = (
        f"📡 Трансляция геопозиции{where}, похоже, закончилась.\n\n"
        f"Если ты всё ещё на линии - включи её заново: скрепка 📎 → Геопозиция → "
        f"«Транслировать геопозицию» → *«Пока не отключу»*, чтобы не пропускать отметки "
        f"об очереди у аэропорта."
    )
    try:
        await bot.send_message(user_id, text, parse_mode='Markdown')
    except Exception as e:
        logger.warning(f"⚠️ Не удалось отправить пуш об окончании трансляции геопозиции пользователю {user_id}: {e}")

async def process_airport_queue_ping(user_id, lat, lon, live_period=None):
    """Обрабатывает один пинг геопозиции (и разовый message.location, и
    последующие edited_message.location трансляции - см. хендлеры ниже) -
    считает расстояние до ближайшего аэропорта (и, для Шереметьево, до
    ближайшей терминальной зоны - см. nearest_airport_zone/
    AIRPORT_TERMINAL_ZONES), шлёт пуш на вход в 3 км/1.5 км, обновляет
    user_state[uid]['airport_queue'] для фонового чекера (30 мин/1 час -
    см. check_airport_queue_timers)."""
    state = user_state.get(user_id)
    if not state or not state.get('airport_queue_active'):
        return
    if state.get('category') in CATEGORIES_WITHOUT_AIRPORTS:
        # Защитный случай - активная трансляция, начатая ДО смены категории
        # на courier/cargo, не должна продолжать слать аэропортовые пуши.
        return
    icao, dist_km, zone_key, zone_label = nearest_airport_zone(lat, lon)
    if icao is None:
        return
    now = datetime.now(ZoneInfo('UTC'))
    aq = dict(state.get('airport_queue') or {})
    # Смена АЭРОПОРТА или, для Шереметьево, смена ЗОНЫ (B <-> C <-> D,
    # это отдельные подъезды - водитель, переехавший из одной в другую,
    # по факту заново въезжает в радиус) - начинаем отслеживание с чистого
    # листа. У однозонных аэропортов zone_key всегда None, so сравнение
    # (icao, zone_key) для них эквивалентно старому сравнению icao.
    if (aq.get('icao'), aq.get('zone_key')) != (icao, zone_key):
        aq = {'icao': icao, 'zone_key': zone_key}
    aq['last_update_at'] = now.isoformat()
    if live_period:
        aq['live_period'] = live_period

    if dist_km <= AIRPORT_QUEUE_RADIUS_OUTER_KM:
        if not aq.get('entered_outer_at'):
            aq['entered_outer_at'] = now.isoformat()
            aq['pushed_30'] = False
            aq['pushed_60'] = False
            await send_airport_queue_push(user_id, icao, 'enter_outer', dist_km, zone_label)
        if dist_km <= AIRPORT_QUEUE_RADIUS_INNER_KM and not aq.get('entered_inner_at'):
            aq['entered_inner_at'] = now.isoformat()
            await send_airport_queue_push(user_id, icao, 'enter_inner', dist_km, zone_label)
    else:
        # Вышел за пределы внешнего радиуса - сбрасываем: при возвращении
        # отсчёт (и пуши на вход/по времени) начнётся заново.
        if aq.get('entered_outer_at'):
            aq = {'icao': icao, 'zone_key': zone_key, 'last_update_at': now.isoformat()}
            if live_period:
                aq['live_period'] = live_period

    state['airport_queue'] = aq

@router.message(lambda message: getattr(message, 'location', None) is not None and user_state.get(message.from_user.id, {}).get('airport_queue_active') and not user_state.get(message.from_user.id, {}).get('nearby_pending'))
async def handle_airport_queue_location(message: types.Message):
    """Срабатывает только на ПЕРВЫЙ пинг живой геопозиции (сама отправка -
    обычное новое сообщение); все следующие обновления той же трансляции
    приходят как edited_message, см. handle_airport_queue_location_update
    ниже - отдельный хендлер их не трогает. Поэтому именно тут (а не там)
    место для разового "геопозиция получена" - по просьбе пользователя,
    чтобы после отправки геопозиции чат не оставался без клавиатуры меню.

    БАГФИКС: "and not ...nearby_pending" в фильтре обязателен. Кнопки
    "🚻 Туалеты"/"🚿 Мойки"/и т.п. (см. show_nearby_prompt/handle_nearby_location
    ниже) просят разовую геопозицию тем же способом - обычным
    message.location. Без этого условия, если у водителя уже включена
    "Очередь у аэропорта" (airport_queue_active=True) и он ОДНОВРЕМЕННО
    ищет, например, туалеты, aiogram матчит хендлеры по порядку регистрации
    и останавливается на первом подошедшем - этот хендлер (зарегистрирован
    раньше handle_nearby_location) перехватывал сообщение с геопозицией
    целиком: вместо списка туалетов/моек пользователь видел пуш про
    аэропорт, а handle_nearby_location вообще не срабатывал. Теперь, пока
    nearby_pending активен (разовый запрос геопозиции для другой кнопки в
    процессе), эта конкретная геопозиция достаётся ЕМУ, а не отслеживанию
    очереди - следующий пинг живой трансляции (edited_message,
    handle_airport_queue_location_update ниже, её этот фильтр не касается)
    обработается как обычно."""
    user_id = message.from_user.id
    await process_airport_queue_ping(
        user_id, message.location.latitude, message.location.longitude,
        live_period=getattr(message.location, 'live_period', None),
    )
    state = user_state.get(user_id) or {}
    await message.answer(
        "📍 Геопозиция получена, слежу за расстоянием до аэропорта.",
        reply_markup=courier_module_keyboard(state.get('category')),
    )

@router.edited_message(lambda message: getattr(message, 'location', None) is not None and user_state.get(message.from_user.id, {}).get('airport_queue_active'))
async def handle_airport_queue_location_update(message: types.Message):
    """Дальнейшие обновления живой геопозиции приходят в Telegram НЕ новыми
    сообщениями, а правками (edit) первого - отдельный апдейт edited_message,
    поэтому отдельный хендлер (обычный @router.message его не ловит)."""
    await process_airport_queue_ping(
        message.from_user.id, message.location.latitude, message.location.longitude,
        live_period=getattr(message.location, 'live_period', None),
    )

@router.message(lambda message: message.text in NEARBY_BUTTON_TO_KIND and user_state.get(message.from_user.id, {}).get('in_courier_module'))
async def show_nearby_prompt(message: types.Message):
    """Нажатие на "🚻 Туалеты"/"🅿️ Парковка"/"🔧 Шиномонтаж"/
    "🚿 Мойки" - запрашивает у водителя геолокацию (кнопка request_location в
    nearby_location_keyboard). Сама выдача ближайших точек - в
    handle_nearby_location ниже, после того как Telegram пришлёт location."""
    user_id = message.from_user.id
    kind = NEARBY_BUTTON_TO_KIND[message.text]
    user_state[user_id]['nearby_pending'] = kind
    cfg = NEARBY_SERVICES[kind]
    await message.answer(
        f"{cfg['emoji']} Отправь геолокацию, чтобы найти ближайшие {cfg['noun']} 👇",
        reply_markup=nearby_location_keyboard(),
    )

@router.message(lambda message: user_state.get(message.from_user.id, {}).get('nearby_pending') and message.text == "❌ Отмена")
async def cancel_nearby_prompt(message: types.Message):
    user_id = message.from_user.id
    category = user_state[user_id].get('category')
    user_state[user_id].pop('nearby_pending', None)
    await message.answer("Отменено", reply_markup=courier_module_keyboard(category))

@router.message(lambda message: getattr(message, 'location', None) is not None and user_state.get(message.from_user.id, {}).get('nearby_pending'))
async def handle_nearby_location(message: types.Message):
    """Водитель прислал геолокацию (кнопка "📍 Отправить геолокацию") после
    show_nearby_prompt - считаем ближайшие NEARBY_RESULTS_COUNT точек по
    прямой (haversine_km) и показываем список с кнопками "Поехали" (маршрут
    в Яндекс Навигаторе на каждую). Reply-клавиатуру (нижнее меню) и инлайн-
    кнопки результатов Telegram нельзя отправить одним сообщением - поэтому
    два отдельных answer(): сначала возвращаем обычное меню инструментов,
    потом отдельным сообщением - сам список с инлайн-кнопками."""
    user_id = message.from_user.id
    state = user_state[user_id]
    kind = state.pop('nearby_pending')
    city = state.get('city')
    cfg = NEARBY_SERVICES[kind]
    lat, lon = message.location.latitude, message.location.longitude

    scored = nearest_nearby_points(kind, city, lat, lon)
    if scored is None:
        await message.answer(
            f"{cfg['emoji']} Данные по разделу «{cfg['label']}» пока не собраны - скоро добавим.",
            reply_markup=courier_module_keyboard(state.get('category')),
        )
        return
    if not scored:
        await message.answer(
            f"{cfg['emoji']} Для твоего города пока нет собранных точек «{cfg['label']}» - сбор идёт постепенно по городам, скоро дойдём и до тебя.",
            reply_markup=courier_module_keyboard(state.get('category')),
        )
        return

    text, keyboard = render_nearby_results(kind, scored)
    await message.answer("Готово 👇", reply_markup=courier_module_keyboard(state.get('category')))
    try:
        await message.answer(text, reply_markup=keyboard, parse_mode='Markdown')
    except Exception as e:
        # Уже был реальный случай: невалидная ссылка в кнопке "Поехали"
        # (кастомная схема yandexnavi://) роняла именно ЭТО сообщение молча -
        # первое ("Готово") доходило, а сам список точек пропадал без всякой
        # ошибки в чате. Больше так не должно случиться ни по какой причине -
        # если что-то всё же сломается, водитель хотя бы получит понятный
        # текст вместо тишины, а в логах будет видно, что упало и почему.
        logger.error(f"❌ Не удалось отправить список точек «{kind}» пользователю {user_id}: {e}")
        await message.answer(
            f"{cfg['emoji']} Не получилось показать список - попробуй ещё раз через минуту.",
        )

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
        await message.answer("Расчёт отменён.", reply_markup=courier_module_keyboard(state.get('category')))
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

    category = user_state.get(message.from_user.id, {}).get('category')
    await message.answer('\n'.join(lines), reply_markup=courier_module_keyboard(category), parse_mode='Markdown')

CITY_MAP = {
    "🏛️ Москва": "moscow", "🕯️ СПб": "spb", "🌲 Новосибирск": "novosibirsk",
    "🏔️ Екатеринбург": "ekb", "🎓 Казань": "kazan", "❄️ Челябинск": "chelyabinsk",
    "🌾 Омск": "omsk", "🏭 Самара": "samara", "🌊 Ростов": "rostov",
    "🏰 Нижний Новгород": "nnovgorod", "🌴 Краснодар": "krasnodar", "🏖️ Сочи": "sochi"
}

@router.message(lambda message: message.text in CITY_MAP)
async def select_city(message: types.Message):
    """ВАЖНО: фильтр - точное совпадение с текстом кнопки из city_keyboard()
    (message.text in CITY_MAP), а НЕ проверка "название города - подстрока
    где-то в тексте сообщения" (было раньше - any(city in message.text ...)).
    Раньше это означало, что город мог неожиданно смениться на любом экране,
    если пользователь просто написал в чат что-то, содержащее название
    города как часть текста - по просьбе пользователя город теперь меняется
    ТОЛЬКО явным нажатием кнопки на экране выбора города (после /start или
    "🏙 Выбор города", см. send_start_screen) - больше нигде и никогда сам не
    "слетает". Фолбэк на "moscow" по умолчанию тоже убран - раз попадание
    сюда теперь возможно только по точному совпадению кнопки из CITY_MAP,
    city_map.get(...) всегда находит город, запасной вариант был не нужен и
    маскировал бы реальную ошибку, если бы вдруг не нашёл."""
    city = CITY_MAP[message.text]
    user_state[message.from_user.id] = {'city': city}
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

    # По просьбе пользователя - сразу после выбора категории предлагаем
    # включить "Очередь у аэропорта" (для тех категорий, кому она вообще
    # видна - см. CATEGORIES_WITHOUT_AIRPORTS), чтобы бот корректно отображал
    # очередь с первой же поездки, а не только если водитель сам вспомнит
    # про эту кнопку внутри "Инструменты водителя". Кнопка "Включить сейчас"
    # сразу активирует отслеживание (enable_airport_queue_tracking) - см.
    # enable_airport_queue_now ниже.
    if selected_category and selected_category not in CATEGORIES_WITHOUT_AIRPORTS:
        suggest_text = (
            "📍 Чтобы бот мог правильно показывать очередь у аэропорта, включи "
            "трансляцию живой геопозиции - тогда уведомления о подъезде к аэропорту "
            "и времени ожидания будут приходить автоматически."
        )
        suggest_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📍 Включить сейчас", callback_data="airport_queue_enable_now")]
        ])
        await message.answer(suggest_text, reply_markup=suggest_kb)

@router.callback_query(lambda c: c.data == "airport_queue_enable_now")
async def enable_airport_queue_now(callback_query: types.CallbackQuery):
    """Кнопка "📍 Включить сейчас" на подсказке сразу после выбора категории
    (см. select_category выше) - включает отслеживание, не заставляя
    водителя идти искать кнопку в "Инструменты водителя" отдельно."""
    user_id = callback_query.from_user.id
    state = user_state.get(user_id)
    if not state or state.get('category') in CATEGORIES_WITHOUT_AIRPORTS:
        await callback_query.answer()
        return
    await callback_query.answer("Включено")
    enable_airport_queue_tracking(user_id)
    await callback_query.message.answer(airport_queue_enable_text(), parse_mode='Markdown')

@router.message(lambda message: message.text == "🌤 Погода")
async def show_weather_forecast(message: types.Message):
    """Ручной просмотр погоды по своему городу - почасовая разбивка (вид
    осадков + температура) на RAIN_FORECAST_HOURS часов вперёд. Второй режим
    фичи - помимо автопуша на скорое начало/усиление осадков (см.
    push_rain_alert/rain_checker выше). Тот же источник (Open-Meteo), что и
    у фоновой проверки, просто без записи состояния в БД - разовый запрос
    по кнопке."""
    user_id = message.from_user.id
    state = user_state.get(user_id, {})
    city = state.get('city')
    category = state.get('category')
    if not city or city not in RAIN_CITY_COORDS:
        await message.answer("Сначала выбери город 🏙")
        return
    city_name = CITY_DISPLAY_NAMES.get(city, city)
    forecast = await fetch_rain_forecast(city)
    text = format_weather_forecast_text(city_name, forecast)
    await message.answer(text, parse_mode='Markdown', reply_markup=services_keyboard(category, city))

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

@router.message(lambda message: message.text == "🔓 Бесплатный VPN TAXI HELPER")
async def show_vpn_bot(message: types.Message):
    """Ссылка на стороннего VPN-бота (реферальная, VPN_BOT_URL) - тот же
    паттерн, что и show_fuel_bot выше: кнопка просто открывает чужой чат,
    без какой-либо интеграции с данными самого Taxi Helper. Название
    "TAXI HELPER" - по просьбе пользователя должно фигурировать везде, где
    упоминается эта кнопка (текст кнопки, заголовок сообщения, инлайн-кнопка)."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔓 Открыть VPN TAXI HELPER", url=VPN_BOT_URL)]
    ])
    text = (
        "🔓 *Бесплатный VPN TAXI HELPER*\n\n"
        "Нажми кнопку ниже, чтобы открыть бота и подключить VPN."
    )
    await message.answer(text, reply_markup=keyboard, parse_mode='Markdown')

ROAD_EVENTS_CHANNEL_LINKS = {
    'moscow': ('https://t.me/dtp777', 'Москва'),
    'spb': ('https://t.me/dtp_spb78', 'Санкт-Петербург'),
}
ROAD_EVENTS_SHOW_COUNT = 5  # сколько последних сообщений пересылать в чат за раз
ROAD_EVENTS_LOOKBACK_HOURS_LABEL = "6 часов"  # для текста в чате - см. LOOKBACK_HOURS в fetch_road_events.py

_URL_RE = re.compile(r'(?:https?://|(?:www\.)?t\.me/|(?:www\.)?telegram\.me/)\S+', re.IGNORECASE)

def strip_urls_for_display(text):
    """Доп. страховка перед отправкой в чат: вырезает любую ссылку (в т.ч.
    голую t.me/... без протокола), которая могла проскочить очистку в
    fetch_road_events.py - иначе Telegram сам подставляет превью-карточку
    канала ("Проголосуйте за канал") поверх нашего сообщения."""
    if not text:
        return text
    return re.sub(r'\n{3,}', '\n\n', _URL_RE.sub('', text)).strip()

def format_road_event_time(iso_time, city):
    """Время сообщения в часовом поясе города (те же EVENT_CITY_TIMEZONE, что
    и у афиши города), в формате ЧЧ:ММ."""
    try:
        dt = datetime.fromisoformat(iso_time)
        tz_name = EVENT_CITY_TIMEZONE.get(city)
        if tz_name:
            dt = dt.astimezone(ZoneInfo(tz_name))
        return dt.strftime('%H:%M')
    except Exception:
        return ''

@router.message(lambda message: message.text == "⛔ Дорожные события")
async def show_road_events(message: types.Message):
    """ДТП и дорожные происшествия по городам - пересылаем сами тексты
    последних сообщений из публичных Telegram-каналов (Москва -> @dtp777 +
    @DtOperativno слиты в одну ленту, СПб -> @dtp_spb78), а не просто даём
    ссылку на канал. Источник данных -
    road_events_data.json, который в фоне обновляет road_events_updater()
    (см. fetch_road_events.py - парсинг публичной веб-версии канала, тот же
    способ, что уже используется для уведомлений Росавиации @favt_info;
    там же фильтр "только про ДТП/перекрытия/аварии" и окно 6 часов).
    disable_web_page_preview=True на всех answer() ниже - без этого Telegram
    иногда сам подтягивал превью-карточку канала ("Проголосуйте за канал")
    по ссылке, которая могла всплыть в тексте поста. Кнопки/ссылки на сам
    канал нарочно НЕТ нигде в этом хендлере (было раньше - убрано по
    просьбе пользователя: не подсвечивать переход в канал вообще, только
    сами новости). Для городов без канала - текст-заглушка."""
    state = user_state.get(message.from_user.id, {})
    city = state.get('city')
    channel = ROAD_EVENTS_CHANNEL_LINKS.get(city)

    if not channel:
        await message.answer(
            "⛔ *Дорожные события*\n\nДля этого города канал с ДТП пока не подключен.",
            reply_markup=services_keyboard(state.get('category'), city),
            parse_mode='Markdown',
            disable_web_page_preview=True,
        )
        return

    _, city_name = channel
    events = get_road_events_for_city(city)

    if not events:
        await message.answer(
            f"⛔ *Дорожные события — {city_name}*\n\n"
            f"За последние {ROAD_EVENTS_LOOKBACK_HOURS_LABEL} новых ДТП/перекрытий не было, "
            "либо данные ещё не собраны.",
            parse_mode='Markdown',
            disable_web_page_preview=True,
        )
        return

    lines = [f"⛔ *Дорожные события — {city_name}* (за последние {ROAD_EVENTS_LOOKBACK_HOURS_LABEL})\n"]
    for event in events[:ROAD_EVENTS_SHOW_COUNT]:
        time_str = format_road_event_time(event.get('time', ''), city)
        text = escape_md(strip_urls_for_display(event.get('text', '').strip()))
        prefix = f"🕐 {time_str}\n" if time_str else ""
        lines.append(f"{prefix}{text}")
    message_text = '\n\n'.join(lines)

    # Telegram режет сообщение на 4096 символах - при большом количестве
    # длинных постов подряд подрезаем, чтобы не словить ошибку отправки.
    if len(message_text) > 4000:
        message_text = message_text[:4000] + "…"

    await message.answer(
        message_text,
        parse_mode='Markdown',
        disable_web_page_preview=True,
    )


def build_concert_event_message(post, city):
    """Текст + инлайн-кнопки для ОДНОГО структурированного концертного
    события (см. parse_event_fields в fetch_concert_events.py) - тот же стиль,
    что build_event_message у TimePad. "🚗 Поехали" - поиск по названию места
    на Яндекс.Картах (как и у TimePad, координат у канала нет, только текстовое
    название площадки/адреса); показывается только если место распознано."""
    tz = ZoneInfo(EVENT_CITY_TIMEZONE.get(city, 'Europe/Moscow'))
    lines = [f"🎤 *{post.get('title') or 'Мероприятие'}*"]
    place = post.get('place')
    if place:
        lines.append(f"📍 {place.capitalize()}")
    try:
        start_dt = datetime.fromisoformat(post['start']).astimezone(tz)
        end_dt = datetime.fromisoformat(post['end']).astimezone(tz) if post.get('end') else None
        if start_dt.date() == (end_dt.date() if end_dt else start_dt.date()) and end_dt:
            date_str = f"{start_dt.strftime('%d.%m, %H:%M')}–{end_dt.strftime('%H:%M')} (окончание ориентировочно)"
        else:
            date_str = start_dt.strftime('%d.%m, %H:%M')
        if not post.get('start_has_explicit_time'):
            date_str += " (точное время не указано в афише)"
        lines.append(f"🗓 {date_str}")
    except Exception:
        pass
    price_rub = post.get('price_rub')
    if price_rub:
        lines.append(f"💵 {price_rub} ₽")
    elif post.get('is_free'):
        lines.append("💵 вход свободный")
    text = '\n'.join(lines)

    buttons = []
    if place:
        from urllib.parse import quote
        buttons.append(InlineKeyboardButton(text="🚗 Поехали", url=f"https://yandex.ru/maps/?text={quote(place)}"))
    if post.get('link'):
        buttons.append(InlineKeyboardButton(text="🔗 Подробнее", url=post['link']))
    keyboard = InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None
    return text, keyboard

@router.message(lambda message: message.text == "🎭 События города")
async def show_city_events(message: types.Message):
    """Афиша - ДВА источника: афиша концертов из Telegram-каналов
    @concerts_moscow/@spb_conc (см. fetch_concert_events.py) - добавлена по
    просьбе пользователя (21.09.2026, структурированный парсинг добавлен
    22.09.2026 - дата/время/место/цена вытаскиваются из текста поста, см.
    parse_event_fields); и TimePad (см. fetch_timepad_data.py) - топ-10
    ближайших крупных событий (см. get_events_for_user, limit=10).
    Фильтр по категории для афиши концертов (по просьбе пользователя,
    22.09.2026): события с ценой >= PRICE_THRESHOLD_RUB видят И такси, И
    Ultima; дешевле/бесплатные/без цены - только такси (см.
    get_upcoming_concert_events_for_category, price_category в
    fetch_concert_events.py). TimePad-события такого фильтра не имеют (там
    свой источник данных без этого деления). ВАЖНО: TimePad собирается
    ЛОКАЛЬНЫМ запуском скрипта (Cloudflare блокирует запросы с датацентровых
    IP) - если пользователь давно его не запускал, timepad_data.json может
    быть пустым/устаревшим, поэтому Telegram-афиша (собирается АВТОМАТИЧЕСКИ
    на Railway, всегда свежая) показывается ПЕРВОЙ. "Нет данных" - только
    если ОБА источника пусты."""
    user_id = message.from_user.id
    if user_id not in user_state or 'city' not in user_state[user_id]:
        await message.answer("Сначала выбери город!")
        return
    city = user_state[user_id]['city']
    category = user_state[user_id].get('category', 'taxi')

    events, city_supported = get_events_for_user(city, category, limit=10)
    concert_events = get_upcoming_concert_events_for_category(city, category, limit=10)

    if not events and not concert_events:
        text = (
            "🎭 *События города*\n\n"
            "На ближайшее время подходящих событий не нашлось (или для этого "
            "города пока нет источника афиши). Загляни позже - данные "
            "обновляются каждые несколько часов."
        )
        await message.answer(text, reply_markup=services_keyboard(category, city), parse_mode='Markdown')
        return

    class_label = CATEGORIES.get(category, {}).get('name', '')
    header = f"🎭 *События города* ({class_label.title() if class_label else 'все'})"
    # Заголовок - обычным сообщением с прикреплённой нижней клавиатурой услуг
    # (она остаётся видна и дальше, повторно прикреплять на каждое сообщение
    # не нужно). Каждое событие - ОТДЕЛЬНЫМ сообщением со СВОЕЙ инлайн-кнопкой
    # "Поехали" (и у концертов, и у TimePad), чтобы кнопка однозначно вела
    # именно к этому месту. Небольшая пауза между отправками - чтобы Telegram
    # не сворачивал быстро идущие подряд сообщения визуально в одну группу.
    await message.answer(header, reply_markup=services_keyboard(category, city), parse_mode='Markdown')

    for post in concert_events:
        text, keyboard = build_concert_event_message(post, city)
        await message.answer(text, reply_markup=keyboard, parse_mode='Markdown', disable_web_page_preview=True)
        await asyncio.sleep(0.1)

    for event in events:
        text, keyboard = build_event_message(event, city)
        await message.answer(text, reply_markup=keyboard, parse_mode='Markdown', disable_web_page_preview=True)
        await asyncio.sleep(0.1)

AIRPORT_MENU_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📥 Прилеты", callback_data="airport_arrivals")],
    [InlineKeyboardButton(text="🔄 Доступность", callback_data="airport_availability")],
    [InlineKeyboardButton(text="📋 Очередь", callback_data="airport_queue")]
])

@router.message(lambda message: message.text == "✈️🚆 Транспорт")
async def show_transport_menu(message: types.Message):
    """Объединённая кнопка "✈️🚆 Транспорт" (было 2 отдельные кнопки -
    "✈️ Аэропорты" и "🚆 Вокзалы" - объединены в одну по просьбе
    пользователя, чтобы короче было главное меню услуг). При нажатии -
    инлайн-подменю с этими двумя вариантами; "🚆 Вокзалы" в нём показывается,
    только если город есть в TRAIN_CITIES (см. STATION_CITY) - иначе только
    "✈️ Аэропорты", без лишнего пункта "недоступно"."""
    user_id = message.from_user.id
    if user_id not in user_state:
        await message.answer("Сначала выбери город!")
        return
    category = user_state[user_id].get('category')
    city = user_state[user_id].get('city')
    if category in CATEGORIES_WITHOUT_AIRPORTS:
        await message.answer("Для этой категории транспорт недоступен.", reply_markup=services_keyboard(category, city))
        return
    buttons = [[InlineKeyboardButton(text="✈️ Аэропорты", callback_data="transport_airports")]]
    if city in TRAIN_CITIES:
        buttons.append([InlineKeyboardButton(text="🚆 Вокзалы", callback_data="transport_trains")])
    await message.answer("Выбери 👇", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(lambda c: c.data == "transport_airports")
async def show_airport_menu(callback_query: types.CallbackQuery):
    await callback_query.answer()
    user_id = callback_query.from_user.id
    if user_id not in user_state:
        await callback_query.message.answer("Сначала выбери город!")
        return
    if user_state[user_id].get('category') in CATEGORIES_WITHOUT_AIRPORTS:
        await callback_query.message.answer("Для этой категории аэропорты недоступны.", reply_markup=services_keyboard(user_state[user_id].get('category'), user_state[user_id].get('city')))
        return
    await callback_query.message.answer("Выбери действие 👇", reply_markup=AIRPORT_MENU_KEYBOARD)

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

@router.callback_query(lambda c: c.data == "transport_trains")
async def show_train_stations_menu(callback_query: types.CallbackQuery):
    """Список вокзалов ВЫБРАННОГО ГОРОДА (см. STATION_CITY и
    fetch_trains_data.py). Пункт "🚆 Вокзалы" и так показывается в подменю
    "✈️🚆 Транспорт" только в городах из TRAIN_CITIES (см. show_transport_menu),
    но проверяем город и здесь на случай, если пользователь сменил город, не
    обновив клавиатуру."""
    await callback_query.answer()
    user_id = callback_query.from_user.id
    if user_id not in user_state or 'city' not in user_state[user_id]:
        await callback_query.message.answer("Сначала выбери город!")
        return
    category = user_state[user_id].get('category')
    city = user_state[user_id]['city']
    if city not in TRAIN_CITIES:
        await callback_query.message.answer(
            "🚆 Вокзалы пока недоступны в этом городе.",
            reply_markup=services_keyboard(category, city),
        )
        return
    keyboard, has_data = build_train_stations_keyboard(category, city)
    if not has_data:
        await callback_query.message.answer(
            "🚆 Данные по вокзалам ещё не загружены - обновляются раз в 12 часов, загляни чуть позже.",
            reply_markup=services_keyboard(category, city),
        )
        return
    await callback_query.message.answer("Выбери вокзал 👇", reply_markup=keyboard)

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
            current_load, _, _ = compute_current_hour_load(airport['icao'], relevant_class, zone_key=airport.get('zone_key'))
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

    user_id = callback_query.from_user.id
    category = user_state.get(user_id, {}).get('category', 'taxi')
    relevant_class = CATEGORY_TO_CLASS.get(category, 'total')

    msg = await callback_query.message.edit_text(f"⏳ Загружаю расписание {airport['name']}...")
    flights = get_airport_flights(airport['icao'])
    zone_key = airport.get('zone_key')
    zone_data_missing = False
    if zone_key:
        # Карточка конкретной терминальной зоны (сейчас только Шереметьево:
        # B/C и D) - фильтруем рейсы ТОЛЬКО этой зоны и делим capacity
        # пропорционально её доле трафика (см. compute_zone_capacity_shares).
        # ИСПРАВЛЕНО 19.09.2026: раньше эта функция игнорировала zone_key
        # полностью - заголовочная загрузка/число рейсов считались по ВСЕМ
        # рейсам аэропорта, даже когда открыта карточка одной конкретной
        # зоны (см. отчёт пользователя - "SVO Терминал D" показывал те же
        # цифры, что и весь аэропорт целиком).
        # ДОРАБОТАНО тем же вечером: если у Yandex Rasp СЕГОДНЯ вообще нет
        # поля terminal ни у одного рейса (has_data=False), строгая
        # фильтрация "рейсов по зоне" даёт ОБЕИМ картам одновременно "0
        # рейсов", хотя реальные рейсы наверняка есть - просто без
        # известного терминала. В этом случае НЕ фильтруем и показываем
        # весь аэропорт с честной пометкой, вместо вводящего в заблуждение
        # нуля (см. отчёт пользователя - B/C и D ОДНОВРЕМЕННО показали 0%
        # на все 8 часов подряд).
        shares, has_zone_data = compute_zone_capacity_shares(airport['icao'])
        if has_zone_data:
            capacity = capacity * shares.get(zone_key, 1.0 / max(len(shares), 1))
            flights = [f for f in flights if flight_terminal_zone(airport['icao'], f.get('terminal')) == zone_key]
        else:
            zone_data_missing = True
    economy_capacity = capacity * ECONOMY_SHARE
    business_capacity = capacity * BUSINESS_SHARE
    class_label = {'economy': 'Эконом-класс', 'business': 'Бизнес-класс', 'total': 'Все классы'}[relevant_class]
    now = get_airport_now(airport['icao'])  # местное время АЭРОПОРТА, не сервера
    text = f"*{airport['emoji']} {airport['name']} - 📥 Прилеты*\n"
    text += f"_Обновлено: {now.strftime('%H:%M:%S')} (местное время аэропорта)_\n"
    text += f"_Пропускная способность: {capacity:.0f} пас/час (эконом {economy_capacity:.0f} / бизнес {business_capacity:.0f})_\n"
    if zone_data_missing:
        text += "_⚠️ Разбивка по терминалам сегодня недоступна (нет данных о терминале в расписании) - показаны цифры по всему аэропорту_\n"
    text += f"_Рекомендации рассчитаны для: {class_label}_\n"
    # Текущая очередь по тарифам (те же крауд-отметки водителей, что и в
    # разделе "📋 Очередь" - см. format_queue_breakdown) - по просьбе
    # пользователя показываем прямо в окне "Прилёты", а не только отдельным
    # пунктом меню.
    text += format_queue_breakdown(city, airport['icao'], category)
    text += "\n\n*📊 ПРОГНОЗ ЗАГРУЖЕННОСТИ АЭРОПОРТА (текущее время +8 часов):*\n\n"
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
        # Разбивка "по терминалам" (zones_in_hour) имеет смысл только если
        # ЭТА карточка ещё не привязана к конкретной зоне (zone_key из
        # airport - переменная выше, ДО цикла) - у Шереметьево оба пункта
        # в списке аэропортов (B/C и D) уже свои конкретные зоны,
        # flights уже отфильтрован под них, так что показывать ещё и
        # разбивку "по терминалам" внутри уже-зональной карточки было бы
        # избыточно (см. фикс 19.09.2026 - раньше карточка "Терминал D"
        # показывала общие для всего аэропорта цифры, а разбивка по
        # терминалам внизу - всегда нули, что вместе выглядело нелогично).
        airport_zones = AIRPORT_TERMINAL_ZONES.get(airport['icao']) if not zone_key else None
        zones_in_hour = {zk: {'flights': 0, 'pax': 0} for zk in airport_zones} if airport_zones else {}
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
                if airport_zones:
                    flight_zone_key = flight_terminal_zone(airport['icao'], flight.get('terminal'))
                    if flight_zone_key and flight_zone_key in zones_in_hour:
                        zones_in_hour[flight_zone_key]['flights'] += 1
                        zones_in_hour[flight_zone_key]['pax'] += flight.get('passengers', 0)
        total_in_hour = economy_in_hour + business_in_hour

        relevant_pax = {'economy': economy_in_hour, 'business': business_in_hour, 'total': total_in_hour}[relevant_class]
        relevant_cap = {'economy': economy_capacity, 'business': business_capacity, 'total': capacity}[relevant_class]
        load = (relevant_pax / relevant_cap) * 100 if relevant_pax > 0 else 0
        emoji = get_load_emoji(load)
        action = get_load_recommendation(load)
        text += f"{emoji} *{hour_display}* | Нагрузка ({class_label.lower()}): *{load:.0f}%*\n"
        text += f"   Рекомендация: *{action}*\n"
        text += f"   🛬 Рейсов: {flights_in_hour} (🇷🇺 внутр. {domestic_in_hour} / 🌍 межд. {international_in_hour})  |  ✈️ Пассажиры: {total_in_hour} (эконом {economy_in_hour} / бизнес {business_in_hour})\n"
        if airport_zones:
            # Рейсы, для которых Yandex не прислал terminal (null) не попадают
            # ни в одну зону - поэтому сумма по зонам может быть МЕНЬШЕ
            # flights_in_hour, это не баг, а честное "для части рейсов
            # терминал неизвестен".
            zone_parts = [f"{data['label'].replace('Терминал', 'Терм.').replace('ы ', ' ')}: {zones_in_hour[zk]['flights']} ({zones_in_hour[zk]['pax']} пас.)"
                          for zk, data in airport_zones.items()]
            text += f"   🛫 По терминалам: {' | '.join(zone_parts)}\n"
        text += "\n"

    text += "_🔴0-50% Не ехать | 🟡51-70% Уточни очередь | 🟢71-100% Занимай очередь | 🟣>100% Срочно ехать_"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="airport_arrivals")]])
    await msg.edit_text(text, reply_markup=keyboard, parse_mode='Markdown')
    await callback_query.answer()

def compute_current_hour_load(airport_icao, relevant_class, hour_offset=0, zone_key=None):
    """Загрузка на ТЕКУЩИЙ час по ПРИЛЁТАМ - используется в списке аэропортов
    (show_airport_info) и в пуше о повышенном спросе (high_demand_alert_checker).
    hour_offset сдвигает "текущий" час вперёд - для заблаговременных пушей.
    zone_key (опционально, см. AIRPORT_TERMINAL_ZONES) - если у аэропорта есть
    деление на терминальные зоны (сейчас только Шереметьево), фильтрует рейсы
    ТОЛЬКО этой зоны и делит capacity пропорционально её доле трафика (см.
    compute_zone_capacity_shares). ИСПРАВЛЕНО 19.09.2026: раньше zone_key
    вообще не принимался и не использовался - обе зоны Шереметьево (B/C и
    D/E/F) в списке аэропортов показывали ОДИНАКОВУЮ загрузку, посчитанную
    по всем рейсам аэропорта сразу, что вводило в заблуждение (см. отчёт
    пользователя "Загрузка неверная Шереметьево...")."""
    now = get_airport_now(airport_icao)
    target_hour = (now.hour + hour_offset) % 24
    flights = get_airport_flights(airport_icao)
    capacity = AIRPORT_CAPACITY.get(airport_icao, 1000)
    if zone_key:
        shares, has_zone_data = compute_zone_capacity_shares(airport_icao)
        if has_zone_data:
            capacity = capacity * shares.get(zone_key, 1.0 / max(len(shares), 1))
            flights = [f for f in flights if flight_terminal_zone(airport_icao, f.get('terminal')) == zone_key]
        # иначе (has_zone_data=False - см. compute_zone_capacity_shares) не
        # фильтруем: у Yandex Rasp сегодня нет поля terminal ни у одного
        # рейса, строгий фильтр дал бы ложный "0" вместо реальных рейсов.
    relevant_cap = {'economy': capacity * ECONOMY_SHARE, 'business': capacity * BUSINESS_SHARE, 'total': capacity}[relevant_class]
    key = {'economy': 'passengers_economy', 'business': 'passengers_business', 'total': 'passengers'}[relevant_class]
    flights_now = [f for f in flights if datetime.fromtimestamp(f.get('firstSeen', 0)).hour == target_hour]
    total_passengers = sum(f.get(key, 0) for f in flights_now)
    load = (total_passengers / relevant_cap) * 100 if total_passengers > 0 else 0
    return load, len(flights_now), target_hour

def compute_current_availability(airport_icao, relevant_class, zone_key=None):
    """Загруженность аэропорта ПРЯМО СЕЙЧАС для конкретного класса (эконом/бизнес/все):
    только прилёты в текущий час (эти пассажиры выходят из терминала прямо сейчас) -
    вылеты больше не собираются (убраны ради экономии квоты, см. get_airport_flights).
    Сравнивается с ЧАСОВОЙ пропускной способностью - раньше тут по ошибке складывались
    пассажиры ВСЕХ рейсов за весь день, что давало 1000-2000%+.
    zone_key - см. compute_current_hour_load выше, тот же принцип фильтрации
    по терминальной зоне и пропорционального деления capacity (фикс 19.09.2026)."""
    now = get_airport_now(airport_icao)
    current_hour = now.hour
    arrivals = get_airport_flights(airport_icao)
    capacity = AIRPORT_CAPACITY.get(airport_icao, 1000)
    if zone_key:
        shares, has_zone_data = compute_zone_capacity_shares(airport_icao)
        if has_zone_data:
            capacity = capacity * shares.get(zone_key, 1.0 / max(len(shares), 1))
            arrivals = [f for f in arrivals if flight_terminal_zone(airport_icao, f.get('terminal')) == zone_key]
        # иначе не фильтруем - см. комментарий в compute_current_hour_load выше.
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
                info = compute_current_availability(airport['icao'], relevant_class, zone_key=airport.get('zone_key'))
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
        info = compute_current_availability(airport['icao'], relevant_class, zone_key=airport.get('zone_key'))
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
        n_arrivals = len(info['arrivals_now'])
        arrivals_word = pluralize_ru(n_arrivals, "рейс", "рейса", "рейсов")
        text += f"🛬 Прилетает в {info['current_hour']:02d}:00-{(info['current_hour']+1)%24:02d}:00: {n_arrivals} {arrivals_word}\n\n"

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

        back_keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="airport_availability")]])
        try:
            await msg.edit_text(text, reply_markup=back_keyboard, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"❌ Не удалось отправить доступность с Markdown-разметкой: {e}")
            plain_text = text.replace('*', '').replace('_', '')
            await msg.edit_text(plain_text, reply_markup=back_keyboard)
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
    buttons = [[InlineKeyboardButton(text=f"{airport['emoji']} {airport['name']}", callback_data=f"queue_airport_{city}_{i}")] for i, airport in enumerate(airports)]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="airport_queue")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.edit_text("Выбери аэропорт 👇", reply_markup=keyboard)

def queue_tariff_multiselect_keyboard(tariffs, selected_idxs):
    """Клавиатура множественного выбора тарифов для отметки в очереди - по
    просьбе пользователя: одна и та же машина/водитель может одновременно
    стоять в очереди сразу НЕСКОЛЬКИХ тарифов (например, "в очереди на
    Эконом 11-15 машин, на Комфорт 15-20") - это разные очереди в одном и
    том же месте, каждая со своей длиной, а не одна отметка на все тарифы
    сразу. ✅/⬜ - чисто визуальный чекбокс, отмеченные тарифы хранятся в
    user_state[uid]['queue_tariffs_selected'] (индексы) до нажатия "Готово"."""
    buttons = []
    for i, t in enumerate(tariffs):
        mark = "✅" if i in selected_idxs else "⬜"
        buttons.append([InlineKeyboardButton(text=f"{mark} {t}", callback_data=f"queue_tariff_toggle_{i}")])
    done_label = f"▶️ Готово ({len(selected_idxs)})" if selected_idxs else "▶️ Готово"
    buttons.append([InlineKeyboardButton(text=done_label, callback_data="queue_tariffs_done")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="airport_queue")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

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
        # Сначала спрашиваем классы (можно несколько сразу - см.
        # queue_tariff_multiselect_keyboard) - у ТАКСИ и ТАКСИ ULTIMA свои
        # варианты (эконом/комфорт/... у такси, business/premier/... у ultima),
        # поэтому кнопки берутся из тарифов уже выбранной категории. Начинаем
        # с чистого выбора при каждом открытии меню - предыдущий набор не
        # запоминается между заходами, чтобы не отметить случайно старым
        # набором тарифов.
        user_state[user_id]['queue_tariffs_selected'] = []
        keyboard = queue_tariff_multiselect_keyboard(tariffs, [])
        await callback_query.message.edit_text("Выбери класс(ы) - можно несколько 👇", reply_markup=keyboard)
    else:
        user_state[user_id]['queue_tariff'] = None
        user_state[user_id]['queue_tariffs_selected'] = None
        await show_queue_airport_picker(callback_query.message, user_id)
    await callback_query.answer()

@router.callback_query(lambda c: c.data.startswith('queue_tariff_toggle_'))
async def toggle_queue_tariff(callback_query: types.CallbackQuery):
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
    selected = list(user_state[user_id].get('queue_tariffs_selected') or [])
    if idx in selected:
        selected.remove(idx)
    else:
        selected.append(idx)
    user_state[user_id]['queue_tariffs_selected'] = selected
    keyboard = queue_tariff_multiselect_keyboard(tariffs, selected)
    await callback_query.message.edit_reply_markup(reply_markup=keyboard)
    await callback_query.answer()

@router.callback_query(lambda c: c.data == "queue_tariffs_done")
async def finish_queue_tariff_selection(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    if user_id not in user_state or 'category' not in user_state[user_id]:
        await callback_query.answer("Начни заново с /start", show_alert=True)
        return
    category = user_state[user_id]['category']
    tariffs = CATEGORIES.get(category, {}).get('tariffs', [])
    selected = user_state[user_id].get('queue_tariffs_selected') or []
    if not selected:
        await callback_query.answer("Выбери хотя бы один класс", show_alert=True)
        return
    # Сортируем по индексу - порядок показа тарифов дальше (при вводе
    # диапазона для каждого по очереди) совпадает с порядком в CATEGORIES,
    # а не с порядком нажатия кнопок.
    selected_names = [tariffs[i] for i in sorted(selected)]
    user_state[user_id]['queue_tariff'] = selected_names[0] if len(selected_names) == 1 else None
    user_state[user_id]['queue_tariffs_multi'] = selected_names
    await show_queue_airport_picker(callback_query.message, user_id)
    await callback_query.answer()

def queue_multi_tariff_line(user_id):
    """Строка "Класс: ..." для шапки сообщений - если выбрано НЕСКОЛЬКО
    тарифов сразу (queue_tariffs_multi, см. queue_tariff_multiselect_keyboard),
    перечисляет их через запятую; иначе - как раньше, через
    queue_class_display (один тариф или категория без тарифов вообще)."""
    multi = user_state.get(user_id, {}).get('queue_tariffs_multi')
    if multi and len(multi) > 1:
        category = user_state.get(user_id, {}).get('category', '')
        cat_name = CATEGORIES.get(category, {}).get('name', category)
        return f"{cat_name} ({', '.join(multi)})"
    return queue_class_display(user_id)

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
        [InlineKeyboardButton(text="🚗 Занять очередь", callback_data=f"join_queue_{city}_{airport_idx}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="airport_queue")]
    ])
    text = f"*{airport['emoji']} {airport['name']}*\nКласс: {queue_multi_tariff_line(user_id)}"
    await callback_query.message.edit_text(text, reply_markup=keyboard, parse_mode='Markdown')
    await callback_query.answer()

def start_queue_multi_progress(user_id):
    """Инициализирует пошаговый проход по нескольким выбранным тарифам
    (queue_tariffs_multi) - для каждого нужно спросить СВОЙ диапазон машин
    (Эконом 11-15, Комфорт 15-20 и т.п. - разные очереди с разной длиной,
    см. queue_tariff_multiselect_keyboard). Прогресс (на каком тарифе
    остановились, что уже собрали) хранится в
    user_state[uid]['queue_multi_progress'] между шагами, а не в
    callback_data - там уже и так city+airport_idx+range_idx, а тарифов
    может быть несколько сразу. Возвращает (tariffs, idx) - список тарифов
    для прохода и индекс текущего (0, если это одиночный тариф/без
    тарифов - тогда список из одного None)."""
    multi = user_state.get(user_id, {}).get('queue_tariffs_multi')
    tariffs = multi if multi else [user_state.get(user_id, {}).get('queue_tariff')]
    user_state[user_id]['queue_multi_progress'] = {'tariffs': tariffs, 'idx': 0, 'results': {}}
    return tariffs, 0

@router.callback_query(lambda c: c.data.startswith('join_queue_'))
async def show_range_picker(callback_query: types.CallbackQuery):
    """Кнопка "Занять очередь" на самом деле не ставит водителя в реальную
    очередь, а открывает выбор диапазона - сколько машин водитель сейчас видит
    в очереди на аэропорту (1-5, 6-10, ... 96-100). Точное число никто не
    посчитает на глаз, а диапазон - реально. Если выбрано несколько тарифов -
    начинает пошаговый проход (см. start_queue_multi_progress): сначала
    спрашивает диапазон для первого, потом для следующего и так далее."""
    user_id = callback_query.from_user.id
    if user_id not in user_state or 'category' not in user_state[user_id]:
        await callback_query.answer("Начни заново с /start", show_alert=True)
        return
    _, _, city, airport_idx_str = callback_query.data.split('_')
    airport_idx = int(airport_idx_str)
    airport = AIRPORTS_INFO[city][airport_idx]

    tariffs, idx = start_queue_multi_progress(user_id)
    await render_range_picker(callback_query.message, user_id, city, airport_idx, airport, tariffs, idx)
    await callback_query.answer()

async def render_range_picker(message, user_id, city, airport_idx, airport, tariffs, idx):
    """Рисует клавиатуру выбора диапазона для tariffs[idx] - вынесено в
    отдельную функцию, т.к. вызывается и из show_range_picker (первый
    тариф), и из submit_range (переход к следующему тарифу)."""
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

    current_tariff = tariffs[idx]
    step_line = f" ({idx + 1}/{len(tariffs)})" if len(tariffs) > 1 else ""
    tariff_line = f"{current_tariff}{step_line}" if current_tariff else queue_class_display(user_id)
    text = f"*{airport['emoji']} {airport['name']}*\nКласс: {tariff_line}\n\nСколько машин сейчас в очереди? Выбери диапазон 👇"
    await message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode='Markdown')

@router.callback_query(lambda c: c.data.startswith('qsub_'))
async def submit_range(callback_query: types.CallbackQuery):
    """Сохраняет отметку для ТЕКУЩЕГО тарифа в пошаговом проходе
    (queue_multi_progress, см. start_queue_multi_progress/render_range_picker).
    Если это был не последний выбранный тариф - сразу показывает выбор
    диапазона для следующего (без возврата в предыдущие меню); если
    последний - сохраняет все собранные отметки и показывает итог по всем
    тарифам сразу."""
    user_id = callback_query.from_user.id
    if user_id not in user_state or 'category' not in user_state[user_id]:
        await callback_query.answer("Начни заново с /start", show_alert=True)
        return
    _, city, airport_idx_str, range_idx_str = callback_query.data.split('_')
    airport_idx = int(airport_idx_str)
    range_idx = int(range_idx_str)
    airport = AIRPORTS_INFO[city][airport_idx]
    range_str = queue_range_label(range_idx)

    progress = user_state[user_id].get('queue_multi_progress')
    if not progress:
        # Защитный случай - progress мог не сохраниться (например, старая
        # сессия/рестарт бота между шагами). Ведём себя как раньше: одна
        # отметка под текущим queue_class_key().
        tariffs, idx = [user_state[user_id].get('queue_tariff')], 0
        progress = {'tariffs': tariffs, 'idx': 0, 'results': {}}

    tariffs = progress['tariffs']
    idx = progress['idx']
    current_tariff = tariffs[idx]
    category = user_state[user_id]['category']
    class_key = f"{category}:{current_tariff}" if current_tariff else category
    queue_submit_report(user_id, city, airport['icao'], class_key, range_str)
    progress['results'][current_tariff or category] = range_str

    if idx + 1 < len(tariffs):
        # Есть ещё тарифы в очереди на отметку - сразу спрашиваем диапазон
        # для следующего, без промежуточного экрана.
        progress['idx'] = idx + 1
        user_state[user_id]['queue_multi_progress'] = progress
        await render_range_picker(callback_query.message, user_id, city, airport_idx, airport, tariffs, idx + 1)
        await callback_query.answer("Отметка сохранена, дальше 👇")
        return

    # Последний (или единственный) тариф - всё собрано, показываем итог.
    user_state[user_id].pop('queue_multi_progress', None)
    local_time = get_airport_now(airport['icao']).strftime('%H:%M')
    if len(progress['results']) > 1:
        results_lines = '\n'.join(f"• {name}: *{rng}*" for name, rng in progress['results'].items())
        text = (
            f"✅ Спасибо! Отметки сохранены в *{local_time}*\n\n"
            f"{airport['emoji']} {airport['name']}\n"
            f"{results_lines}"
        )
    else:
        text = (
            f"✅ Спасибо! Отметка *{range_str}* сохранена в *{local_time}*\n\n"
            f"{airport['emoji']} {airport['name']}\n"
            f"Класс: {queue_multi_tariff_line(user_id)}"
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


def _data_file_age_minutes(path):
    """Возраст ДАННЫХ в минутах по полю 'generated_at' ВНУТРИ файла (не по
    mtime на диске!) - или None если файла нет/не читается/нет поля.

    ИСПРАВЛЕНО 19.09.2026 в ночь: изначально использовался os.path.getmtime()
    (время последнего изменения файла на диске). На Railway это оказалось
    БЕССМЫСЛЕННЫМ - при каждом деплое контейнер собирается заново из git
    checkout, и mtime файла становится временем СБОРКИ образа, а не временем
    реального сбора данных. В логах это выглядело как "flights_data.json
    свежий (5мин < 25мин)" сразу после рестарта, хотя данные внутри были
    54.9 ЧАСА устаревшими (см. поле generated_at) - защита от лишних
    прогонов при рестарте из-за этого сама блокировала получение свежих
    данных после разблокировки ключа. Теперь читаем реальную дату сбора из
    содержимого файла."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        generated_at = data.get('generated_at')
        if not generated_at:
            return None
        generated_dt = datetime.fromisoformat(generated_at)
        return (datetime.now() - generated_dt).total_seconds() / 60
    except Exception:
        return None


async def airports_data_updater():
    """Фоновая задача для flights_data.json. Днём (06:00-24:00 МСК) - каждые
    FLIGHTS_DAY_INTERVAL_HOURS часов, ночью (00:00-06:00,
    FLIGHTS_NIGHT_START_HOUR-FLIGHTS_NIGHT_END_HOUR) - реже, каждые
    FLIGHTS_NIGHT_INTERVAL_HOURS часов (рейсов мало, но не ноль - по просьбе
    пользователя ночью тоже собираем, просто пореже). Запускается сразу при
    старте бота, чтобы данные были свежими с первого деплоя - НО ТОЛЬКО если
    данные реально устарели (см. MIN_FRESH_AGE_MINUTES ниже).

    ИСПРАВЛЕНО 19.09.2026: раньше запускался БЕЗУСЛОВНО при каждом старте
    бота, даже если данные были только что обновлены - на Railway это
    означало, что каждый редеплой (а их за день бывает несколько подряд при
    активной разработке) добавлял ЕЩЁ ОДИН внеплановый цикл запросов к
    Yandex Rasp сверх обычного расписания. Именно череда редеплоев в течение
    одного дня внесла свой вклад в блокировку ключа 19.09.2026 (см. письмо
    Яндекса о превышении лимита)."""
    MIN_FRESH_AGE_MINUTES = 25  # меньше половины FLIGHTS_DAY_INTERVAL_HOURS (1ч=60мин)
    while True:
        hour = datetime.now(ZoneInfo('Europe/Moscow')).hour
        is_night = FLIGHTS_NIGHT_START_HOUR <= hour < FLIGHTS_NIGHT_END_HOUR
        interval_hours = FLIGHTS_NIGHT_INTERVAL_HOURS if is_night else FLIGHTS_DAY_INTERVAL_HOURS

        age_min = _data_file_age_minutes(FLIGHTS_DATA_FILE)
        if age_min is not None and age_min < MIN_FRESH_AGE_MINUTES:
            logger.info(
                f"⏭️  flights_data.json свежий ({age_min:.0f}мин < {MIN_FRESH_AGE_MINUTES}мин) - "
                f"пропускаю внеплановое обновление (вероятно, бот только что перезапустился)"
            )
        else:
            try:
                logger.info(f"🔄 Обновляю flights_data.json из Yandex Rasp API... ({'ночь' if is_night else 'день'})")
                async with _yandex_api_lock:
                    await asyncio.to_thread(fetch_yandex_data.main)
                logger.info("✅ flights_data.json обновлён")
            except Exception as e:
                logger.error(f"❌ Ошибка фонового обновления flights_data.json: {e}")
        await asyncio.sleep(interval_hours * 3600)


async def trains_data_updater():
    """Фоновая задача для trains_data.json (7 вокзалов по 6 городам, включая
    Сапсан - см. STATION_CITY и fetch_trains_data.py). Независимый от
    аэропортов график - раз в TRAINS_UPDATE_INTERVAL_HOURS часов,
    круглосуточно, без привязки к дню/ночи. Запускается сразу при старте
    бота - НО ТОЛЬКО если данные реально устарели (см. комментарий в
    airports_data_updater про редеплои 19.09.2026 - та же защита тут)."""
    MIN_FRESH_AGE_MINUTES = 180  # меньше половины TRAINS_UPDATE_INTERVAL_HOURS (6ч=360мин)
    while True:
        age_min = _data_file_age_minutes(TRAINS_DATA_FILE)
        if age_min is not None and age_min < MIN_FRESH_AGE_MINUTES:
            logger.info(
                f"⏭️  trains_data.json свежий ({age_min:.0f}мин < {MIN_FRESH_AGE_MINUTES}мин) - "
                f"пропускаю внеплановое обновление (вероятно, бот только что перезапустился)"
            )
        else:
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

# ==================== ПОГОДА / ОСАДКИ ====================
# Идея пользователя: осадки (дождь/снег/ливень и т.д.) ощутимо увеличивают
# спрос на такси и курьеров - люди уходят с улицы под крышу и заказывают
# вместо того чтобы идти пешком. Два режима:
# (1) упреждающий пуш водителям выбранного города за RAIN_LEAD_MINUTES минут
#     ДО начала (или заметного усиления) осадков - не "уже идёт", а "скоро
#     начнётся" (см. check_rain_transitions/push_rain_alert ниже);
# (2) кнопка "🌤 Погода" на верхнем уровне меню (рядом с "🔄 Отдать заказ") -
#     ручной просмотр почасовой разбивки на RAIN_FORECAST_HOURS часов вперёд:
#     вид осадков + температура на каждый час (см. show_weather_forecast).

async def fetch_rain_forecast(city):
    """Почасовой прогноз (weathercode, температура) на RAIN_FORECAST_HOURS
    часов вперёд по городу через Open-Meteo. Возвращает None при ошибке
    сети/API - вызывающий код должен уметь пропустить город в этом прогоне,
    а не упасть."""
    coords = RAIN_CITY_COORDS.get(city)
    if not coords:
        return None
    lat, lon = coords
    params = {
        'latitude': lat,
        'longitude': lon,
        'current': 'weathercode,temperature_2m',
        'hourly': 'weathercode,temperature_2m',
        'forecast_hours': RAIN_FORECAST_HOURS,
        'timezone': 'auto',
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(OPEN_METEO_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    logger.warning(f"⚠️ Open-Meteo вернул {resp.status} для города {city}")
                    return None
                return await resp.json()
    except Exception as e:
        logger.warning(f"⚠️ Не удалось получить прогноз Open-Meteo для {city}: {e}")
        return None

def find_upcoming_precip_event(forecast):
    """Ищет ближайшее почасовое окно с осадками в пределах RAIN_LEAD_MINUTES
    от текущего момента - то есть "начнётся достаточно скоро, чтобы имело
    смысл предупредить водителя сейчас". Возвращает None, если в этом окне
    осадков нет, иначе dict {hour_offset, time, code, weight, name, emoji}."""
    if not forecast:
        return None
    hourly = forecast.get('hourly', {})
    times = hourly.get('time', [])
    codes = hourly.get('weathercode', [])
    lead_hours_window = max(1, -(-RAIN_LEAD_MINUTES // 60))  # округление вверх - для 30 мин достаточно проверить час[0..1]
    for i in range(min(lead_hours_window + 1, len(times), len(codes))):
        code = codes[i]
        if code in PRECIP_WEATHERCODES:
            name, weight, emoji = describe_weathercode(code)
            return {'hour_offset': i, 'time': times[i], 'code': code, 'weight': weight, 'name': name, 'emoji': emoji}
    return None

def find_precip_event_end(forecast, start_offset):
    """От часа start_offset ищет первый ЧАС ПОСЛЕ него без осадков - до него
    считаем, что событие продолжается. Возвращает длительность в часах
    (минимум 1) или None, если по прогнозу не прекратится в пределах
    RAIN_FORECAST_HOURS."""
    hourly = forecast.get('hourly', {})
    codes = hourly.get('weathercode', [])
    for i in range(start_offset + 1, len(codes)):
        if codes[i] not in PRECIP_WEATHERCODES:
            return i - start_offset
    return None

async def push_rain_alert(city, event):
    """Рассылает упреждающий пуш водителям и курьерам выбранного города о
    скором начале (или усилении) осадков - в отличие от
    push_airport_status_change это касается ВСЕХ категорий (Такси/Ultima/
    Курьер/Грузовое такси - у всех может вырасти спрос), без фильтра по
    CATEGORIES_WITHOUT_AIRPORTS."""
    if not bot:
        return
    city_name = CITY_DISPLAY_NAMES.get(city, city)
    # Open-Meteo отдаёт почасовую гранулярность - hour_offset=0 это текущий
    # час (событие уже идёт), hour_offset=1 - следующий час (начнётся в
    # пределах ближайших ~60 минут, не обязательно ровно через
    # RAIN_LEAD_MINUTES - формулировка ниже намеренно не даёт точность до
    # минуты, которой у источника данных просто нет).
    if event['hour_offset'] == 0:
        when_text = "начался"
    else:
        when_text = f"ожидается в ближайшие {RAIN_LEAD_MINUTES} минут"
    duration_hours = find_precip_event_end(event['_forecast'], event['hour_offset'])
    if duration_hours:
        duration_text = f"продлится примерно {duration_hours} ч"
    else:
        duration_text = f"по прогнозу не прекратится в ближайшие {RAIN_FORECAST_HOURS} ч"
    text = (
        f"{event['emoji']} *{city_name}*\n\n"
        f"{event['name'].capitalize()} {when_text}, {duration_text}.\n\n"
        f"Через {RAIN_LEAD_MINUTES} минут в городе ожидается больше заказов - "
        f"хорошее время быть на линии."
    )
    recipients = [
        uid for uid, state in list(user_state.items())
        if isinstance(state, dict) and state.get('city') == city and notifications_enabled(state, 'weather')
    ]
    if not recipients:
        logger.info(f"{event['emoji']} В городе {city} ожидаются осадки ({event['name']}), но известных пользователей нет (либо все отключили эти пуши)")
        return
    logger.info(f"{event['emoji']} В городе {city} ожидаются осадки ({event['name']}) - рассылаю {len(recipients)} пользователям")
    sent, failed = 0, 0
    for user_id in recipients:
        try:
            await bot.send_message(user_id, text, parse_mode='Markdown')
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning(f"⚠️ Не удалось отправить пуш о погоде пользователю {user_id}: {e}")
        await asyncio.sleep(0.05)
    logger.info(f"{event['emoji']} Пуш о погоде в {city} разослан: {sent} успешно, {failed} ошибок")

async def check_rain_transitions():
    """Раз в прогон смотрит ближайшее окно осадков (в пределах
    RAIN_LEAD_MINUTES) по каждому городу и решает, нужен ли пуш:
    - НЕТ сохранённого события для этого времени начала -> новое событие,
      пушим (это и есть переход "нет предупреждения -> есть");
    - ЕСТЬ сохранённое, но с тем же временем начала и той же/меньшей силой ->
      это тот же прогноз, уже предупредили, повторно не пушим;
    - ЕСТЬ сохранённое с тем же временем начала, но сила ВЫРОСЛА (дождь ->
      ливень) -> по просьбе пользователя это отдельный повод для пуша
      (усиление), пушим ещё раз с обновлённым описанием;
    - окно осадков закончилось (find_upcoming_precip_event вернул None) ->
      сбрасываем сохранённое состояние, чтобы следующее событие снова
      считалось новым.
    Не пушит вообще на первом прогоне после деплоя эффективно так же, как и
    остальные проверки - т.к. на первом прогоне previous просто пуст, а
    записанное состояние появляется до первого возможного пуша (сохраняем
    ДО отправки), поэтому реальный риск дублей и не имеет значения, что БД
    "холодная" - худший случай - один лишний пуш сразу после деплоя, если
    событие уже активно, что не является спамом, а вполне уместным пушом."""
    previous = load_all_rain_states()
    for city in RAIN_CITY_COORDS:
        forecast = await fetch_rain_forecast(city)
        if not forecast:
            continue  # не удалось узнать - не трогаем сохранённое состояние
        event = find_upcoming_precip_event(forecast)
        prev_start, prev_weight = previous.get(city, (None, None))

        if event is None:
            if prev_start is not None:
                save_rain_state(city, None, None)  # осадки закончились - сбрасываем, следующее событие снова "новое"
            continue

        event['_forecast'] = forecast
        # Абсолютное время начала (а не просто offset) - чтобы отличать
        # "то же самое ещё не наступившее событие" от "новое, отдельное окно
        # осадков позже" даже если оба сейчас попадают в lead-окно.
        event_start = event['time']
        is_new_event = (prev_start != event_start)
        is_stronger = (prev_weight is not None and event['weight'] > prev_weight)

        if is_new_event or is_stronger:
            save_rain_state(city, event_start, event['weight'])
            await push_rain_alert(city, event)

def format_weather_forecast_text(city_name, forecast):
    """Почасовая разбивка для ручного режима (кнопка "🌤 Погода") -
    вид осадков (или "ясно"/"облачно" и т.д. по weathercode) + температура на
    каждый из RAIN_FORECAST_HOURS часов вперёд, не только ближайшее окно
    (в отличие от find_upcoming_precip_event, который смотрит только на
    RAIN_LEAD_MINUTES вперёд для целей автопуша)."""
    if not forecast:
        return f"🌤 *{city_name}*\n\nНе удалось получить прогноз погоды - попробуй ещё раз через минуту."

    current = forecast.get('current', {})
    cur_code = current.get('weathercode')
    cur_temp = current.get('temperature_2m')
    cur_name, _, cur_emoji = describe_weathercode(cur_code) if cur_code is not None else ('нет данных', 0, '🌤')
    temp_str = f"{round(cur_temp)}°C" if cur_temp is not None else "н/д"
    lines = [f"🌤 *{city_name}*", f"\nСейчас: {cur_emoji} {cur_name}, {temp_str}", "\n*Прогноз на 12 часов:*"]

    hourly = forecast.get('hourly', {})
    times = hourly.get('time', [])
    codes = hourly.get('weathercode', [])
    temps = hourly.get('temperature_2m', [])
    for t, code, temp in zip(times, codes, temps):
        try:
            hour_label = datetime.fromisoformat(t).strftime('%H:%M')
        except ValueError:
            hour_label = t
        name, weight, emoji = describe_weathercode(code)
        temp_label = f"{round(temp)}°C" if temp is not None else "н/д"
        if weight > 0:
            lines.append(f"{hour_label} — {emoji} {name}, {temp_label}")
        else:
            lines.append(f"{hour_label} — {emoji} {temp_label}")
    return '\n'.join(lines)

async def rain_checker():
    """Фоновая задача: раз в RAIN_CHECK_INTERVAL_MINUTES минут опрашивает
    Open-Meteo по каждому из 12 городов и шлёт упреждающий пуш, если в
    ближайшие RAIN_LEAD_MINUTES минут ожидаются осадки (или существующие
    осадки усиливаются) - см. check_rain_transitions."""
    while True:
        try:
            await check_rain_transitions()
        except Exception as e:
            logger.error(f"❌ Ошибка фоновой проверки погоды: {e}")
        await asyncio.sleep(RAIN_CHECK_INTERVAL_MINUTES * 60)

# ==================== ПРАЗДНИКИ (пуши) ====================

def holiday_key(holiday):
    """Уникальный идентификатор конкретной записи HOLIDAYS - дата+название+
    город - используется для дедупа в holiday_pushes_sent (нельзя просто
    использовать дату, т.к. в один день теоретически может быть больше
    одного праздника)."""
    y, m, d = holiday['date']
    return f"{y:04d}-{m:02d}-{d:02d}:{holiday['name']}:{holiday.get('city', 'ALL')}"

async def push_holiday_alert(holiday, kind):
    """Рассылает пуш о празднике - 'lead' (за HOLIDAY_LEAD_DAYS до) или 'day'
    (в сам день праздника). Национальный праздник (is_national=True) - всем
    известным пользователям во всех городах; городской (День города) -
    только пользователям этого конкретного города."""
    if not bot:
        return
    if holiday.get('is_national'):
        recipients = [
            uid for uid, state in list(user_state.items())
            if isinstance(state, dict) and notifications_enabled(state, 'holidays')
        ]
        where = "по всем городам"
    else:
        city = holiday.get('city')
        city_name = CITY_DISPLAY_NAMES.get(city, city)
        recipients = [
            uid for uid, state in list(user_state.items())
            if isinstance(state, dict) and state.get('city') == city and notifications_enabled(state, 'holidays')
        ]
        where = f"в городе {city_name}"

    if kind == 'lead':
        when_text = f"Завтра, {holiday['date'][2]:02d}.{holiday['date'][1]:02d} - {holiday['name']}"
    else:
        when_text = f"Сегодня {holiday['name']}"
    text = (
        f"{holiday['emoji']} *{when_text}*\n\n"
        f"В праздники обычно растёт спрос на такси и доставку - люди едут в гости, "
        f"на мероприятия, заказывают подарки. Хорошее время быть на линии."
    )

    if not recipients:
        logger.info(f"{holiday['emoji']} {holiday['name']} ({kind}), но известных пользователей {where} нет (либо все отключили эти пуши)")
        return
    logger.info(f"{holiday['emoji']} {holiday['name']} ({kind}) - рассылаю {len(recipients)} пользователям {where}")
    sent, failed = 0, 0
    for user_id in recipients:
        try:
            await bot.send_message(user_id, text, parse_mode='Markdown')
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning(f"⚠️ Не удалось отправить пуш о празднике пользователю {user_id}: {e}")
        await asyncio.sleep(0.05)
    logger.info(f"{holiday['emoji']} Пуш о празднике «{holiday['name']}» ({kind}) разослан: {sent} успешно, {failed} ошибок")

async def check_holidays():
    """Раз в прогон смотрит календарь HOLIDAYS: для каждого праздника,
    который наступает РОВНО через HOLIDAY_LEAD_DAYS дней - шлёт разовое
    напоминание (kind='lead', дедуп через holiday_pushes_sent - лишний прогон
    в пределах того же дня не дублирует); для каждого праздника, который
    идёт СЕГОДНЯ - шлёт пуш (kind='day'), и т.к. пользователь просил "каждые
    12 часов" в сам день, day-пуш дедупится не на весь день, а на
    12-часовой слот (00:00-11:59 / 12:00-23:59 по UTC), поэтому один и тот
    же holiday_key+kind='day' может уйти дважды за день (утром и вечером),
    но не чаще."""
    today = datetime.now(ZoneInfo('UTC')).date()
    lead_target = today + timedelta(days=HOLIDAY_LEAD_DAYS)
    half_day_slot = 0 if datetime.now(ZoneInfo('UTC')).hour < 12 else 1

    for holiday in HOLIDAYS:
        y, m, d = holiday['date']
        h_date = datetime(y, m, d).date()
        key = holiday_key(holiday)
        city_key = 'ALL' if holiday.get('is_national') else holiday.get('city', 'ALL')

        if h_date == lead_target:
            if not was_holiday_push_sent(key, city_key, 'lead'):
                mark_holiday_push_sent(key, city_key, 'lead')
                await push_holiday_alert(holiday, 'lead')

        if h_date == today:
            slot_kind = f'day_{half_day_slot}'
            if not was_holiday_push_sent(key, city_key, slot_kind):
                mark_holiday_push_sent(key, city_key, slot_kind)
                await push_holiday_alert(holiday, 'day')

async def holiday_checker():
    """Фоновая задача: раз в HOLIDAY_CHECK_INTERVAL_MINUTES минут проверяет
    календарь праздников (см. check_holidays) - разовый пуш за
    HOLIDAY_LEAD_DAYS день(-я) до и до двух пушей (утро/вечер) в сам день
    праздника."""
    while True:
        try:
            await check_holidays()
        except Exception as e:
            logger.error(f"❌ Ошибка фоновой проверки праздников: {e}")
        await asyncio.sleep(HOLIDAY_CHECK_INTERVAL_MINUTES * 60)

async def check_airport_queue_timers():
    """Раз в AIRPORT_QUEUE_CHECK_INTERVAL_MINUTES проходит по всем активным
    отслеживаниям (user_state[...]['airport_queue_active']) и досылает пуши
    "уже 30 минут/1 час рядом" по прошедшему времени - независимо от того,
    приходят ли новые пинги геопозиции прямо сейчас (см. docstring у
    AIRPORT_QUEUE_CHECK_INTERVAL_MINUTES). Если новых пингов геопозиции нет
    дольше AIRPORT_QUEUE_STALE_TIMEOUT_MINUTES (инструкция рекомендует делиться
    "Пока не отключу", так что ориентироваться на live_period самого сообщения
    больше нельзя - см. комментарий у константы) - трансляция, судя по всему,
    прервалась: гасит отслеживание и присылает напоминание включить её заново
    (по просьбе пользователя, чтобы водитель не забывал) вместо того, чтобы
    молча пушить бесконечно того, кто уже уехал, ИЛИ молча остановиться без
    единого слова тому, кто забыл, что нужно включить трансляцию заново."""
    now = datetime.now(ZoneInfo('UTC'))
    for user_id, state in list(user_state.items()):
        if not isinstance(state, dict) or not state.get('airport_queue_active'):
            continue
        aq = state.get('airport_queue') or {}

        last_update_str = aq.get('last_update_at')
        if last_update_str:
            try:
                last_update = datetime.fromisoformat(last_update_str)
            except ValueError:
                last_update = now
            if (now - last_update).total_seconds() > AIRPORT_QUEUE_STALE_TIMEOUT_MINUTES * 60:
                state['airport_queue_active'] = False
                state['airport_queue'] = {}
                await send_airport_queue_expired_push(user_id, aq.get('icao'))
                continue

        icao = aq.get('icao')
        entered_at_str = aq.get('entered_outer_at')
        if not icao or not entered_at_str:
            continue

        try:
            entered_at = datetime.fromisoformat(entered_at_str)
        except ValueError:
            continue
        elapsed_minutes = (now - entered_at).total_seconds() / 60

        # zone_label для 30/60-минутных пушей восстанавливаем из сохранённого
        # zone_key (см. process_airport_queue_ping) - у аэропортов без зон
        # (AIRPORT_TERMINAL_ZONES) zone_key всегда None, .get(None) тоже даст
        # None, текст пуша не меняется.
        zone_label = None
        zone_key = aq.get('zone_key')
        if zone_key:
            zone_data = AIRPORT_TERMINAL_ZONES.get(icao, {}).get(zone_key)
            if zone_data:
                zone_label = zone_data['label']

        aq_updated = dict(aq)
        changed = False
        pushed_30, pushed_60 = AIRPORT_QUEUE_TIME_PUSHES_MIN
        if elapsed_minutes >= pushed_30 and not aq.get('pushed_30'):
            await send_airport_queue_push(user_id, icao, 30, zone_label=zone_label)
            aq_updated['pushed_30'] = True
            changed = True
        if elapsed_minutes >= pushed_60 and not aq.get('pushed_60'):
            await send_airport_queue_push(user_id, icao, 60, zone_label=zone_label)
            aq_updated['pushed_60'] = True
            changed = True
        if changed:
            state['airport_queue'] = aq_updated

async def airport_queue_checker():
    """Фоновая задача: раз в AIRPORT_QUEUE_CHECK_INTERVAL_MINUTES минут
    проверяет таймеры "давно рядом с аэропортом" (см. check_airport_queue_timers)
    - пуши на вход в 3 км/1.5 км шлются сразу по факту пинга геопозиции (см.
    process_airport_queue_ping), эта задача только за пуши по времени."""
    while True:
        try:
            await check_airport_queue_timers()
        except Exception as e:
            logger.error(f"❌ Ошибка фоновой проверки очереди у аэропорта: {e}")
        await asyncio.sleep(AIRPORT_QUEUE_CHECK_INTERVAL_MINUTES * 60)

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
        and notifications_enabled(state, 'airport_status')
    ]
    if not recipients:
        logger.info(f"📢 Статус {icao} изменился ({old_status} -> {new_status}), но в городе {city} сейчас нет известных водителей с доступом к аэропортам (либо все отключили эти пуши)")
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
# По просьбе пользователя пуш шлётся не на разовый скачок >100% в один
# конкретный час, а только на УСТОЙЧИВУЮ перегрузку - подряд идущие часы с
# прогнозом выше порога. HIGH_DEMAND_STREAK_HOURS=2 - именно "2 часа подряд".
HIGH_DEMAND_STREAK_HOURS = 2
HIGH_DEMAND_THRESHOLD = 100
HIGH_DEMAND_CHECK_INTERVAL_MINUTES = 15
# Обратное к CATEGORY_TO_CLASS, но только категории с доступом к аэропортам -
# Курьер/Грузовое такси используют relevant_class='total' и в этот пуш не попадают.
RELEVANT_CLASS_TO_CATEGORY = {'economy': 'taxi', 'business': 'ultima'}

async def push_high_demand_alert(icao, airport, relevant_class, hour_from, hour_to, target_date, loads):
    """Рассылает заблаговременный пуш о повышенном спросе: прогноз загрузки
    прилётов через HIGH_DEMAND_LEAD_HOURS часа даёт фиолетовый уровень (>100%,
    "СРОЧНО") НЕ на один час, а на HIGH_DEMAND_STREAK_HOURS часов ПОДРЯД
    (hour_from..hour_to включительно) - устойчивая перегрузка, а не разовый
    скачок. Получают только водители категории, которой соответствует
    relevant_class (эконом -> Такси, бизнес -> Ultima) - как и в пуше о смене
    статуса, каждому добавляется персональный блок текущей очереди по его
    собственным тарифам."""
    city = ICAO_TO_CITY.get(icao)
    category = RELEVANT_CLASS_TO_CATEGORY.get(relevant_class)
    if not city or not bot or not category:
        return
    class_name = CATEGORIES[category]['name']
    hour_to_end = (hour_to + 1) % 24
    loads_str = ', '.join(f"{l:.0f}%" for l in loads)
    base_text = (
        f"🟣 *{airport['emoji']} {airport['name']}*\n\n"
        f"Через {HIGH_DEMAND_LEAD_HOURS} часа (~{hour_from:02d}:00-{hour_to_end:02d}:00) ожидается "
        f"*устойчивый повышенный спрос* на прилёты ({class_name}) - "
        f"{HIGH_DEMAND_STREAK_HOURS} часа подряд прогноз загрузки выше 100% ({loads_str})."
    )

    # Копия списка - рассылка идёт не одну секунду, а user_state тем временем
    # может меняться (кто-то жмёт кнопки), нельзя итерировать "живой" словарь
    recipients = [
        (uid, state) for uid, state in list(user_state.items())
        if isinstance(state, dict) and state.get('city') == city and state.get('category') == category
        and notifications_enabled(state, 'high_demand')
    ]
    if not recipients:
        logger.info(f"📢 Прогноз устойчивого спроса {icao} ({relevant_class}, {hour_from:02d}:00-{hour_to_end:02d}:00), но в городе {city} нет известных водителей категории {category} (либо все отключили эти пуши)")
        return

    logger.info(f"📢 Прогноз устойчивого спроса {icao} ({relevant_class}, {hour_from:02d}:00-{hour_to_end:02d}:00, загрузка {loads_str}) - рассылаю {len(recipients)} водителям категории {category}")
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
    """Проверяет прогноз загрузки прилётов для каждого (аэропорт, релевантный
    класс) на предмет УСТОЙЧИВОЙ перегрузки - HIGH_DEMAND_STREAK_HOURS часов
    ПОДРЯД с прогнозом >100% (фиолетовый уровень), а не разовый скачок в
    один час. Окно начинается через HIGH_DEMAND_LEAD_HOURS часа от текущего
    момента (т.е. пуш - это предупреждение ЗА HIGH_DEMAND_LEAD_HOURS часа ДО
    начала этих HIGH_DEMAND_STREAK_HOURS часов перегрузки, по просьбе
    пользователя). Не чаще одного раза на конкретный (аэропорт, класс, дата,
    первый час окна) слот: фоновая проверка идёт каждые
    HIGH_DEMAND_CHECK_INTERVAL_MINUTES минут и без дедупликации водитель
    получил бы несколько одинаковых пушей подряд про одно и то же окно."""
    cleanup_old_high_demand_alerts()
    for icao, airport in ICAO_TO_AIRPORT.items():
        if icao in PERMANENTLY_CLOSED_AIRPORTS or airport.get('closed'):
            continue
        for relevant_class in ('economy', 'business'):
            try:
                # Смотрим HIGH_DEMAND_STREAK_HOURS часов подряд, начиная через
                # HIGH_DEMAND_LEAD_HOURS часов от сейчас: hour_offset =
                # LEAD_HOURS, LEAD_HOURS+1, ... LEAD_HOURS+STREAK_HOURS-1.
                streak = [
                    compute_current_hour_load(icao, relevant_class, hour_offset=HIGH_DEMAND_LEAD_HOURS + i)
                    for i in range(HIGH_DEMAND_STREAK_HOURS)
                ]
            except Exception as e:
                logger.error(f"❌ Не удалось посчитать прогноз спроса {icao}/{relevant_class}: {e}")
                continue
            loads = [s[0] for s in streak]
            if any(load <= HIGH_DEMAND_THRESHOLD for load in loads):
                continue  # хотя бы один час из окна не превышает порог - это не устойчивая перегрузка
            hour_from = streak[0][2]
            hour_to = streak[-1][2]
            target_date = (get_airport_now(icao) + timedelta(hours=HIGH_DEMAND_LEAD_HOURS)).strftime('%Y-%m-%d')
            if was_high_demand_alert_sent(icao, relevant_class, target_date, hour_from):
                continue
            await push_high_demand_alert(icao, airport, relevant_class, hour_from, hour_to, target_date, loads)
            mark_high_demand_alert_sent(icao, relevant_class, target_date, hour_from)

async def high_demand_alert_checker():
    """Фоновая задача: раз в HIGH_DEMAND_CHECK_INTERVAL_MINUTES минут проверяет
    прогноз спроса на прилёты и заранее (за HIGH_DEMAND_LEAD_HOURS часа) шлёт
    пуш водителям, если ожидается HIGH_DEMAND_STREAK_HOURS часов ПОДРЯД
    фиолетового уровня (>100%) - устойчивая перегрузка, а не разовый скачок."""
    while True:
        try:
            await check_high_demand_alerts()
        except Exception as e:
            logger.error(f"❌ Ошибка фоновой проверки повышенного спроса: {e}")
        await asyncio.sleep(HIGH_DEMAND_CHECK_INTERVAL_MINUTES * 60)

# ==================== ПУШ О ЗЕЛЁНОМ УРОВНЕ СПРОСА (71-100%) ====================
# По просьбе пользователя - отдельный пуш для зелёного уровня (🟢 71-100%,
# "Занимай очередь"), не только для фиолетового (>100%, "Срочно ехать").
# Текст и тон специально другие - зелёный это НЕ срочность, а "имеет смысл
# подъехать и встать в очередь заранее", отдельная от фиолетового формулировка.
GREEN_DEMAND_LEAD_HOURS = 2
# По условию пользователя - "3 часа подряд" (не 2, как у фиолетового уровня).
GREEN_DEMAND_STREAK_HOURS = 3
GREEN_DEMAND_THRESHOLD_LOW = 70   # нижняя граница зелёного (см. get_load_emoji)
GREEN_DEMAND_THRESHOLD_HIGH = 100  # верхняя граница - выше уже фиолетовый, это отдельный пуш
GREEN_DEMAND_CHECK_INTERVAL_MINUTES = 15

async def push_green_demand_alert(icao, airport, relevant_class, hour_from, hour_to, target_date, loads):
    """Пуш о зелёном уровне спроса (71-100%, "Занимай очередь") -
    GREEN_DEMAND_STREAK_HOURS часов подряд в этом диапазоне. Отдельная от
    push_high_demand_alert формулировка: без "СРОЧНО", тон - "стоит подъехать
    заранее и занять очередь", а не "аврал"."""
    city = ICAO_TO_CITY.get(icao)
    category = RELEVANT_CLASS_TO_CATEGORY.get(relevant_class)
    if not city or not bot or not category:
        return
    class_name = CATEGORIES[category]['name']
    hour_to_end = (hour_to + 1) % 24
    loads_str = ', '.join(f"{l:.0f}%" for l in loads)
    base_text = (
        f"🟢 *{airport['emoji']} {airport['name']}*\n\n"
        f"Через {GREEN_DEMAND_LEAD_HOURS} часа (~{hour_from:02d}:00-{hour_to_end:02d}:00) ожидается "
        f"*повышенный спрос* на прилёты ({class_name}) - "
        f"{GREEN_DEMAND_STREAK_HOURS} часа подряд прогноз загрузки 71-100% ({loads_str}). "
        f"*Занимай очередь* заранее - к началу окна освободится место."
    )

    recipients = [
        (uid, state) for uid, state in list(user_state.items())
        if isinstance(state, dict) and state.get('city') == city and state.get('category') == category
        and notifications_enabled(state, 'high_demand')
    ]
    if not recipients:
        logger.info(f"📢 Прогноз зелёного спроса {icao} ({relevant_class}, {hour_from:02d}:00-{hour_to_end:02d}:00), но в городе {city} нет известных водителей категории {category} (либо все отключили эти пуши)")
        return

    logger.info(f"📢 Прогноз зелёного спроса {icao} ({relevant_class}, {hour_from:02d}:00-{hour_to_end:02d}:00, загрузка {loads_str}) - рассылаю {len(recipients)} водителям категории {category}")
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
            logger.warning(f"⚠️ Не удалось отправить пуш о зелёном спросе пользователю {user_id}: {e}")
        await asyncio.sleep(0.05)
    logger.info(f"📢 Пуш о зелёном спросе по {icao} разослан: {sent} успешно, {failed} ошибок")

async def check_green_demand_alerts():
    """Тот же принцип, что check_high_demand_alerts, но для зелёного уровня
    (71-100%, GREEN_DEMAND_STREAK_HOURS=3 часа подряд) с отдельным дедупом,
    чтобы не пересекаться с фиолетовым пушем по тому же слоту."""
    cleanup_old_green_demand_alerts()
    for icao, airport in ICAO_TO_AIRPORT.items():
        if icao in PERMANENTLY_CLOSED_AIRPORTS or airport.get('closed'):
            continue
        for relevant_class in ('economy', 'business'):
            try:
                streak = [
                    compute_current_hour_load(icao, relevant_class, hour_offset=GREEN_DEMAND_LEAD_HOURS + i)
                    for i in range(GREEN_DEMAND_STREAK_HOURS)
                ]
            except Exception as e:
                logger.error(f"❌ Не удалось посчитать прогноз зелёного спроса {icao}/{relevant_class}: {e}")
                continue
            loads = [s[0] for s in streak]
            # Все часы окна должны быть строго в зелёном диапазоне (71-100%) -
            # если хоть один час выходит за пределы (ниже 71% или выше 100%,
            # т.е. уже фиолетовый уровень), это не устойчивый зелёный период.
            if any(load <= GREEN_DEMAND_THRESHOLD_LOW or load > GREEN_DEMAND_THRESHOLD_HIGH for load in loads):
                continue
            hour_from = streak[0][2]
            hour_to = streak[-1][2]
            target_date = (get_airport_now(icao) + timedelta(hours=GREEN_DEMAND_LEAD_HOURS)).strftime('%Y-%m-%d')
            if was_green_demand_alert_sent(icao, relevant_class, target_date, hour_from):
                continue
            await push_green_demand_alert(icao, airport, relevant_class, hour_from, hour_to, target_date, loads)
            mark_green_demand_alert_sent(icao, relevant_class, target_date, hour_from)

async def green_demand_alert_checker():
    """Фоновая задача: раз в GREEN_DEMAND_CHECK_INTERVAL_MINUTES минут проверяет
    прогноз на зелёный уровень спроса (71-100%, 3 часа подряд) и шлёт пуш
    заранее, отдельно от фиолетового high_demand_alert_checker."""
    while True:
        try:
            await check_green_demand_alerts()
        except Exception as e:
            logger.error(f"❌ Ошибка фоновой проверки зелёного спроса: {e}")
        await asyncio.sleep(GREEN_DEMAND_CHECK_INTERVAL_MINUTES * 60)

# ==================== ПУШ "ЧАСЫ ПИКА" ====================
# По просьбе пользователя - пуш за PEAK_HOUR_PUSH_LEAD_MINUTES (30) минут ДО
# начала часа пика (см. WEEKDAY_HOUR_LOAD/find_upcoming_peak_start выше).
# Проверяем каждые PEAK_HOUR_CHECK_INTERVAL_MINUTES минут - заметно чаще, чем
# лид в 30 минут, чтобы не пропустить окно между проверками (10 минут - тот
# же порядок частоты, что и у остальных фоновых проверок бота).
PEAK_HOUR_CHECK_INTERVAL_MINUTES = 10

async def push_peak_hour_alert(city, target_date, target_hour, label, start_dt):
    """Рассылает пуш "через 30 минут начинается час пик" водителям такси/
    Ultima в этом городе (курьеру/грузовому такси не актуально - та же
    логика, что у "🧭 Куда ехать", см. CATEGORIES_WITHOUT_AIRPORTS)."""
    if not bot:
        return
    city_name = CITY_DISPLAY_NAMES.get(city, city)
    text = (
        f"📅 *{city_name}*\n\n"
        f"Через {PEAK_HOUR_PUSH_LEAD_MINUTES} минут ({start_dt.strftime('%H:%M')}) начинается "
        f"*{label}* - самое время выехать в оживлённый район или к аэропорту."
    )
    recipients = [
        uid for uid, state in list(user_state.items())
        if isinstance(state, dict) and state.get('city') == city
        and state.get('category') not in CATEGORIES_WITHOUT_AIRPORTS
        and notifications_enabled(state, 'peak_hours')
    ]
    if not recipients:
        logger.info(f"📅 Час пика через {PEAK_HOUR_PUSH_LEAD_MINUTES} мин в городе {city} ({target_date} {target_hour:02d}:00), но нет известных водителей такси/Ultima (либо все отключили эти пуши)")
        return
    logger.info(f"📅 Час пика через {PEAK_HOUR_PUSH_LEAD_MINUTES} мин в городе {city} ({target_date} {target_hour:02d}:00) - рассылаю {len(recipients)} водителям")
    sent, failed = 0, 0
    for user_id in recipients:
        try:
            await bot.send_message(user_id, text, parse_mode='Markdown')
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning(f"⚠️ Не удалось отправить пуш о часе пика пользователю {user_id}: {e}")
        await asyncio.sleep(0.05)  # Telegram допускает ~30 сообщений/сек в разные чаты
    logger.info(f"📅 Пуш о часе пика по городу {city} разослан: {sent} успешно, {failed} ошибок")

async def check_peak_hour_alerts():
    """Проверяет каждый город бота на предмет "час пик начинается через
    PEAK_HOUR_PUSH_LEAD_MINUTES минут" - тот же дедуп-паттерн, что и у
    check_high_demand_alerts (не шлём повторно один и тот же слот)."""
    cleanup_old_peak_hour_alerts()
    for city in AIRPORTS_INFO:
        try:
            upcoming = find_upcoming_peak_start(city)
        except Exception as e:
            logger.error(f"❌ Не удалось проверить час пика для города {city}: {e}")
            continue
        if not upcoming:
            continue
        if was_peak_hour_alert_sent(city, upcoming['target_date'], upcoming['target_hour']):
            continue
        await push_peak_hour_alert(city, upcoming['target_date'], upcoming['target_hour'], upcoming['label'], upcoming['start_dt'])
        mark_peak_hour_alert_sent(city, upcoming['target_date'], upcoming['target_hour'])

async def peak_hour_alert_checker():
    """Фоновая задача: раз в PEAK_HOUR_CHECK_INTERVAL_MINUTES минут проверяет
    приближение часа пика по каждому городу и заранее (за
    PEAK_HOUR_PUSH_LEAD_MINUTES минут) шлёт пуш водителям такси/Ultima."""
    while True:
        try:
            await check_peak_hour_alerts()
        except Exception as e:
            logger.error(f"❌ Ошибка фоновой проверки часов пика: {e}")
        await asyncio.sleep(PEAK_HOUR_CHECK_INTERVAL_MINUTES * 60)

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

async def road_events_updater():
    """Фоновая задача: раз в ROAD_EVENTS_UPDATE_INTERVAL_MINUTES минут читает
    публичные веб-версии каналов @dtp777 (Москва) и @dtp_spb78 (СПб) и
    обновляет road_events_data.json (см. fetch_road_events.py - тот же
    способ сбора, что и у favt_notices_updater выше)."""
    while True:
        try:
            logger.info("🔄 Обновляю road_events_data.json (ДТП по городам)...")
            await asyncio.to_thread(fetch_road_events.main)
            logger.info("✅ road_events_data.json обновлён")
        except Exception as e:
            logger.error(f"❌ Ошибка фонового обновления road_events_data.json: {e}")
        await asyncio.sleep(ROAD_EVENTS_UPDATE_INTERVAL_MINUTES * 60)

async def concert_events_updater():
    """Фоновая задача: раз в CONCERT_EVENTS_UPDATE_INTERVAL_MINUTES минут
    читает публичные веб-версии каналов @concerts_moscow (Москва) и
    @spb_conc (СПб) и обновляет concert_events_data.json (см.
    fetch_concert_events.py - тот же способ сбора, что и у
    road_events_updater выше)."""
    while True:
        try:
            logger.info("🔄 Обновляю concert_events_data.json (афиша концертов)...")
            await asyncio.to_thread(fetch_concert_events.main)
            logger.info("✅ concert_events_data.json обновлён")
        except Exception as e:
            logger.error(f"❌ Ошибка фонового обновления concert_events_data.json: {e}")
        await asyncio.sleep(CONCERT_EVENTS_UPDATE_INTERVAL_MINUTES * 60)

async def mos_road_data_updater():
    """Фоновая задача: раз в MOS_ROAD_DATA_UPDATE_INTERVAL_MINUTES минут
    запрашивает официальный API data.mos.ru (см. fetch_mos_road_data.py).
    Молча ничего не делает, если MOS_DATA_API_KEY не задан в переменных
    окружения Railway (ключ пользователь регистрирует и присылает сам)."""
    if not os.getenv('MOS_DATA_API_KEY'):
        logger.warning("⚠️ MOS_DATA_API_KEY не задан в переменных окружения Railway - mos_road_data.json не будет обновляться автоматически")
        return
    while True:
        try:
            logger.info("🔄 Обновляю mos_road_data.json из API data.mos.ru...")
            await asyncio.to_thread(fetch_mos_road_data.main)
            logger.info("✅ mos_road_data.json обновлён")
        except Exception as e:
            logger.error(f"❌ Ошибка фонового обновления mos_road_data.json: {e}")
        await asyncio.sleep(MOS_ROAD_DATA_UPDATE_INTERVAL_MINUTES * 60)

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
    asyncio.create_task(road_events_updater())
    asyncio.create_task(concert_events_updater())
    # mos_road_data_updater() ОТКЛЮЧЁН - apidata.mos.ru не резолвится даже с
    # серверов Railway (NameResolutionError на 'apidata.mos.ru' в логах),
    # не только из среды разработки. Похоже, домен просто недоступен из
    # датацентров вообще (частая практика блокировки у российских
    # госпорталов данных для зарубежных/датацентровых IP) - дело не в ключе
    # и не в датасете. Код (fetch_mos_road_data.py, load_mos_road_data,
    # mos_road_data_updater) оставлен на будущее - вдруг ограничение снимут
    # или появится способ ходить туда не с датацентрового IP.
    # asyncio.create_task(mos_road_data_updater())
    asyncio.create_task(high_demand_alert_checker())
    asyncio.create_task(green_demand_alert_checker())
    asyncio.create_task(rain_checker())
    asyncio.create_task(holiday_checker())
    asyncio.create_task(airport_queue_checker())
    asyncio.create_task(peak_hour_alert_checker())
    # allowed_updates передаём ЯВНО (а не полагаемся на автоматическое
    # dp.resolve_used_update_types()) - похоже, это и была причина, почему
    # пуши "Очередь у аэропорта" не приходили: Telegram Bot API запоминает
    # список allowed_updates между вызовами getUpdates на своей стороне, и
    # если хоть раз polling стартовал БЕЗ edited_message в списке (например,
    # до того как в коде появился хендлер @router.edited_message, много
    # деплоев назад) - сервер мог продолжать не присылать edited_message
    # апдейты вообще, даже после того как код научился их обрабатывать. В
    # логах Railway это выглядело как "Update id=... is not handled" на
    # каждое обновление живой геопозиции - апдейт до бота не долетал в
    # обрабатываемом виде. message/edited_message/callback_query - все типы
    # апдейтов, которые реально используются хендлерами в этом файле (см.
    # @router.message/@router.edited_message/@router.callback_query).
    await dp.start_polling(bot, allowed_updates=['message', 'edited_message', 'callback_query'])

if __name__ == '__main__':
    asyncio.run(main())
