"""Слой доступа к данным — стабильный API дашборда.

UI вызывает только эти функции и ничего не знает про источник данных.
Сейчас все они делегируют в `data/stubs.py` (заглушки). При интеграции с
ClickHouse тело каждой функции заменится на реальный параметризованный запрос
через `data.clickhouse.execute(...)`, а сигнатуры и формат результата
останутся прежними.

Соглашение о `filters` — dict:
    {
        "players": list[str],   # адреса акул/китов (обязательно)
        "pools":   list[str],   # адреса пулов; пусто = весь рынок
        "time_range": str,      # ключ из config.TIME_RANGES
    }
"""

from __future__ import annotations

import config
from data import stubs


# --- Кейс 1: Анализ рынка ---------------------------------------------------
def get_top_pools(filters: dict):
    """Топ-50 пулов по объёму. DataFrame[pool, volume]."""
    return stubs.top_pools(config.TOP_POOLS_LIMIT)


def get_market_metrics(filters: dict, pair: str | None = None) -> dict:
    """7 метрик рынка: total + разбивка по паре. См. config.MARKET_METRICS."""
    return stubs.market_metrics(pair)


def get_metric_timeseries(filters: dict, metric: str, pair: str | None = None):
    """Динамика конкретной метрики по времени. DataFrame[time, value]."""
    return stubs.metric_timeseries(metric, filters.get("time_range", "today"), pair)


# --- Кейс 2: Анализ тренда --------------------------------------------------
def get_pools_left(filters: dict, reference: str):
    """Пулы, где играли в reference-окне, но не сегодня. DataFrame[pool, volume]."""
    return stubs.pools_left(reference)


def get_pools_entered(filters: dict, reference: str):
    """Пулы, где появились сегодня, но не было в reference-окне."""
    return stubs.pools_entered(reference)


def get_daily_changes(filters: dict, metric: str, group_by: str):
    """Изменение метрики по дням, широкий формат (колонка на серию)."""
    return stubs.daily_changes(metric, group_by)


def get_heatmap_sharks_pools(filters: dict, metric: str):
    """Хитмап 1: строки=пулы, колонки=акулы, значения=metric."""
    return stubs.heatmap_sharks_pools(metric, config.HEATMAP_POOLS_LIMIT)


def get_heatmap_time_pools(filters: dict, metric: str):
    """Хитмап 2: строки=пулы, колонки=время, значения=metric."""
    return stubs.heatmap_time_pools(metric, filters.get("time_range", "today"), config.HEATMAP_POOLS_LIMIT)


def get_area_by_pool(filters: dict, metric: str, limit: int = config.AREA_POOLS_LIMIT):
    """Filled area 1: время × объём, серии=пулы (ограничено limit)."""
    return stubs.area_by_pool(metric, filters.get("time_range", "today"), limit)


def get_area_by_shark(filters: dict, metric: str, limit: int = config.AREA_SHARKS_LIMIT):
    """Filled area 2: время × объём, серии=акулы (ограничено limit)."""
    return stubs.area_by_shark(metric, filters.get("time_range", "today"), limit)
