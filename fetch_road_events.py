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
    Москва          - @dtp777, @DtOperativno (несколько каналов на город -
                       см. ROAD_EVENTS_CHANNELS ниже, результаты сливаются
                       в одну ленту по времени, с дедупом по ссылке/тексту)
    Санкт-Петербург - @dtp_spb78   (https://t.me/dtp_spb78)
"""
import os
import json
import logging
from datetime import datetime, timedelta, timezone

import re

import requests
from bs4 import BeautifulSoup

import geocoding_utils

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'road_events_data.json')
LOOKBACK_HOURS = 6
MAX_MESSAGES_PER_CITY = 15  # сколько последних сообщений хранить/показывать на город

# По просьбе пользователя (22.09.2026): "вынеси на карту дорожные события
# города где есть адреса" - часть постов канала содержит адрес прямо в
# тексте (улица/шоссе/набережная + номер дома, либо "N км МКАД"), из него
# можно вытащить координаты и показать событие меткой на карте (см.
# /map/road_events в main.py). У постов БЕЗ узнаваемого адреса координат не
# будет - на карту они не попадают (ровно как просил пользователь), но
# продолжают показываться в обычном списке "⛔ Дорожные события" в боте.
# Сам геокодер (Nominatim/OpenStreetMap, кэш на диске) вынесен в общий
# geocoding_utils.py - им же пользуется fetch_concert_events.py для афиши
# (та же просьба пользователя, следом за дорожными событиями).

# Типовые обозначения улиц/дорог в постах о ДТП. Русские адреса встречаются
# в ДВУХ порядках слов - "ул. Тверская" (обозначение улицы ПЕРЕД названием)
# и "Кутузовский проспект" (прилагательное+обозначение ПОСЛЕ, обозначение -
# последнее слово) - нужны оба варианта регулярки (см. ADDRESS_RE_MARKER_FIRST/
# _LAST ниже). "МКАД"/"ТТК"/"N км" - отдельно, там номер дома не при чём
# (там километр трассы), см. KM_ROAD_RE.
#
# ВАЖНО: паттерн НЕ использует общий флаг re.IGNORECASE - если бы он был
# общим, классы [А-ЯЁ] (заглавная буква - начало названия улицы) стали бы
# по флагу IGNORECASE матчить и строчные буквы тоже, из-за чего короткое
# слово вроде "д." (сокращение "дом") перед номером дома ошибочно
# засчитывалось бы отдельным "словом названия улицы" и обрубало бы захват
# перед самим номером (проверено на практике - без этой оговорки "ул.
# Тверская д.5" превращалось в "ул. Тверская д" без номера). Поэтому
# регистронезависимость (?i:...) применяется ТОЛЬКО к самим маркерам
# (ул./шоссе/проспект и т.п.), а не ко всему паттерну.
_STREET_MARKERS = (
    r'ул\.', r'улиц\w*',
    r'просп\.', r'проспект\w*', r'пр-?т\.?',
    r'ш\.', r'шоссе\w*',
    r'наб\.', r'набережн\w*',
    r'пер\.', r'переулок\w*',
    r'б-р\.?', r'бульвар\w*',
    r'проезд\w*', r'аллея\w*', r'туп\.', r'тупик\w*', r'мост\w*',
)
_MARKER_GROUP = r'(?i:' + '|'.join(_STREET_MARKERS) + r')'
_HOUSE_NUMBER = r'(?:,?\s*(?:д\.?|дом)?\s*\d{1,4}[а-яА-Я]?)?'
# "ул. Тверская, 5" / "шоссе Энтузиастов"
ADDRESS_RE_MARKER_FIRST = re.compile(
    _MARKER_GROUP + r'\s+[А-ЯЁ][а-яё\-]*(?:\s+[А-ЯЁ][а-яё\-]*){0,3}' + _HOUSE_NUMBER
)
# "Кутузовский проспект, 12" / "Ленинградское шоссе"
ADDRESS_RE_MARKER_LAST = re.compile(
    r'[А-ЯЁ][а-яё\-]*(?:\s+[А-ЯЁ][а-яё\-]*){0,2}\s+' + _MARKER_GROUP + _HOUSE_NUMBER
)
# "41-й км МКАД", "МКАД 12 км", "35 км Ленинградского шоссе" и т.п.
KM_ROAD_RE = re.compile(
    r'(?:\d{1,3}[\-\s]?(?:й)?\s*км\s+(?:[А-ЯЁ][а-яё\-]*(?:\s+[А-ЯЁ][а-яё\-]*){0,2}\s*)?(?:МКАД|ТТК|шоссе)?'
    r'|(?:МКАД|ТТК)\s*,?\s*\d{1,3}[\-\s]?(?:й)?\s*км)',
    re.IGNORECASE,
)


def extract_address(text):
    """Первое похожее на адрес вхождение в тексте поста, либо None - пробует
    оба порядка слов (см. ADDRESS_RE_MARKER_FIRST/_LAST выше), затем
    "N км ..." (KM_ROAD_RE). Возвращает как есть (с исходным
    регистром/пунктуацией) - именно эта строка идёт в геокодер. Это
    эвристика, не полноценный разбор адресов - часть постов без чёткого
    адреса или с нестандартной формулировкой не распознается, такие посты
    просто не попадают на карту (см. geocode_notices ниже)."""
    for pattern in (ADDRESS_RE_MARKER_FIRST, ADDRESS_RE_MARKER_LAST, KM_ROAD_RE):
        m = pattern.search(text)
        if m:
            return m.group(0).strip(' ,')
    return None


# Бот-город -> список username-ов каналов без @ (может быть несколько на
# город - см. merge_city_messages ниже: результаты всех каналов сливаются в
# одну ленту по городу, сортируются по времени, дедупятся по ссылке на
# сообщение (а если ссылки почему-то нет - по паре (время, текст), на случай
# если один и тот же инцидент запостили в обоих каналах почти одновременно).
ROAD_EVENTS_CHANNELS = {
    'moscow': ['dtp777', 'DtOperativno'],
    'spb': ['dtp_spb78'],
}

# Каналы @dtp777/@dtp_spb78 называются "ДТП И ЧП" - помимо аварий постят и
# общие городские происшествия (кража телефонов, отменённый концерт, нож на
# улице и т.п.). Пользователь просил показывать только то, что реально про
# ДТП/аварии/перекрытия/заторы. При этом у московского канала многие посты
# про реальные ДТП - это ТОЛЬКО короткая подпись с адресом (суть видна на
# фото/видео, которое мы не читаем), без слова "ДТП" в тексте - поэтому
# чистый inclusion-фильтр по ключевым словам обрубил бы и их. Логика в три
# шага: 1) есть явный дорожный маркер -> оставляем; 2) есть явный маркер
# "это не про дорогу" (кража/концерт/нож и т.п.) -> убираем; 3) сигналов нет
# (типичная короткая подпись с адресом) -> оставляем по умолчанию, канал и
# так тематический.
ROAD_INCIDENT_KEYWORDS = (
    'дтп', 'авари', 'столкнов', 'наезд', 'наеха', 'сбил', 'сбила', 'сбили',
    'врезал', 'въехал', 'опрокину', 'перевернул', 'перекрыт', 'перекрытие',
    'затор', 'пробк', 'кювет', 'занос', 'вылетел с трассы', 'слетел с дороги',
)
NON_ROAD_KEYWORDS = (
    'концерт', 'магазин', 'мошенничеств', 'нож', 'кража', 'украл', 'ограбил',
    'ограбление', 'наркотик', 'изъят', 'агрессор', 'проголосуйте',
)


def is_road_incident(text):
    """True, если текст поста реально про ДТП/аварию/перекрытие/затор (или
    не содержит явных признаков, что это НЕ про дорогу - см. комментарий
    выше), False - если это явно другая городская новость канала."""
    text_lower = text.lower()
    if any(kw in text_lower for kw in ROAD_INCIDENT_KEYWORDS):
        return True
    if any(kw in text_lower for kw in NON_ROAD_KEYWORDS):
        return False
    return True


# По просьбе пользователя (19.09.2026): "оценка перекрытий города" - из
# общей ленты ДТП/происшествий отдельно выделяем те посты, где явно речь о
# ПЕРЕКРЫТИИ/ограничении проезда (а не просто авария/затор без блокировки
# дороги) - показываются отдельным блоком наверху "⛔ Дорожные события"
# (см. show_road_events в main.py), т.к. для водителя это самое важное:
# участок, где вообще нельзя проехать, а не просто задержка.
CLOSURE_KEYWORDS = (
    'перекрыт', 'перекрытие', 'перекрытии', 'ограничен движен',
    'движение закрыто', 'движение приостановлено', 'полностью закрыт',
    'частично перекрыт', 'объезд', 'закрыт проезд', 'закрыт для проезда',
)


def is_road_closure(text):
    """True, если пост явно про ПЕРЕКРЫТИЕ/ограничение движения (не просто
    авария/затор) - см. CLOSURE_KEYWORDS. Уже предполагается, что текст
    прошёл is_road_incident (это подмножество дорожных постов)."""
    text_lower = text.lower()
    return any(kw in text_lower for kw in CLOSURE_KEYWORDS)


# По просьбе пользователя (20.09.2026): "делай пуши перекрытий... и крупные
# ДТП" - отдельно от is_road_closure нужен признак "это КРУПНАЯ авария", не
# каждая мелкая (см. AskUserQuestion: "есть слова-маркеры серьёзности" -
# пострадавшие/погибшие, много машин, скорая/спасатели на месте). Как и
# is_road_closure, это подмножество постов, уже прошедших is_road_incident.
SEVERE_INCIDENT_KEYWORDS = (
    'пострадал', 'пострадали', 'пострадавш', 'погиб', 'скончал', 'жертв',
    'реанимац', 'госпитализ', 'скорая', 'скорую', 'спасатели', 'мчс',
    'зажат', 'многочисленных пострадав', 'массовое дтп', 'крупное дтп',
    'серьёзное дтп', 'серьезное дтп', 'опрокину', 'перевернул',
    'вылетел с трассы', 'слетел с дороги', 'несколько машин',
    'много машин', 'массовая авария', 'цепочка машин', 'столкнулись',
)


def is_severe_incident(text):
    """True, если пост про КРУПНУЮ аварию (пострадавшие/погибшие, скорая/
    спасатели на месте, массовая авария из нескольких машин и т.п.) - см.
    SEVERE_INCIDENT_KEYWORDS. Отдельно от is_road_closure - перекрытие не
    обязательно тяжёлая авария (может быть плановый ремонт), и наоборот
    тяжёлая авария не обязательно перекрывает дорогу целиком. Используется
    для пушей о крупных ДТП (см. push_road_incident_alerts в main.py) -
    НЕ каждая мелкая авария, только с явными признаками серьёзности."""
    text_lower = text.lower()
    return any(kw in text_lower for kw in SEVERE_INCIDENT_KEYWORDS)


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

    # 4. Любая ссылка (в т.ч. голый t.me/... без протокола) - иначе Telegram
    # сам подставит превью-карточку канала поверх нашего сообщения.
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

        # Оставляем только то, что реально про ДТП/аварии/перекрытия (см.
        # комментарий у is_road_incident) - канал публикует и общие городские
        # новости, они водителю не нужны в этой кнопке.
        if not is_road_incident(text):
            continue

        # Прямая ссылка на конкретное сообщение (для "переслать" пользователю
        # можно использовать t.me/<channel>/<id> как fallback, если понадобится).
        link_tag = time_tag.find_parent('a')
        msg_link = link_tag.get('href') if link_tag else None

        notices.append({
            'time': msg_time.isoformat(),
            'text': text,
            'link': msg_link,
            'is_closure': is_road_closure(text),
            'is_severe': is_severe_incident(text),
        })

    notices.sort(key=lambda n: n['time'], reverse=True)
    return notices[:MAX_MESSAGES_PER_CITY]


def merge_city_messages(per_channel_notices):
    """Сливает списки сообщений от нескольких каналов одного города в одну
    ленту: сортирует по времени (новые сверху) и дедупит - в первую очередь
    по прямой ссылке на сообщение (надёжнее всего, у неё разные каналы не
    могут случайно совпасть), а если ссылки нет - по паре (время, текст),
    на случай если один и тот же инцидент независимо запостили в двух
    каналах почти секунда в секунду с одинаковым текстом (маловероятно, но
    дешёво подстраховаться)."""
    merged = []
    for notices in per_channel_notices:
        merged.extend(notices)
    merged.sort(key=lambda n: n['time'], reverse=True)

    seen = set()
    deduped = []
    for n in merged:
        key = ('link', n['link']) if n.get('link') else ('text', n['time'], n['text'])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(n)
    return deduped[:MAX_MESSAGES_PER_CITY]


def geocode_notices(notices, bot_city, cache):
    """Пытается вытащить адрес из текста каждого поста и геокодировать его
    (см. extract_address/geocode_address выше) - добавляет 'address'/'lat'/
    'lon' в те, где это получилось. Посты без узнаваемого адреса или с
    адресом, который не удалось геокодировать, остаются без lat/lon - они
    по-прежнему видны в обычном списке "Дорожные события" в боте, просто не
    попадают на карту (см. /map/road_events в main.py)."""
    geocoded_count = 0
    for n in notices:
        address = extract_address(n['text'])
        if not address:
            continue
        coords = geocoding_utils.geocode_address(address, bot_city, cache, namespace='road')
        if coords:
            n['address'] = address
            n['lat'], n['lon'] = coords
            geocoded_count += 1
    return geocoded_count


def main():
    result = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'lookback_hours': LOOKBACK_HOURS,
        'cities': {},
    }
    geocode_cache = geocoding_utils.load_geocode_cache()
    for bot_city, channel_usernames in ROAD_EVENTS_CHANNELS.items():
        channels_label = ', '.join(f'@{c}' for c in channel_usernames)
        logger.info(f"🔄 Обновляю дорожные события для {bot_city} ({channels_label})...")
        per_channel = [fetch_channel_messages(c) for c in channel_usernames]
        notices = merge_city_messages(per_channel)
        geocoded_count = geocode_notices(notices, bot_city, geocode_cache)
        result['cities'][bot_city] = notices
        logger.info(
            f"✅ {bot_city}: {len(notices)} сообщений за последние {LOOKBACK_HOURS}ч "
            f"(из {len(channel_usernames)} канал(ов)), с адресом на карте: {geocoded_count}"
        )
    geocoding_utils.save_geocode_cache(geocode_cache)

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info(f"💾 Сохранено в {OUTPUT_FILE}")


if __name__ == '__main__':
    main()
