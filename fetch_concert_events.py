#!/usr/bin/env python3
"""
Фоновый сборщик афиши концертов из публичных Telegram-каналов по городам -
второй источник для кнопки "🎭 События города" (в дополнение к TimePad, см.
fetch_timepad_data.py). Тот же способ сбора, что и fetch_road_events.py: чтение
публичной веб-версии канала https://t.me/s/<channel> - обычная HTML-страница,
без бот-токена/API-ключа/логина, работает с любого IP (в т.ч. с Railway - в
отличие от TimePad, который Cloudflare блокирует именно с датацентровых IP,
поэтому TimePad собирается отдельно и ЛОКАЛЬНО, см. докстринг
fetch_timepad_data.py). Каналы жанрово посвящены афише целиком, поэтому, в
отличие от fetch_road_events.py (там нужен фильтр "это правда про ДТП"),
здесь такой фильтр не нужен - почти всё в канале релевантно, отсеиваем только
явный технический мусор (голое фото без подписи, чистое промо самого канала).

=== КАК ЗАПУСКАТЬ ===
    pip install requests beautifulsoup4
    python3 fetch_concert_events.py

Каналы (см. CONCERT_EVENTS_CHANNELS ниже):
    Москва          - @concerts_moscow (https://t.me/concerts_moscow)
    Санкт-Петербург - @spb_conc        (https://t.me/spb_conc)
"""
import os
import json
import logging
import re
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'concert_events_data.json')
LOOKBACK_HOURS = 24 * 14  # афиша живёт неделями вперёд, не часами - окно шире, чем у ДТП (6ч)
MAX_MESSAGES_PER_CITY = 20  # сколько последних постов хранить/показывать на город

# Бот-город -> username канала без @. Один канал на город пока (в отличие от
# ROAD_EVENTS_CHANNELS, где на Москву два) - если пользователь пришлёт ещё
# каналы, сюда можно добавить список, как там.
CONCERT_EVENTS_CHANNELS = {
    'moscow': ['concerts_moscow'],
    'spb': ['spb_conc'],
}

PROMO_KEYWORDS = (
    'подписаться', 'прислать новость', 'подпишись', 'наш чат', 'реклама',
    'по всем вопросам', 'сотрудничество', 'связь с администрацией',
)
PROMO_TAIL_RE = re.compile(
    r'(Подписаться|Прислать новость|Подпишись|По всем вопросам|Сотрудничество).*$',
    re.IGNORECASE | re.DOTALL,
)


def strip_channel_promo(text, channel_username):
    """Тот же принцип очистки, что strip_channel_promo в fetch_road_events.py -
    убираем рекламный хвост самого канала и любые ссылки/юзернеймы, чтобы
    внутри Taxi Helper не показывать рекламу стороннего канала."""
    if not text:
        return text
    text = PROMO_TAIL_RE.sub('', text).strip()
    lines = text.split('\n')
    kept = []
    for line in lines:
        line_lower = line.lower()
        if any(kw in line_lower for kw in PROMO_KEYWORDS):
            continue
        if channel_username and f'@{channel_username.lower()}' in line_lower:
            continue
        kept.append(line)
    text = '\n'.join(kept)
    text = re.sub(r'@\w+', '', text)
    text = re.sub(r'(?:https?://|(?:www\.)?t\.me/|(?:www\.)?telegram\.me/)\S+', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text


def fetch_channel_messages(channel_username):
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

    posts = []
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
        text = strip_channel_promo(raw_text, channel_username)

        # Пост без текста (только фото/видео без подписи) - показывать
        # нечего, пропускаем. В отличие от ДТП-канала, здесь НЕ фильтруем по
        # теме - весь канал целиком про концерты/мероприятия.
        if not text:
            continue

        link_tag = time_tag.find_parent('a')
        msg_link = link_tag.get('href') if link_tag else None

        posts.append({
            'time': msg_time.isoformat(),
            'text': text,
            'link': msg_link,
        })

    posts.sort(key=lambda p: p['time'], reverse=True)
    return posts[:MAX_MESSAGES_PER_CITY]


def merge_city_messages(per_channel_posts):
    """Тот же принцип слияния/дедупа, что merge_city_messages в
    fetch_road_events.py - на случай, если для города позже добавят
    несколько каналов."""
    merged = []
    for posts in per_channel_posts:
        merged.extend(posts)
    merged.sort(key=lambda p: p['time'], reverse=True)

    seen = set()
    deduped = []
    for p in merged:
        key = ('link', p['link']) if p.get('link') else ('text', p['time'], p['text'])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(p)
    return deduped[:MAX_MESSAGES_PER_CITY]


def main():
    result = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'lookback_hours': LOOKBACK_HOURS,
        'cities': {},
    }
    for bot_city, channel_usernames in CONCERT_EVENTS_CHANNELS.items():
        channels_label = ', '.join(f'@{c}' for c in channel_usernames)
        logger.info(f"🔄 Обновляю афишу концертов для {bot_city} ({channels_label})...")
        per_channel = [fetch_channel_messages(c) for c in channel_usernames]
        posts = merge_city_messages(per_channel)
        result['cities'][bot_city] = posts
        logger.info(f"✅ {bot_city}: {len(posts)} постов (из {len(channel_usernames)} канал(ов))")

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info(f"💾 Сохранено в {OUTPUT_FILE}")


if __name__ == '__main__':
    main()
