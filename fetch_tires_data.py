#!/usr/bin/env python3
"""
Шиномонтажи для кнопки "🔧 Шиномонтаж" (модуль "Инструменты водителя").
Источник - OpenStreetMap через Overpass API (публичный, ключ не нужен).
Вызывается вручную/по расписанию (как fetch_parking_data.py,
fetch_yandex_data.py и fetch_timepad_data.py) - НЕ фоновой задачей внутри
самого бота.

⚠️ ВАЖНО - egress: см. подробное объяснение в fetch_parking_data.py (тот же
хост overpass-api.de, та же блокировка и то же решение - fetch из браузера).

Тег shop=tyres. Фильтр "бесплатно" не применяется - это платные сервисы по
определению, бот просто показывает ближайший.

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
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tires_data.json')
REQUEST_TIMEOUT = 100

CITY_BBOX = {
    'moscow': (55.49, 37.32, 55.96, 37.97),
}

GRID_STEP = 0.0015  # ~120 м на широте Москвы


def fetch_city_tires(bbox):
    south, west, north, east = bbox
    query = f'''[out:json][timeout:90];
(
  node["shop"="tyres"]({south},{west},{north},{east});
  way["shop"="tyres"]({south},{west},{north},{east});
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
        logger.info(f"🔄 Тяну шиномонтажи для {bot_city}...")
        try:
            points = fetch_city_tires(bbox)
            result['cities'][bot_city] = points
            logger.info(f"✅ {bot_city}: {len(points)} шиномонтажей (после дедупа)")
        except Exception as e:
            logger.error(f"❌ Не удалось получить шиномонтажи для {bot_city}: {e}")
            result['cities'][bot_city] = []
        time.sleep(0.5)

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False)
    logger.info(f"💾 Сохранено в {OUTPUT_FILE}")


if __name__ == '__main__':
    main()
