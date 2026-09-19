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
#
# Каналы для ekb/samara/chelyabinsk/krasnodar/sochi найдены поиском
# (22.09.2026), НЕ подтверждены пользователем лично - в отличие от
# concerts_moscow/spb_conc, которые прислал сам пользователь. Тематика шире,
# чем "только концерты" (общегородская афиша - выставки, детские события и
# т.п.), и не факт, что все посты следуют формату "Дата:"/"Место:"/"Цена:" (см.
# parse_event_fields) - посты, которые не распознаются парсером, просто не
# получат структурированных полей (start=None) и не попадут в "предстоящие"
# события (см. get_upcoming_concert_events_for_category в main.py), но
# ошибок не вызовут. Если канал окажется неактуальным/закрытым - fetch этого
# города в fetch_channel_messages просто вернёт пустой список, без падения
# всего скрипта. Novosibirsk/kazan/omsk/rostov пока не подключены - поиск не
# дал убедительных кандидатов (см. обсуждение с пользователем 22.09.2026).
CONCERT_EVENTS_CHANNELS = {
    'moscow': ['concerts_moscow'],
    'spb': ['spb_conc'],
    'ekb': ['kudaekb'],
    'samara': ['afisha_samara'],
    'chelyabinsk': ['meropriyatiya_che'],
    'krasnodar': ['afisha_krr'],
    'sochi': ['SochiEventy'],
}

PROMO_KEYWORDS = (
    'подписаться', 'прислать новость', 'подпишись', 'наш чат', 'реклама',
    'по всем вопросам', 'сотрудничество', 'связь с администрацией',
)
PROMO_TAIL_RE = re.compile(
    r'(Подписаться|Прислать новость|Подпишись|По всем вопросам|Сотрудничество).*$',
    re.IGNORECASE | re.DOTALL,
)


# === Парсинг структурированных полей из текста поста ===
# По просьбе пользователя (22.09.2026) нужны не просто сырые тексты постов,
# а структурированные данные: дата/время начала, место (для адреса/навигации),
# цена (для фильтра доступности по категории). Посты в этих каналах НЕ имеют
# машиночитаемой разметки - только свободный текст вида "Дата: 18 ноября" /
# "Место: Pravda" / "Цена: 1300 ₽" (не всегда все три строки присутствуют, не
# всегда в этом порядке). Парсим построчно по префиксу - надёжнее, чем одна
# большая регулярка на весь текст, и не ломается, если порядок строк другой.
MONTHS_RU = {
    'январ': 1, 'феврал': 2, 'март': 3, 'апрел': 4, 'ма': 5, 'июн': 6,
    'июл': 7, 'август': 8, 'сентябр': 9, 'октябр': 10, 'ноябр': 11, 'декабр': 12,
}

_DATE_LINE_RE = re.compile(r'^\s*(?:дата|когда)\s*:\s*(.+)$', re.IGNORECASE)
_PLACE_LINE_RE = re.compile(r'^\s*(?:место|адрес|площадка|локация)\s*:\s*(.+)$', re.IGNORECASE)
_PRICE_LINE_RE = re.compile(r'^\s*(?:цена|вход|билет[ыи]?|стоимость)\s*:\s*(.+)$', re.IGNORECASE)
# "18 ноября" / "18 ноября 20:00" / "19 сентября 14:30"
_DATE_VALUE_RE = re.compile(
    r'(\d{1,2})\s+([а-яё]+)(?:\s+(\d{1,2}):(\d{2}))?', re.IGNORECASE
)
_PRICE_NUMBER_RE = re.compile(r'(\d[\d\s]*\d|\d)\s*(?:₽|руб)')
_FREE_KEYWORDS = ('свободный', 'бесплатн', 'вход своб')

# Средняя длительность мероприятия, когда время окончания в посте не указано
# (почти никогда не указано) - по просьбе пользователя, чисто оценка, не факт.
DEFAULT_EVENT_DURATION_HOURS = 1.5

# Порог цены для правила категорий (по просьбе пользователя, 22.09.2026):
# от PRICE_THRESHOLD_RUB - показываем и такси, и Ultima; дешевле - только
# такси (Ultima это не интересно, публика не премиальная). Если цена в посте
# вообще не указана ("вход свободный"/нет строки цены) - считаем БЕСПЛАТНЫМ и
# тоже показываем только такси (см. обсуждение с пользователем 22.09.2026).
PRICE_THRESHOLD_RUB = 900


def _parse_month(word):
    word_lower = word.lower()
    for prefix, month_num in MONTHS_RU.items():
        if word_lower.startswith(prefix):
            return month_num
    return None


def parse_event_datetime(date_value, post_time_iso):
    """Разбирает значение строки "Дата: ..." в (start_dt, has_explicit_time).
    Год не указывается в постах - берём ближайшее будущее (если получившаяся
    дата в прошлом относительно даты самого поста, значит имелся в виду
    следующий год - актуально только на стыке декабря/января). Время может
    отсутствовать (только дата) - тогда has_explicit_time=False и дальше
    используется заглушка вечернего времени, а не полночь, чтобы не путать
    сортировку/отображение."""
    m = _DATE_VALUE_RE.search(date_value)
    if not m:
        return None, False
    day = int(m.group(1))
    month = _parse_month(m.group(2))
    if month is None:
        return None, False
    hour = int(m.group(3)) if m.group(3) else 20  # заглушка - типичное вечернее время концерта
    minute = int(m.group(4)) if m.group(4) else 0
    has_explicit_time = m.group(3) is not None

    try:
        post_dt = datetime.fromisoformat(post_time_iso)
    except Exception:
        post_dt = datetime.now(timezone.utc)
    year = post_dt.year
    try:
        candidate = datetime(year, month, day, hour, minute, tzinfo=post_dt.tzinfo or timezone.utc)
    except ValueError:
        return None, False
    if candidate < post_dt - timedelta(days=3):
        # Дата "в прошлом" относительно поста больше чем на пару дней - скорее
        # всего, имелся в виду следующий год (пост в декабре про январь).
        try:
            candidate = candidate.replace(year=year + 1)
        except ValueError:
            pass
    return candidate, has_explicit_time


def parse_price(price_value):
    """Возвращает (price_rub или None, is_free). is_free=True для явных
    "вход свободный"/"бесплатно" - price_rub остаётся None, но это НЕ то же
    самое, что "цена не указана вообще" (см. compute_price_category ниже -
    обе ситуации сейчас трактуются одинаково по просьбе пользователя, но
    оставлены разными полями на случай, если логика позже разъедется)."""
    value_lower = price_value.lower()
    if any(kw in value_lower for kw in _FREE_KEYWORDS):
        return None, True
    m = _PRICE_NUMBER_RE.search(price_value)
    if m:
        digits = re.sub(r'\s', '', m.group(1))
        try:
            return int(digits), False
        except ValueError:
            return None, False
    return None, False


def compute_price_category(price_rub, is_free, has_price_info):
    """Правило по просьбе пользователя (22.09.2026): цена >= PRICE_THRESHOLD_RUB
    -> событие показывается И такси, И Ultima ('all'); дешевле, бесплатное или
    вообще без указанной цены ("по регистрации" и т.п.) -> только такси
    ('taxi_only') - Ultima это не интересно."""
    if price_rub is not None and price_rub >= PRICE_THRESHOLD_RUB:
        return 'all'
    return 'taxi_only'


def parse_event_fields(text, post_time_iso):
    """Построчно ищет "Дата:"/"Место:"/"Цена:" (и синонимы) в тексте поста,
    плюс заголовок (первая строка, обычно КАПСОМ - название мероприятия).
    Возвращает dict с разобранными полями - что не нашлось, остаётся None.
    Не падает и не бросает исключений на постах без разметки вообще (просто
    все поля будут None, кроме title)."""
    lines = text.split('\n')
    title = lines[0].strip() if lines else ''
    date_value = None
    place_value = None
    price_value = None
    for line in lines[1:]:
        if date_value is None:
            m = _DATE_LINE_RE.match(line)
            if m:
                date_value = m.group(1).strip()
                continue
        if place_value is None:
            m = _PLACE_LINE_RE.match(line)
            if m:
                place_value = m.group(1).strip()
                continue
        if price_value is None:
            m = _PRICE_LINE_RE.match(line)
            if m:
                price_value = m.group(1).strip()

    start_dt, has_explicit_time = (None, False)
    if date_value:
        start_dt, has_explicit_time = parse_event_datetime(date_value, post_time_iso)

    end_dt = None
    if start_dt:
        end_dt = start_dt + timedelta(hours=DEFAULT_EVENT_DURATION_HOURS)

    price_rub, is_free = (None, False)
    if price_value:
        price_rub, is_free = parse_price(price_value)

    price_category = compute_price_category(price_rub, is_free, has_price_info=price_value is not None)

    return {
        'title': title or None,
        'start': start_dt.isoformat() if start_dt else None,
        'start_has_explicit_time': has_explicit_time,
        'end': end_dt.isoformat() if end_dt else None,
        'place': place_value,
        'price_rub': price_rub,
        'is_free': is_free,
        'price_category': price_category,
    }


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

        post = {
            'time': msg_time.isoformat(),
            'text': text,
            'link': msg_link,
        }
        post.update(parse_event_fields(text, post['time']))
        posts.append(post)

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
