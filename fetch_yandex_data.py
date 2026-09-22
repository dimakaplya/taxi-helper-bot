#!/usr/bin/env python3
"""
Скрипт для ЛОКАЛЬНОГО запуска (на твоём компьютере, не в облаке Railway!)

Тянет реальное расписание рейсов из Yandex Rasp API для всех 14 аэропортов,
оценивает пассажиропоток по типу борта и сохраняет всё в flights_data.json.
Бот на Railway читает этот файл вместо хардкода.

=== КАК ПОЛУЧИТЬ API-КЛЮЧ ===
1. Зайди на https://yandex.ru/dev/rasp/
2. "Получить доступ" -> зарегистрируй приложение
3. Получишь ключ вида "1234567a-89bc-01de-2345-f6789012345a"
4. Экспортируй его: export YANDEX_RASP_API_KEY="твой_ключ"

=== КАК ЗАПУСКАТЬ ===
    pip install requests
    python3 fetch_yandex_data.py

ИЗМЕНЕНО 22.09.2026 (прямая просьба пользователя - "Краснодар/Ростов ночью
закрыты, нет смысла собирать... по остальным раз в 3 часа, крупные города
(Москва/СПб/Сочи/Новосибирск/Казань/Н.Новгород) каждый час, ночью раз в 2
часа... вылеты только Москва/СПб, с 10 до 20") - теперь НЕ все аэропорты
опрашиваются с одной частотой:
  - Ростов (URRP) - постоянно закрыт (closed=true в config.json), не
    опрашивается вообще, как и раньше.
  - "Крупные" (см. BIG_CITY_ICAO: Москва x3, СПб, Сочи, Новосибирск,
    Казань, Нижний Новгород) - прилёты каждый час, а в ночное окно (см.
    NIGHT_START_HOUR/NIGHT_END_HOUR, по МЕСТНОМУ времени аэропорта) - раз в
    2 часа.
  - Остальные (Екатеринбург/Челябинск/Омск/Самара/Краснодар) - прилёты раз
    в REGULAR_FETCH_INTERVAL_HOURS=3 часа днём, ночью не опрашиваются
    вообще (эти аэропорты реально не принимают рейсы ночью - расход квоты
    на пустой ответ бессмысленен).
  - Вылеты (см. DEPARTURE_ICAO) - только Москва+СПб, и только в окне
    DEPARTURE_COLLECT_START_HOUR..END_HOUR (10:00-20:00) - сигнал "волна
    вылетов" в "Куда ехать" нужен днём, ночью вылетов из аэропортов почти
    нет.
Само решение "опрашивать ли этот аэропорт в этот час" считается внутри
main() по местному часу КАЖДОГО аэропорта отдельно (см. should_fetch_arrivals/
should_fetch_departures) - крону достаточно дёргать скрипт РАЗ В ЧАС, вся
экономия квоты происходит уже внутри одного запуска.

Рекомендуется гонять по крону раз в час:
    crontab -e
    0 * * * * cd /путь/к/проекту && /usr/bin/python3 fetch_yandex_data.py >> fetch.log 2>&1

Скрипт сам ведёт счётчик запросов за сегодня (api_usage_log.json) и откажется
запускаться, если дневной лимит (500) почти исчерпан - так что даже если крон
случайно настроят слишком часто, ключ не заблокируют за превышение.

После каждого успешного запуска - закоммить и запушь flights_data.json,
Railway подхватит новый файл через авто-деплой (GitHub webhook).

ПРИМЕЧАНИЕ: публичный stations_list Яндекса не содержит IATA-кодов вообще
(проверено - 0 из 10074 станций типа plane), поэтому yandex_code станций
для всех 14 аэропортов найден вручную (сопоставлением по названию) и
захардкожен ниже в AIRPORTS. Если Яндекс сменит коды - можно найти новый
через https://yandex.ru/dev/rasp/doc/ru/reference/query-schedule (раздел
"Список станций") или методом /v3.0/stations_list/.
"""
import os
import json
import time
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

# ИСПРАВЛЕНО 21.09.2026 (баг найден пользователем - "0 рейсов по всем
# аэропортам ночью, хотя дневной лимит запросов чистый"): get_today_usage()
# ниже считала "сегодня" через datetime.now() БЕЗ часового пояса - на
# Railway контейнер живёт в UTC, а не в московском времени. С полуночи до
# 3 часов ночи по Москве (21:00-00:00 UTC) datetime.now() в UTC всё ещё
# показывает ВЧЕРАШНЮЮ дату - и именно эта "вчерашняя" дата уходила в
# Yandex Rasp API как параметр 'date' запроса расписания. Yandex честно
# отвечал "0 рейсов" - не потому что рейсов нет, а потому что запрашивали
# уже полностью прошедшие сутки. Теперь дата считается явно по московскому
# времени (аэропорты бота все в РФ, отдельный часовой пояс на аэропорт не нужен).
MSK_TZ = ZoneInfo('Europe/Moscow')

from config_loader import get_all_airports

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

API_KEY = os.getenv('YANDEX_RASP_API_KEY', 'ВСТАВЬ_СВОЙ_КЛЮЧ_СЮДА')
BASE_URL = 'https://api.rasp.yandex.net/v3.0'

# ⚠️ На Railway диск эфемерный: при каждом редеплое контейнер стартует с чистой
# файловой системой, и api_usage_log.json (счётчик запросов за сегодня) обнуляется -
# именно поэтому дневная квота ключа у Яндекса всё равно превышается, хотя
# DAILY_SAFETY_LIMIT ниже вроде бы должен это предотвращать (см. flights_data_updater()
# в main_airports_24h.py - она дёргает main() сразу при каждом старте бота), и
# именно поэтому в 19.09.2026 ключ был заблокирован на сутки за превышение лимита
# (счётчик каждый раз обнулялся раньше, чем успевал реально что-то ограничить).
#
# ИСПРАВЛЕНО 20.09.2026 (повторная жалоба - "расписание рейсов снова не
# обновляется"): раньше для использования постоянного Railway Volume
# требовалось ВРУЧНУЮ завести переменную окружения DATA_DIR=/data в Railway -
# отдельным шагом от подключения самого Volume, про который легко забыть (и
# забыли - Volume подключили только ради БД, см. _resolve_db_file() в
# main.py, а про DATA_DIR никто не вспомнил). Теперь путь определяется
# АВТОМАТИЧЕСКИ, той же логикой, что уже проверена и работает для БД: если
# каталог /data существует и доступен на запись (Volume подключён) - используем
# его без какой-либо ручной настройки; иначе - как раньше, рядом со скриптом.
# DATA_DIR (если задан) по-прежнему имеет приоритет - для нестандартных
# запусков (например, локально на своём компьютере с кастомным путём).
def _resolve_data_dir():
    env_dir = os.getenv('DATA_DIR')
    if env_dir:
        return env_dir
    railway_volume_dir = '/data'
    if os.path.isdir(railway_volume_dir) and os.access(railway_volume_dir, os.W_OK):
        return railway_volume_dir
    return os.path.dirname(os.path.abspath(__file__))

DATA_DIR = _resolve_data_dir()
OUTPUT_FILE = os.path.join(DATA_DIR, 'flights_data.json')
USAGE_LOG_FILE = os.path.join(DATA_DIR, 'api_usage_log.json')

# Дневной лимит ключа Yandex Rasp API. Если сегодня уже потрачено
# DAILY_SAFETY_LIMIT запросов - скрипт откажется запускаться, чтобы не
# словить блокировку ключа за превышение.
# УМЕНЬШЕНО с 90% до 70% ПОСЛЕ инцидента 19.09.2026 (ключ заблокирован
# Яндексом на сутки за превышение лимита) - см. письмо от Яндекса:
# "Доступ к сервису API Яндекс.Расписаний заблокирован из-за превышений
# лимита". Этот порог реально защищает ТОЛЬКО если DATA_DIR указывает на
# постоянный volume на Railway (см. комментарий выше) - без него счётчик
# обнуляется при каждом рестарте контейнера и эта защита не работает.
DAILY_QUOTA = 500
DAILY_SAFETY_LIMIT = int(DAILY_QUOTA * 0.7)


def load_usage_log():
    if os.path.exists(USAGE_LOG_FILE):
        try:
            with open(USAGE_LOG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_usage_log(log):
    with open(USAGE_LOG_FILE, 'w', encoding='utf-8') as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def get_today_usage(log):
    today = datetime.now(MSK_TZ).strftime('%Y-%m-%d')
    return today, log.get(today, 0)

# Список 14 аэропортов бота (IATA/ICAO коды + yandex_code станции) теперь
# читается из config.json (см. config_loader.py) - раньше дублировался
# вручную здесь и в main.py/AIRPORTS_INFO, рассинхрон таких копий уже
# приводил к реальному багу (Шереметьево показывал "все нули"). Дедуп по
# icao: у Шереметьево в config.json ДВЕ записи (терминальные зоны bc/d,
# нужны для main.py), но здесь достаточно опросить API один раз на icao -
# fetch_schedule() всё равно тянет ВЕСЬ аэропорт целиком, зоны фильтруются
# уже в main.py при показе (flight_terminal_zone), а не на этапе сбора.
_seen_icao = set()
AIRPORTS = []
for _a in get_all_airports():
    if _a['icao'] in _seen_icao:
        continue
    _seen_icao.add(_a['icao'])
    entry = {
        'iata': _a['iata'], 'icao': _a['icao'], 'name': _a['name'], 'yandex_code': _a['yandex_code'],
        # ДОБАВЛЕНО 21.09.2026 (продолжение фикса часового пояса выше) -
        # дата запроса к Yandex Rasp теперь считается ПО КАЖДОМУ аэропорту
        # отдельно, его собственным часовым поясом из config.json, а не
        # одним общим MSK на всех. Иначе для дальних от Москвы городов
        # (Новосибирск +4ч, Екатеринбург/Челябинск +2ч, Омск +3ч) в вечернем
        # окне (когда в Москве ещё "сегодня", а там уже "почти завтра" или
        # наоборот) можно было запросить не тот день - тот же класс бага,
        # что чинили для MSK-окна, просто в другом временном окне для
        # каждого города.
        'timezone': _a.get('timezone', 'Europe/Moscow'),
    }
    if _a.get('closed'):
        entry['closed'] = True
    AIRPORTS.append(entry)

# Оценка пассажировместимости по типу борта (используется когда Yandex
# отдаёт модель самолёта в thread.vehicle). Если тип неизвестен - дефолт.
AIRCRAFT_CAPACITY = {
    'boeing 737': 180, 'b737': 180, '737': 180,
    'boeing 777': 350, 'b777': 350, '777': 350,
    'boeing 767': 250, 'b767': 250,
    'boeing 787': 290, 'b787': 290, 'dreamliner': 290,
    'airbus a320': 180, 'a320': 180,
    'airbus a321': 220, 'a321': 220,
    'airbus a319': 140, 'a319': 140,
    'airbus a330': 300, 'a330': 300,
    'airbus a350': 315, 'a350': 315,
    'sukhoi superjet': 98, 'ssj100': 98, 'ssj-100': 98,
    'mc-21': 180, 'mc21': 180,
    # e170/e190 идут ДО общего 'embraer' - иначе "in vehicle" матчится на
    # 'embraer' раньше, чем цикл доходит до 'e170', и E170 (78 мест)
    # ошибочно получает вместимость 100 (баг найден и исправлен 21.09.2026)
    'e170': 78, 'e190': 100, 'embraer': 100,
    'atr': 70, 'crj': 90,
}
DEFAULT_PASSENGERS = 170

# Средняя загрузка рейса (load factor) - не все места в самолёте заняты.
# 85% - стандартный отраслевой показатель загрузки для регулярных рейсов.
AVERAGE_LOAD_FACTOR = 0.85

# Разбивка фактических пассажиров по классам обслуживания
ECONOMY_SHARE = 0.85
BUSINESS_SHARE = 0.15


def estimate_passengers(thread):
    """Возвращает (economy, business, total) пассажиров на рейсе.
    Вместимость борта x средняя загрузка (85%) x разбивка эконом/бизнес (85/15)."""
    vehicle = (thread.get('vehicle') or '').lower()
    capacity = DEFAULT_PASSENGERS
    for key, cap in AIRCRAFT_CAPACITY.items():
        if key in vehicle:
            capacity = cap
            break

    total = round(capacity * AVERAGE_LOAD_FACTOR)
    economy = round(total * ECONOMY_SHARE)
    business = total - economy
    return economy, business, total


# Эвристика "внутренний/международный рейс". Компактный ответ Yandex Rasp
# API /schedule/ не отдаёт явного флага международности для точки
# отправления/назначения (только её название), поэтому определяем по
# городу: если это российский город из списка ниже - рейс внутренний, иначе
# международный. Список - все региональные центры + основные курортные и
# крупные города; этого достаточно для подавляющего большинства реальных
# направлений с этих 14 аэропортов. Не претендует на 100% точность (мелкий
# посёлок не из списка ошибочно уйдёт в "международные"), но для водителя
# такая разбивка всё равно полезнее, чем её отсутствие.
RUSSIAN_CITIES = {
    'москва', 'санкт-петербург', 'петербург', 'сочи', 'адлер', 'казань', 'екатеринбург',
    'новосибирск', 'краснодар', 'ростов-на-дону', 'ростов', 'уфа', 'омск', 'самара',
    'нижний новгород', 'челябинск', 'красноярск', 'пермь', 'волгоград', 'воронеж',
    'саратов', 'тюмень', 'тольятти', 'барнаул', 'иркутск', 'хабаровск', 'новокузнецк',
    'оренбург', 'кемерово', 'томск', 'рязань', 'астрахань', 'пенза', 'липецк', 'киров',
    'чебоксары', 'калининград', 'тула', 'ульяновск', 'ижевск', 'ярославль', 'махачкала',
    'владивосток', 'ставрополь', 'симферополь', 'сургут', 'нижневартовск', 'белгород',
    'архангельск', 'владимир', 'курск', 'смоленск', 'калуга', 'чита', 'орёл', 'орел',
    'волжский', 'мурманск', 'тверь', 'иваново', 'брянск', 'магнитогорск', 'йошкар-ола',
    'улан-удэ', 'грозный', 'владикавказ', 'нальчик', 'череповец', 'вологда', 'саранск',
    'сыктывкар', 'нижний тагил', 'стерлитамак', 'новороссийск', 'йошкар ола', 'орск',
    'бийск', 'петрозаводск', 'благовещенск', 'великий новгород', 'новгород', 'псков',
    'королёв', 'королев', 'братск', 'ангарск', 'пятигорск', 'находка', 'сызрань',
    'норильск', 'златоуст', 'каменск-уральский', 'южно-сахалинск', 'элиста', 'абакан',
    'нефтеюганск', 'старый оскол', 'бердск', 'рыбинск', 'димитровград', 'новочеркасск',
    'нальчик', 'нарткала', 'черкесск', 'майкоп', 'горно-алтайск', 'кызыл', 'анадырь',
    'магадан', 'салехард', 'ноябрьск', 'новый уренгой', 'нижневартовск', 'мирный',
    'якутск', 'петропавловск-камчатский', 'петропавловск камчатский', 'южно сахалинск',
    'комсомольск-на-амуре', 'уссурийск', 'арсеньев', 'минеральные воды', 'геленджик',
    'анапа', 'калуга', 'орёл', 'иваново', 'кострома', 'нарьян-мар', 'ухта', 'воркута',
    'усинск', 'соловки', 'калевала', 'учалы',
}

def is_domestic_flight(point_title):
    """True - рейс внутренний (город найден в списке российских), False -
    международный. При неизвестном/пустом названии считаем внутренним, чтобы
    не пугать водителя ложным ярлыком "международный" на ровном месте."""
    if not point_title:
        return True
    # "Москва (Шереметьево)" -> "москва"; "г. Сочи" -> "сочи"
    city = point_title.split('(')[0].strip().lower()
    city = city.replace('г. ', '').replace('город ', '').strip()
    return city in RUSSIAN_CITIES


# Разделители, которыми в thread.title склеены оба конца маршрута
# ("Абакан — Москва", изредка через обычный дефис). Используется только как
# ПОСЛЕДНИЙ fallback в extract_point_city ниже, когда в ответе API нет
# структурированного объекта с городом.
_THREAD_TITLE_SEPARATORS = (' — ', ' – ', ' — ', '—', '–', ' - ')


def extract_point_city(thread_title, event):
    """Вытаскивает ОДИН город (откуда рейс - для arrival, куда - для
    departure) из thread.title вида "Абакан — Москва". Раньше в fallback
    (когда structured-поле с точкой отправления/назначения недоступно, см.
    parse_flights) передавался ВЕСЬ title целиком - is_domestic_flight
    сравнивал это как один город и НИКОГДА не находил совпадение в
    RUSSIAN_CITIES (составная строка "Абакан — Москва" не равна ни "абакан",
    ни "москва" по отдельности), из-за чего рейсы поголовно помечались
    международными, даже 100% внутренние по России. Эта функция разбивает
    title по разделителю и берёт нужный конец маршрута."""
    if not thread_title:
        return thread_title
    for sep in _THREAD_TITLE_SEPARATORS:
        if sep in thread_title:
            parts = [p.strip() for p in thread_title.split(sep) if p.strip()]
            if len(parts) >= 2:
                return parts[0] if event == 'arrival' else parts[-1]
            break
    return thread_title


# ДОБАВЛЕНО 22.09.2026 (прямая просьба пользователя - "если будем собирать
# вылеты, это тоже неплохой показатель, который можно включить в систему
# расчёта - если много вылетов в аэропорту, значит нужно держаться центра/
# гостиниц, чтобы ждать заказ, едущий В аэропорт"): вылеты СНОВА собираются
# (были убраны 19-21.09.2026 ради экономии квоты). ИЗМЕНЕНО 22.09.2026 (та же
# просьба, уточнение голосовым - "вылет собирает только с таких аэропортов
# как Москва Санкт-Петербург... с 10 утра до 8 вечера") - список расширен с 3
# московских до Москвы+СПб, но добавлено дневное окно (см.
# DEPARTURE_COLLECT_START_HOUR/END_HOUR ниже) вместо круглосуточного сбора.
DEPARTURE_ICAO = {'UUEE', 'UUWW', 'UUDD', 'ULLI'}  # Шереметьево, Внуково, Домодедово, Пулково
DEPARTURE_COLLECT_START_HOUR = 10
DEPARTURE_COLLECT_END_HOUR = 20  # [10, 20) по МЕСТНОМУ времени аэропорта (для этих 4 - Europe/Moscow)

# ==================== ТИРЫ ЧАСТОТЫ ОПРОСА АЭРОПОРТОВ (ДОБАВЛЕНО 22.09.2026) ====================
# Прямая просьба пользователя: "Краснодар с 12 ночи до 6 утра вообще нет
# смысла собирать данные - аэропорт закрыт... по остальным - раз в 3 часа...
# крупные города - Москва/СПб/Сочи/Новосибирск/Казань/Нижний Новгород -
# прилёт и вылет каждый час, ночью раз в 2 часа". Ростов (URRP) отдельно
# просить не нужно - он уже помечен closed=true в config.json и полностью
# пропускается существующей проверкой airport.get('closed') в main() (см.
# ниже), как и раньше.
BIG_CITY_ICAO = {
    'UUEE', 'UUWW', 'UUDD',  # Москва (Шереметьево/Внуково/Домодедово)
    'ULLI',  # Санкт-Петербург (Пулково)
    'URSS',  # Сочи (Адлер)
    'UNNT',  # Новосибирск (Толмачёво)
    'UWKD',  # Казань
    'UWGG',  # Нижний Новгород
}
REGULAR_FETCH_INTERVAL_HOURS = 3  # обычные (не "крупные") аэропорты - раз в 3 часа днём
NIGHT_START_HOUR = 0
NIGHT_END_HOUR = 6  # [0, 6) по МЕСТНОМУ времени аэропорта - ночное окно

def should_fetch_arrivals(icao, local_hour):
    """Решает, стоит ли опрашивать ПРИЛЁТЫ этого аэропорта в данный час его
    местного времени - см. комментарий у BIG_CITY_ICAO выше. Вызывается
    заново для каждого аэропорта при каждом запуске (крон - раз в час),
    поэтому сама разница в частоте получается без разных crontab-записей."""
    is_night = NIGHT_START_HOUR <= local_hour < NIGHT_END_HOUR
    if icao in BIG_CITY_ICAO:
        return (local_hour % 2 == 0) if is_night else True
    if is_night:
        return False
    return local_hour % REGULAR_FETCH_INTERVAL_HOURS == 0

def should_fetch_departures(icao, local_hour):
    """Решает, стоит ли опрашивать ВЫЛЕТЫ этого аэропорта - см.
    DEPARTURE_ICAO/DEPARTURE_COLLECT_START_HOUR/END_HOUR выше."""
    if icao not in DEPARTURE_ICAO:
        return False
    return DEPARTURE_COLLECT_START_HOUR <= local_hour < DEPARTURE_COLLECT_END_HOUR

REQUEST_COUNT = 0  # глобальный счётчик реальных запросов к API за этот запуск


def fetch_schedule(station_code, event, date_str):
    """event: 'arrival' или 'departure'. Пагинирует через offset, пока не соберёт все
    рейсы за день. Останавливается как только страница пришла неполной (batch < page_limit) -
    это значит что дальше данных нет, и лишний "пустой" запрос на подтверждение не нужен.

    При 429 (Too Many Requests) делает до RETRY_ATTEMPTS повторов с нарастающей
    паузой (RETRY_BACKOFF_BASE * попытка секунд) вместо немедленного отказа -
    иначе временный rate-limit молча превращается в "0 рейсов" и затирает в
    flights_data.json реальные данные пустыми (см. инцидент 19.09.2026: почти
    все запросы подряд поймали 429, и файл перезаписался нулями для ВСЕХ
    аэропортов, включая те, что вообще не при чём)."""
    global REQUEST_COUNT
    all_items = []
    offset = 0
    page_limit = 500
    max_pages = 10  # защита от бесконечного цикла - максимум 5000 рейсов на аэропорт/направление
    RETRY_ATTEMPTS = 5
    RETRY_BACKOFF_BASE = 6  # секунды: 6, 12, 18, 24 - суммарно ~60с максимум на одну страницу.
    # УВЕЛИЧЕНО 19.09.2026: с базой 3с (макс. ожидание 9с перед последней
    # попыткой) почти ВСЕ аэропорты подряд ловили 429 даже на 4-й попытке -
    # см. Railway-логи за 19.09.2026 20:20-20:24, где 429 держался несколько
    # МИНУТ подряд независимо от паузы. Значит окно лимита Yandex Rasp шире,
    # чем предполагалось - официальной цифры rps/rpm в доках нет (только
    # суточная квота 500), поэтому увеличиваем паузы эмпирически.
    first_page_failed = False
    for page_num in range(max_pages):
        data = None
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                resp = requests.get(
                    f'{BASE_URL}/schedule/',
                    params={
                        'apikey': API_KEY,
                        'station': station_code,
                        'date': date_str,
                        'event': event,
                        'transport_types': 'plane',
                        'lang': 'ru_RU',
                        'limit': page_limit,
                        'offset': offset,
                    },
                    timeout=30,
                )
                # ИСПРАВЛЕНО 21.09.2026 (жалоба пользователя - "почему в
                # Яндексе показывает 159, а ты 368 из 500" - собственный
                # счётчик расходился с реальным счётчиком Яндекса почти в
                # 2.5 раза). Раньше REQUEST_COUNT увеличивался на КАЖДУЮ
                # попытку, включая повторы после 429 (Too Many Requests) -
                # при долгих сериях 429 (см. комментарий выше про
                # RETRY_BACKOFF_BASE, инцидент 19.09.2026, когда 429 держался
                # несколько минут подряд) один логический запрос к
                # /schedule/ мог задваиваться-запятеряться локально в
                # REQUEST_COUNT до 5 раз (RETRY_ATTEMPTS), хотя реальный
                # дневной лимит Яндекса, судя по личному кабинету
                # пользователя, эти отклонённые 429-попытки вообще не
                # считает. Теперь считаем только запросы, которые реально
                # дошли до обработки (НЕ 429) - именно они и расходуют
                # дневную квоту ключа.
                if resp.status_code != 429:
                    REQUEST_COUNT += 1
                if resp.status_code == 429:
                    if attempt < RETRY_ATTEMPTS:
                        wait_s = RETRY_BACKOFF_BASE * attempt
                        logger.warning(
                            f"⏳ 429 Too Many Requests ({event}, {station_code}, offset={offset}), "
                            f"попытка {attempt}/{RETRY_ATTEMPTS} - жду {wait_s}с..."
                        )
                        time.sleep(wait_s)
                        continue
                    else:
                        logger.error(
                            f"❌ 429 Too Many Requests ({event}, {station_code}, offset={offset}) - "
                            f"исчерпаны все {RETRY_ATTEMPTS} попыток"
                        )
                        break
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:
                logger.error(f"❌ Ошибка запроса schedule ({event}, {station_code}, offset={offset}): {e}")
                break

        if data is None:
            # Не удалось получить страницу (429 после всех попыток, либо другая
            # ошибка). Если это была САМАЯ ПЕРВАЯ страница - помечаем весь
            # запрос как провалившийся (first_page_failed), чтобы main() не
            # спутал "не смогли получить данные" с "сегодня рейсов нет" и не
            # затёр реальный кэш нулями. Если провалилась страница пагинации
            # НЕ первая - данные, собранные до этого, всё равно возвращаем
            # (лучше неполный список, чем ничего).
            if page_num == 0:
                first_page_failed = True
            break

        batch = data.get('schedule', [])
        all_items.extend(batch)

        # Страница пришла короче лимита (или пустая) - дальше данных точно нет,
        # не делаем лишний запрос ради подтверждения
        if len(batch) < page_limit:
            break
        offset += page_limit
        time.sleep(1.5)

    if first_page_failed:
        return None  # сигнал "не удалось получить данные", отличается от [] ("рейсов правда нет")
    return all_items


def parse_flights(schedule_items, event):
    # ПРОВЕРЕНО 21.09.2026 (пользователь спросил, учитывает ли бот статус
    # рейса - задержан/отменён/вовремя): диагностика сырого ответа Yandex
    # Rasp API (эндпоинт /schedule/ по станции) показала, что там ВООБЩЕ НЕТ
    # поля со статусом рейса - только 'thread' (номер/перевозчик/борт),
    # 'terminal', 'arrival'/'departure' (плановое время по расписанию),
    # служебные 'except_days'/'days' (дни недели, по которым рейс вообще
    # ходит - не статус конкретного дня). Yandex Rasp на этом эндпоинте
    # отдаёт только статичное расписание, без live-данных - учитывать
    # задержки/отмены НЕЧЕМ, этих данных здесь просто нет на входе.
    flights = []
    for item in schedule_items:
        thread = item.get('thread', {}) or {}
        time_str = item.get(event)  # ISO datetime строка
        if not time_str:
            continue
        try:
            dt = datetime.fromisoformat(time_str)
        except Exception:
            continue

        carrier = (thread.get('carrier') or {}).get('title', 'N/A')
        number = thread.get('number', '')
        title = thread.get('title', '')
        vehicle = thread.get('vehicle', '') or ''
        economy, business, total = estimate_passengers(thread)

        # Направление: для arrival - откуда, для departure - куда. У Yandex
        # Rasp API для эндпоинта расписания станции нет structured-поля
        # "departure_from"/"destination_to" на самом item (это поля другого
        # эндпоинта - поиска маршрута) - на практике point почти всегда None,
        # и мы попадаем в fallback. Раньше fallback брал thread.title ЦЕЛИКОМ
        # ("Абакан — Москва") - см. extract_point_city выше, почему это
        # ломало is_domestic_flight. Теперь fallback вытаскивает из title
        # именно тот город, который нужен (откуда/куда).
        point = item.get('departure_from') if event == 'arrival' else item.get('destination_to')
        point_title = None
        if isinstance(point, dict):
            point_title = point.get('title')
        if not point_title:
            point_title = extract_point_city(title, event)

        # Yandex Rasp API отдаёт terminal прямо на item ("A", "B", "C", "D"
        # и т.п. или null, если данных нет) - см. документацию "Расписание
        # рейсов по станции". Нужен для аэропортов с несколькими
        # терминалами (по просьбе пользователя - Шереметьево: бот делит
        # рейсы на зону "A/B/C/VIP" и отдельно "D", см. TERMINAL_ZONE_MAP
        # в main.py). Для остальных аэропортов (один терминал или Яндекс не
        # прислал данные) - просто None, ничего не меняется.
        terminal = item.get('terminal')

        flights.append({
            'time': dt.strftime('%H:%M'),
            'point': point_title,
            'airline': carrier,
            'flight': number,
            'aircraft': vehicle,
            'passengers': total,
            'passengers_economy': economy,
            'passengers_business': business,
            'domestic': is_domestic_flight(point_title),
            'terminal': terminal,
        })
    return flights


def fetch_departures_if_needed(icao, name, station_code, airport_today, previous_result, target_dict, local_hour):
    """Довесок к основному прогону прилётов (ДОБАВЛЕНО 22.09.2026) - вылеты
    только для DEPARTURE_ICAO и только в дневном окне (см.
    should_fetch_departures выше). Сознательно НЕ участвует в circuit
    breaker'е прилётов (consecutive_failures/circuit_broken в main()) - сбой
    здесь просто оставляет предыдущие вылеты (или пустой список) и не
    останавливает прогон по остальным аэропортам, вылеты не настолько
    критичны, чтобы ради них рисковать блокировкой ключа. Вне окна сбора -
    просто оставляет то, что уже было в target_dict (не трогает 'departures'
    вообще), а не затирает пустым списком."""
    if not should_fetch_departures(icao, local_hour):
        # Вне окна сбора - переносим предыдущие вылеты как есть (если были),
        # чтобы moscow_departure_wave_info() в main.py не остался без данных
        # только из-за того, что этот конкретный час вне окна 10-20.
        prev_airport = (previous_result or {}).get('airports', {}).get(icao)
        prev_departures = (prev_airport or {}).get('departures')
        if prev_departures:
            target_dict['departures'] = prev_departures
        return
    raw = fetch_schedule(station_code, 'departure', airport_today)
    if raw is None:
        prev_airport = (previous_result or {}).get('airports', {}).get(icao)
        prev_departures = (prev_airport or {}).get('departures')
        target_dict['departures'] = prev_departures if prev_departures else []
        if prev_departures:
            logger.warning(f"⚠️ {name}: не удалось получить вылеты - оставляю предыдущие ({len(prev_departures)})")
        else:
            logger.warning(f"⚠️ {name}: не удалось получить вылеты, и прошлых данных тоже нет")
        return
    departures_today = parse_flights(raw, 'departure')
    if not departures_today:
        prev_airport = (previous_result or {}).get('airports', {}).get(icao)
        prev_departures = (prev_airport or {}).get('departures')
        if prev_departures:
            departures_today = prev_departures
    target_dict['departures'] = departures_today
    logger.info(f"✅ {name}: {len(departures_today)} вылетов")
    time.sleep(1.5)


def main():
    """Возвращает 'daily_limit_reached', если пропустили запуск из-за
    DAILY_SAFETY_LIMIT (ИСПРАВЛЕНО 20.09.2026 - по просьбе пользователя
    отличать "лимит запросов на сегодня исчерпан" от временного сбоя: этот
    случай не имеет смысла ретраить каждые несколько минут, он пройдёт
    только на следующие сутки - вызывающий код в main.py's
    airports_data_updater() по этому значению сразу прекращает короткие
    повторные попытки вместо того чтобы жать их бесполезно ещё ~час)."""
    global REQUEST_COUNT
    # ИСПРАВЛЕНО 21.09.2026 - REQUEST_COUNT обнулялся только один раз при
    # импорте модуля (строка объявления выше), а main() вызывается многократно
    # за время жизни одного процесса бота (airports_data_updater раз в час +
    # /forcefetch). Без сброса здесь счётчик расхода квоты накапливался от
    # запуска к запуску и задваивал уже учтённые в usage_log.json запросы
    # (new_total_today = used_today + REQUEST_COUNT ниже) - могло приводить
    # к ложному "дневной лимит исчерпан" задолго до реального исчерпания.
    REQUEST_COUNT = 0
    if API_KEY == 'ВСТАВЬ_СВОЙ_КЛЮЧ_СЮДА':
        logger.error("❌ Не задан YANDEX_RASP_API_KEY! См. инструкцию в шапке файла.")
        return None

    usage_log = load_usage_log()
    today, used_today = get_today_usage(usage_log)
    if used_today >= DAILY_SAFETY_LIMIT:
        logger.error(
            f"🚫 Сегодня уже потрачено {used_today}/{DAILY_QUOTA} запросов (порог безопасности "
            f"{DAILY_SAFETY_LIMIT}) - пропускаю запуск, чтобы не превысить дневной лимит ключа. "
            f"Попробуй завтра или запускай реже."
        )
        return 'daily_limit_reached'
    logger.info(f"📊 Уже потрачено сегодня: {used_today}/{DAILY_QUOTA} запросов")

    # Предыдущий результат - на случай, если запрос к какому-то аэропорту
    # провалится (см. fetch_schedule/first_page_failed): тогда оставляем в
    # новом файле его СТАРЫЕ данные вместо того, чтобы затирать нулями (см.
    # инцидент 19.09.2026 - массовый 429 от Yandex Rasp API на старте бота
    # переписал flights_data.json пустыми прилётами для всех аэропортов).
    previous_result = None
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
                previous_result = json.load(f)
        except Exception as e:
            logger.warning(f"⚠️ Не удалось прочитать предыдущий {OUTPUT_FILE}: {e}")

    result = {
        'generated_at': datetime.now().isoformat(),
        'date': today,
        'airports': {},
    }

    # CIRCUIT BREAKER (добавлено 19.09.2026): если ключ целиком заблокирован
    # Яндексом (см. инцидент 19.09.2026 - письмо "Доступ к сервису API
    # Яндекс.Расписаний заблокирован из-за превышений лимита", ключ висит
    # заблокированным ДО СЛЕДУЮЩИХ СУТОК), то долбить ОСТАЛЬНЫЕ ~12
    # аэропортов подряд после первых же провалов - чистая трата времени и
    # только усугубляет ситуацию (лишние запросы к уже заблокированному
    # ключу). Если подряд провалилось CONSECUTIVE_FAILURES_CIRCUIT_BREAK
    # аэропортов - останавливаем весь прогон досрочно, сохраняя прошлые
    # данные для всех ОСТАВШИХСЯ аэропортов как есть (без единого лишнего
    # запроса), вместо того чтобы упрямо идти по всему списку.
    CONSECUTIVE_FAILURES_CIRCUIT_BREAK = 3
    consecutive_failures = 0
    circuit_broken = False

    for airport in AIRPORTS:
        iata, icao, name, station_code = airport['iata'], airport['icao'], airport['name'], airport['yandex_code']
        # Дата - по СВОЕМУ часовому поясу аэропорта (см. комментарий у
        # AIRPORTS выше), а не по общему московскому "today".
        try:
            airport_local_now = datetime.now(ZoneInfo(airport.get('timezone', 'Europe/Moscow')))
            airport_today = airport_local_now.strftime('%Y-%m-%d')
            local_hour = airport_local_now.hour
        except Exception:
            airport_today = today
            local_hour = datetime.now(MSK_TZ).hour

        if airport.get('closed'):
            logger.info(f"⏭️  {name} ({iata}) закрыт - пропускаю без единого запроса к API")
            result['airports'][icao] = {'iata': iata, 'arrivals': [], 'closed': True}
            continue

        if not station_code:
            logger.warning(f"⚠️  Нет кода станции для {iata}, пропускаю")
            result['airports'][icao] = {'iata': iata, 'arrivals': []}
            continue

        if circuit_broken:
            # Ключ, судя по всему, заблокирован целиком - не делаем ни
            # одного лишнего запроса, просто сохраняем прошлые данные.
            prev_airport = (previous_result or {}).get('airports', {}).get(icao)
            if prev_airport and prev_airport.get('arrivals'):
                result['airports'][icao] = prev_airport
                logger.warning(f"⏭️  {name}: пропускаю запрос (ключ похоже заблокирован) - оставляю предыдущие данные")
            else:
                result['airports'][icao] = {'iata': iata, 'arrivals': []}
            continue

        # ДОБАВЛЕНО 22.09.2026 (см. BIG_CITY_ICAO/should_fetch_arrivals выше,
        # прямая просьба пользователя про ночные окна и частоту опроса) - вне
        # своего "окна" этот час просто пропускаем БЕЗ единого запроса к API,
        # оставляя предыдущие данные как есть (в т.ч. departures, если были -
        # см. fetch_departures_if_needed ниже, у него своя отдельная логика
        # для того же случая).
        if not should_fetch_arrivals(icao, local_hour):
            prev_airport = (previous_result or {}).get('airports', {}).get(icao)
            if prev_airport:
                result['airports'][icao] = prev_airport
                logger.info(f"⏭️  {name}: вне окна опроса (местное время {local_hour}:00) - оставляю предыдущие данные без запроса")
            else:
                result['airports'][icao] = {'iata': iata, 'arrivals': []}
                logger.info(f"⏭️  {name}: вне окна опроса (местное время {local_hour}:00), прошлых данных тоже нет - пусто")
            continue

        logger.info(f"✈️  Обрабатываю {name} ({iata}/{icao})...")
        raw_schedule = fetch_schedule(station_code, 'arrival', airport_today)

        if raw_schedule is None:
            # Не удалось получить данные (429/ошибка) даже после ретраев -
            # оставляем прошлые данные этого аэропорта как есть, если они
            # были, вместо того чтобы писать пустой список.
            prev_airport = (previous_result or {}).get('airports', {}).get(icao)
            if prev_airport and prev_airport.get('arrivals'):
                result['airports'][icao] = prev_airport
                logger.warning(
                    f"⚠️ {name}: не удалось получить свежие данные - оставляю "
                    f"предыдущие ({len(prev_airport['arrivals'])} прилётов, "
                    f"устарели с {previous_result.get('generated_at', '?')})"
                )
            else:
                result['airports'][icao] = {'iata': iata, 'arrivals': []}
                logger.error(f"❌ {name}: не удалось получить данные, и прошлых данных тоже нет")

            consecutive_failures += 1
            if consecutive_failures >= CONSECUTIVE_FAILURES_CIRCUIT_BREAK:
                circuit_broken = True
                logger.error(
                    f"🚫 {consecutive_failures} аэропорта(ов) подряд не удалось получить - похоже, ключ "
                    f"заблокирован Яндексом целиком (см. письмо 'Доступ к сервису API Яндекс.Расписаний "
                    f"заблокирован'). Останавливаю прогон досрочно, оставшиеся аэропорты беру из кэша "
                    f"без дополнительных запросов."
                )
            else:
                time.sleep(2.0)
            continue

        consecutive_failures = 0
        arrivals_today = parse_flights(raw_schedule, 'arrival')

        # ДОБАВЛЕНО 21.09.2026 (прямая просьба пользователя - "пусть и старые,
        # но данные, лучше чем ничего"): раньше "прошлые данные вместо нулей"
        # подставлялись ТОЛЬКО когда запрос к API явно провалился
        # (raw_schedule is None, см. блок выше). Но бывает и другой случай -
        # API ответил УСПЕШНО, без ошибки, но с пустым списком рейсов (даже
        # после починки бага с часовым поясом 20-21.09.2026 такое возможно:
        # временный сбой на стороне Яндекса, редкий пустой ответ и т.п.). Раньше
        # это тихо затирало предыдущие хорошие данные нулями. Теперь, если
        # СВЕЖИЙ ответ пуст, а с прошлого запуска остались непустые данные по
        # этому же аэропорту - оставляем старые данные (drivers видят цифры,
        # пусть и не совсем свежие, вместо голого "0 рейсов").
        if not arrivals_today:
            prev_airport = (previous_result or {}).get('airports', {}).get(icao)
            if prev_airport and prev_airport.get('arrivals'):
                result['airports'][icao] = prev_airport
                logger.warning(
                    f"⚠️ {name}: свежий ответ API пуст (0 рейсов) - оставляю предыдущие "
                    f"{len(prev_airport['arrivals'])} прилётов (устарели с "
                    f"{previous_result.get('generated_at', '?')}) вместо нулей"
                )
                time.sleep(2.0)
                fetch_departures_if_needed(icao, name, station_code, airport_today, previous_result, result['airports'][icao], local_hour)
                continue

        result['airports'][icao] = {
            'iata': iata,
            'arrivals': arrivals_today,
        }
        logger.info(f"✅ {name}: {len(arrivals_today)} прилётов")
        time.sleep(2.0)  # не долбим API слишком часто (было 0.3с, потом 0.5с - всё равно 429
        # почти на каждом аэропорте подряд, см. инцидент 19.09.2026 20:20-20:24 МСК)
        fetch_departures_if_needed(icao, name, station_code, airport_today, previous_result, result['airports'][icao], local_hour)

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # 'departures' больше не собираются (см. докстринг модуля - убраны ради
    # экономии квоты), поэтому считаем только arrivals. Раньше здесь было
    # a['departures'], которого в словаре нет - падало с KeyError: 'departures'
    # на каждом запуске (см. лог "Ошибка фонового обновления: 'departures'").
    total_flights = sum(len(a.get('arrivals', [])) for a in result['airports'].values())
    logger.info(f"💾 Сохранено в {OUTPUT_FILE} ({total_flights} рейсов всего)")

    empty_airports = [a['name'] for a in AIRPORTS if not a.get('closed') and result['airports'].get(a['icao'], {}).get('arrivals') == []]
    if empty_airports:
        logger.warning(f"⚠️  Без рейсов на сегодня: {', '.join(empty_airports)} (может быть нормально для некоторых южных аэропортов, или просто нет рейсов в системе на сегодняшнюю дату)")

    # Фиксируем фактический расход в дневном лог-файле (переживает перезапуски по крону)
    new_total_today = used_today + REQUEST_COUNT
    usage_log[today] = new_total_today
    # чистим записи старше недели, чтобы файл не рос бесконечно
    cutoff = (datetime.now(MSK_TZ) - timedelta(days=7)).strftime('%Y-%m-%d')
    usage_log = {d: v for d, v in usage_log.items() if d >= cutoff}
    save_usage_log(usage_log)

    logger.info(f"📊 Потрачено запросов за этот запуск: {REQUEST_COUNT} | Всего сегодня: {new_total_today}/{DAILY_QUOTA}")
    if REQUEST_COUNT:
        max_runs_per_day = DAILY_QUOTA // REQUEST_COUNT
        interval_minutes = (24 * 60) // max_runs_per_day if max_runs_per_day else None
        if interval_minutes:
            logger.info(f"📈 При таком расходе на цикл можно запускать максимум {max_runs_per_day} раз/сутки (раз в ~{interval_minutes} мин). Скрипт сам не даст себя запустить, если дневной лимит подходит к концу.")

    return 'ok'


if __name__ == '__main__':
    main()
