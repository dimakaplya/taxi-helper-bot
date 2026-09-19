#!/usr/bin/env python3
"""
Фоновый сборщик данных о дорожной ситуации Москвы с портала открытых данных
Правительства Москвы (data.mos.ru / apidata.mos.ru).

=== ПРО API ===
apidata.mos.ru - официальный REST API портала. Ключ передаётся в query
string как ?api_key=<ключ> (получен через личный кабинет на data.mos.ru,
пользователь зарегистрировал и прислал сам - см. переменную окружения
MOS_DATA_API_KEY на Railway, в коде ключ не хранится).

Базовые эндпоинты (см. официальную документацию apidata.mos.ru/Docs):
    GET https://apidata.mos.ru/v1/datasets                       - список датасетов
    GET https://apidata.mos.ru/v1/datasets/{id}                  - структура датасета
    GET https://apidata.mos.ru/v1/datasets/{id}/rows              - данные датасета

Используемый датасет - MOS_ROAD_DATASET_ID (по умолчанию 62747, "Табло
отображения информации на улично-дорожной сети"). ВАЖНО: содержимое и точная
структура полей этого датасета не были проверены заранее - сайты
data.mos.ru/apidata.mos.ru недоступны для проверки из окружения, где
писался этот скрипт (DNS/robots.txt блокируют исходящий запрос). Поэтому
main() сохраняет СЫРОЙ ответ API как есть (raw_rows) в mos_road_data.json,
а разбор конкретных полей (какие именно колонки содержат текст
сообщения/место/время) нужно доделать по факту первого реального ответа -
см. логи Railway после деплоя (там будет напечатан пример первой строки).

=== КАК ЗАПУСКАТЬ ===
    pip install requests
    MOS_DATA_API_KEY=<ключ> python3 fetch_mos_road_data.py
"""
import os
import json
import logging
from datetime import datetime, timezone

import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mos_road_data.json')

API_BASE = "https://apidata.mos.ru/v1"
MOS_DATA_API_KEY = os.getenv('MOS_DATA_API_KEY')
MOS_ROAD_DATASET_ID = os.getenv('MOS_ROAD_DATASET_ID', '62747')

# Сколько строк максимум запрашивать за раз - у датасета "Табло" может быть
# много записей (по одной на каждое информационное табло города), нам не
# нужны все, только последние/самые релевантные - но раз структура сортировки
# ещё не проверена, берём разумный потолок и посмотрим на реальные данные.
ROWS_TOP = 50


def fetch_dataset_rows(dataset_id, api_key, top=ROWS_TOP):
    url = f"{API_BASE}/datasets/{dataset_id}/rows"
    params = {'api_key': api_key, '$top': top}
    try:
        resp = requests.get(url, params=params, timeout=20, headers={'User-Agent': 'Mozilla/5.0'})
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"❌ Не удалось получить датасет {dataset_id} с apidata.mos.ru: {e}")
        return None


def main():
    result = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'dataset_id': MOS_ROAD_DATASET_ID,
        'raw_rows': None,
        'error': None,
    }

    if not MOS_DATA_API_KEY:
        logger.warning("⚠️ MOS_DATA_API_KEY не задан в переменных окружения - пропускаю обновление mos_road_data.json")
        result['error'] = 'no_api_key'
    else:
        logger.info(f"🔄 Запрашиваю датасет {MOS_ROAD_DATASET_ID} с apidata.mos.ru...")
        rows = fetch_dataset_rows(MOS_ROAD_DATASET_ID, MOS_DATA_API_KEY)
        if rows is None:
            result['error'] = 'fetch_failed'
        else:
            result['raw_rows'] = rows
            # Печатаем в лог пример первой строки - чтобы по логам Railway
            # можно было увидеть реальную структуру полей датасета и
            # доделать разбор (см. докстринг модуля выше).
            if isinstance(rows, list) and rows:
                logger.info(f"✅ Получено строк: {len(rows)}. Пример первой строки: {json.dumps(rows[0], ensure_ascii=False)[:2000]}")
            else:
                logger.info(f"✅ Ответ API получен, но это не непустой список - тип: {type(rows).__name__}, содержимое: {json.dumps(rows, ensure_ascii=False)[:2000]}")

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info(f"💾 Сохранено в {OUTPUT_FILE}")


if __name__ == '__main__':
    main()
