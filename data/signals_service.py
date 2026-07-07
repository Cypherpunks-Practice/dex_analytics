"""Единая точка данных для страницы «Сигналы».

UI (callbacks.refresh_signals) вызывает только `get_signal_matches()`; внутри —
гибрид двух БД: сигналы из Postgres (`data/signals_queries.py`), сделки акул и
китов из ClickHouse eywa (`data/new_queries.py`), сопоставление —
`data/matching.py` (build_matches).

`USE_STUB = True` (общий флаг из `data/clickhouse.py`) переключает на офлайн-
заглушку: детерминированные фейковые сигналы и СВЯЗНЫЕ с ними трейды (часть
сигналов покрыта сплитами/мультихопом, есть шум в чужих блоках) прогоняются
через реальный `build_matches` — пайплайн работает end-to-end без обеих БД.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import config
from data import clickhouse, matching, new_queries, signals_queries


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
    """Нижняя граница `timestamp` для запроса сигналов по выбранной дате.

    Ширину окна берём из `config.SIGNALS_TIME_WINDOWS` и откладываем от анкера —
    максимальной метки времени в таблице сигналов (анкер «по данным», т.к. дамп
    статичен и метки лежат в прошлом; ср. `config.TIME_ANCHOR`). Единицу БД
    (секунды/миллисекунды) определяем по величине анкера — как в
    `signals_queries.get_signals`. `"all"` / неизвестный ключ → None (без нижней
    границы, тянем всё).
    """
    window = config.SIGNALS_TIME_WINDOWS.get(time_range)
    if window is None:
        return None
    anchor = signals_queries.get_max_timestamp()
    if anchor is None:
        return None
    per_second = 1000 if float(anchor) > 1e12 else 1
    return int(anchor) - int(window.total_seconds()) * per_second


def iter_signal_matches(limit: int = config.SIGNALS_QUERY_LIMIT,
                        block_window: int = config.SIGNAL_BLOCK_WINDOW,
                        time_range: str = config.DEFAULT_TIME_RANGE):
    """Генератор батчей покрытия: yield-ит ``(batch_summary_df, idx, total)``.

    Сигналы тянутся из Postgres ОДИН раз (свежие первыми — ORDER BY timestamp DESC),
    затем трейды (все трейдеры/пары) и матчинг идут БАТЧАМИ по сигналам. Размер батча
    в сигналах = ``SIGNALS_BATCH_BLOCKS // (2*block_window+1)`` — так набор блоков (а с
    ним объём выборки и пик памяти) на батч ограничен независимо от ширины окна.

    ``idx`` — индекс батча с 0, ``total`` — общее число батчей (для прогресса).
    STUB отдаёт один батч. Пустая выборка сигналов → генератор ничего не yield-ит.
    """
    block_window = _as_block_window(block_window)
    if clickhouse.USE_STUB:
        signals_df, trades_df = _stub_signals_and_trades(limit)
        summary, _ = matching.build_matches(signals_df, trades_df, block_window=block_window)
        yield summary, 0, 1
        return

    min_ts = _query_min_timestamp(time_range)
    ts_kwargs = {} if min_ts is None else {"min_timestamp": min_ts}
    # timestamp_increase=False → ORDER BY timestamp DESC: при упоре в limit
    # оставляем самые свежие сигналы окна.
    signals_df = signals_queries.get_signals(
        limit, timestamp_increase=False, **ts_kwargs)
    n = len(signals_df)
    if n == 0:
        return

    batch_size = max(1, config.SIGNALS_BATCH_BLOCKS // (2 * block_window + 1))
    total = (n + batch_size - 1) // batch_size
    for idx in range(total):
        batch = signals_df.iloc[idx * batch_size:(idx + 1) * batch_size]
        trades_df = new_queries.get_trades(
            matching.signal_pair_blocks(batch), window_size=block_window)
        summary, _ = matching.build_matches(batch, trades_df, block_window=block_window)
        yield summary, idx, total


def get_signal_matches(limit: int = config.SIGNALS_QUERY_LIMIT,
                       block_window: int = config.SIGNAL_BLOCK_WINDOW,
                       time_range: str = config.DEFAULT_TIME_RANGE):
    """(summary_df, matches_df) — не-прогрессивный путь (стаб/тесты/совместимость).

    Склеивает все батчи `iter_signal_matches` в один summary. `matches_df` на этом
    пути не нужен (UI берёт только summary) — возвращаем пустой каркас по контракту.
    """
    parts = [batch for batch, _, _ in
             iter_signal_matches(limit, block_window, time_range)]
    summary = (pd.concat(parts, ignore_index=True) if parts
               else pd.DataFrame(columns=matching._SUMMARY_COLS))
    return summary, pd.DataFrame(columns=matching._MATCHES_COLS)


# --------------------------------------------------------------------------- #
# Заглушка: связные сигналы + трейды (детерминированный seed).
# --------------------------------------------------------------------------- #
# Фиксированный «алфавит» адресов токенов (нижний регистр, как в БД после
# lower). Каждый адрес — повторение одной hex-цифры, чтобы адреса были
# различимы по любой подстроке (важно для фильтра «Токен» на странице).
_STUB_TOKENS = ["0x" + f"{i:x}" * 40 for i in range(1, 9)]


def _stub_hop(token_in: str, token_out: str) -> dict:
    """Хоп маршрута в формате route из Postgres (см. self-test matching.py)."""
    return {"fee_rate": 0.003, "protocol": {"id": 0, "version": 3},
            "decimals_in": 18, "decimals_out": 18,
            "token_in_address": token_in, "token_out_address": token_out}


def _stub_trade(hop: dict, block: int, usd: float, ts, rng) -> dict:
    """Трейд в формате get_trades (колонки контракта new_queries).

    token_a=token_in, token_b=token_out + side='sell' → направление свопа
    совпадает с хопом сигнала (in→out), поэтому ориентированный граф покрытия
    видит цепочку в нужную сторону.
    """
    return dict(
        block_number=block,
        token_a=hop["token_in_address"],
        token_b=hop["token_out_address"],
        usd_amount=float(usd),
        trader_address=f"0xshark{int(rng.integers(1, 6)):034x}",
        bribe=str(int(rng.integers(10**15, 10**18))),
        priority_fee=str(int(rng.integers(10**14, 10**16))),
        side="sell",
        swap_timestamp=ts,
    )


def _stub_signals_and_trades(limit: int):
    """~80 сигналов за ~40 дней; ~60% получают покрывающие трейды.

    Покрытие разнообразное: сплиты по 1-2 трейда на хоп со случайным
    коэффициентом 0.5-1.2 от нужного объёма, поэтому есть и полностью, и
    частично покрытые сигналы; плюс шумовые трейды в чужих блоках, которые
    матчинг обязан отбросить.
    """
    rng = np.random.default_rng(42)
    n = min(limit, 80)
    base_block = 24791000
    t0 = pd.Timestamp("2026-05-18 12:00:00")

    signals, trades = [], []
    for i in range(n):
        block = base_block + int(rng.integers(0, 5000))
        ia, ib, ic = rng.choice(len(_STUB_TOKENS), size=3, replace=False)
        a, b, c = _STUB_TOKENS[ia], _STUB_TOKENS[ib], _STUB_TOKENS[ic]
        route = [_stub_hop(a, b)]
        if rng.integers(1, 3) == 2:                      # 1 или 2 хопа
            route.append(_stub_hop(b, c))
        amount = float(rng.uniform(1_000, 500_000))
        ts = t0 - pd.Timedelta(minutes=int(rng.integers(0, 60 * 24 * 40)))
        signals.append(dict(
            request_id=i + 1, ts=ts,
            base_token=route[-1]["token_out_address"], quote_token=a,
            quote_amount=amount, bribe=float(rng.uniform(0, 2)),
            found_block=block, route=route,
            profit=float(rng.uniform(-500, 5_000)),
        ))
        if rng.random() < 0.6:                           # покрывающие трейды
            for hop in route:
                splits = int(rng.integers(1, 3))
                for _ in range(splits):
                    usd = amount / splits * float(rng.uniform(0.5, 1.2))
                    trades.append(_stub_trade(hop, block, usd, ts, rng))
        if rng.random() < 0.3:                           # шум: чужой блок
            trades.append(_stub_trade(
                route[0], block + 7, float(rng.uniform(1_000, 50_000)), ts, rng))

    signals_df = pd.DataFrame(signals)
    trades_df = pd.DataFrame(trades, columns=[
        "block_number", "token_a", "token_b", "usd_amount",
        "trader_address", "bribe", "priority_fee", "side", "swap_timestamp"])
    return signals_df, trades_df
