#!/usr/bin/env python3
"""
Фоновый сборщик коротких новостей/афиши из публичных Telegram-каналов по
городам - для бегущей строки над картой (см. MAP_TICKER_API_PATH/
handle_map_ticker_api в main.py). Тот же способ чтения, что и у
fetch_road_events.py/fetch_favt_notices.py - публичная веб-версия канала
https://t.me/s/<channel>, без бот-токена и без логина.

В отличие от fetch_road_events.py здесь НЕТ фильтра "это про ДТП/дорогу" -
каналы общие городские (новости/афиша), берём все посты с текстом.
Каждый пост сжимается до одной короткой строки (см. compress_text) - в
бегущую строку идёт только "самое основное", а не весь пост целиком.

=== КАК ЗАПУСКАТЬ ===
    pip install requests beautifulsoup4
    python3 fetch_city_ticker.py

Каналы (присланы пользователем 28.09.2026):
    Москва     - @moscowmap (новости), @moscowsee (афиша)
    Краснодар  - @krd_tipich_ru (новости)
"""
import os
import json
import logging
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

import fetch_road_events  # переиспользуем strip_channel_promo/merge_city_messages - тот же формат постов

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'city_ticker_news.json')

# Новости живут в бегущей строке недолго по своей природе - смысла тащить
# сутками старые посты нет (в отличие от road events, где перекрытие может
# быть актуально долго). 12ч - компромисс между "свежо" и "хоть что-то есть
# в ленте, если город тихий".
LOOKBACK_HOURS = 12
MAX_MESSAGES_PER_CITY = 15

# Бот-город -> список username-ов новостных/афишных каналов без @ (см.
# докстринг выше - откуда взяты). У spb/sochi новостных каналов пока нет -
# для них бегущая строка просто не покажет блок новостей (пробки/ДТП/
# реклама остаются), это ожидаемо, а не ошибка.
TICKER_NEWS_CHANNELS = {
    'moscow': ['moscowmap', 'moscowsee'],
    'krasnodar': ['krd_tipich_ru'],
}


def compress_text(text, max_chars=110):
    """"Сжимает и выдаёт самое основное" (прямая формулировка пользователя) -
    без ИИ-суммаризации (в проекте нет вызовов LLM ни для чего подобного,
    добавлять внешний API-ключ и задержку под одну строку в бегущей строке
    не оправдано): берёт первое предложение поста, а если оно само длиннее
    max_chars - обрезает по границе слова с "…". Пустые строки/переносы
    внутри поста склеиваются пробелом, чтобы получилась одна строка для
    ленты."""
    flat = ' '.join(text.split())
    if not flat:
        return ''
    first_sentence = flat
    for stop in ('. ', '! ', '? ', '.\n', '!\n', '?\n'):
        idx = flat.find(stop)
        if idx != -1:
            first_sentence = flat[:idx + 1]
            break
    candidate = first_sentence if len(first_sentence) <= max_chars else flat
    if len(candidate) <= max_chars:
        return candidate.strip()
    truncated = candidate[:max_chars].rsplit(' ', 1)[0].rstrip(' ,.-')
    return (truncated or candidate[:max_chars]) + '…'


def fetch_channel_messages(channel_username):
    """Как fetch_road_events.fetch_channel_messages, но БЕЗ фильтра
    is_road_incident - канал общегородской (новости/афиша), а не тематический
    ДТП-канал, поэтому весь текстовый контент подходит."""
    url = f'https://t.me/s/{channel_username}'
    try:
        resp = requests.get(url, timeout=20, headers={'User-Agent': 'Mozilla/5.0'})
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"❌ Не удалось загрузить {url}: {e}")
        return []

    soup = BeautifulSoup(resp.text, 'html.parser')
    messages = soup.select('.tgme_widget_message_wrap')
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    notices = []
    for msg in messages:
        time_tag = msg.select_one('.tgme_widget_message_date time')
        if not time_tag or not time_tag.get('datetime'):
            continue
        try:
            msg_time = datetime.fromisoformat(time_tag['datetime'])
        except Exception:
            continue
        if msg_time.tzinfo is None:
            msg_time = msg_time.replace(tzinfo=timezone.utc)
        if msg_time < cutoff:
            continue

        text_tag = msg.select_one('.tgme_widget_message_text')
        raw_text = text_tag.get_text(separator='\n', strip=True) if text_tag else ''
        text = fetch_road_events.strip_channel_promo(raw_text, channel_username)
        if not text:
            continue

        compressed = compress_text(text)
        if not compressed:
            continue

        link_tag = time_tag.find_parent('a')
        msg_link = link_tag.get('href') if link_tag else None

        notices.append({
            'time': msg_time.isoformat(),
            'text': compressed,
            'link': msg_link,
        })

    notices.sort(key=lambda n: n['time'], reverse=True)
    return notices[:MAX_MESSAGES_PER_CITY]


def main():
    result = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'lookback_hours': LOOKBACK_HOURS,
        'cities': {},
    }
    for bot_city, channel_usernames in TICKER_NEWS_CHANNELS.items():
        channels_label = ', '.join(f'@{c}' for c in channel_usernames)
        logger.info(f"🔄 Обновляю новости для бегущей строки {bot_city} ({channels_label})...")
        per_channel = [fetch_channel_messages(c) for c in channel_usernames]
        notices = fetch_road_events.merge_city_messages(per_channel)
        result['cities'][bot_city] = notices[:MAX_MESSAGES_PER_CITY]
        logger.info(f"✅ {bot_city}: {len(notices)} новостей за последние {LOOKBACK_HOURS}ч (из {len(channel_usernames)} канал(ов))")

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info(f"💾 Сохранено в {OUTPUT_FILE}")


if __name__ == '__main__':
    main()
