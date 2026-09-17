#!/usr/bin/env python3
"""
Поезда дальнего следования по избранным вокзалам 6 городов бота - Москва
(Казанский, Ленинградский), Санкт-Петербург (Московский), Краснодар
(Краснодар-1), Сочи (Адлер), Нижний Новгород (Московский), Казань
(Казань-Пасс.) - для оценки пассажиропотока/спроса на такси у вокзалов, по
аналогии с fetch_yandex_data.py для аэропортов. Привязка станция -> город
бота вынесена в STATION_CITY в main_airports_24h.py (используется для
фильтрации кнопки "🚆 Вокзалы" и списка вокзалов по городу).

⚠️ ВАЖНО: используется ТОТ ЖЕ ключ YANDEX_RASP_API_KEY и та же дневная квота
(500 запросов/сутки) что и у fetch_yandex_data.py - поэтому вся логика учёта
расхода (load_usage_log/save_usage_log/get_today_usage/DAILY_QUOTA/
DAILY_SAFETY_LIMIT/DATA_DIR) ИМПОРТИРУЕТСЯ из fetch_yandex_data.py, а не
дублируется. Если завести отдельный счётчик - оба скрипта будут думать, что
у каждого есть свои отдельные 500 запросов, и вместе легко пробьют реальный
общий лимit ключа (та же ошибка, что уже привела к блокировке ключа один раз).

Берём только transport_types=train (поезда дальнего следования), НЕ
suburban (электрички) - электричек на порядок больше по объёму (нужна
пагинация, риск для квоты) и по смыслу это не тот трафик "пассажир с
чемоданом, ищет такси", что дальние поезда.

Рижский и Савёловский вокзалы не включены - там дальних поездов почти не
осталось, в основном электрички (см. обсуждение с пользователем). Если
понадобятся другие вокзалы - коды станций (система "yandex", префикс "s")
можно посмотреть на rasp.yandex.ru/station/<numeric_id>/ (без префикса s
в URL, добавить вручную при использовании в API).

Только ПРИБЫТИЯ (event=arrival) - водителю такси интересны пассажиры,
которые ПРИЕЗЖАЮТ и ищут машину, а не уезжающие. Заодно это вдвое дешевле по
квоте, чем тянуть ещё и отправления.
"""
import os
import time
import logging
from datetime import datetime

import requests

from fetch_yandex_data import (
    API_KEY, BASE_URL, DATA_DIR,
    load_usage_log, save_usage_log, get_today_usage,
    DAILY_QUOTA, DAILY_SAFETY_LIMIT,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

OUTPUT_FILE = os.path.join(DATA_DIR, 'trains_data.json')

# Коды станций (система "yandex", префикс "s") - найдены по rasp.yandex.ru/station/<id>/
# и проверены по прямым ссылкам вида rasp.yandex.ru/station/<id>/?event=arrival.
STATIONS = [
    {'name': 'Казанский вокзал', 'code': 's2000003'},
    {'name': 'Ленинградский вокзал', 'code': 's2006004'},
    {'name': 'Московский вокзал (СПб)', 'code': 's9602494'},
    {'name': 'Краснодар-1', 'code': 's9613602'},
    {'name': 'Адлер', 'code': 's9613054'},
    {'name': 'Московский вокзал (Нижний Новгород)', 'code': 's9612089'},
    {'name': 'Казань-Пасс.', 'code': 's9623141'},
]

REQUEST_COUNT = 0  # счётчик реальных запросов к API за этот запуск (см. quota-комментарий выше)


def fetch_station_arrivals(station_code, date_str):
    """Только прибытия (event=arrival), только поезда дальнего следования
    (transport_types=train, БЕЗ электричек). Пагинация - на случай если
    вдруг рейсов окажется больше лимита страницы (для двух вокзалов дальнего
    следования это маловероятно, но не будем на это полагаться молча)."""
    global REQUEST_COUNT
    all_items = []
    offset = 0
    page_limit = 100
    max_pages = 5
    for _ in range(max_pages):
        try:
            resp = requests.get(
                f'{BASE_URL}/schedule/',
                params={
                    'apikey': API_KEY,
                    'station': station_code,
                    'date': date_str,
                    'event': 'arrival',
                    'transport_types': 'train',
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
            logger.error(f"❌ Ошибка запроса schedule (arrival, {station_code}, offset={offset}): {e}")
            break

        batch = data.get('schedule', [])
        all_items.extend(batch)

        if len(batch) < page_limit:
            break
        offset += page_limit
        time.sleep(0.2)

    return all_items


def is_sapsan_thread(thread, number, title, short_title):
    """Сапсан - самый важный поезд для спроса на такси (много пассажиров разом,
    прибывает по расписанию день в день). Точный набор полей API под Сапсан не
    проверен вживую (нет доступа к ключу в этой среде) - поэтому проверяем сразу
    несколько вероятных мест по документации Yandex Rasp: express_type,
    transport_subtype.title, и как запасной вариант - подстрока "сапсан" в
    title/short_title/number. Если после реального запуска окажется, что что-то
    из этого не срабатывает - надо свериться с живым ответом API и поправить."""
    express_type = (thread.get('express_type') or '').lower()
    if 'sapsan' in express_type or 'сапсан' in express_type:
        return True
    subtype_title = ((thread.get('transport_subtype') or {}).get('title') or '').lower()
    if 'сапсан' in subtype_title:
        return True
    haystack = f"{title} {short_title} {number}".lower()
    return 'сапсан' in haystack


# Известные "фирменные" (премиальные/брендовые) поезда дальнего следования по
# направлениям НАШИХ вокзалов - Москва-Казань, Москва-СПб, Москва-Н.Новгород,
# Москва-Краснодар/Адлер и т.п. Сознательно НЕ включены названия городов
# (Москва, Казань, Сочи...) - они попадаются в title/short_title КАЖДОГО
# поезда как часть маршрута ("Москва Казанская - Казань"), а не только у
# фирменных, и дали бы массу ложных срабатываний. Список собран по открытым
# данным о фирменных поездах РЖД на этих направлениях - наверняка неполный,
# и без доступа к живому API не проверялся на реальных ответах Yandex Rasp -
# после первого реального запуска стоит свериться и дополнить.
FIRMENNY_TRAIN_NAMES = {
    'татарстан', 'кама', 'стриж', 'буревестник', 'красная стрела',
    'николаевский экспресс', 'мегаполис', 'смена', 'юность',
    'северная пальмира', 'кубань', 'ставрополье', 'жигули', 'лев толстой',
    'чувашия', 'волга',
}


def is_firmenny_thread(thread, number, title, short_title):
    """"Фирменный" поезд - между обычным и Сапсаном по статусу (см.
    FIRMENNY_TRAIN_NAMES выше про источник и ограничения списка). Сапсан
    проверяем ОТДЕЛЬНО и раньше (is_sapsan_thread) - эта функция для
    остальных "именных" поездов повышенной комфортности."""
    express_type = (thread.get('express_type') or '').lower()
    if any(k in express_type for k in ('firm', 'фирм', 'premium')):
        return True
    subtype_title = ((thread.get('transport_subtype') or {}).get('title') or '').lower()
    if 'фирменный' in subtype_title:
        return True
    haystack = f"{title} {short_title} {number}".lower()
    return any(name in haystack for name in FIRMENNY_TRAIN_NAMES)


# Оценка пассажиропотока - у API нет поля вместимости поезда (в отличие от
# самолётов, где есть тип борта - см. AIRCRAFT_CAPACITY в fetch_yandex_data.py),
# поэтому это ГРУБАЯ прикидка по типу поезда, явно помеченная как оценка:
#   - Сапсан: стандартный состав ~604 места, в пиковые дни пускают сдвоенные
#     составы (~1200 мест) - берём широкий диапазон.
#   - Фирменный: состав обычно полный (плацкарт+купе+СВ), но без удвоения
#     как у Сапсана - диапазон между Сапсаном и обычным поездом.
#   - Обычный поезд дальнего следования: состав сильно варьируется (от
#     нескольких вагонов до полноразмерного ночного поезда) - тоже диапазон.
SAPSAN_PASSENGERS = (600, 1000)
FIRMENNY_PASSENGERS = (400, 700)
REGULAR_TRAIN_PASSENGERS = (300, 600)


def estimate_train_passengers(is_sapsan, is_firmenny):
    if is_sapsan:
        return SAPSAN_PASSENGERS
    if is_firmenny:
        return FIRMENNY_PASSENGERS
    return REGULAR_TRAIN_PASSENGERS


def parse_trains(schedule_items):
    """schedule_items - результат fetch_station_arrivals (только прибытия,
    только transport_types=train - пригородные электрички не запрашиваются
    вообще, см. fetch_station_arrivals)."""
    trains = []
    seen_keys = set()
    for item in schedule_items:
        thread = item.get('thread', {}) or {}
        time_str = item.get('arrival')
        if not time_str:
            continue
        try:
            dt = datetime.fromisoformat(time_str)
        except Exception:
            continue

        carrier = (thread.get('carrier') or {}).get('title', 'N/A')
        number = thread.get('number', '')
        title = thread.get('title', '')
        short_title = thread.get('short_title', '')

        # Дедупликация - Yandex Rasp /schedule/ иногда отдаёт ОДНУ И ТУ ЖЕ
        # нитку дважды в ответе (замечено на живых данных пользователя -
        # Адлер/Сириус, у сквозных поездов не со старта маршрута). thread.uid -
        # самый надёжный идентификатор нитки, если он есть в ответе; если нет -
        # запасной ключ (время прибытия + номер поезда) ловит подавляющее
        # большинство настоящих дублей, не путая два РАЗНЫХ поезда, которые
        # случайно прибывают в одну минуту (у тех номер будет другой).
        dedup_key = thread.get('uid') or (dt.strftime('%Y-%m-%d %H:%M'), number)
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        is_sapsan = is_sapsan_thread(thread, number, title, short_title)
        # Фирменный проверяем только если это не Сапсан - Сапсан и так
        # приоритетнее любого другого "фирменного" статуса.
        is_firmenny = (not is_sapsan) and is_firmenny_thread(thread, number, title, short_title)
        pax_min, pax_max = estimate_train_passengers(is_sapsan, is_firmenny)

        # Направление - откуда идёт поезд (для arrival - departure_from). Если
        # Yandex не отдал departure_from (бывает у сквозных поездов не со
        # старта маршрута - тоже замечено на Адлере/Сириусе) - берём title
        # нитки, но он обычно выглядит как "Город А — Город Б" (маршрут
        # целиком, а не только пункт отправления) - в этом случае оставляем
        # только часть ДО тире. Разделители с пробелами вокруг ("Город —
        # Город", "Город - Город"), а не голый дефис - иначе сломались бы
        # названия городов с дефисом без пробелов ("Ростов-на-Дону").
        point = item.get('departure_from')
        point_title = None
        if isinstance(point, dict):
            point_title = point.get('title')
        if not point_title:
            point_title = title
            for dash in (' — ', ' - '):
                if dash in point_title:
                    point_title = point_title.split(dash)[0].strip()
                    break

        trains.append({
            'time': dt.strftime('%H:%M'),
            'point': point_title,
            'carrier': carrier,
            'number': number,
            'title': title,
            'is_sapsan': is_sapsan,
            'is_firmenny': is_firmenny,
            'passengers_min': pax_min,
            'passengers_max': pax_max,
        })
    trains.sort(key=lambda t: t['time'])
    return trains


def main():
    if API_KEY == 'ВСТАВЬ_СВОЙ_КЛЮЧ_СЮДА':
        logger.error("❌ Не задан YANDEX_RASP_API_KEY! См. инструкцию в шапке fetch_yandex_data.py.")
        return

    # Общий с fetch_yandex_data.py лог расхода - читаем ЗАНОВО прямо перед
    # стартом, чтобы увидеть актуальный остаток, даже если этот запуск идёт
    # сразу после fetch_yandex_data.main() в рамках одного цикла обновления.
    usage_log = load_usage_log()
    today, used_today = get_today_usage(usage_log)
    if used_today >= DAILY_SAFETY_LIMIT:
        logger.error(
            f"🚫 Сегодня уже потрачено {used_today}/{DAILY_QUOTA} запросов (общий счётчик с "
            f"fetch_yandex_data.py) - пропускаю обновление поездов, чтобы не превысить дневной лимит ключа."
        )
        return
    logger.info(f"📊 Уже потрачено сегодня (общий счётчик): {used_today}/{DAILY_QUOTA} запросов")

    result = {
        'generated_at': datetime.now().isoformat(),
        'date': today,
        'stations': {},
    }

    for station in STATIONS:
        name, code = station['name'], station['code']
        logger.info(f"🚆 Обрабатываю {name}...")
        arrivals = parse_trains(fetch_station_arrivals(code, today))
        result['stations'][code] = {
            'name': name,
            'arrivals': arrivals,
        }
        logger.info(f"✅ {name}: {len(arrivals)} прибытий")
        time.sleep(0.3)

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        import json
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info(f"💾 Сохранено в {OUTPUT_FILE}")

    # Дописываем СВОЙ расход в ОБЩИЙ лог поверх того, что уже накопилось за
    # сегодня (в т.ч. от fetch_yandex_data.py, если он уже отработал в этом
    # же цикле) - перечитываем на случай, если файл успел измениться.
    usage_log = load_usage_log()
    today, used_today = get_today_usage(usage_log)
    new_total_today = used_today + REQUEST_COUNT
    usage_log[today] = new_total_today
    save_usage_log(usage_log)
    logger.info(f"📊 Потрачено запросов на поезда за этот запуск: {REQUEST_COUNT} | Всего сегодня: {new_total_today}/{DAILY_QUOTA}")


if __name__ == '__main__':
    main()
