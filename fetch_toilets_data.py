#!/usr/bin/env python3
"""
Места с туалетом для кнопки "🚻 Туалеты рядом" (модуль "Инструменты
водителя"). Источник - OpenStreetMap через Overpass API (публичный, ключ не
нужен). Вызывается вручную/по расписанию (как fetch_parking_data.py,
fetch_yandex_data.py и fetch_timepad_data.py) - НЕ фоновой задачей внутри
самого бота.

⚠️ ВАЖНО - egress: см. подробное объяснение в fetch_parking_data.py (тот же
хост overpass-api.de, та же блокировка и то же решение - fetch из браузера).

По просьбе пользователя список расширен: помимо явных общественных туалетов
(amenity=toilets) тянем ещё категории мест, где туалет почти наверняка есть
(хоть и не гарантирован явным тегом в OSM) - заправки, кафе, рестораны:
  toilet     - amenity=toilets (явный туалет)
  fuel       - amenity=fuel (АЗС)
  cafe       - amenity=cafe
  restaurant - amenity=restaurant
ТЦ (shop=mall/department_store) пользователь попросил НЕ включать (было в
первой версии, убрано по прямой просьбе) - в ТЦ туалет часто не быстро найти/
дойти (охрана, несколько этажей), это не то же самое, что заправка или кафе
у дороги.
Каждая точка помечена полем "kind" - бот показывает его в списке (иконкой),
чтобы водитель понимал, что это не гарантированный туалет, а место, где он,
скорее всего, есть (заведение можно попросить/купить что-то по пути).

Для явных туалетов фильтр "бесплатно" НЕ применяется - amenity=toilets в OSM
почти всегда общедоступные точки, доп. фильтрация по fee/access для них не
так надёжна, как для парковок. Для заправок/кафе/ресторанов такого фильтра
нет вообще - это платные заведения по определению (зайти можно всегда, а вот
туалет бесплатно или для клиентов - уже на месте видно).

Точки дедуплицированы по сетке ~120м - но ОТДЕЛЬНО для каждого kind, чтобы,
например, заправка и стоящее рядом кафе не схлопнулись в одну точку (это
разные места, оба стоит показать).

Помимо названия сохраняем opening_hours (часы работы) - тег в OSM есть у
заметной части точек и не требует доп. запросов. Цену/стоимость НЕ собираем -
пользователь подтвердил, что цены на услуги в OSM почти никогда нет (некому
вносить), показывать в боте будем без неё.
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
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'toilets_data.json')
REQUEST_TIMEOUT = 100

# Бот-город -> bbox (south, west, north, east). Расширено на все 12 городов
# бота (см. CITY_DISPLAY_NAMES в main.py) - границы приблизительные, по
# урбанизированному ядру города + запас (как у Москвы - МКАД + запас, а не
# вся адм. территория с удалёнными посёлками). Сочи - отдельный случай:
# официальный "Большой Сочи" тянется ~145км вдоль берега (от Лазаревского до
# границы с Абхазией), но для водителя актуален центр + Адлер (там аэропорт
# AER) - взят именно этот отрезок побережья, а не вся адм. граница.
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

# OSM-теги для каждой категории - (ключ_тега, значение_тега) -> наш "kind".
# ТЦ (shop=mall/department_store) сюда намеренно НЕ входит - убрано по
# просьбе пользователя, см. docstring модуля.
TOILET_LIKE_TAGS = [
    (('amenity', 'toilets'), 'toilet'),
    (('amenity', 'fuel'), 'fuel'),
    (('amenity', 'cafe'), 'cafe'),
    (('amenity', 'restaurant'), 'restaurant'),
]


def classify(tags):
    """Возвращает kind по тегам элемента, или None, если ни одна из
    TOILET_LIKE_TAGS категорий не подошла (не должно случаться - Overpass
    запрос уже фильтрует ровно по этим тегам, но проверяем на всякий случай)."""
    for (key, value), kind in TOILET_LIKE_TAGS:
        if tags.get(key) == value:
            return kind
    return None


def fetch_city_toilets(bbox):
    south, west, north, east = bbox
    filters = '\n  '.join(
        f'node["{key}"="{value}"]({south},{west},{north},{east});\n  '
        f'way["{key}"="{value}"]({south},{west},{north},{east});'
        for (key, value), _kind in TOILET_LIKE_TAGS
    )
    query = f'''[out:json][timeout:90];
(
  {filters}
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
        raw_points.append((round(lat, 6), round(lon, 6), tags.get('name'), tags.get('opening_hours'), kind))

    # Дедуп по сетке - ОТДЕЛЬНО на каждый kind (см. docstring модуля), чтобы
    # разные заведения рядом друг с другом не схлопывались в одно.
    cells = {}
    for lat, lon, name, hours, kind in raw_points:
        key = (kind, round(lat / GRID_STEP), round(lon / GRID_STEP))
        existing = cells.get(key)
        if not existing or (not existing[2] and name):
            cells[key] = (lat, lon, name, hours, kind)

    return [
        {'lat': lat, 'lon': lon, 'name': name, 'hours': hours, 'kind': kind}
        for lat, lon, name, hours, kind in cells.values()
    ]


def main():
    result = {
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        'cities': {},
    }
    for bot_city, bbox in CITY_BBOX.items():
        logger.info(f"🔄 Тяну туалеты+заправки/кафе/рестораны для {bot_city}...")
        try:
            points = fetch_city_toilets(bbox)
            result['cities'][bot_city] = points
            by_kind = {}
            for p in points:
                by_kind[p['kind']] = by_kind.get(p['kind'], 0) + 1
            logger.info(f"✅ {bot_city}: {len(points)} точек (после дедупа) - {by_kind}")
        except Exception as e:
            logger.error(f"❌ Не удалось получить точки для {bot_city}: {e}")
            result['cities'][bot_city] = []
        time.sleep(0.5)

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False)
    logger.info(f"💾 Сохранено в {OUTPUT_FILE}")


if __name__ == '__main__':
    main()
