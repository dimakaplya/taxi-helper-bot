#!/usr/bin/env python3
"""
Второй источник афиши города (в дополнение к KudaGo, см. fetch_events_data.py) -
TimePad (dev.timepad.ru), публичный API. В отличие от fetch_yandex_data.py
запускается НЕ вручную с компьютера, а фоновой задачей прямо внутри бота на
Railway (timepad_data_updater() в main_airports_24h.py) - токен хранится в
переменной окружения Railway, а не в этом файле.

=== КАК ПОЛУЧИТЬ ТОКЕН ===
1. Зайди на https://dev.timepad.ru/, зарегистрируйся/войди
2. Раздел получения токена (см. "Токен API" в документации разработчика)
3. Экспортируй его на Railway: переменная окружения TIMEPAD_TOKEN

=== ИЗВЕСТНОЕ ОГРАНИЧЕНИЕ: 403 С RAILWAY ===
Cloudflare у TimePad блокирует запросы именно с IP-адресов Railway (403
Forbidden) - подтверждено: смена User-Agent на браузерный НЕ помогла (тот же
403 после фикса и редеплоя), а точно тот же запрос с браузерного IP отвечает
200. Значит блокировка по IP/репутации датацентра, а не по заголовкам - это
нельзя починить правкой запроса. Отправка токена через сторонний
relay-прокси (например r.jina.ai) рассматривалась и ОТКЛОНЕНА - это отдаёт
API-токен пользователя незнакомому третьему сервису, что неприемлемо для
учётных данных. Решение см. в диалоге с пользователем/README проекта -
нужен способ выполнять запрос с НЕ-датацентрового IP без передачи токена
третьей стороне (например: собственный маленький relay пользователя,
запрос вручную с компьютера по крону, или смена облачного провайдера).

=== ПОЧЕМУ ПОКРЫВАЕТ ТОЛЬКО МОСКВУ ===
Пока подключена только Москва (TIMEPAD_CITY_MAP). У TimePad нет координат
места (только текстовый адрес) и нет отдельного price-поля в верхнем уровне
события - цена лежит в registration_data.price_min/price_max. Общий фид
города огромный (десятки тысяч событий) и в основном состоит из мелких
самостоятельных квестов-экскурсий без гида и небольших бизнес-встреч
(20-200 человек) - это НЕ события, вызывающие всплеск спроса на такси.
Поэтому здесь двойной фильтр:
  1. По категориям - только "Концерты", "Вечеринки", "Бизнес", "Искусство и
     культура", "Театры" (TIMEPAD_CATEGORY_IDS) - без "Экскурсии и
     путешествия", где сидят все мелкие квесты.
  2. По размеру - только события с registration_data.tickets_total >=
     TIMEPAD_MIN_TICKETS (200) - отсекает мелкие бизнес-завтраки/круглые
     столы, оставляет форумы/концерты/фестивали заметного масштаба.
"""
import os
import time
import logging
from datetime import datetime, timezone

import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BASE_URL = 'https://api.timepad.ru/v1'
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'timepad_data.json')

TIMEPAD_TOKEN = os.getenv('TIMEPAD_TOKEN', '')

# Бот-город -> название города у TimePad (параметр cities). Пока только
# Москва - см. пояснение в шапке файла.
TIMEPAD_CITY_MAP = {
    'moscow': 'Москва',
}

# Категории TimePad, id получены живым запросом к /v1/events.json и сверены
# по названию (единого справочника категорий с постоянными id в открытой
# документации нет, поэтому id захардкожены и подписаны для проверки):
#   217 - Бизнес, 457 - Вечеринки, 458 - Выставки,
#   459 - Театры, 460 - Концерты, 525 - Искусство и культура
TIMEPAD_CATEGORY_IDS = [217, 457, 458, 459, 460, 525]

# Минимальное число билетов на событие (registration_data.tickets_total) -
# порог "заметного масштаба", отсекающий мелкие бизнес-встречи/круглые столы.
# Именно tickets_total (сколько всего билетов выпущено на событие), а НЕ
# оставшееся число - при их отсутствии/нуле событие пропускаем (не можем
# подтвердить масштаб, лучше не показать, чем засорить афишу мелким).
TIMEPAD_MIN_TICKETS = 200

EVENTS_LOOKAHEAD_DAYS = 30
EVENTS_PER_CITY = 40
REQUEST_TIMEOUT = 20

def fetch_city_events(timepad_city):
    """Тянет ближайшие крупные события города одним проходом с пагинацией
    (TimePad отдаёт events.json с limit/skip). Фильтр по категориям и
    starts_at_min делается на стороне TimePad (query-параметры), фильтр по
    tickets_total - на нашей стороне (в ответе нет такого query-параметра)."""
    if not TIMEPAD_TOKEN:
        logger.warning("⚠️ TIMEPAD_TOKEN не задан - пропускаю TimePad")
        return []

    now_dt = datetime.now(timezone.utc)
    starts_at_min = now_dt.strftime('%Y-%m-%dT%H:%M:%S')

    normalized = []
    skip = 0
    limit = 100
    max_pages = 5  # защита от бесконечной пагинации (до 500 событий на город)
    # TimePad стоит за Cloudflare. Обычный User-Agent библиотеки requests
    # ("python-requests/2.x") сначала казался причиной 403 (тот же запрос из
    # браузера отвечал 200) - но после смены на браузерный User-Agent 403 с
    # Railway остался БЕЗ ИЗМЕНЕНИЙ (проверено дважды на живых логах Railway
    # после пуша фикса). Значит дело не в заголовках, а в самом IP-адресе
    # Railway - датацентровые IP часто в чёрных списках антибот-защиты
    # Cloudflare независимо от заголовков запроса. Заголовки оставлены (не
    # мешают), но обхода блокировки не дают - см. _fetch_via_relay ниже.
    headers = {
        'Authorization': f'Bearer {TIMEPAD_TOKEN}',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                       '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
        'Accept': 'application/json',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
    }

    for _ in range(max_pages):
        params = {
            'cities': timepad_city,
            'category_ids': ','.join(str(c) for c in TIMEPAD_CATEGORY_IDS),
            'starts_at_min': starts_at_min,
            'sort': '+starts_at',
            'limit': limit,
            'skip': skip,
            'fields': 'description_short,starts_at,ends_at,location,registration_data,categories,poster_image',
        }
        data = None
        try:
            resp = requests.get(f'{BASE_URL}/events.json', params=params, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.HTTPError as e:
            # 403 конкретно - похоже на блокировку IP датацентра Cloudflare
            # (см. комментарий выше), а не на разовый сбой - пробуем достучаться
            # через relay-прокси (см. _fetch_via_relay), а не просто сдаёмся.
            if e.response is not None and e.response.status_code == 403:
                logger.warning(f"⚠️ TimePad вернул 403 напрямую ({timepad_city}, skip={skip}) - пробую через relay-прокси...")
                data = _fetch_via_relay(f'{BASE_URL}/events.json', params, headers)
                if data is None:
                    logger.error(f"❌ Relay-прокси тоже не смог получить события TimePad ({timepad_city}, skip={skip})")
                    break
            else:
                logger.error(f"❌ Ошибка запроса событий TimePad ({timepad_city}, skip={skip}): {e}")
                break
        except Exception as e:
            logger.error(f"❌ Ошибка запроса событий TimePad ({timepad_city}, skip={skip}): {e}")
            break

        values = data.get('values', [])
        if not values:
            break

        for ev in values:
            reg = ev.get('registration_data') or {}
            tickets_total = reg.get('tickets_total') or 0
            if tickets_total < TIMEPAD_MIN_TICKETS:
                continue

            starts_at = ev.get('starts_at')
            if not starts_at:
                continue
            try:
                start_dt = datetime.strptime(starts_at[:19], '%Y-%m-%dT%H:%M:%S')
                start_ts = int(start_dt.replace(tzinfo=_parse_tz(starts_at)).timestamp())
            except Exception:
                continue

            ends_at = ev.get('ends_at')
            end_ts = start_ts
            if ends_at:
                try:
                    end_dt = datetime.strptime(ends_at[:19], '%Y-%m-%dT%H:%M:%S')
                    end_ts = int(end_dt.replace(tzinfo=_parse_tz(ends_at)).timestamp())
                except Exception:
                    end_ts = start_ts

            location = ev.get('location') or {}
            # У TimePad нет координат площадки вообще (только текстовый
            # адрес) - маршрутная кнопка "🚗 Поехали" строится в боте как
            # ссылка-ПОИСК по адресу в Яндекс.Картах, а не rtext с
            # координатами, как для KudaGo (см. build_event_message).
            address = (location.get('address') or '').strip()

            normalized.append({
                'title': (ev.get('name') or '').strip(),
                'start': start_ts,
                'end': end_ts,
                'place_title': '',  # у TimePad нет отдельного названия площадки, только адрес
                'place_address': address,
                'place_lat': None,
                'place_lon': None,
                'tickets_total': tickets_total,
                'categories': [c.get('name') for c in _as_list(ev.get('categories')) if c],
                'url': ev.get('url', ''),
                'source': 'timepad',
            })

        if len(values) < limit:
            break
        skip += limit
        time.sleep(0.2)

    normalized.sort(key=lambda e: e['start'])
    return normalized[:EVENTS_PER_CITY]


def _as_list(value):
    """categories у TimePad иногда приходит списком, иногда одиночным
    объектом (замечено на живых данных) - приводим к списку в обоих случаях."""
    if not value:
        return []
    return value if isinstance(value, list) else [value]


def _parse_tz(iso_str):
    """TimePad отдаёт даты со смещением в конце строки (+0300) - парсим его
    вручную, т.к. на Python <3.11 datetime.fromisoformat не всегда съедает
    такой формат смещения без двоеточия."""
    from datetime import timedelta, timezone as tz
    offset_str = iso_str[19:]  # всё после YYYY-MM-DDTHH:MM:SS
    if not offset_str or offset_str == 'Z':
        return tz.utc
    sign = 1 if offset_str[0] == '+' else -1
    hh = int(offset_str[1:3])
    mm = int(offset_str[3:5]) if len(offset_str) >= 5 else 0
    return tz(sign * timedelta(hours=hh, minutes=mm))


def main():
    import json
    result = {
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        'cities': {},
    }
    for bot_city, timepad_city in TIMEPAD_CITY_MAP.items():
        logger.info(f"🔄 Тяну события TimePad для {bot_city} ({timepad_city})...")
        try:
            events = fetch_city_events(timepad_city)
            result['cities'][bot_city] = events
            logger.info(f"✅ {bot_city}: {len(events)} событий (TimePad, tickets_total>={TIMEPAD_MIN_TICKETS})")
        except Exception as e:
            logger.error(f"❌ Не удалось получить события TimePad для {bot_city}: {e}")
            result['cities'][bot_city] = []
        time.sleep(0.3)

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info(f"💾 Сохранено в {OUTPUT_FILE}")


if __name__ == '__main__':
    main()
