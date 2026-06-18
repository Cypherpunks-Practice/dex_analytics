"""Генератор правдоподобных случайных данных (заглушки).

Этот модуль временно заменяет реальные запросы к ClickHouse. Все функции
возвращают `pandas.DataFrame` (или dict) в том же формате, в котором позже
будут возвращать настоящие запросы, поэтому при подключении БД достаточно
переписать `data/queries.py`, а UI трогать не придётся.

Данные генерируются случайно при каждом вызове — так видно, что автообновление
дашборда действительно работает.
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

# Фиксированный «словарь» правдоподобных названий пар токенов для пулов.
_TOKENS = [
    "WETH", "USDC", "USDT", "DAI", "WBTC", "PEPE", "SHIB", "LINK",
    "UNI", "AAVE", "MKR", "LDO", "ARB", "OP", "MATIC", "CRV",
]


def _rng(*parts) -> random.Random:
    """Детерминированный RNG по «соли» — для стабильных списков пулов/акул,
    но с вкраплением времени там, где нужна изменчивость."""
    seed = hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()
    return random.Random(int(seed[:8], 16))


def _pair_name(i: int) -> str:
    r = _rng("pair", i)
    a, b = r.sample(_TOKENS, 2)
    return f"{a}/{b}"


def _pool_addr(i: int) -> str:
    h = hashlib.md5(f"pool-{i}".encode()).hexdigest()
    return "0x" + h[:40]


def _pool_label(i: int) -> str:
    """Человекочитаемая подпись пула: пара + укороченный адрес."""
    return f"{_pair_name(i)} ({_pool_addr(i)[:6]}…{_pool_addr(i)[-4:]})"


# --- Топ пулов --------------------------------------------------------------
def top_pools(limit: int) -> pd.DataFrame:
    """Топ пулов по объёму (случайный, отсортированный по убыванию)."""
    n = limit
    vols = np.sort(np.random.gamma(2.0, 50_000, n))[::-1]
    return pd.DataFrame(
        {
            "pool": [_pool_label(i) for i in range(n)],
            "volume": np.round(vols, 2),
        }
    )


# --- Метрики анализа рынка --------------------------------------------------
def market_metrics(pair: str | None) -> dict:
    """Семь метрик ТЗ. Каждая = total + разбивка по парам (DataFrame).

    Если задан `pair`, разбивка фильтруется только по совпадающим парам.
    """
    pairs = [_pair_name(i) for i in range(8)]
    if pair:
        pairs = [p for p in pairs if pair.lower() in p.lower()] or pairs[:1]

    def by_pair(scale: float) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "pair": pairs,
                "value": np.round(np.random.gamma(2.0, scale, len(pairs)), 2),
            }
        )

    sizes = np.random.gamma(2.0, 5_000, 500)
    return {
        "trade_volume": {"total": round(float(sizes.sum()), 2), "by_pair": by_pair(80_000)},
        "bribe_volume": {"total": round(float(np.random.gamma(2.0, 3_000)), 2), "by_pair": by_pair(4_000)},
        "avg_size": {"total": round(float(sizes.mean()), 2), "by_pair": by_pair(5_000)},
        "median_size": {"total": round(float(np.median(sizes)), 2), "by_pair": by_pair(4_500)},
        "max_size": {"total": round(float(sizes.max()), 2), "by_pair": by_pair(30_000)},
        "min_size": {"total": round(float(sizes.min()), 2), "by_pair": by_pair(200)},
        "trade_count": {"total": int(len(sizes)), "by_pair": by_pair(60)},
    }


def metric_timeseries(metric: str, time_range: str, pair: str | None) -> pd.DataFrame:
    """Динамика метрики по времени в выбранном диапазоне."""
    points = {
        "last_hour": 12, "today": 24, "yesterday": 24,
        "week": 7, "month": 30, "all": 24,
    }.get(time_range, 24)
    base = datetime(2026, 6, 18, 0, 0, 0)
    step = timedelta(hours=1) if points >= 12 and time_range in ("last_hour", "today", "yesterday", "all") else timedelta(days=1)
    times = [base - step * (points - 1 - i) for i in range(points)]
    scale = {"trade_count": 60}.get(metric, 5_000)
    values = np.round(np.cumsum(np.random.normal(0, scale * 0.3, points)) + np.random.gamma(2.0, scale, points), 2)
    return pd.DataFrame({"time": times, "value": np.abs(values)})


# --- Анализ тренда: diff пулов ----------------------------------------------
def _pool_diff(n: int) -> pd.DataFrame:
    idx = sorted(random.sample(range(100), n))
    return pd.DataFrame(
        {
            "pool": [_pool_label(i) for i in idx],
            "volume": np.round(np.random.gamma(2.0, 40_000, n), 2),
        }
    )


def pools_left(reference: str) -> pd.DataFrame:
    """Пулы, где игроки были в reference-окне, но не сегодня."""
    return _pool_diff(random.randint(3, 7))


def pools_entered(reference: str) -> pd.DataFrame:
    """Пулы, где игроки появились сегодня, но не было в reference-окне."""
    return _pool_diff(random.randint(3, 7))


def daily_changes(metric: str, group_by: str) -> pd.DataFrame:
    """Изменение метрики по дням, в широком формате (колонка на серию)
    для построения сгруппированного графика."""
    days = [datetime(2026, 6, 18) - timedelta(days=d) for d in range(13, -1, -1)]
    if group_by == "player":
        series = [_shark_label(i) for i in range(4)]
    else:
        series = [_pool_label(i) for i in range(4)]
    scale = {"trade_count": 50}.get(metric, 4_000)
    data = {"day": days}
    for s in series:
        data[s] = np.round(np.abs(np.cumsum(np.random.normal(0, scale * 0.4, len(days))) + np.random.gamma(2.0, scale, len(days))), 2)
    return pd.DataFrame(data)


# --- Акулы ------------------------------------------------------------------
def _shark_addr(i: int) -> str:
    h = hashlib.md5(f"shark-{i}".encode()).hexdigest()
    return "0x" + h[:40]


def _shark_label(i: int) -> str:
    a = _shark_addr(i)
    return f"{a[:6]}…{a[-4:]}"


# --- Хитмапы ----------------------------------------------------------------
def heatmap_sharks_pools(metric: str, n_pools: int) -> pd.DataFrame:
    """Матрица: строки — пулы, колонки — акулы, значения — метрика.
    Формат: индекс = пулы, колонки = акулы (удобно для chart type=heatmap)."""
    sharks = [_shark_label(i) for i in range(6)]
    pools = [_pool_label(i) for i in range(n_pools)]
    scale = {"trade_count": 50}.get(metric, 5_000)
    mat = np.round(np.random.gamma(1.5, scale, (len(pools), len(sharks))), 2)
    return pd.DataFrame(mat, index=pools, columns=sharks)


def heatmap_time_pools(metric: str, time_range: str, n_pools: int) -> pd.DataFrame:
    """Матрица: строки — пулы, колонки — временные точки, значения — метрика."""
    points = {"week": 7, "month": 30}.get(time_range, 12)
    base = datetime(2026, 6, 18)
    cols = [(base - timedelta(days=points - 1 - i)).strftime("%m-%d") for i in range(points)]
    pools = [_pool_label(i) for i in range(n_pools)]
    scale = {"trade_count": 50}.get(metric, 5_000)
    mat = np.round(np.random.gamma(1.5, scale, (len(pools), points)), 2)
    return pd.DataFrame(mat, index=pools, columns=cols)


# --- Filled area ------------------------------------------------------------
def area_by_pool(metric: str, time_range: str, limit: int) -> pd.DataFrame:
    """Перетекание средств между пулами во времени.
    Широкий формат: колонка времени + по колонке на каждый пул."""
    points = {"week": 7, "month": 30}.get(time_range, 24)
    base = datetime(2026, 6, 18)
    step = timedelta(days=1) if points in (7, 30) else timedelta(hours=1)
    times = [base - step * (points - 1 - i) for i in range(points)]
    scale = {"trade_count": 50}.get(metric, 5_000)
    data = {"time": times}
    for i in range(limit):
        data[_pool_label(i)] = np.round(np.abs(np.random.gamma(2.0, scale, points)), 2)
    return pd.DataFrame(data)


def area_by_shark(metric: str, time_range: str, limit: int) -> pd.DataFrame:
    """Изменение объёмов акул во времени.
    Широкий формат: колонка времени + по колонке на каждую акулу."""
    points = {"week": 7, "month": 30}.get(time_range, 24)
    base = datetime(2026, 6, 18)
    step = timedelta(days=1) if points in (7, 30) else timedelta(hours=1)
    times = [base - step * (points - 1 - i) for i in range(points)]
    scale = {"trade_count": 50}.get(metric, 5_000)
    data = {"time": times}
    for i in range(limit):
        data[_shark_label(i)] = np.round(np.abs(np.random.gamma(2.0, scale, points)), 2)
    return pd.DataFrame(data)
