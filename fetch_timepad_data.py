#!/usr/bin/env python3
"""
Скрипт для ЛОКАЛЬНОГО запуска (на твоём компьютере, не в облаке Railway!) -
как fetch_yandex_data.py. Второй источник афиши города (TimePad,
dev.timepad.ru), в дополнение к тому, что раньше был KudaGo (KudaGo с тех
пор убран из бота полностью).

=== ПОЧЕМУ ЛОКАЛЬНО, А НЕ ФОНОВОЙ ЗАДАЧЕЙ НА RAILWAY ===
Изначально запускался фоновой задачей прямо внутри бота на Railway. Не
сработало: Cloudflare у TimePad блокирует запросы именно с IP-адресов
Railway (403 Forbidden) - подтверждено живыми логами: тот же токен и
параметры с Railway отвечали 403, из браузера/с обычного домашнего IP -
200. Смена User-Agent на браузерный НЕ помогла - значит блокировка по
IP/репутации датацентра, а не по заголовкам запроса. Обход через сторонний
relay-прокси (например r.jina.ai) рассматривался и ОТКЛОНЁН - это означало
бы отправить твой API-токен незнакомому стороннему сервису, что
неприемлемо для учётных данных. Поэтому - как и с Yandex Rasp - скрипт
тянет данные с твоего домашнего IP (не датацентрового, не в чёрных списках
антибот-защиты) и просто сохраняет результат в файл.

=== КАК ПОЛУЧИТЬ ТОКЕН ===
1. Зайди на https://dev.timepad.ru/, зарегистрируйся/войди
2. Раздел получения токена (см. "Токен API" в документации разработчика)
3. Экспортируй его: export TIMEPAD_TOKEN="твой_токен"

=== КАК ЗАПУСКАТЬ ===
    pip install requests
    python3 fetch_timepad_data.py

Рекомендуется гонять по крону раз в несколько часов (события обновляются
медленно, часто дёргать незачем):
    crontab -e
    0 */3 * * * cd /путь/к/проекту && TIMEPAD_TOKEN="твой_токен" /usr/bin/python3 fetch_timepad_data.py >> fetch_timepad.log 2>&1

После каждого успешного запуска - закоммить и запушь timepad_data.json,
Railway подхватит новый файл через авто-деплой (GitHub webhook), как и с
flights_data.json/trains_data.json.

=== ПОЧЕМУ ДВОЙНОЙ ФИЛЬТР ===
У TimePad нет координат места (только текстовый адрес) и нет отдельного
price-поля в верхнем уровне события - цена лежит в
registration_data.price_min/price_max. Общий фид города огромный (в Москве -
десятки тысяч событий) и в основном состоит из мелких самостоятельных
квестов-экскурсий без гида и небольших бизнес-встреч (20-200 человек) - это
НЕ события, вызывающие всплеск спроса на такси. Поэтому здесь двойной фильтр:
  1. По категориям - только "Концерты", "Вечеринки", "Бизнес", "Искусство и
     культура", "Театры" (TIMEPAD_CATEGORY_IDS) - без "Экскурсии и
     путешествия", где сидят все мелкие квесты.
  2. По размеру - только события с registration_data.tickets_total >=
     TIMEPAD_MIN_TICKETS (200) - отсекает мелкие бизнес-завтраки/круглые
     столы, оставляет форумы/концерты/фестивали заметного масштаба.

=== ПОЧЕМУ НЕ ВСЕ 12 ГОРОДОВ БОТА ===
ДОБАВЛЕНО 23.09.2026 (прямая просьба пользователя "другие города") - живой
пробный запрос к /v1/events.json по каждому из 12 городов бота (с тем же
фильтром категорий/tickets_total) показал, что у Челябинска, Омска, Самары
и Ростова-на-Дону на TimePad почти нет крупных событий нужных категорий
(0 штук на первой странице выдачи) - в TIMEPAD_CITY_MAP они пока НЕ
включены, чтобы не тратить лимит запросов (60/мин с одного IP) на города,
где физически нечего показывать. Остальные 7 городов (Питер, Новосибирск,
Екатеринбург, Казань, Нижний Новгород, Краснодар, Сочи) показали реальный
объём (от 1 до 6+ крупных событий) и подключены вместе с Москвой.
"""
import os
import time
import logging
from datetime import datetime, timedelta, timezone

import requests

import geocoding_utils

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BASE_URL = 'https://api.timepad.ru/v1'
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'timepad_data.json')

TIMEPAD_TOKEN = os.getenv('TIMEPAD_TOKEN', '')

# Бот-город -> название города у TimePad (параметр cities). См. "ПОЧЕМУ НЕ
# ВСЕ 12 ГОРОДОВ БОТА" в шапке файла - chelyabinsk/omsk/samara/rostov
# сознательно не подключены (почти нет крупных событий нужных категорий).
TIMEPAD_CITY_MAP = {
    'moscow': 'Москва',
    'spb': 'Санкт-Петербург',
    'novosibirsk': 'Новосибирск',
    'ekb': 'Екатеринбург',
    'kazan': 'Казань',
    'nnovgorod': 'Нижний Новгород',
    'krasnodar': 'Краснодар',
    'sochi': 'Сочи',
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
# ИЗМЕНЕНО 23.09.2026 (живой запуск на 8 городов - TimePad стабильно отвечает
# за 15-25с на запрос, судя по всему из-за собственной нагрузки на их
# стороне, а не сети/проверено с домашнего IP) - было 20с, реальные запросы
# стабильно попадали в таймаут на границе. Увеличено с запасом.
REQUEST_TIMEOUT = 45

def fetch_city_events(timepad_city):
    """Тянет ближайшие крупные события города одним проходом с пагинацией
    (TimePad отдаёт events.json с limit/skip). Фильтр по категориям и
    starts_at_min делается на стороне TimePad (query-параметры), фильтр по
    tickets_total - на нашей стороне (в ответе нет такого query-параметра)."""
    if not TIMEPAD_TOKEN:
        logger.warning("⚠️ TIMEPAD_TOKEN не задан - пропускаю TimePad")
        return []
    try:
        TIMEPAD_TOKEN.encode('latin-1')
    except UnicodeEncodeError:
        # HTTP-заголовки должны быть latin-1/ASCII - если в скопированном
        # токене затесался "умный" символ (например кавычка “ ” вместо
        # обычной " при копировании из Заметок/мессенджера), requests падает
        # с невнятным 'latin-1' codec can't encode... Ловим это здесь и
        # говорим человеческим языком, что не так, вместо крипто-трейсбека.
        logger.error(
            "❌ TIMEPAD_TOKEN содержит символы, которые нельзя отправить в HTTP-заголовке "
            "(не ASCII) - похоже, при копировании токена попал лишний символ, например "
            "\"умная\" кавычка из Заметок/мессенджера. Скопируй токен ещё раз, лучше из "
            "адресной строки браузера или простого текстового редактора, и проверь, что "
            "export TIMEPAD_TOKEN=\"...\" использует ОБЫЧНЫЕ прямые кавычки."
        )
        return []

    now_dt = datetime.now(timezone.utc)
    starts_at_min = now_dt.strftime('%Y-%m-%dT%H:%M:%S')
    # ИСПРАВЛЕНО 23.09.2026 (прямая просьба пользователя - "на какой период
    # можешь выкачивать... на месяц можешь?") - EVENTS_LOOKAHEAD_DAYS был
    # объявлен, но НИГДЕ не применялся к запросу: реальная глубина выборки
    # зависела только от EVENTS_PER_CITY/max_pages (сколько КРУПНЫХ событий
    # наберётся вперёд по времени), а не от календарного периода - для
    # редких городов (Сочи, Екатеринбург) это могло утянуть события far за
    # горизонт месяца, для частых (Москва, Питер) - наоборот, меньше месяца.
    # Теперь starts_at_max явно ограничивает окно EVENTS_LOOKAHEAD_DAYS днями.
    starts_at_max = (now_dt + timedelta(days=EVENTS_LOOKAHEAD_DAYS)).strftime('%Y-%m-%dT%H:%M:%S')

    normalized = []
    skip = 0
    limit = 100
    max_pages = 5  # защита от бесконечной пагинации (до 500 событий на город)
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
            'starts_at_max': starts_at_max,
            'sort': '+starts_at',
            'limit': limit,
            'skip': skip,
            'fields': 'description_short,starts_at,ends_at,location,registration_data,categories,poster_image',
        }
        try:
            resp = requests.get(f'{BASE_URL}/events.json', params=params, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 403:
                logger.error(
                    f"❌ TimePad вернул 403 ({timepad_city}, skip={skip}) - если запускаешь это НЕ с "
                    "домашнего компьютера, а с облачного сервера (Railway, VPS и т.п.), это известное "
                    "ограничение: Cloudflare у TimePad блокирует датацентровые IP. См. пояснение в "
                    "шапке файла - скрипт рассчитан на запуск именно с домашнего IP."
                )
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

            # ДОБАВЛЕНО 24.09.2026 (прямая просьба пользователя - "легкое
            # краткое описание мероприятия" в карточке события бота) -
            # description_short УЖЕ запрашивался в 'fields' параметра API
            # (см. выше), но раньше просто не сохранялся в normalized.json -
            # бот его не показывал вообще. Обрезка длины - на стороне бота
            # (см. _timepad_event_description/EVENT_DESCRIPTION_MAX_CHARS в
            # main.py), здесь сохраняем как прислал TimePad.
            description_short = (ev.get('description_short') or '').strip()

            normalized.append({
                'title': (ev.get('name') or '').strip(),
                'start': start_ts,
                'end': end_ts,
                'place_title': '',  # у TimePad нет отдельного названия площадки, только адрес
                'place_address': address,
                'place_lat': None,
                'place_lon': None,
                'description_short': description_short,
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


def geocode_events(events, bot_city, cache):
    """Заполняет place_lat/place_lon по текстовому place_address (см.
    комментарий у fetch_city_events выше - у TimePad нет своих координат
    площадки) через общий geocoding_utils - по просьбе пользователя
    (22.09.2026): "Афишу тоже выноси" (на карту). ВАЖНО: этот скрипт
    запускается ЛОКАЛЬНО (не на Railway - см. докстринг файла), поэтому
    геокодирование TimePad-афиши работает только если пользователь сам
    периодически запускает fetch_timepad_data.py на своей машине - события
    без адреса или без успешного геокодирования остаются без lat/lon, на
    карту не попадают, но по-прежнему видны в обычном разделе "🎭 События
    города" в боте."""
    geocoded_count = 0
    for ev in events:
        address = ev.get('place_address')
        if not address:
            continue
        coords = geocoding_utils.geocode_address(address, bot_city, cache, namespace='addr')
        if coords:
            ev['place_lat'], ev['place_lon'] = coords
            geocoded_count += 1
    return geocoded_count


def main():
    """ДОБАВЛЕНО 23.09.2026 (прямая просьба пользователя - "другие города" +
    практика: одиночный процесс на все 8 городов не укладывается в лимит
    времени одного вызова device_bash, каждый город - это ~15-20с запрос к
    TimePad + геокодирование адресов через Nominatim с лимитом 1 req/sec) -
    необязательный аргумент --city ДОБАВЛЯЕТ (не переписывает целиком) один
    город к уже существующему timepad_data.json, так можно гонять города по
    одному отдельными запусками, не теряя уже собранные данные по другим."""
    import json
    import sys

    only_city = None
    if len(sys.argv) >= 3 and sys.argv[1] == '--city':
        only_city = sys.argv[2]
        if only_city not in TIMEPAD_CITY_MAP:
            logger.error(f"❌ Неизвестный город '{only_city}', ожидается один из: {list(TIMEPAD_CITY_MAP)}")
            return

    result = {'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'), 'cities': {}}
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
                existing = json.load(f)
            result['cities'] = existing.get('cities', {})
        except Exception as e:
            logger.warning(f"⚠️ Не удалось прочитать существующий {OUTPUT_FILE}, начинаю с нуля: {e}")

    cities_to_run = {only_city: TIMEPAD_CITY_MAP[only_city]} if only_city else TIMEPAD_CITY_MAP

    geocode_cache = geocoding_utils.load_geocode_cache()
    for bot_city, timepad_city in cities_to_run.items():
        logger.info(f"🔄 Тяну события TimePad для {bot_city} ({timepad_city})...")
        try:
            events = fetch_city_events(timepad_city)
            geocoded_count = geocode_events(events, bot_city, geocode_cache)
            result['cities'][bot_city] = events
            logger.info(
                f"✅ {bot_city}: {len(events)} событий (TimePad, tickets_total>={TIMEPAD_MIN_TICKETS}), "
                f"с адресом на карте: {geocoded_count}"
            )
        except Exception as e:
            logger.error(f"❌ Не удалось получить события TimePad для {bot_city}: {e}")
            result['cities'][bot_city] = []
        time.sleep(0.3)
    geocoding_utils.save_geocode_cache(geocode_cache)

    result['generated_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info(f"💾 Сохранено в {OUTPUT_FILE}")


if __name__ == '__main__':
    main()
