#!/usr/bin/env python3
"""
Электрозарядки для кнопки "🔌 Электрозарядки" (модуль "Инструменты водителя").
Источник - OpenStreetMap через Overpass API (публичный, ключ не нужен).
Вызывается вручную/по расписанию (как fetch_parking_data.py,
fetch_yandex_data.py и fetch_timepad_data.py) - НЕ фоновой задачей внутри
самого бота.

⚠️ ВАЖНО - egress: см. подробное объяснение в fetch_parking_data.py (тот же
хост overpass-api.de, та же блокировка и то же решение - fetch из браузера).

Тег amenity=charging_station. Сохраняем дополнительно socket-теги (тип
разъёма) в поле 'sockets' - если они есть в OSM, но бот пока их не
показывает отдельно (только название/часы/дистанция, как у остальных
разделов) - задел на будущее, не обязательный для отображения.

Точки дедуплицированы по сетке ~120м.
"""
import os
import json
import time
import logging
from datetime import datetime, timezone

import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

OVERPASS_URL = 'https://overpass-api.de/api/interpreter'
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ev_charging_data.json')
REQUEST_TIMEOUT = 100

# Бот-город -> bbox (south, west, north, east). Все 12 городов бота (см.
# CITY_DISPLAY_NAMES в main.py) - границы приблизительные, по урбанизированному
# ядру города + запас (как у Москвы - МКАД + запас, а не вся адм. территория с
# удалёнными посёлками). Сочи - отдельный случай: официальный "Большой Сочи"
# тянется ~145км вдоль берега (от Лазаревского до границы с Абхазией), но для
# водителя актуален центр + Адлер (там аэропорт AER) - взят именно этот
# отрезок побережья, а не вся адм. граница.
CITY_BBOX = {
    'moscow': (55.49, 37.32, 55.96, 37.97),
    'spb': (59.75, 29.85, 60.10, 30.75),
    'novosibirsk': (54.85, 82.75, 55.15, 83.20),
    'ekb': (56.70, 60.40, 56.95, 60.80),
    'kazan': (55.65, 48.95, 55.90, 49.35),
    'chelyabinsk': (55.05, 61.25, 55.30, 61.60),
    'omsk': (54.85, 73.15, 55.10, 73.50),
    'samara': (53.05, 49.95, 53.35, 50.35),
    'rostov': (47.10, 39.50, 47.35, 39.90),
    'nnovgorod': (56.15, 43.75, 56.45, 44.15),
    'krasnodar': (44.90, 38.80, 45.15, 39.15),
    'sochi': (43.38, 39.60, 43.70, 40.00),
}

GRID_STEP = 0.0015  # ~120 м на широте Москвы


def fetch_city_ev_charging(bbox):
    south, west, north, east = bbox
    query = f'''[out:json][timeout:90];
(
  node["amenity"="charging_station"]({south},{west},{north},{east});
  way["amenity"="charging_station"]({south},{west},{north},{east});
);
out center tags;'''
    resp = requests.post(OVERPASS_URL, data={'data': query}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    raw_points = []
    for el in data.get('elements', []):
        tags = el.get('tags') or {}
        if el.get('type') == 'node':
            lat, lon = el.get('lat'), el.get('lon')
        else:
            center = el.get('center') or {}
            lat, lon = center.get('lat'), center.get('lon')
        if lat is None or lon is None:
            continue
        raw_points.append((round(lat, 6), round(lon, 6), tags.get('name'), tags.get('opening_hours')))

    cells = {}
    for lat, lon, name, hours in raw_points:
        key = (round(lat / GRID_STEP), round(lon / GRID_STEP))
        existing = cells.get(key)
        if not existing or (not existing[2] and name):
            cells[key] = (lat, lon, name, hours)

    return [{'lat': lat, 'lon': lon, 'name': name, 'hours': hours} for lat, lon, name, hours in cells.values()]


def main():
    result = {
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        'cities': {},
    }
    for bot_city, bbox in CITY_BBOX.items():
        logger.info(f"🔄 Тяну электрозарядки для {bot_city}...")
        try:
            points = fetch_city_ev_charging(bbox)
            result['cities'][bot_city] = points
            logger.info(f"✅ {bot_city}: {len(points)} электрозарядок (после дедупа)")
        except Exception as e:
            logger.error(f"❌ Не удалось получить электрозарядки для {bot_city}: {e}")
            result['cities'][bot_city] = []
        time.sleep(0.5)

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False)
    logger.info(f"💾 Сохранено в {OUTPUT_FILE}")


if __name__ == '__main__':
    main()
