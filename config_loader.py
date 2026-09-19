"""Единая точка чтения config.json (список аэропортов/вокзалов бота).

Раньше этот справочник дублировался вручную в трёх файлах: main.py
(AIRPORTS_INFO/STATION_CITY/STATION_CAPACITY/AIRPORT_CAPACITY/
AIRPORT_TIMEZONE/AIRPORT_COORDS), fetch_yandex_data.py (AIRPORTS) и
fetch_trains_data.py (STATIONS). Именно рассинхрон между такими копиями
уже приводил к реальному багу (Шереметьево показывал "все нули" из-за
несовпадения кода станции). Теперь все три файла читают config.json
через этот модуль - один источник правды, никакого ручного дублирования.

config.json лежит рядом с *.py в корне репозитория. Если файл не найден
или битый - кидаем понятную ошибку при старте (лучше упасть сразу и
явно, чем тихо работать с пустыми списками аэропортов/вокзалов)."""
import json
import os

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')

_cache = None


def load_config():
    """Читает и кэширует config.json (кэш - на весь процесс: справочник
    меняется только через git push + редеплой, перечитывать на лету не
    нужно). Бросает исключение с понятным текстом, если файла нет или
    JSON битый - чтобы проблема была видна сразу в логах Railway при
    старте, а не проявлялась потом непонятной нехваткой аэропорта."""
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(_CONFIG_PATH, 'r', encoding='utf-8') as f:
            _cache = json.load(f)
    except FileNotFoundError:
        raise RuntimeError(f"config.json не найден рядом с кодом ({_CONFIG_PATH}) - справочник аэропортов/вокзалов недоступен")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"config.json повреждён (невалидный JSON): {e}")
    return _cache


def get_cities():
    """dict {city_key: {'airports': [...], 'stations': [...]}} - как в config.json."""
    return load_config()['cities']


def get_all_airports():
    """Плоский список ВСЕХ записей аэропортов по всем городам (эквивалент
    прохода по старому AIRPORTS_INFO.values()), каждая запись дополнена
    полем 'city'."""
    result = []
    for city, data in get_cities().items():
        for airport in data.get('airports', []):
            entry = dict(airport)
            entry['city'] = city
            result.append(entry)
    return result


def get_all_stations():
    """Плоский список ВСЕХ записей вокзалов по всем городам, каждая
    запись дополнена полем 'city'."""
    result = []
    for city, data in get_cities().items():
        for station in data.get('stations', []):
            entry = dict(station)
            entry['city'] = city
            result.append(entry)
    return result
