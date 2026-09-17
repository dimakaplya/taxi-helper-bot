#!/usr/bin/env python3
"""
Афиша города (концерты/спектакли/выставки/фестивали) для кнопки "🎭 События
города". Источник - публичный бесплатный API KudaGo (docs.kudago.com), ключ
не нужен. Вызывается фоново из бота (events_data_updater() в
main_airports_24h.py), как и fetch_yandex_data.py/fetch_favt_notices.py.

⚠️ ВАЖНОЕ ОГРАНИЧЕНИЕ: KudaGo покрывает всего 5 городов - Москва, СПб,
Екатеринбург, Казань, Нижний Новгород (проверено запросом к
/public-api/v1.4/locations/). Из 12 городов бота под это попадают только 4
(moscow, spb, ekb, kazan) - см. KUDAGO_CITY_MAP. Остальные 8 городов
(novosibirsk, chelyabinsk, omsk, samara, rostov, ufa, krasnodar, sochi)
остаются без данных о событиях - бот должен явно показывать "нет данных",
а не молчать. Если найдётся альтернативный источник для этих городов -
дополнять сюда же, формат events_data.json рассчитан на это (cities - это
словарь бот-город -> список событий, добавить город можно независимо).

Категории событий разделены под классы такси (сама фильтрация - на стороне
бота, здесь просто тянем всё сразу одним запросом на город):
  - Ultima (значимые события): concert, theater
  - Такси эконом/комфорт (более массовые): exhibition, festival
"""
import os
import json
import time
import logging
from datetime import datetime, timezone

import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BASE_URL = 'https://kudago.com/public-api/v1.4'
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'events_data.json')

# Бот-город -> код локации KudaGo (slug из /public-api/v1.4/locations/).
KUDAGO_CITY_MAP = {
    'moscow': 'msk',
    'spb': 'spb',
    'ekb': 'ekb',
    'kazan': 'kzn',
}

EVENT_CATEGORIES = ['concert', 'theater', 'exhibition', 'festival']
EVENTS_LOOKAHEAD_DAYS = 30  # не тянем события дальше чем на месяц вперёд
EVENTS_PER_CITY = 40  # сколько ближайших (по дате) событий сохраняем на город
REQUEST_TIMEOUT = 20


def fetch_city_events(kudago_slug):
    """Тянет ближайшие события города по всем EVENT_CATEGORIES ОДНИМ запросом
    (KudaGo принимает список категорий через запятую), затем для каждого
    события ищет БЛИЖАЙШУЮ будущую дату из списка его показов - у
    регулярных мероприятий (абонементные спектакли, повторяющиеся концерты)
    "dates" содержит вообще все показы, включая прошлые и далёкие будущие,
    а не одну дату события."""
    now = int(time.time())
    until = now + EVENTS_LOOKAHEAD_DAYS * 86400
    raw_events = []
    page = 1
    max_pages = 5  # защита от бесконечной пагинации (до ~500 событий на город)
    while page <= max_pages:
        try:
            resp = requests.get(
                f'{BASE_URL}/events/',
                params={
                    'location': kudago_slug,
                    'lang': 'ru',
                    'categories': ','.join(EVENT_CATEGORIES),
                    'fields': 'title,price,is_free,place,dates,categories,site_url',
                    'expand': 'place',
                    'actual_since': now,
                    'actual_until': until,
                    'order_by': '-publication_date',
                    'page_size': 100,
                    'page': page,
                },
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"❌ Ошибка запроса событий KudaGo ({kudago_slug}, стр. {page}): {e}")
            break

        results = data.get('results', [])
        raw_events.extend(results)
        if not data.get('next') or not results:
            break
        page += 1
        time.sleep(0.2)

    # Приводим к плоской записи с ближайшим показом - так боту не нужно
    # заново парсить массив дат при каждом нажатии кнопки.
    normalized = []
    for ev in raw_events:
        upcoming = [d['start'] for d in ev.get('dates', []) if d.get('start') and now <= d['start'] <= until]
        if not upcoming:
            continue
        place = ev.get('place') or {}
        normalized.append({
            'title': (ev.get('title') or '').strip().capitalize(),
            'start': min(upcoming),
            'place_title': place.get('title', ''),
            'place_address': place.get('address', ''),
            'price': ev.get('price') or '',
            'is_free': bool(ev.get('is_free')),
            'categories': ev.get('categories', []),
            'url': ev.get('site_url', ''),
        })

    normalized.sort(key=lambda e: e['start'])
    return normalized[:EVENTS_PER_CITY]


def main():
    result = {
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        'cities': {},
    }
    for bot_city, kudago_slug in KUDAGO_CITY_MAP.items():
        logger.info(f"🔄 Тяну события KudaGo для {bot_city} ({kudago_slug})...")
        try:
            events = fetch_city_events(kudago_slug)
            result['cities'][bot_city] = events
            logger.info(f"✅ {bot_city}: {len(events)} событий")
        except Exception as e:
            logger.error(f"❌ Не удалось получить события для {bot_city}: {e}")
            result['cities'][bot_city] = []
        time.sleep(0.3)

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info(f"💾 Сохранено в {OUTPUT_FILE}")


if __name__ == '__main__':
    main()
