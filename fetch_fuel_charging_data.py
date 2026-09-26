#!/usr/bin/env python3
"""
Заправки (АЗС) и электрозаправки для карты водителей (см. блок "КАРТА
ВОДИТЕЛЕЙ" в main.py - слои 'fuel'/'charging', фильтры на карте, крауд-
статусы бензина/розеток). Источник - OpenStreetMap через Overpass API
(amenity=fuel, amenity=charging_station), публичный, ключ не нужен. Тот же
паттерн сбора, что уже используется для парковок/туалетов (см.
fetch_parking_data.py/fetch_toilets_data.py) - вызывается вручную/по
расписанию, НЕ фоновой задачей внутри самого бота.

⚠️ ВАЖНО - egress: см. подробное объяснение в fetch_parking_data.py (тот же
хост overpass-api.de, та же блокировка что из облачного контейнера, что с
компьютера пользователя через device_bash). Собрано вручную через fetch() в
контексте страницы браузера (у Overpass открыт CORS) - тот же приём, что и
для парковок/туалетов/TimePad. Этот скрипт можно пробовать запускать как
обычно (вдруг egress когда-то расширят), но если он падает с
ConnectionError/403 - решение то же: собрать через браузер вручную и
обновить fuel_charging_data.json.

У каждой точки есть 'id' - строка вида "node/12345" или "way/12345" (тип +
id самого объекта в OpenStreetMap). Это СТАБИЛЬНЫЙ ключ - именно по нему
бот привязывает крауд-статусы (какой бензин есть/нет, свободна ли зарядка,
см. gas_station_fuel_status/charging_station_status в main.py). Он не
меняется, даже если этот скрипт запустить снова и точки в файле
пересортируются/сместятся - реальный OSM-объект остаётся тем же самым, если
только кто-то не удалит саму точку из OpenStreetMap. Точки без id (устаревший
формат до 22.09.2026) при повторной публикации файла считаются НОВЫМИ -
все их крауд-отметки потеряются, поэтому id сохраняем обязательно.

Точки дедуплицированы по сетке ~120м, ОТДЕЛЬНО для fuel и charging (тот же
приём, что у toilets/parking) - при дедупе внутри одной ячейки побеждает id
точки с непустым названием.

Для зарядок дополнительно сохраняем 'sockets' (число портов на каждый тип
разъёма, из тегов socket:type2/socket:ccs/socket:chademo/... в OSM - есть
далеко не у всех точек) и 'operator' (сеть/оператор, если указан).
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
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fuel_charging_data.json')
REQUEST_TIMEOUT = 100

# Тот же набор 12 городов и те же bbox, что в fetch_parking_data.py/
# fetch_toilets_data.py - см. комментарий там про источник границ.
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

# ДОБАВЛЕНО 26.09.2026 (прямая просьба пользователя - "загрузи базы МО
# области зарядок заправок") - CITY_BBOX['moscow'] покрывает только сам
# город (примерно в границах МКАД), Московская область в него не попадает.
# Бот уже трактует город 'moscow' как "Москва + область" целиком (та же
# логика, что у moscow_district_demand.json/moscow_delivery_demand.json -
# см. main.py), поэтому здесь ДОПОЛНИТЕЛЬНО (не вместо bbox, а вместе с
# ним - см. fetch_all_moscow/main ниже) тянем ещё и всю область отдельным
# запросом area["name"="Московская область"] (сам город Москва - отдельный
# субъект РФ в OSM, area с этим именем НЕ включает его, дублей с bbox выше
# почти нет, а редкие ID, которые всё же совпадут на границе, отфильтровываются
# по id + повторным сеточным дедупом в fetch_all_moscow).
MOSCOW_REGION_AREA_NAME = 'Московская область'

GRID_STEP = 0.0015  # ~120 м на широте Москвы


def classify(tags):
    if tags.get('amenity') == 'fuel':
        return 'fuel'
    if tags.get('amenity') == 'charging_station':
        return 'charging'
    return None


def socket_info(tags):
    sockets = {}
    for key, val in tags.items():
        if not key.startswith('socket:'):
            continue
        if key.endswith((':output', ':voltage', ':current')):
            continue
        name = key[len('socket:'):]
        try:
            n = int(val)
        except (TypeError, ValueError):
            n = 1
        sockets[name] = n
    return sockets


def fetch_city(bbox):
    south, west, north, east = bbox
    query = f'''[out:json][timeout:90];
(
  node["amenity"="fuel"]({south},{west},{north},{east});
  way["amenity"="fuel"]({south},{west},{north},{east});
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
        kind = classify(tags)
        if not kind:
            continue
        if el.get('type') == 'node':
            lat, lon = el.get('lat'), el.get('lon')
        else:
            center = el.get('center') or {}
            lat, lon = center.get('lat'), center.get('lon')
        if lat is None or lon is None:
            continue
        osm_id = f"{el.get('type')}/{el.get('id')}"
        name = tags.get('name') or tags.get('brand') or tags.get('operator')
        point = {
            'id': osm_id,
            'lat': round(lat, 6),
            'lon': round(lon, 6),
            'name': name,
            'kind': kind,
        }
        if kind == 'charging':
            point['sockets'] = socket_info(tags)
            point['operator'] = tags.get('operator') or tags.get('brand')
        raw_points.append(point)

    # Дедуп по сетке - отдельно на fuel/charging (та же схема, что у
    # toilets/parking) - в спорной ячейке побеждает точка с непустым именем.
    cells = {}
    for point in raw_points:
        key = (point['kind'], round(point['lat'] / GRID_STEP), round(point['lon'] / GRID_STEP))
        existing = cells.get(key)
        if not existing or (not existing.get('name') and point.get('name')):
            cells[key] = point

    return list(cells.values())


def dedup_points(points):
    """Сеточный дедуп (см. fetch_city выше), но принимает уже готовый общий
    список точек - используется в fetch_all_moscow, чтобы убрать редкие
    почти-дубли на стыке город/область (после дедупа по id)."""
    cells = {}
    order = []
    for point in points:
        key = (point['kind'], round(point['lat'] / GRID_STEP), round(point['lon'] / GRID_STEP))
        existing = cells.get(key)
        if not existing:
            cells[key] = point
            order.append(key)
        elif not existing.get('name') and point.get('name'):
            cells[key] = point
    return [cells[k] for k in order]


def fetch_area(area_name):
    """Как fetch_city, но area["name"=...] вместо bbox - см.
    MOSCOW_REGION_AREA_NAME выше."""
    query = f'''[out:json][timeout:180];
area["name"="{area_name}"]["admin_level"="4"]->.a;
(
  node["amenity"="fuel"](area.a);
  way["amenity"="fuel"](area.a);
  node["amenity"="charging_station"](area.a);
  way["amenity"="charging_station"](area.a);
);
out center tags;'''
    resp = requests.post(OVERPASS_URL, data={'data': query}, timeout=max(REQUEST_TIMEOUT, 190))
    resp.raise_for_status()
    data = resp.json()

    raw_points = []
    for el in data.get('elements', []):
        tags = el.get('tags') or {}
        kind = classify(tags)
        if not kind:
            continue
        if el.get('type') == 'node':
            lat, lon = el.get('lat'), el.get('lon')
        else:
            center = el.get('center') or {}
            lat, lon = center.get('lat'), center.get('lon')
        if lat is None or lon is None:
            continue
        osm_id = f"{el.get('type')}/{el.get('id')}"
        name = tags.get('name') or tags.get('brand') or tags.get('operator')
        point = {'id': osm_id, 'lat': round(lat, 6), 'lon': round(lon, 6), 'name': name, 'kind': kind}
        if kind == 'charging':
            point['sockets'] = socket_info(tags)
            point['operator'] = tags.get('operator') or tags.get('brand')
        raw_points.append(point)
    return dedup_points(raw_points)


def fetch_all_moscow():
    """'moscow' = город + вся область одним набором точек (см.
    MOSCOW_REGION_AREA_NAME выше) - bbox-точки города + area-точки области,
    дедуп по id (сначала) и затем ещё раз по сетке (на случай, если
    какая-то точка на границе всё же не сматчилась по id)."""
    city_points = fetch_city(CITY_BBOX['moscow'])
    time.sleep(0.5)
    region_points = fetch_area(MOSCOW_REGION_AREA_NAME)
    city_ids = {p['id'] for p in city_points}
    combined = city_points + [p for p in region_points if p['id'] not in city_ids]
    return dedup_points(combined)


def main():
    result = {
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        'cities': {},
    }
    for bot_city, bbox in CITY_BBOX.items():
        logger.info(f"🔄 Тяну заправки+зарядки для {bot_city}...")
        try:
            # ИЗМЕНЕНО 26.09.2026 - см. fetch_all_moscow/MOSCOW_REGION_AREA_NAME
            # выше: только для 'moscow' добавляем область к городу.
            points = fetch_all_moscow() if bot_city == 'moscow' else fetch_city(bbox)
            result['cities'][bot_city] = points
            fuel = sum(1 for p in points if p['kind'] == 'fuel')
            charging = sum(1 for p in points if p['kind'] == 'charging')
            logger.info(f"✅ {bot_city}: {len(points)} точек (fuel={fuel}, charging={charging})")
        except Exception as e:
            logger.error(f"❌ Не удалось получить точки для {bot_city}: {e}")
            result['cities'][bot_city] = []
        time.sleep(0.5)

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False)
    logger.info(f"💾 Сохранено в {OUTPUT_FILE}")


if __name__ == '__main__':
    main()
