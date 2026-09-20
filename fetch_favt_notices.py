#!/usr/bin/env python3
"""
Фоновый сборщик уведомлений из официального канала Росавиации (@favt_info,
"Говорит Росавиация") о временных ограничениях приёма/выпуска ВС в аэропортах.

Публичная веб-версия канала https://t.me/s/favt_info не требует бот-токена,
API-ключа или логина - это обычная HTML-страница, доступная всем. Именно
поэтому это НЕ Bot API (боты не могут читать историю чужих каналов) и НЕ
MTProto-клиент с логином по номеру телефона - просто чтение публичной
веб-версии, как обычный браузер.

=== КАК ЗАПУСКАТЬ ===
    pip install requests beautifulsoup4
    python3 fetch_favt_notices.py

Ограничений по количеству запросов тут нет (это не платный API), поэтому
можно дёргать часто - см. FAVT_UPDATE_INTERVAL_MINUTES в main.py (по
умолчанию каждые 15 минут, т.к. уведомления о закрытии/открытии аэропорта
критичны по времени).
"""
import os
import json
import logging
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

CHANNEL_URL = 'https://t.me/s/favt_info'
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'favt_notices.json')
LOOKBACK_HOURS = 12

# Сопоставление названий аэропортов (как их пишет Росавиация в тексте
# уведомлений) с ICAO-кодами из основного списка бота (main.py /
# fetch_yandex_data.py) - чтобы можно было показать уведомление именно
# на карточке нужного аэропорта.
AIRPORT_NAME_TO_ICAO = {
    'шереметьево': 'UUEE',
    'домодедово': 'UUDD',
    'внуково': 'UUWW',
    'пулково': 'ULLI',
    'толмачёво': 'UNNT', 'толмачево': 'UNNT',
    'кольцово': 'USSS',
    'казан': 'UWKD',  # "Казань", "Казани", "Казанский" - без окончания
    'баландино': 'USCC', 'челябинск': 'USCC',
    'омск': 'UNOO',
    'курумоч': 'UWWW', 'самар': 'UWWW',  # "Самара", "Самары"
    'платов': 'URRP', 'ростов': 'URRP',
    'уфа': 'UWUU', 'уфы': 'UWUU',
    'пашковский': 'URKK', 'краснодар': 'URKK',
    'сочи': 'URSS', 'адлер': 'URSS',
}


def find_mentioned_airports(text):
    text_lower = text.lower()
    found = set()
    for name, icao in AIRPORT_NAME_TO_ICAO.items():
        if name in text_lower:
            found.add(icao)
    return sorted(found)


def fetch_notices():
    try:
        resp = requests.get(CHANNEL_URL, timeout=20, headers={'User-Agent': 'Mozilla/5.0'})
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"❌ Не удалось загрузить {CHANNEL_URL}: {e}")
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

        # ИСПРАВЛЕНО 20.09.2026 (жалоба пользователя - "Домодедово закрыт в
        # боте, а по факту открыт", подтверждено диагностикой в логах:
        # уведомление со ВРЕМЕНЕМ снятия ограничений, но ТЕКСТОМ старого
        # закрытия): сообщение о СНЯТИИ ограничений в канале часто идёт как
        # ОТВЕТ (reply) на более раннее сообщение о ВВЕДЕНИИ ограничений -
        # видно на скриншоте канала: серый блок с цитатой закрытия сверху,
        # а сам новый текст "СНЯТЫ..." снизу. У Telegram веб-превью (t.me/s/)
        # цитата встроена ВНУТРИ того же .tgme_widget_message_wrap и тоже
        # размечена классом .tgme_widget_message_text - select_one() берёт
        # ПЕРВОЕ совпадение в порядке DOM, а цитата в разметке идёт раньше
        # собственного текста сообщения - поэтому доставался текст ЦИТАТЫ
        # (старое "ВВЕДЕНЫ") вместо текста самого нового сообщения ("СНЯТЫ"),
        # при этом время (msg_time, из футера самого сообщения, а не цитаты)
        # бралось верно - отсюда время нового сообщения при тексте старого.
        # Теперь берём ПОСЛЕДНЕЕ совпадение .tgme_widget_message_text в
        # сообщении - это и есть собственный текст (после цитаты, если она
        # есть), у обычных сообщений без цитаты там всё равно только один
        # элемент, так что для них ничего не меняется.
        text_tags = msg.select('.tgme_widget_message_text')
        if not text_tags:
            continue
        text_tag = text_tags[-1]
        text = text_tag.get_text(separator=' ', strip=True)
        if not text:
            continue

        notices.append({
            'time': msg_time.isoformat(),
            'text': text,
            'airports': find_mentioned_airports(text),
        })

    notices.sort(key=lambda n: n['time'], reverse=True)
    return notices


def main():
    notices = fetch_notices()
    result = {
        'generated_at': datetime.now().isoformat(),
        'source': CHANNEL_URL,
        'lookback_hours': LOOKBACK_HOURS,
        'notices': notices,
    }
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info(f"💾 Сохранено {len(notices)} уведомлений Росавиации за последние {LOOKBACK_HOURS}ч в {OUTPUT_FILE}")


if __name__ == '__main__':
    main()
