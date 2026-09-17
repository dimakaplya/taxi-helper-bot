#!/usr/bin/env python3
"""
Поезда дальнего следования по избранным вокзалам Москвы (Казанский,
Ленинградский) - для оценки пассажиропотока/спроса на такси у вокзалов, по
аналогии с fetch_yandex_data.py для аэропортов.

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
STATIONS = [
    {'name': 'Казанский вокзал', 'code': 's2000003'},
    {'name': 'Ленинградский вокзал', 'code': 's2006004'},
]

REQUEST_COUNT = 0  # счётчик реальных запросов к API за этот запуск (см. quota-комментарий выше)


def fetch_station_schedule(station_code, event, date_str):
    """event: 'arrival' или 'departure'. Только поезда дальнего следования
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
                    'event': event,
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
            logger.error(f"❌ Ошибка запроса schedule ({event}, {station_code}, offset={offset}): {e}")
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


def parse_trains(schedule_items, event):
    trains = []
    for item in schedule_items:
        thread = item.get('thread', {}) or {}
        time_str = item.get(event)
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

        # Направление: для arrival - откуда идёт поезд, для departure - куда
        point = item.get('departure_from') if event == 'arrival' else item.get('destination_to')
        point_title = None
        if isinstance(point, dict):
            point_title = point.get('title')
        if not point_title:
            point_title = title  # fallback на название нитки маршрута

        trains.append({
            'time': dt.strftime('%H:%M'),
            'point': point_title,
            'carrier': carrier,
            'number': number,
            'is_sapsan': is_sapsan_thread(thread, number, title, short_title),
            'title': title,
        })
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
        arrivals = parse_trains(fetch_station_schedule(code, 'arrival', today), 'arrival')
        departures = parse_trains(fetch_station_schedule(code, 'departure', today), 'departure')
        result['stations'][code] = {
            'name': name,
            'arrivals': arrivals,
            'departures': departures,
        }
        logger.info(f"✅ {name}: {len(arrivals)} прибытий, {len(departures)} отправлений")
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
