from __future__ import annotations

import pandas as pd

# Порядок колонок выходных фреймов — это контракт, на него садится UI.
_SUMMARY_COLS = [
    "request_id", "signal_timestamp", "base_token", "quote_token",
    "token_a", "token_b", "signal_amount", "signal_bribe", "signal_fee",
    "found_block", "n_hops", "swap_timestamp", "swap_amount", "swap_user_id",
    "swap_bribe", "swap_fee", "covering_volume", "n_trades", "covered",
]
_MATCHES_COLS = [
    "request_id", "hop_index", "n_hops", "signal_timestamp", "token_a", "token_b",
    "signal_amount", "signal_bribe", "signal_fee", "found_block",
    "swap_block", "swap_timestamp", "swap_amount", "swap_user_id", "swap_bribe",
    "swap_fee", "pair_key", "trade_id",
]


_SIG_KEY_COLS = ["request_id", "hop_index", "n_hops", "pair_key",
                 "token_in", "token_out", "fee_rate", "found_block"]


def _pair_keys(a: pd.Series, b: pd.Series) -> pd.Series:
    """Канонический (не зависящий от порядка) ключ пары: ``"minaddr|maxaddr"``.

    Векторно: min/max двух строковых колонок через ``Series.where`` + конкат.
    Вход считаем уже приведённым к нижнему регистру.
    """
    lo = a.where(a <= b, b)
    hi = b.where(a <= b, a)
    return lo + "|" + hi


def _explode_route(signals_df: pd.DataFrame) -> pd.DataFrame:
    """``route`` → по строке на хоп: ключи матчинга + fee_rate хопа.

    Возвращает ``[request_id, hop_index, n_hops, pair_key, token_in, token_out,
    fee_rate, found_block]``. Сигнал-уровневые поля (ts, объём, bribe) не тащим —
    их подмешиваем позже по ``request_id``.

    Векторно: ``explode`` маршрута + доступ к полям хопа через ``.str["key"]``.
    Пустой/``None`` route после ``explode`` даёт ``NaN`` — отбрасываем (``.notna()``),
    т.е. такой сигнал просто не даёт хопов (как старое ``route or []``).
    """
    ex = signals_df[["request_id", "found_block", "route"]].reset_index(drop=True)
    ex = ex.explode("route", ignore_index=False)
    ex = ex[ex["route"].notna()]
    if ex.empty:
        return pd.DataFrame(columns=_SIG_KEY_COLS)

    ti = ex["route"].str["token_in_address"].str.lower()
    to = ex["route"].str["token_out_address"].str.lower()
    out = pd.DataFrame({
        "request_id": ex["request_id"].values,
        "hop_index": ex.groupby(level=0).cumcount().values,
        "pair_key": _pair_keys(ti, to).values,
        "token_in": ti.values,
        "token_out": to.values,
        "fee_rate": ex["route"].str["fee_rate"].values,
        "found_block": ex["found_block"].values,
    })
    # n_hops = число хопов сигнала (после отбрасывания пустых) — размер группы.
    out["n_hops"] = out.groupby("request_id")["hop_index"].transform("size")
    return out[_SIG_KEY_COLS]


def signal_pair_blocks(signals_df: pd.DataFrame) -> list[tuple[str, str, int]]:
    """Точный набор ``(token_lo, token_hi, block)`` для пушдауна в ClickHouse.

    Именно это отдаётся в ``get_trades`` — БД фильтрует свопы по
    ``(least(token_a,token_b), greatest(token_a,token_b), block_number) IN {...}``
    и возвращает только настоящих кандидатов (не декартово произведение блоки×токены).
    """
    sk = _explode_route(signals_df)
    if sk.empty:
        return []
    lo = sk["pair_key"].str.split("|").str[0]
    hi = sk["pair_key"].str.split("|").str[1]
    uniq = pd.DataFrame({"lo": lo, "hi": hi, "block": sk["found_block"]}).drop_duplicates()
    return list(uniq.itertuples(index=False, name=None))


def build_matches(signals_df: pd.DataFrame, trades_df: pd.DataFrame,
                  block_window: int = 0):
    """Сопоставить сигналы и трейды → ``(signal_summary_df, matches_df)``.

    Матчинг: hash-join по ``pair_key`` + оконный фильтр по блоку
    ``|block_number - found_block| <= block_window`` (``block_window`` приходит с
    фронтенда; 0 = точное совпадение блока). Дедуп трейда в пределах сигнала,
    covering_volume по максимальной ноге (без задвоения мультихопа), представитель
    = трейд с максимальным ``swap_amount``.

    Покрытие — по ФАКТУ наличия: сигнал покрыт, если по его паре есть хотя бы одна
    сделка в окне ``±block_window`` (``covered = n_trades > 0``), объём не важен.
    """
    sig_keys = _explode_route(signals_df)

    # Нормализация трейдов: lower адресов, канонический pair_key, устойчивый trade_id.
    t = trades_df.reset_index(drop=True).copy()
    t["token_a"] = t["token_a"].str.lower()
    t["token_b"] = t["token_b"].str.lower()
    t["trader_address"] = t["trader_address"].str.lower()
    t["pair_key"] = _pair_keys(t["token_a"], t["token_b"])
    t["trade_id"] = t.index

    # Hash-join по паре (трейды уже сужены БД до нужных пар/диапазона блоков),
    # затем оконный фильтр по расстоянию блока — так ловим сделки в ±block_window,
    # а не только в точном found_block.
    cand = sig_keys.merge(t, on="pair_key", how="inner")
    if not cand.empty:
        cand = cand[(cand["block_number"] - cand["found_block"]).abs() <= block_window]
    # Один трейд учитывается сигналу один раз (страховка от мультихоп-совпадений пары
    # и от нескольких блоков окна, попавших под один и тот же трейд).
    cand = cand.drop_duplicates(["request_id", "trade_id"])

    # Сигнал-уровневые поля подмешиваем по request_id (без merge → без коллизий имён).
    si = signals_df.set_index("request_id")
    s_ts, s_amt, s_bribe = si["ts"], si["quote_amount"], si["bribe"]

    matches = pd.DataFrame({
        "request_id": cand["request_id"].values,
        "hop_index": cand["hop_index"].values,
        "n_hops": cand["n_hops"].values,
        "signal_timestamp": cand["request_id"].map(s_ts).values,
        "token_a": cand["token_in"].values,
        "token_b": cand["token_out"].values,
        "signal_amount": cand["request_id"].map(s_amt).values,
        "signal_bribe": cand["request_id"].map(s_bribe).values,
        "signal_fee": cand["fee_rate"].values,
        "found_block": cand["found_block"].values,
        "swap_block": cand["block_number"].values,
        "swap_timestamp": cand["swap_timestamp"].values,
        "swap_amount": cand["usd_amount"].values,
        "swap_user_id": cand["trader_address"].values,
        "swap_bribe": cand["bribe"].values,
        "swap_fee": cand["priority_fee"].values,
        "pair_key": cand["pair_key"].values,
        "trade_id": cand["trade_id"].values,
    }, columns=_MATCHES_COLS)

    # --- агрегаты по сигналу ---
    # covering_volume: сумма по хопу, затем МАКСИМУМ по хопам (мультихоп не задваиваем).
    hop_vol = matches.groupby(["request_id", "hop_index"])["swap_amount"].sum()
    covering_volume = hop_vol.groupby("request_id").max()
    n_trades = matches.groupby("request_id")["trade_id"].nunique()
    # представитель = трейд с максимальным объёмом.
    if not matches.empty:
        rep = matches.loc[matches.groupby("request_id")["swap_amount"].idxmax()]
        rep = rep.set_index("request_id")
    else:
        rep = matches.set_index("request_id")

    # --- signal_summary_df: ВСЕ сигналы (left-join агрегатов) ---
    summary = pd.DataFrame({
        "request_id": signals_df["request_id"].values,
        "signal_timestamp": signals_df["ts"].values,
        "base_token": signals_df["base_token"].str.lower().values,
        "quote_token": signals_df["quote_token"].str.lower().values,
        "signal_amount": signals_df["quote_amount"].values,
        "signal_bribe": signals_df["bribe"].values,
        "found_block": signals_df["found_block"].values,
        "n_hops": signals_df["route"].apply(lambda r: len(r) if r else 0).values,
    }).set_index("request_id")

    # Поля представителя (NaN, если трейдов у сигнала нет).
    for col in ("token_a", "token_b", "signal_fee", "swap_timestamp",
                "swap_amount", "swap_user_id", "swap_bribe", "swap_fee"):
        summary[col] = rep[col] if col in rep else pd.NA

    summary["covering_volume"] = covering_volume
    summary["covering_volume"] = summary["covering_volume"].fillna(0.0)
    summary["n_trades"] = n_trades
    summary["n_trades"] = summary["n_trades"].fillna(0).astype(int)
    # Покрытие по факту наличия сделки в окне ±block_window (объём — справочно).
    summary["covered"] = summary["n_trades"] > 0

    summary = summary.reset_index()[_SUMMARY_COLS]
    return summary, matches


def fetch_and_match(get_signals, get_trades, limit: int,
                    block_window: int = 0, **kwargs):
    """Оркестрация гибрида: тянем сигналы из Postgres, узкий запрос трейдов в CH, матчим.

    ``get_signals`` / ``get_trades`` — функции коллег (передаём их как аргументы,
    чтобы модуль оставался чистым и тестируемым). ``limit`` — сколько сигналов
    тянуть; ``block_window`` — окно покрытия ±N блоков (ввод с фронтенда). Его же
    отдаём в ``get_trades`` (``window_size``), чтобы БД вернула трейды на всю
    ширину окна, а не только вокруг точного блока.
    """
    signals_df = get_signals(limit, **kwargs)
    trades_df = get_trades(signal_pair_blocks(signals_df), window_size=block_window)
    return build_matches(signals_df, trades_df, block_window=block_window)


# --------------------------------------------------------------------------- #
# Self-test на маленьких фикстурах (python -m data.matching).
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import datetime as _dt

    UNI = "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984"
    WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
    USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
    t0 = _dt.datetime(2026, 5, 18, 12, 0, 0)

    def _hop(a, b, fee=0.003):
        return {"fee_rate": fee, "protocol": {"id": 0, "version": 1},
                "decimals_in": 18, "decimals_out": 18,
                "token_in_address": a, "token_out_address": b}

    signals = pd.DataFrame([
        # 1: single-hop, есть сделки → покрыт (covering_volume 600+500=1100)
        dict(request_id=1, ts=t0, base_token=WETH, quote_token=UNI, quote_amount=1000.0,
             bribe=1.0, found_block=100, route=[_hop(UNI, WETH)]),
        # 2: single-hop, сделка есть, но объёма мало (400) → всё равно ПОКРЫТ (по факту)
        dict(request_id=2, ts=t0, base_token=WETH, quote_token=UNI, quote_amount=1000.0,
             bribe=1.0, found_block=201, route=[_hop(UNI, WETH)]),
        # 3: multi-hop, ноги по ~1000; covering = max(1000,1000)=1000 (НЕ 2000)
        dict(request_id=3, ts=t0, base_token=USDT, quote_token=UNI, quote_amount=1000.0,
             bribe=1.0, found_block=302, route=[_hop(UNI, WETH), _hop(WETH, USDT, 0.0005)]),
        # 4: без трейдов → не покрыт
        dict(request_id=4, ts=t0, base_token=WETH, quote_token=UNI, quote_amount=1000.0,
             bribe=1.0, found_block=403, route=[_hop(UNI, WETH)]),
        # 5: пустой route — не даёт хопов, не матчится (сторож для explode + notna)
        dict(request_id=5, ts=t0, base_token=WETH, quote_token=UNI, quote_amount=1000.0,
             bribe=1.0, found_block=504, route=[]),
    ])

    trades = pd.DataFrame([
        # сигнал 1 (found_block 100), пара в перевёрнутом порядке/регистре — проверка pair_key
        dict(block_number=100, token_a=WETH.lower(), token_b=UNI.lower(), usd_amount=600.0,
             trader_address="0xShark1", bribe="1000000000", priority_fee="500", swap_timestamp=t0),
        dict(block_number=100, token_a=UNI, token_b=WETH, usd_amount=500.0,
             trader_address="0xShark1", bribe="2000000000", priority_fee="600", swap_timestamp=t0),
        # сигнал 2 (found_block 201): сделка в СОСЕДНЕМ блоке 202 — ловится только окном ±N
        dict(block_number=202, token_a=UNI, token_b=WETH, usd_amount=400.0,
             trader_address="0xShark2", bribe="1", priority_fee="1", swap_timestamp=t0),
        # сигнал 3 (found_block 302) — обе ноги, точный блок
        dict(block_number=302, token_a=UNI, token_b=WETH, usd_amount=1000.0,
             trader_address="0xShark3", bribe="1", priority_fee="1", swap_timestamp=t0),
        dict(block_number=302, token_a=WETH, token_b=USDT, usd_amount=1000.0,
             trader_address="0xShark3", bribe="1", priority_fee="1", swap_timestamp=t0),
        # шум: правильная пара, но блок далеко за окном — не должен матчиться
        dict(block_number=999, token_a=UNI, token_b=WETH, usd_amount=9999.0,
             trader_address="0xNoise", bribe="1", priority_fee="1", swap_timestamp=t0),
    ])

    # Окно ±2: покрывает точные блоки (1,3) и соседний блок сигнала 2 (202 vs 201).
    summary, matches = build_matches(signals, trades, block_window=2)

    # контракт колонок
    assert list(summary.columns) == _SUMMARY_COLS, summary.columns.tolist()
    assert list(matches.columns) == _MATCHES_COLS, matches.columns.tolist()
    # 5 сигналов в summary, 5 матчей (2+1+2), шум не попал
    assert len(summary) == 5, len(summary)
    assert len(matches) == 5, len(matches)
    assert 4 not in set(matches["request_id"]), "непокрытый сигнал не должен быть в matches"
    assert 5 not in set(matches["request_id"]), "пустой route не должен давать матчей"

    s = summary.set_index("request_id")
    # covered = по факту наличия сделки в окне (объём не важен)
    assert bool(s.loc[1, "covered"]) is True
    assert bool(s.loc[2, "covered"]) is True   # 400 < 1000, но сделка есть → покрыт
    assert bool(s.loc[3, "covered"]) is True
    assert bool(s.loc[4, "covered"]) is False
    # мультихоп не задваивается
    assert s.loc[3, "covering_volume"] == 1000.0, s.loc[3, "covering_volume"]
    # single-hop = сумма сплитов
    assert s.loc[1, "covering_volume"] == 1100.0, s.loc[1, "covering_volume"]
    # представитель = максимальный трейд; в matches виден фактический блок сделки
    assert s.loc[1, "swap_amount"] == 600.0, s.loc[1, "swap_amount"]
    assert int(matches.set_index("request_id").loc[2, "swap_block"]) == 202
    # непокрытый: swap_* пуст, счётчики по нулям
    assert pd.isna(s.loc[4, "swap_amount"])
    assert s.loc[4, "covering_volume"] == 0.0
    assert int(s.loc[4, "n_trades"]) == 0
    # пустой route: в summary есть, n_hops=0, не покрыт, swap пуст
    assert bool(s.loc[5, "covered"]) is False
    assert int(s.loc[5, "n_hops"]) == 0
    assert pd.isna(s.loc[5, "swap_amount"])
    # адреса приведены к нижнему регистру
    assert s.loc[1, "swap_user_id"] == "0xshark1"
    # dtypes для сортировки
    assert pd.api.types.is_float_dtype(summary["signal_amount"])
    assert pd.api.types.is_integer_dtype(summary["found_block"])
    assert pd.api.types.is_datetime64_any_dtype(summary["signal_timestamp"])

    # --- граница окна: тот же набор сигналов/трейдов при block_window=0 ---------
    # Сигнал 2 (сделка в блоке 202, found_block 201) при точном матче НЕ покрыт,
    # а шум/дальние блоки как и раньше отсекаются.
    summary0 = build_matches(signals, trades, block_window=0)[0].set_index("request_id")
    assert bool(summary0.loc[1, "covered"]) is True    # точный блок 100
    assert bool(summary0.loc[2, "covered"]) is False   # 202 вне ±0 от 201
    assert bool(summary0.loc[3, "covered"]) is True    # точный блок 302

    print("OK: все проверки пройдены")
    print(summary.to_string(index=False))
