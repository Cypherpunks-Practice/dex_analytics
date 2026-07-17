"""Единая точка данных для страницы «Сигналы».

UI (callbacks.refresh_signals) вызывает только `iter_signal_matches()`. Внутри —
ClickHouse: он материализует сигналы из Postgres (`signals_legs`), сам сшивает их
со сделками и отдаёт ПО СТРОКЕ НА СИГНАЛ (см. `data/signals_queries.py`).

Батчи. Раньше они резались по БЛОКАМ и защищали pandas от OOM: матчинг тянул в
память все свопы окна. Теперь сшивка идёт в БД, и батч режется по СИГНАЛАМ —
он ограничивает пик памяти ClickHouse на джойне (база живая и растёт) и заодно
даёт прогрессивную дорисовку таблицы.

`USE_STUB = True` (общий флаг из `data/clickhouse.py`) переключает на офлайн-
заглушку: детерминированный summary без обеих БД — так страницу можно смотреть
и править без доступа к ClickHouse.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import config
from data import clickhouse, signals_queries

# Контракт колонок summary живёт рядом с запросом, который его порождает.
SUMMARY_COLS = signals_queries.SUMMARY_COLS


def _as_block_window(value) -> int:
    """`block_window` с фронтенда приходит строкой (tgb.input); приводим к int.

    Пусто / мусор / отрицательное → дефолт `config.SIGNAL_BLOCK_WINDOW`.
    """
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return config.SIGNAL_BLOCK_WINDOW
    return n if n >= 0 else config.SIGNAL_BLOCK_WINDOW


def _query_min_timestamp(time_range: str):
    """Нижняя граница времени для выборки сигналов по выбранной дате.

    Ширину окна берём из `config.SIGNALS_TIME_WINDOWS` и откладываем от анкера —
    максимальной метки времени материализованных сигналов (анкер «по данным»,
    ср. `config.TIME_ANCHOR`). `"all"` / неизвестный ключ → None (без нижней
    границы: тянем всё, что есть в пределах SIGNALS_RETENTION_DAYS).
    """
    window = config.SIGNALS_TIME_WINDOWS.get(time_range)
    if window is None:
        return None
    anchor = signals_queries.get_max_timestamp()
    if anchor is None:
        return None
    return anchor - window


def iter_signal_matches(limit: int = config.SIGNALS_QUERY_LIMIT,
                        block_window: int = config.SIGNAL_BLOCK_WINDOW,
                        time_range: str = config.DEFAULT_TIME_RANGE):
    """Генератор батчей покрытия: yield-ит ``(batch_summary_df, idx, total)``.

    Сигналы окна «Дата» режутся на батчи по `config.SIGNALS_BATCH_SIZE`; каждый
    батч — отдельный запрос к ClickHouse (сортировка «свежие первыми», поэтому при
    упоре в `limit` останутся самые свежие). Сам срез выбирает БД: гнать в неё
    список из тысяч id нельзя — он не помещается в HTTP-параметр.

    ``idx`` — индекс батча с 0, ``total`` — общее число батчей (для прогресса).
    STUB отдаёт один батч. Пустая выборка → генератор ничего не yield-ит.
    """
    block_window = _as_block_window(block_window)
    if clickhouse.USE_STUB:
        yield _stub_summary(limit, block_window), 0, 1
        return

    min_ts = _query_min_timestamp(time_range)
    n = min(signals_queries.get_signal_count(min_ts), int(limit))
    if n <= 0:
        return

    size = max(1, int(config.SIGNALS_BATCH_SIZE))
    total = (n + size - 1) // size
    for idx in range(total):
        offset = idx * size
        yield (signals_queries.get_signal_summary(
                   offset, min(size, n - offset), block_window, min_ts),
               idx, total)


def get_signal_matches(limit: int = config.SIGNALS_QUERY_LIMIT,
                       block_window: int = config.SIGNAL_BLOCK_WINDOW,
                       time_range: str = config.DEFAULT_TIME_RANGE) -> pd.DataFrame:
    """Весь summary одним куском (стаб / тесты / не-прогрессивный путь)."""
    parts = [batch for batch, _, _ in
             iter_signal_matches(limit, block_window, time_range)]
    return (pd.concat(parts, ignore_index=True) if parts
            else signals_queries.empty_summary())


# --- Поблочное сравнение брайбов с заданным конкурентом ----------------------
BRIBE_CMP_COLS = signals_queries.BRIBE_CMP_COLS


def empty_bribe_comparison() -> pd.DataFrame:
    """Пустой каркас сравнения брайбов (стартовое значение состояния таблицы)."""
    return signals_queries.empty_bribe_comparison()


def get_bribe_comparison(competitor: str,
                         time_range: str = config.DEFAULT_TIME_RANGE) -> pd.DataFrame:
    """Наш суммарный брайб vs брайб конкурента по блокам; окно — выбранная «Дата».

    Пустой адрес → пустой каркас. STUB отдаёт детерминированную выборку без БД.
    """
    competitor = str(competitor or "").strip().lower()
    if not competitor:
        return empty_bribe_comparison()
    if clickhouse.USE_STUB:
        return _stub_bribe_comparison(competitor)
    min_ts = _query_min_timestamp(time_range)
    return signals_queries.get_bribe_comparison(competitor, min_ts=min_ts)


# --------------------------------------------------------------------------- #
# Заглушка: детерминированный summary без обеих БД (USE_STUB=true).
# --------------------------------------------------------------------------- #
_STUB_PAIRS = [
    ("ETH", "USD", "WETH/USDC Uv3 0.05% SELL | ETH/USDT Perp OKX BUY"),
    ("BTC", "USD", "WBTC/USDT Uv3 0.3% SELL | BTC/USDT Perp Binance BUY"),
    ("LINK", "ETH", "LINK/ETH Uv2 BUY | LINK/USDT Perp OKX SELL"),
    ("XAU", "USD", "PAXG/USDC Uv3 0.3% SELL | XAU/USDT Perp OKX BUY"),
]


def _stub_summary(limit: int, block_window: int) -> pd.DataFrame:
    """~80 сигналов за ~40 дней; часть покрыта атомарно, часть — по объёму.

    Форма и типы совпадают с боевым `get_signal_summary`, поэтому страница,
    фильтры и статистика проверяются end-to-end без ClickHouse и Postgres.
    """
    rng = np.random.default_rng(42)
    n = min(int(limit), 80)
    base_block = 25_500_000
    t0 = pd.Timestamp("2026-07-14 09:00:00")

    rows = []
    for i in range(n):
        base, quote, route = _STUB_PAIRS[int(rng.integers(0, len(_STUB_PAIRS)))]
        n_hops = int(rng.integers(1, 6))
        found_block = base_block - int(rng.integers(0, 5_000))
        amount = float(rng.uniform(1_000, 500_000))
        bribe = float(rng.uniform(1e-5, 5e-3))

        # Атомарное покрытие возможно только при >=2 ногах — как в боевом запросе.
        kind = ""
        if rng.random() < 0.45:
            kind = "atomic" if n_hops >= config.SIGNAL_MIN_ATOMIC_LEGS else "volume"
        covered = kind != ""
        covered_legs = (int(rng.integers(2, n_hops + 1)) if kind == "atomic"
                        else 1 if kind == "volume" else 0)
        comp_bribe = float(rng.uniform(1e-5, 5e-3)) if covered else np.nan
        volume = amount * float(rng.uniform(0.5, 1.2)) if covered else 0.0
        swap_block = found_block + int(rng.integers(-block_window, block_window + 1))

        rows.append({
            "request_id": f"stub-{i:04d}",
            "signal_timestamp": t0 - pd.Timedelta(minutes=int(rng.integers(0, 60 * 24 * 40))),
            "found_block": found_block,
            "base_token": base, "quote_token": quote,
            "token_a": base, "token_b": quote,
            "signal_amount": amount,
            "profit": float(rng.uniform(-500, 5_000)),
            "route": route,
            "signal_bribe": bribe,
            "signal_fee": 0.003,
            "n_hops": n_hops,
            "covered": covered,
            "coverage_kind": kind,
            "covered_legs": covered_legs,
            "coverage_ratio": covered_legs / n_hops,
            "covering_volume": volume,
            "n_trades": int(rng.integers(1, 5)) if covered else 0,
            "swap_timestamp": (t0 - pd.Timedelta(minutes=int(rng.integers(0, 60 * 24 * 40)))
                               if covered else pd.NaT),
            "swap_block": swap_block if covered else pd.NA,
            "swap_route_str": route.split(" | ")[0] if covered else "",
            "swap_amount": volume if covered else np.nan,
            "swap_user_id": f"0xshark{int(rng.integers(1, 6)):034x}" if covered else "",
            "swap_bribe": comp_bribe,
            "swap_fee": float(rng.uniform(1e-6, 1e-4)) if covered else np.nan,
            "competitor_bribe": comp_bribe,
            "bribe_edge": bribe - comp_bribe if covered else np.nan,
        })

    df = pd.DataFrame(rows, columns=SUMMARY_COLS)
    df["swap_block"] = df["swap_block"].astype("Int64")
    return df


def _stub_bribe_comparison(competitor: str) -> pd.DataFrame:
    """~30 блоков сравнения брайбов; детерминированно по адресу конкурента.

    Форма и типы совпадают с боевым get_bribe_comparison, поэтому секция и её
    таблица проверяются end-to-end без ClickHouse.
    """
    seed = sum(competitor.encode()) or 1        # стабильно, без PYTHONHASHSEED
    rng = np.random.default_rng(seed)
    base_block = 25_500_000

    rows = []
    for i in range(30):
        our = float(rng.uniform(0, 5e-3))
        comp = float(rng.uniform(0, 5e-3))
        rows.append({
            "block": base_block - i,
            "n_signals": int(rng.integers(0, 5)),
            "our_bribe": our,
            "n_tx": int(rng.integers(0, 3)),
            "competitor_bribe": comp,
            "bribe_edge": our - comp,
        })

    df = pd.DataFrame(rows, columns=BRIBE_CMP_COLS)
    for col in ("block", "n_signals", "n_tx"):
        df[col] = df[col].astype("int64")
    return df
