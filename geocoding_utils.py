#!/usr/bin/env python3
"""
Общий бесплатный геокодер (адрес/название места -> координаты) для всех
фоновых сборщиков, которым нужно положить что-то на карту водителей (см.
/map/road_events, /map/events в main.py): fetch_road_events.py (ДТП по
адресу из текста поста) и fetch_concert_events.py (афиша по названию
площадки). Вынесено в отдельный модуль, чтобы не дублировать одну и ту же
логику кэша/rate-limit в обоих сборщиках - по просьбе пользователя
(22.09.2026), после "вынеси на карту дорожные события" следом попросил то
же самое для афиши.

Используется Nominatim (OpenStreetMap) - тот же провайдер тайлов, что уже
на карте водителей (map_webapp_html в main.py), без API-ключа. По его
usage policy: не более 1 запроса в секунду и обязательный User-Agent с
контактом - см. geocode_address ниже.
"""
import os
import json
import time
import logging
import requests

logger = logging.getLogger(__name__)

GEOCODE_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'geocode_cache.json')
NOMINATIM_URL = 'https://nominatim.openstreetmap.org/search'
NOMINATIM_USER_AGENT = 'TaxiHelperBot/1.0 (https://github.com/dimakaplya/taxi-helper-bot)'
CITY_GEOCODE_HINT = {'moscow': 'Москва', 'spb': 'Санкт-Петербург'}


def load_geocode_cache():
    try:
        with open(GEOCODE_CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_geocode_cache(cache):
    try:
        with open(GEOCODE_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"⚠️ Не удалось сохранить кэш геокодирования: {e}")


def geocode_address(query, bot_city, cache, namespace='addr'):
    """Координаты произвольного запроса (адрес улицы ИЛИ название площадки/
    заведения) через Nominatim, с диск-кэшем. namespace разделяет кэш для
    разных видов запросов на один и тот же текст (например, "Pravda" как
    адрес и "Pravda" как название клуба - маловероятно, но дёшево
    подстраховаться), ключ включает и город (одна и та же улица/площадка
    существует в разных городах). cache хранит и УДАЧНЫЕ, и НЕУДАЧНЫЕ
    попытки (None), чтобы не долбить по одному и тому же нераспознанному
    запросу на каждом цикле обновления. Возвращает [lat, lon] или None."""
    city_hint = CITY_GEOCODE_HINT.get(bot_city, '')
    key = f'{namespace}::{bot_city}::{query.lower()}'
    if key in cache:
        return cache[key]
    coords = None
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={'format': 'json', 'q': f'{query}, {city_hint}, Россия', 'limit': 1},
            headers={'User-Agent': NOMINATIM_USER_AGENT},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
        if results:
            coords = [float(results[0]['lat']), float(results[0]['lon'])]
    except Exception as e:
        logger.warning(f"⚠️ Геокодирование не удалось для '{query}' ({bot_city}): {e}")
    cache[key] = coords
    time.sleep(1.1)  # Nominatim usage policy - не чаще 1 запроса/сек, только для НОВЫХ (некэшированных) запросов
    return coords
