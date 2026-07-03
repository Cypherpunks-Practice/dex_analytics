from __future__ import annotations

from collections import defaultdict, deque

import numpy as np
import pandas as pd
from . import queries

# Порядок колонок выходных фреймов — это контракт, на него садится UI.
# swap_route — печатный фактический маршрут покрывающей сделки ("tokА → … → tokZ").
_SUMMARY_COLS = [
    "request_id", "signal_timestamp", "base_token", "quote_token",
    "token_a", "token_b", "signal_amount", "signal_bribe", "signal_fee",
    "found_block", "n_hops", "swap_timestamp", "swap_amount", "swap_user_id",
    "swap_bribe", "swap_fee", "covering_volume", "n_trades", "swap_route", "covered",
    "route", "profit", "swap_block",
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


def _signal_endpoints(signals_df: pd.DataFrame) -> pd.DataFrame:
    """``request_id`` → концы маршрута сигнала (нижний регистр) + fee первого хопа.

    ``start`` = ``token_in`` первого хопа, ``end`` = ``token_out`` последнего.
    Это ЦЕЛЕВАЯ пара сигнала (начальный↔конечный токен), к которой сводится
    покрытие. Пустой ``route`` → ``start=end=None`` (такой сигнал не покрывается).
    Возвращает ``[request_id, start, end, n_hops, first_fee]``.
    """
    rows = []
    for rid, route in zip(signals_df["request_id"].values, signals_df["route"].values):
        if not route:
            rows.append((rid, None, None, 0, np.nan))
            continue
        start = str(route[0]["token_in_address"]).lower()
        end = str(route[-1]["token_out_address"]).lower()
        rows.append((rid, start, end, len(route), route[0].get("fee_rate", np.nan)))
    return pd.DataFrame(rows, columns=["request_id", "start", "end", "n_hops", "first_fee"])


def _best_route(ta, tb, traders, usd, tid, i0, i1, start, end):
    """Лучший покрывающий маршрут в срезе сделок ``[i0, i1)`` или ``None``.

    Сделки среза группируем по игроку и в НЕОРИЕНТИРОВАННОМ графе токенов одного
    игрока ищем BFS путь ``start → end``. Длина 1 = прямая сделка в целевой паре
    (любой игрок); длина ≥2 = цепочка сделок ОДНОГО игрока (как route сигнала или
    отличная). Лучший = кратчайший по числу сделок, при равенстве — максимальный
    суммарный объём. get_trades не отдаёт направление свопа → граф неориентированный.
    """
    by_player = defaultdict(list)
    for pos in range(i0, i1):
        by_player[traders[pos]].append(pos)

    best_key = None
    best_rec = None
    for player, positions in by_player.items():
        adj = defaultdict(list)
        for pos in positions:
            adj[ta[pos]].append((tb[pos], pos))
            adj[tb[pos]].append((ta[pos], pos))
        if start not in adj:
            continue
        # BFS: token → (предыдущий token, позиция ребра-сделки).
        prev = {start: (None, None)}
        q = deque([start])
        while q:
            cur = q.popleft()
            if cur == end:
                break
            for nb, pos in adj[cur]:
                if nb not in prev:
                    prev[nb] = (cur, pos)
                    q.append(nb)
        if end not in prev:
            continue
        # Реконструкция пути от end к start.
        path_tokens, edge_pos, node = [], [], end
        while node is not None:
            path_tokens.append(node)
            pnode, pos = prev[node]
            if pos is not None:
                edge_pos.append(pos)
            node = pnode
        path_tokens.reverse()
        edge_pos.reverse()
        vol = float(sum(usd[p] for p in edge_pos))
        key = (len(edge_pos), -vol)
        if best_key is None or key < best_key:
            rep_pos = max(edge_pos, key=lambda p: usd[p])
            best_key = key
            best_rec = {
                "covered": True,
                "swap_route": " → ".join(path_tokens),
                "swap_user_id": player,
                "route_volume": vol,
                "route_n_trades": len(edge_pos),
                "rep_trade_id": tid[rep_pos],
            }
    return best_rec


_COV_COLS = ["covered", "swap_route", "swap_user_id",
             "route_volume", "route_n_trades", "rep_trade_id"]


def _covering_routes(signals_df: pd.DataFrame, t: pd.DataFrame,
                     block_window: int) -> pd.DataFrame:
    """Для каждого сигнала — покрывающий маршрут в окне ``±block_window``.

    Возвращает DataFrame (index=``request_id``) с колонками ``_COV_COLS`` только по
    ПОКРЫТЫМ сигналам (остальные подмешиваются как not-covered в ``build_matches``).
    Кандидаты сигнала = сделки с ``|block - found_block| <= block_window`` (быстрый
    срез окна через ``searchsorted`` по отсортированному ``block_number``).
    """
    ep = _signal_endpoints(signals_df)
    fb = signals_df.set_index("request_id")["found_block"]
    empty = pd.DataFrame(columns=_COV_COLS, index=pd.Index([], name="request_id"))
    if t.empty:
        return empty

    ts = t.sort_values("block_number").reset_index(drop=True)
    blocks = ts["block_number"].to_numpy()
    ta = ts["token_a"].to_numpy()
    tb = ts["token_b"].to_numpy()
    traders = ts["trader_address"].to_numpy()
    usd = pd.to_numeric(ts["usd_amount"], errors="coerce").fillna(0.0).to_numpy()
    tid = ts["trade_id"].to_numpy()

    records = {}
    for rid, start, end in zip(ep["request_id"].values,
                               ep["start"].values, ep["end"].values):
        if start is None or end is None or start == end:
            continue
        b = int(fb.get(rid))
        i0 = int(np.searchsorted(blocks, b - block_window, side="left"))
        i1 = int(np.searchsorted(blocks, b + block_window, side="right"))
        if i1 <= i0:
            continue
        rec = _best_route(ta, tb, traders, usd, tid, i0, i1, start, end)
        if rec is not None:
            records[rid] = rec

    if not records:
        return empty
    out = pd.DataFrame.from_dict(records, orient="index")[_COV_COLS]
    out.index.name = "request_id"
    return out


def signal_pair_blocks(signals_df: pd.DataFrame) -> list[tuple[str, str, int]]:
    """Точный набор ``(token_lo, token_hi, block)`` для пушдауна в ClickHouse.

    Именно это отдаётся в ``get_trades`` — БД фильтрует свопы по
    ``(least(token_a,token_b), greatest(token_a,token_b), block_number) IN {...}``
    и возвращает только настоящих кандидатов (не декартово произведение блоки×токены).
    Помимо пар хопов эмитим ЦЕЛЕВУЮ пару ``(start, end)`` каждого сигнала — иначе
    прямые сделки целевой пары для multi-hop вообще не попали бы в выборку.
    """
    parts = []
    sk = _explode_route(signals_df)
    if not sk.empty:
        lo = sk["pair_key"].str.split("|").str[0]
        hi = sk["pair_key"].str.split("|").str[1]
        parts.append(pd.DataFrame({"lo": lo.values, "hi": hi.values,
                                   "block": sk["found_block"].values}))

    ep = _signal_endpoints(signals_df)
    ep = ep[ep["start"].notna() & ep["end"].notna() & (ep["start"] != ep["end"])]
    if not ep.empty:
        fb = signals_df.set_index("request_id")["found_block"]
        tlo = ep["start"].where(ep["start"] <= ep["end"], ep["end"])
        thi = ep["end"].where(ep["start"] <= ep["end"], ep["start"])
        parts.append(pd.DataFrame({"lo": tlo.values, "hi": thi.values,
                                   "block": ep["request_id"].map(fb).values}))

    if not parts:
        return []
    uniq = pd.concat(parts, ignore_index=True).drop_duplicates()
    return list(uniq.itertuples(index=False, name=None))


def build_matches(signals_df: pd.DataFrame, trades_df: pd.DataFrame,
                  block_window: int = 0):
    """Сопоставить сигналы и трейды → ``(signal_summary_df, matches_df)``.

    ``matches_df`` — сырые сделки, совпавшие по паре хопа сигнала в окне
    ``|block_number - found_block| <= block_window`` (hash-join по ``pair_key`` +
    оконный фильтр); справочный слой.

    Покрытие (``covered`` + печатный ``swap_route``) считает ``_covering_routes``:
    сигнал покрыт, если в окне ``±block_window`` есть путь ``start → end`` (целевая
    пара) — прямая сделка в целевой паре ЛИБО цепочка сделок ОДНОГО игрока. Объём
    на покрытие не влияет; ``covering_volume``/``n_trades`` описывают найденный
    маршрут (сумма объёмов и число сделок пути), представитель ``swap_*`` — макс-
    объёмная сделка этого маршрута.
    """
    sig_keys = _explode_route(signals_df)    

    tokens_dict = queries.get_tokens_dict()
    def _route_to_str(r):
        if not r:
            return ""
        pairs = []
        for h in r:
                addr_in = h.get("token_in_address", "").lower()
                addr_out = h.get("token_out_address", "").lower()
                symbol_in = tokens_dict.get(addr_in, addr_in[:10] + "...")
                symbol_out = tokens_dict.get(addr_out, addr_out[:10] + "...")
                pairs.append(f"{symbol_in} -> {symbol_out}")
        return " | ".join(pairs)   

    # Нормализация трейдов: lower адресов, канонический pair_key, устойчивый trade_id.
    # lower() важен: боевой get_trades уже отдаёт нижний регистр (безвредно), но так
    # матчинг устойчив к смешанному регистру и совпадает с ключами сигнала.
    t = trades_df.reset_index(drop=True).copy()
    t["token_a"] = t["token_a"].str.lower()
    t["token_b"] = t["token_b"].str.lower()
    t["trader_address"] = t["trader_address"].str.lower()
    t["pair_key"] = _pair_keys(t["token_a"], t["token_b"])
    t["trade_id"] = t.index

    # Сигнал-уровневые поля подмешиваем по request_id (без merge → без коллизий имён).
    si = signals_df.set_index("request_id")
    s_ts, s_amt, s_bribe = si["ts"], si["quote_amount"], si["bribe"]
    # token_a/token_b в выдаче = ЦЕЛЕВАЯ пара сигнала (base/quote), по имени, не по позиции.
    s_token_a = si["base_token"].str.lower()
    s_token_b = si["quote_token"].str.lower()

    #-----------
    # Разворачиваем трейды по окну блоков до слияния, чтобы избежать декартова взрыва
    t_win_parts = []
    for off in range(-block_window, block_window + 1):
        part = t.copy()
        part["cover_block"] = part["block_number"] + off
        t_win_parts.append(part)
    
    if t_win_parts:
        t_win = pd.concat(t_win_parts, ignore_index=True)
    else:
        t_win = pd.DataFrame(columns=t.columns.tolist() + ["cover_block"])

    # Equi-join по ключу пары и целевому блоку (без пост-фильтрации)
    cand = sig_keys.merge(
        t_win, 
        left_on=["pair_key", "found_block"], 
        right_on=["pair_key", "cover_block"], 
        how="inner"
    )
    cand = cand.drop_duplicates(["request_id", "trade_id"])
    #------------------------------------

    matches = pd.DataFrame({
        "request_id": cand["request_id"].values,
        "hop_index": cand["hop_index"].values,
        "n_hops": cand["n_hops"].values,
        "signal_timestamp": cand["request_id"].map(s_ts).values,
        "token_a": cand["request_id"].map(s_token_a).values,
        "token_b": cand["request_id"].map(s_token_b).values,
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

    # --- покрытие по маршруту (start→end в окне блоков) ---
    cov = _covering_routes(signals_df, t, block_window)
    ep = _signal_endpoints(signals_df).set_index("request_id")
    t_by_id = t.set_index("trade_id")

    # --- signal_summary_df: ВСЕ сигналы (left-join покрытия) ---
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

    summary["token_a"] = s_token_a
    summary["token_b"] = s_token_b
    # signal_fee — fee первого хопа сигнала (атрибут сигнала, не сделки).
    summary["signal_fee"] = ep["first_fee"]

    # Поля покрытия (для не покрытых → not-covered / NaN / пустая строка).
    # eq(True): NaN (не покрыт) → False, без FutureWarning про downcast на fillna.
    summary["covered"] = cov["covered"].reindex(summary.index).eq(True)
    summary["swap_route"] = cov["swap_route"].reindex(summary.index).fillna("")
    summary["swap_user_id"] = cov["swap_user_id"].reindex(summary.index)
    summary["covering_volume"] = cov["route_volume"].reindex(summary.index).fillna(0.0)
    summary["n_trades"] = cov["route_n_trades"].reindex(summary.index).fillna(0).astype(int)

    # Представитель swap_* — макс-объёмная сделка покрывающего маршрута (по rep_trade_id).
    rep_id = cov["rep_trade_id"].reindex(summary.index)
    summary["swap_amount"] = rep_id.map(t_by_id["usd_amount"])
    summary["swap_bribe"] = rep_id.map(t_by_id["bribe"])
    summary["swap_fee"] = rep_id.map(t_by_id["priority_fee"])
    summary["swap_timestamp"] = rep_id.map(t_by_id["swap_timestamp"])
    # Блок покрывающей сделки (диагностика: сравнить с found_block при ±block_window).
    # Int64 (nullable) → у непокрытых <NA>, без float-хвоста «24791000.0».
    summary["swap_block"] = rep_id.map(t_by_id["block_number"]).astype("Int64")

    # numeric_cols = ["signal_amount", "signal_bribe", "signal_fee",
    #                 "swap_amount", "swap_bribe", "swap_fee", "covering_volume"]
    # summary["covering_volume"] = covering_volume
    # summary["covering_volume"] = summary["covering_volume"].fillna(0.0)
    # summary["n_trades"] = n_trades
    # summary["n_trades"] = summary["n_trades"].fillna(0).astype(int)
    # summary["covered"] = summary["covering_volume"] >= summary["signal_amount"]
    summary["route"] = signals_df["route"].apply(_route_to_str).values
    # profit — атрибут сигнала из Postgres; в стабе/self-test его может не быть.
    summary["profit"] = (signals_df["profit"].values
                         if "profit" in signals_df.columns else np.nan)

    numeric_cols = ["signal_amount", "signal_bribe", "signal_fee", 
                    "swap_amount", "swap_bribe", "swap_fee", "covering_volume", "profit"]
    for col in numeric_cols:
        if col in summary.columns:
            summary[col] = pd.to_numeric(summary[col], errors="coerce")
        if col in matches.columns:
            matches[col] = pd.to_numeric(matches[col], errors="coerce")
    
    summary = summary.reset_index()[_SUMMARY_COLS]

    # print(summary.iloc[0])
    # print(summary.iloc[1])
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
    import sys as _sys

    # swap_route содержит "→"; на Windows-консоли (cp1251) print иначе падает.
    try:
        _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # Различимые токены (по одной hex-цифре) — канонический порядок = алфавитный.
    A, B, C, D, E = ("0x" + ch * 40 for ch in "abcde")
    t0 = _dt.datetime(2026, 5, 18, 12, 0, 0)

    def _hop(a, b, fee=0.003):
        return {"fee_rate": fee, "protocol": {"id": 0, "version": 1},
                "decimals_in": 18, "decimals_out": 18,
                "token_in_address": a, "token_out_address": b}

    def _tr(block, a, b, usd, trader):
        return dict(block_number=block, token_a=a, token_b=b, usd_amount=float(usd),
                    trader_address=trader, bribe="1000000000", priority_fee="500",
                    swap_timestamp=t0)

    # base_token = конечный токен маршрута, quote_token = начальный (как в проде).
    def _sig(rid, fb, route):
        start = route[0]["token_in_address"] if route else A
        end = route[-1]["token_out_address"] if route else A
        return dict(request_id=rid, ts=t0, base_token=end, quote_token=start,
                    quote_amount=1000.0, bribe=1.0, found_block=fb, route=route,
                    profit=10.0)

    # Блоки сценариев разнесены на ≥100 → окна ±2 не пересекаются, токены изолированы.
    signals = pd.DataFrame([
        _sig(1, 100, [_hop(A, B)]),                       # single-hop, покрыт по факту
        _sig(2, 201, [_hop(A, B)]),                       # single-hop, покрыт соседним блоком
        _sig(3, 302, [_hop(A, B), _hop(B, C)]),           # multi-hop, прямая целевая пара {A,C}
        _sig(4, 402, [_hop(A, B), _hop(B, D)]),           # multi-hop, цепочка = route сигнала
        _sig(5, 502, [_hop(A, C), _hop(C, D)]),           # multi-hop, ОТЛИЧНАЯ цепочка A-E-D
        _sig(6, 602, [_hop(A, B), _hop(B, C)]),           # multi-hop, есть лишь одна нога → НЕ покрыт
        _sig(7, 702, [_hop(A, B), _hop(B, C)]),           # multi-hop, ноги у РАЗНЫХ игроков → НЕ покрыт
        _sig(8, 802, []),                                 # пустой route → НЕ покрыт
    ])

    trades = pd.DataFrame([
        # s1: единственная сделка в ВЕРХНЕМ регистре — проверка восстановленного lower()
        _tr(100, A.upper(), B.upper(), 600.0, "0xShark1"),
        _tr(202, A, B, 400.0, "0xShark2"),                # s2: соседний блок (202 vs 201)
        _tr(302, A, C, 1500.0, "0xSharkD"),               # s3: прямая целевая пара {A,C}
        _tr(402, A, B, 700.0, "0xP"),                     # s4: цепочка одного игрока P
        _tr(402, B, D, 800.0, "0xP"),                     #     A-B-D
        _tr(502, A, E, 300.0, "0xQ"),                     # s5: игрок Q идёт через E (не через C)
        _tr(502, E, D, 400.0, "0xQ"),                     #     A-E-D ≠ route сигнала A-C-D
        _tr(602, A, B, 999.0, "0xR"),                     # s6: только одна нога {A,B}
        _tr(702, A, B, 100.0, "0xP1"),                    # s7: {A,B} у P1
        _tr(702, B, C, 100.0, "0xP2"),                    # s7: {B,C} у P2 (другой игрок)
        _tr(999, A, B, 9999.0, "0xNoise"),                # шум: блок далеко за окном
    ])

    summary, matches = build_matches(signals, trades, block_window=2)
    s = summary.set_index("request_id")

    # контракт колонок и размеры
    assert list(summary.columns) == _SUMMARY_COLS, summary.columns.tolist()
    assert list(matches.columns) == _MATCHES_COLS, matches.columns.tolist()
    assert len(summary) == 8, len(summary)

    # --- покрытие ---
    assert bool(s.loc[1, "covered"]) is True   # single-hop, сделка есть (uppercase → lower)
    assert bool(s.loc[2, "covered"]) is True   # single-hop, соседний блок в окне
    assert bool(s.loc[3, "covered"]) is True   # multi-hop, прямая целевая пара {A,C}
    assert bool(s.loc[4, "covered"]) is True   # multi-hop, цепочка A-B-D одного игрока
    assert bool(s.loc[5, "covered"]) is True   # multi-hop, отличная цепочка A-E-D
    assert bool(s.loc[6, "covered"]) is False  # только одна нога мультихопа
    assert bool(s.loc[7, "covered"]) is False  # ноги у разных игроков
    assert bool(s.loc[8, "covered"]) is False  # пустой route

    # --- печатный маршрут (фактические токены пути) ---
    assert s.loc[3, "swap_route"] == f"{A} → {C}", s.loc[3, "swap_route"]
    assert s.loc[4, "swap_route"] == f"{A} → {B} → {D}", s.loc[4, "swap_route"]
    assert s.loc[5, "swap_route"] == f"{A} → {E} → {D}", s.loc[5, "swap_route"]
    assert s.loc[6, "swap_route"] == "" and s.loc[8, "swap_route"] == ""

    # --- объём/число сделок = характеристики маршрута (не покрытие) ---
    assert s.loc[1, "covering_volume"] == 600.0 and int(s.loc[1, "n_trades"]) == 1
    assert s.loc[4, "covering_volume"] == 1500.0 and int(s.loc[4, "n_trades"]) == 2
    assert s.loc[5, "covering_volume"] == 700.0 and int(s.loc[5, "n_trades"]) == 2
    assert int(s.loc[6, "n_trades"]) == 0 and s.loc[6, "covering_volume"] == 0.0

    # --- представитель = макс-объёмная сделка маршрута; игрок в нижнем регистре ---
    assert s.loc[4, "swap_amount"] == 800.0 and s.loc[4, "swap_user_id"] == "0xp"
    assert s.loc[5, "swap_amount"] == 400.0 and s.loc[5, "swap_user_id"] == "0xq"
    assert s.loc[1, "swap_user_id"] == "0xshark1"
    assert pd.isna(s.loc[6, "swap_amount"]) and pd.isna(s.loc[6, "swap_user_id"])

    # --- signal_fee = fee первого хопа; пустой route → NaN ---
    assert s.loc[1, "signal_fee"] == 0.003
    assert pd.isna(s.loc[8, "signal_fee"])

    # --- matches_df — справочный слой хоп-пар, развязан с покрытием ---
    m_ids = set(matches["request_id"])
    assert 8 not in m_ids                       # пустой route не даёт хоп-матчей
    assert 3 not in m_ids                       # покрыт целевой парой {A,C} — не хоп-пара
    assert {1, 2, 4, 6, 7} <= m_ids             # сделки на хоп-парах (в т.ч. непокрытых 6,7)

    # dtypes для сортировки на фронте
    assert pd.api.types.is_float_dtype(summary["signal_amount"])
    assert pd.api.types.is_integer_dtype(summary["found_block"])
    assert pd.api.types.is_datetime64_any_dtype(summary["signal_timestamp"])

    # --- граница окна: block_window=0 → сосед s2 (202 vs 201) уже НЕ покрыт ---
    s0 = build_matches(signals, trades, block_window=0)[0].set_index("request_id")
    assert bool(s0.loc[1, "covered"]) is True   # точный блок 100
    assert bool(s0.loc[2, "covered"]) is False  # 202 вне ±0 от 201
    assert bool(s0.loc[4, "covered"]) is True   # цепочка в точном блоке 402

    print("OK: все проверки пройдены")
    print(summary.to_string(index=False))
