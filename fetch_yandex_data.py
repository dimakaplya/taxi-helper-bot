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

Только ПРИЛЁТЫ (вылеты убраны ради экономии квоты) - при лимите ключа 500
запросов/сутки и ~14-25 запросов за один запуск (13 активных аэропортов x 1
направление + пагинация для крупных) помещается заметно больше запусков в
сутки, чем раньше (когда тянули оба направления). Точное число запросов
конкретно у тебя скрипт печатает в конце каждого запуска ("Потрачено запросов").

Рекомендуется гонять по крону раз в 2 часа:
    crontab -e
    0 */2 * * * cd /путь/к/проекту && /usr/bin/python3 fetch_yandex_data.py >> fetch.log 2>&1

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

import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

API_KEY = os.getenv('YANDEX_RASP_API_KEY', 'ВСТАВЬ_СВОЙ_КЛЮЧ_СЮДА')
BASE_URL = 'https://api.rasp.yandex.net/v3.0'

# ⚠️ На Railway диск эфемерный: при каждом редеплое контейнер стартует с чистой
# файловой системой, и api_usage_log.json (счётчик запросов за сегодня) обнуляется -
# именно поэтому дневная квота ключа у Яндекса всё равно превышается, хотя
# DAILY_SAFETY_LIMIT ниже вроде бы должен это предотвращать (см. flights_data_updater()
# в main_airports_24h.py - она дёргает main() сразу при каждом старте бота). Если в
# Railway подключить постоянный volume (Settings -> Volumes, mount path напр. /data) и
# задать переменную окружения DATA_DIR=/data - счётчик и flights_data.json переживут
# редеплои, и защита от блокировки ключа реально заработает. Без volume - переменную
# просто не задавай, всё останется как раньше (файлы рядом со скриптом).
DATA_DIR = os.getenv('DATA_DIR') or os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(DATA_DIR, 'flights_data.json')
USAGE_LOG_FILE = os.path.join(DATA_DIR, 'api_usage_log.json')

# Дневной лимит ключа Yandex Rasp API. Если сегодня уже потрачено
# DAILY_SAFETY_LIMIT запросов - скрипт откажется запускаться, чтобы не
# словить блокировку ключа за превышение (оставляем запас 10% под сам
# этот запуск + ретраи).
DAILY_QUOTA = 500
DAILY_SAFETY_LIMIT = int(DAILY_QUOTA * 0.9)


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
    today = datetime.now().strftime('%Y-%m-%d')
    return today, log.get(today, 0)

# 14 аэропортов бота: IATA/ICAO коды + yandex_code станции (найден вручную
# сопоставлением по названию через stations_list, см. примечание выше).
# yandex_code = None -> аэропорт не найден / закрыт.
AIRPORTS = [
    {'iata': 'SVO', 'icao': 'UUEE', 'name': 'Шереметьево',       'yandex_code': 's9600213'},
    {'iata': 'DME', 'icao': 'UUDD', 'name': 'Домодедово',        'yandex_code': 's9600216'},
    {'iata': 'VKO', 'icao': 'UUWW', 'name': 'Внуково',           'yandex_code': 's9600215'},
    {'iata': 'LED', 'icao': 'ULLI', 'name': 'Пулково',           'yandex_code': 's9600366'},
    {'iata': 'OVB', 'icao': 'UNNT', 'name': 'Толмачёво',         'yandex_code': 's9600374'},
    {'iata': 'SVX', 'icao': 'USSS', 'name': 'Кольцово',          'yandex_code': 's9600370'},
    {'iata': 'KZN', 'icao': 'UWKD', 'name': 'Казань',            'yandex_code': 's9600379'},
    {'iata': 'CEK', 'icao': 'USCC', 'name': 'Баландино',         'yandex_code': 's9623444'},
    {'iata': 'OMS', 'icao': 'UNOO', 'name': 'Омск',              'yandex_code': 's9600390'},
    {'iata': 'KUF', 'icao': 'UWWW', 'name': 'Курумоч',           'yandex_code': 's9600380'},
    {'iata': 'RND', 'icao': 'URRP', 'name': 'Платов (Ростов)',   'yandex_code': 's9866615', 'closed': True},  # ⚠️ закрыт для гражданских полётов - данные не собираем, экономим квоту
    {'iata': 'GOJ', 'icao': 'UWGG', 'name': 'Стригино (Нижний Новгород)', 'yandex_code': 's9623052'},  # заменил Уфу по просьбе пользователя (11-й город бота)
    {'iata': 'KRR', 'icao': 'URKK', 'name': 'Пашковский (Краснодар)', 'yandex_code': 's9623123'},  # вновь открыт с 11.09.2025 (был закрыт с 2022) - запросы к API не пропускаем
    {'iata': 'AER', 'icao': 'URSS', 'name': 'Сочи',              'yandex_code': 's9623547'},
]

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
    'embraer': 100, 'e170': 78, 'e190': 100,
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
    RETRY_ATTEMPTS = 4
    RETRY_BACKOFF_BASE = 3  # секунды: 3, 6, 9, 12 - суммарно ~30с максимум на одну страницу
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
        time.sleep(0.5)

    if first_page_failed:
        return None  # сигнал "не удалось получить данные", отличается от [] ("рейсов правда нет")
    return all_items


def parse_flights(schedule_items, event):
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


def main():
    if API_KEY == 'ВСТАВЬ_СВОЙ_КЛЮЧ_СЮДА':
        logger.error("❌ Не задан YANDEX_RASP_API_KEY! См. инструкцию в шапке файла.")
        return

    usage_log = load_usage_log()
    today, used_today = get_today_usage(usage_log)
    if used_today >= DAILY_SAFETY_LIMIT:
        logger.error(
            f"🚫 Сегодня уже потрачено {used_today}/{DAILY_QUOTA} запросов (порог безопасности "
            f"{DAILY_SAFETY_LIMIT}) - пропускаю запуск, чтобы не превысить дневной лимит ключа. "
            f"Попробуй завтра или запускай реже."
        )
        return
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

    for airport in AIRPORTS:
        iata, icao, name, station_code = airport['iata'], airport['icao'], airport['name'], airport['yandex_code']
        logger.info(f"✈️  Обрабатываю {name} ({iata}/{icao})...")

        if airport.get('closed'):
            logger.info(f"⏭️  {name} ({iata}) закрыт - пропускаю без единого запроса к API")
            result['airports'][icao] = {'iata': iata, 'arrivals': [], 'closed': True}
            continue

        if not station_code:
            logger.warning(f"⚠️  Нет кода станции для {iata}, пропускаю")
            result['airports'][icao] = {'iata': iata, 'arrivals': []}
            continue

        raw_schedule = fetch_schedule(station_code, 'arrival', today)

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
            time.sleep(0.5)
            continue

        arrivals_today = parse_flights(raw_schedule, 'arrival')

        result['airports'][icao] = {
            'iata': iata,
            'arrivals': arrivals_today,
        }
        logger.info(f"✅ {name}: {len(arrivals_today)} прилётов")
        time.sleep(0.5)  # не долбим API слишком часто (был 0.3с - оказалось мало, см. 429 19.09.2026)

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
    cutoff = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    usage_log = {d: v for d, v in usage_log.items() if d >= cutoff}
    save_usage_log(usage_log)

    logger.info(f"📊 Потрачено запросов за этот запуск: {REQUEST_COUNT} | Всего сегодня: {new_total_today}/{DAILY_QUOTA}")
    if REQUEST_COUNT:
        max_runs_per_day = DAILY_QUOTA // REQUEST_COUNT
        interval_minutes = (24 * 60) // max_runs_per_day if max_runs_per_day else None
        if interval_minutes:
            logger.info(f"📈 При таком расходе на цикл можно запускать максимум {max_runs_per_day} раз/сутки (раз в ~{interval_minutes} мин). Скрипт сам не даст себя запустить, если дневной лимит подходит к концу.")


if __name__ == '__main__':
    main()
