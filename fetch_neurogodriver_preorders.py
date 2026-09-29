#!/usr/bin/env python3
"""
Фоновый сборщик "предзаказов" из публичного Telegram-канала t.me/NeurogoDriver -
для экрана "ПРЕДЗАКАЗЫ" (см. блок "ПРЕДЗАКАЗЫ (канал NeurogoDriver, WebApp)" в
main.py). Тот же способ сбора, что у fetch_road_events.py/fetch_concert_events.py -
публичная веб-версия канала https://t.me/s/<channel> не требует бот-токена,
API-ключа или логина, обычная HTML-страница.

ДОБАВЛЕНО 28.09.2026 (прямая просьба пользователя - "предзаказы бери отсюда
только формируй по нашей структуре отдать заказ. И формируй кнопку перехода
в на страницу это канала с этим заказом" - https://t.me/NeurogoDriver).

=== ВАЖНО: НЕ ДОВЕРЯТЬ WebFetch/пересказу формата постов ===
Реальный формат постов этого канала проверялся НАПРЯМУЮ через requests+bs4
(20 живых постов, см. историю разработки) - никогда не через пересказ
WebFetch/суммаризацию, которая, как выяснилось на практике в этом же проекте
(на похожей задаче парсинга другого канала), может ПЕРЕФРАЗИРОВАТЬ/ПРИДУМАТЬ
точный текст постов вместо дословной цитаты. На практике у канала оказалось
МИНИМУМ 5 разных шаблонов постов (см. examples ниже и extract_route/
extract_price/extract_tariff) - единого жёсткого формата нет, поэтому парсер
"best-effort": извлекает то, что может по распознанным меткам, а ПОЛНЫЙ
СЫРОЙ ТЕКСТ поста ВСЕГДА показывается на экране как основа/подстраховка (см.
'raw_text' в результате) - если что-то не распозналось, водитель всё равно
видит исходный текст поста целиком.

Примеры реальных постов (дословно, разные шаблоны):
  1) Бот-карточка (метка на своей строке, значение - на следующей):
     "🕒\n29 сент., 06:30\n🕒\n🚘\nТариф:\nСтандарт\n📍\nОт:\nпосёлок Луч...\n
      🏁\nДо:\nЛенинский проспект, 8, Москва\n🗺\n84 км\n•\n1 ч 26 мин\n💸\n
      3 825 ₽\nна руки\n✅\nЗАБРАЛИ\n✅"          (взят - ЗАБРАЛИ/ЗАКРЫТ)
  2) Ручной пост, метка+значение в одной строке:
     "29.09\n21.00-22.00\nОткуда: Ленинградская область...\nКуда: Санкт-
      Петербург аэропорт Пулково\n✅\nМИНИВЕН 8 пасс+8 чемоданов\n✅\n146 км\n
      10.000 вруки"                              (свои ✅...✅ вокруг класса
                                                   авто - НЕ статус "взят"!)
  3) 🅰️/🅱️-вариант:
     "🚘\n🚖\n.универсал!!! ...\n🅰️\nснт Скобельцино...\n🅱️\nпгт Нахабино\n
      💬\n. 2 взр, вещи\n📈\n. 110 км\n💰\n.4500 на руки без платных"
  4) "Заказ на <время>" + "Класс:"/Откуда/Куда:
     "Заказ на Сейчас\n🚕\nКласс: стандарт\n📍\nОткуда  село Титовка...\n📍\n
      Куда: Ржев (тверская обл)\n📏\nРасстояние: 995км.\n27.000р + платка"
  5) Совсем произвольный текст без меток (Брянск/Рязань-Тула и т.п.) -
     структурные поля не извлекаются, но пост всё равно показывается по
     raw_text (см. выше).

Статус "взят"/"закрыт": шаблон 1 использует "✅\nЗАБРАЛИ\n✅" или
"✅\nЗАКРЫТ\n✅" - но пример 2 показывает, что голые ✅ вокруг текста НЕ
всегда означают статус (там это просто декоративная рамка вокруг класса
машины) - поэтому статус определяется ИСКЛЮЧИТЕЛЬНО по наличию слов
ЗАБРАЛИ/ЗАКРЫТ (заглавными, как их всегда публикует канал), а не по
наличию символа ✅ самого по себе. Одиночный "❌" в конце поста (шаблон 3/4)
на практике встречается непоследовательно (не всегда значит "закрыт") -
осознанно НЕ используется как сигнал статуса.

=== КАК ЗАПУСКАТЬ ===
    pip install requests beautifulsoup4
    python3 fetch_neurogodriver_preorders.py
"""
import os
import re
import json
import logging
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'neurogodriver_preorders_data.json')

NEUROGODRIVER_CHANNEL = 'NeurogoDriver'
LOOKBACK_HOURS = 24
MAX_MESSAGES = 40

# ЗАБРАЛИ/ЗАКРЫТ - именно заглавными (см. докстринг выше про ложный сигнал
# от декоративных ✅...✅ вокруг класса авто в некоторых постах) - канал
# всегда публикует статус заглавными буквами, поэтому регистрозависимая
# проверка "== ...' попадает только в реальный статус, а не в случайное
# слово с тем же корнем где-то в середине текста поста.
STATUS_TAKEN_MARKERS = ('ЗАБРАЛИ', 'ЗАКРЫТ')

# Спам/реклама/предупреждения о мошенниках - не заказы, в ленту не попадают.
NOISE_KEYWORDS = (
    'мошенник', 'бесплатный бот', 'без подписки и списаний', 'verify_neurobot',
)


def is_noise(text):
    t = text.lower()
    return any(kw in t for kw in NOISE_KEYWORDS)


def is_taken(text):
    return any(marker in text for marker in STATUS_TAKEN_MARKERS)


def _clean_value(value):
    """Убирает неразрывные пробелы (\\xa0, встречаются в части постов вместо
    обычного пробела после метки) и схлопывает повторные пробелы."""
    if not value:
        return value
    value = value.replace('\xa0', ' ')
    value = re.sub(r'\s{2,}', ' ', value).strip(' ,')
    return value or None


def _extract_labeled_value(lines, labels):
    """lines - строки текста поста. labels - варианты подписи поля (например
    ('От', 'Откуда')). У канала встречаются ДВА варианта разметки: значение
    на ТОЙ ЖЕ строке после подписи ("Откуда: Москва") ИЛИ подпись отдельной
    строкой, значение - на СЛЕДУЮЩЕЙ ("От:\\nМосква" - шаблон бот-карточек).
    (?![а-яёА-ЯЁ0-9]) после метки ОБЯЗАТЕЛЕН - без него короткая метка "От"
    матчится как префикс более длинного слова "Откуда" и теряет значение
    (реальный баг, найденный и исправленный при разработке на живых постах
    канала - см. докстринг модуля)."""
    for i, line in enumerate(lines):
        stripped = line.strip()
        for label in labels:
            m = re.match(r'^' + re.escape(label) + r'(?![а-яёА-ЯЁ0-9])\s*:?\s*(.*)$', stripped, re.IGNORECASE)
            if not m:
                continue
            rest = _clean_value(m.group(1))
            if rest:
                return rest
            for j in range(i + 1, len(lines)):
                nxt = _clean_value(lines[j])
                if nxt:
                    return nxt
            return None
    return None


def extract_route(text):
    lines = text.split('\n')
    pickup = _extract_labeled_value(lines, ('От', 'Откуда'))
    dropoff = _extract_labeled_value(lines, ('До', 'Куда'))
    if not pickup:
        pickup = _extract_labeled_value(lines, ('🅰️', '🅰'))
    if not dropoff:
        dropoff = _extract_labeled_value(lines, ('🅱️', '🅱'))
    return pickup, dropoff


def extract_tariff(text):
    return _extract_labeled_value(text.split('\n'), ('Тариф', 'Класс'))


def extract_distance_km(text):
    m = re.search(r'(\d+(?:[.,]\d+)?)\s*км', text, re.IGNORECASE)
    return m.group(1) if m else None


# Наблюдаемые в постах разделители тысяч - обычный/неразрывный пробел, точка
# или запятая (см. "3 825 ₽" / "1\xa0700 ₽" / "10.000 вруки" / "11.500") -
# ВСЕГДА целые тысячи, а не дробная часть (копейки в постах не встречались),
# поэтому нормализация - просто вычищаем все НЕ-цифры из найденного блока.
_PRICE_NUM = r'(\d[\d\s .,]{0,9}\d|\d)'
PRICE_SUFFIX_RE = re.compile(_PRICE_NUM + r'\s*(?:₽|руб|р\.?\b|на\s*руки|вруки)', re.IGNORECASE)
PRICE_PREFIX_RE = re.compile(r'(?:на\s*руки|вруки|цена|стоимость)\s*[:\-]?\s*' + _PRICE_NUM, re.IGNORECASE)


def extract_price(text):
    """Ищет сумму рядом с валютным/"на руки"-контекстом - и после числа
    ("3 825 ₽", "27.000р"), и ПЕРЕД числом ("на руки 11.500", встречается у
    части ручных постов). Без такого контекста рядом число НЕ считается
    ценой (иначе легко ложно поймать км/время/число пассажиров) - в этом
    случае просто вернёт None, а водитель увидит полный raw_text поста."""
    m = PRICE_SUFFIX_RE.search(text)
    if not m:
        m = PRICE_PREFIX_RE.search(text)
    if not m:
        return None
    digits = re.sub(r'\D', '', m.group(1))
    return digits or None


def fetch_channel_posts(channel_username=NEUROGODRIVER_CHANNEL):
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
        # Часть постов - только фото/видео без подписи (текста нет вообще) -
        # показывать в карточке нечего, пропускаем.
        if not raw_text:
            continue
        # Zero-width-символы (​/‌) - невидимый префикс у ботовских
        # карточек (см. пример 1 в докстринге), не несут смысла, убираем,
        # чтобы raw_text на экране не начинался с "пустых" символов.
        clean_text = re.sub(r'[​‌‍]', '', raw_text).strip()
        if not clean_text:
            continue

        if is_noise(clean_text):
            continue

        link_tag = time_tag.find_parent('a')
        msg_link = link_tag.get('href') if link_tag else f'https://t.me/{channel_username}'
        # message_id - последний сегмент ссылки (t.me/<channel>/<id>) - нужен
        # отдельно для дедупа/сортировки на фронте, если пригодится.
        msg_id = None
        if msg_link:
            m_id = re.search(r'/(\d+)$', msg_link)
            if m_id:
                msg_id = int(m_id.group(1))

        taken = is_taken(clean_text)
        pickup, dropoff = extract_route(clean_text)
        posts.append({
            'id': msg_id,
            'link': msg_link,
            'time': msg_time.isoformat(),
            'taken': taken,
            'pickup': pickup,
            'dropoff': dropoff,
            'tariff': extract_tariff(clean_text),
            'distance_km': extract_distance_km(clean_text),
            'price': extract_price(clean_text),
            'raw_text': clean_text,
        })

    # Дедуп точных повторов (канал иногда постит один и тот же заказ дважды
    # подряд, см. докстринг - тот же принцип, что merge_city_messages в
    # fetch_road_events.py) - по паре (raw_text, taken), сохраняя более
    # СВЕЖИЙ повтор (список уже отсортирован по документу сверху вниз - у
    # t.me/s/ старые посты выше, свежие ниже, поэтому идём с конца).
    seen = set()
    deduped = []
    for post in reversed(posts):
        key = (post['raw_text'], post['taken'])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(post)
    deduped.reverse()

    # Свежие сначала, открытые (не разобранные) заказы - основной случай для
    # экрана "Предзаказы"; taken-посты остаются в данных (на случай, если
    # понадобится диагностика), но handle_preorders_data_api в main.py их
    # отфильтровывает перед показом.
    deduped.sort(key=lambda p: p['time'], reverse=True)
    return deduped[:MAX_MESSAGES]


def main():
    result = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'lookback_hours': LOOKBACK_HOURS,
        'channel': NEUROGODRIVER_CHANNEL,
        'posts': [],
    }
    try:
        posts = fetch_channel_posts()
        result['posts'] = posts
        open_count = sum(1 for p in posts if not p['taken'])
        logger.info(
            f"✅ NeurogoDriver: {len(posts)} постов за последние {LOOKBACK_HOURS}ч "
            f"(открытых: {open_count}, взято/закрыто: {len(posts) - open_count})"
        )
    except Exception as e:
        logger.exception(f"❌ Ошибка сбора предзаказов из @{NEUROGODRIVER_CHANNEL}: {e}")

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info(f"💾 Сохранено в {OUTPUT_FILE}")


if __name__ == '__main__':
    main()
