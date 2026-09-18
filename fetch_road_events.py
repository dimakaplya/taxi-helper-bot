#!/usr/bin/env python3
"""
Фоновый сборщик сообщений о ДТП и дорожных происшествиях из публичных
Telegram-каналов по городам - для кнопки "Дорожные события" (аналог
fetch_favt_notices.py, который тем же способом читает @favt_info).

Публичная веб-версия канала https://t.me/s/<channel> не требует бот-токена,
API-ключа или логина - это обычная HTML-страница, доступная всем (как и
у @favt_info). Именно поэтому это НЕ Bot API (боты не могут читать историю
чужих каналов без прав администратора в них) и НЕ MTProto-клиент с логином
по номеру телефона - просто чтение публичной веб-версии, как обычный
браузер. Ограничений по количеству запросов нет, можно обновлять часто (см.
ROAD_EVENTS_UPDATE_INTERVAL_MINUTES в main.py).

=== КАК ЗАПУСКАТЬ ===
    pip install requests beautifulsoup4
    python3 fetch_road_events.py

Каналы:
    Москва          - @dtp777      (https://t.me/dtp777)
    Санкт-Петербург - @dtp_spb78   (https://t.me/dtp_spb78)
"""
import os
import json
import logging
from datetime import datetime, timedelta, timezone

import re

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'road_events_data.json')
LOOKBACK_HOURS = 12
MAX_MESSAGES_PER_CITY = 15  # сколько последних сообщений хранить/показывать на город

# Бот-город -> (username канала без @, ссылка для кнопки "Открыть канал")
ROAD_EVENTS_CHANNELS = {
    'moscow': 'dtp777',
    'spb': 'dtp_spb78',
}


PROMO_KEYWORDS = (
    'подписаться', 'прислать новость', 'подпишись', 'наш чат', 'реклама',
    'плохо грузит telegram', 'читай и смотри в max',
)
# Соответствует хвосту вида "Подписаться @dtp777 Прислать новость @dtp777bot"
# ИЛИ "Плохо грузит Telegram? - Читай и смотри в MAX" (второй канал так
# рекламирует переход в MAX-мессенджер) - вырезаем именно от начала этого
# блока и до конца текста, а не всю строку/весь текст целиком, чтобы не
# потерять полезное описание ДТП, если оно склеено с промо без переноса строки.
PROMO_TAIL_RE = re.compile(
    r'(Подписаться|Прислать новость|Подпишись|📲?\s*Плохо грузит Telegram).*$',
    re.IGNORECASE | re.DOTALL,
)


def strip_channel_promo(text, channel_username):
    """Убирает из текста сообщения рекламный хвост самого канала - "Подписаться
    @dtp777" / "Прислать новость @dtp777bot" и любое упоминание юзернейма
    канала, чтобы пользователь бота не видел рекламу стороннего канала внутри
    Taxi Helper. Сначала отрезает весь promo-хвост целиком (даже если он
    склеен с текстом ДТП без переноса строки), затем подчищает построчно то,
    что могло остаться (например, промо в середине текста)."""
    if not text:
        return text

    # 1. Основной случай: промо всегда идёт последним блоком - отрезаем всё
    # начиная с первого "Подписаться"/"Прислать новость"/"Подпишись".
    text = PROMO_TAIL_RE.sub('', text).strip()

    # 2. Подчистка построчно на случай промо где-то ещё (реклама других
    # каналов/чатов, отдельно стоящий @username).
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

    # 3. Любой оставшийся @username (например, реклама третьего канала).
    text = re.sub(r'@\w+', '', text)
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
        text = strip_channel_promo(raw_text, channel_username)

        # У части постов в таких каналах нет текста (только фото/видео),
        # либо после чистки рекламного хвоста ничего не осталось - такие
        # пропускаем, показывать нечего.
        if not text:
            continue

        # Прямая ссылка на конкретное сообщение (для "переслать" пользователю
        # можно использовать t.me/<channel>/<id> как fallback, если понадобится).
        link_tag = time_tag.find_parent('a')
        msg_link = link_tag.get('href') if link_tag else None

        notices.append({
            'time': msg_time.isoformat(),
            'text': text,
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
    for bot_city, channel_username in ROAD_EVENTS_CHANNELS.items():
        logger.info(f"🔄 Обновляю дорожные события для {bot_city} (@{channel_username})...")
        notices = fetch_channel_messages(channel_username)
        result['cities'][bot_city] = notices
        logger.info(f"✅ {bot_city}: {len(notices)} сообщений за последние {LOOKBACK_HOURS}ч")

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info(f"💾 Сохранено в {OUTPUT_FILE}")


if __name__ == '__main__':
    main()
