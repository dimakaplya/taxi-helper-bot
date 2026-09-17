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
    {'iata': 'SVO', 'icao': 'UUWW', 'name': 'Шереметьево',       'yandex_code': 's9600213'},
    {'iata': 'DME', 'icao': 'UUDD', 'name': 'Домодедово',        'yandex_code': 's9600216'},
    {'iata': 'VKO', 'icao': 'UUWL', 'name': 'Внуково',           'yandex_code': 's9600215'},
    {'iata': 'LED', 'icao': 'UULP', 'name': 'Пулково',           'yandex_code': 's9600366'},
    {'iata': 'OVB', 'icao': 'UNNT', 'name': 'Толмачёво',         'yandex_code': 's9600374'},
    {'iata': 'SVX', 'icao': 'USSS', 'name': 'Кольцово',          'yandex_code': 's9600370'},
    {'iata': 'KZN', 'icao': 'UWKD', 'name': 'Казань',            'yandex_code': 's9600379'},
    {'iata': 'CEK', 'icao': 'UUCC', 'name': 'Баландино',         'yandex_code': 's9623444'},
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


REQUEST_COUNT = 0  # глобальный счётчик реальных запросов к API за этот запуск


def fetch_schedule(station_code, event, date_str):
    """event: 'arrival' или 'departure'. Пагинирует через offset, пока не соберёт все
    рейсы за день. Останавливается как только страница пришла неполной (batch < page_limit) -
    это значит что дальше данных нет, и лишний "пустой" запрос на подтверждение не нужен."""
    global REQUEST_COUNT
    all_items = []
    offset = 0
    page_limit = 500
    max_pages = 10  # защита от бесконечного цикла - максимум 5000 рейсов на аэропорт/направление
    for _ in range(max_pages):
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
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"❌ Ошибка запроса schedule ({event}, {station_code}, offset={offset}): {e}")
            break

        batch = data.get('schedule', [])
        all_items.extend(batch)

        # Страница пришла короче лимита (или пустая) - дальше данных точно нет,
        # не делаем лишний запрос ради подтверждения
        if len(batch) < page_limit:
            break
        offset += page_limit
        time.sleep(0.2)

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

        # Направление: для arrival - откуда, для departure - куда
        point = item.get('departure_from') if event == 'arrival' else item.get('destination_to')
        point_title = None
        if isinstance(point, dict):
            point_title = point.get('title')
        if not point_title:
            point_title = title  # fallback на название нитки маршрута

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

        arrivals_today = parse_flights(fetch_schedule(station_code, 'arrival', today), 'arrival')

        result['airports'][icao] = {
            'iata': iata,
            'arrivals': arrivals_today,
        }
        logger.info(f"✅ {name}: {len(arrivals_today)} прилётов")
        time.sleep(0.3)  # не долбим API слишком часто

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    total_flights = sum(len(a['arrivals']) + len(a['departures']) for a in result['airports'].values())
    logger.info(f"💾 Сохранено в {OUTPUT_FILE} ({total_flights} рейсов всего)")

    empty_airports = [a['name'] for a in AIRPORTS if not a.get('closed') and result['airports'].get(a['icao'], {}).get('arrivals') == [] and result['airports'].get(a['icao'], {}).get('departures') == []]
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
