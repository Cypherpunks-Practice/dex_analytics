"""Слой доступа к данным — стабильный API дашборда.

UI вызывает только эти функции и ничего не знает про источник данных. Запросы
идут к ClickHouse через `data.clickhouse.execute(...)` и опираются на
агрегирующее представление `mv_dex_analytics_data` (AggregatingMergeTree):
оно хранит по (минута, пул, игрок) агрегатные состояния, из которых -Merge-ом
собираются все метрики анализа рынка. Читаемые подписи пар токенов добавляет
вспомогательное представление `dim_pool_pair` (создаётся в `data.clickhouse`),
подписи игроков — таблица `traders`.

`data.clickhouse.USE_STUB = True` переключает все функции обратно на заглушки
`data/stubs.py` (офлайн-разработка) — сигнатуры и формы возврата при этом те же.

Соглашение о `filters` — dict:
    {
        "players": list[str],   # адреса акул/китов; пусто = все из таблицы traders
        "pools":   list[str],   # адреса пулов; пусто = весь рынок
        "time_range": str,      # ключ из config.TIME_RANGES
    }

Окна времени считаются от единой точки отсчёта `_now_sql()` — логика «как будто
БД живая»: фоновое автообновление перезапрашивает данные, и при пополнении дампа
окна сами сдвигаются вперёд. Точка отсчёта переключается одной константой
`config.TIME_ANCHOR`: "now" — серверное now() (живая БД), "data" —
max(minute_bucket) представления (статический дамп, чтобы короткие окна
`today`/`last_hour`/`yesterday` не были пустыми на метках из прошлого).

--- SQL, которым в БД создано представление (для справки) ---------------------

    -- номер блока -> время utc (аппроксимация)
    toDateTime(1775121779 + (block_number - 24791000) * 12.0376)

    CREATE TABLE IF NOT EXISTS mv_dex_analytics_data (
        minute_bucket DateTime,
        pool_address String,
        trader_address String,
        trades_count   AggregateFunction(count, UInt64),
        total_volume   AggregateFunction(sum, Float64),
        total_bribe    AggregateFunction(sum, Float64),
        min_volume     AggregateFunction(min, Float64),
        max_volume     AggregateFunction(max, Float64),
        median_volume  AggregateFunction(quantiles(0.5), Float64)
    ) ENGINE = AggregatingMergeTree()
    ORDER BY (minute_bucket, pool_address, trader_address);

    CREATE MATERIALIZED VIEW IF NOT EXISTS mv_dex_analytics TO mv_dex_analytics_data
    AS SELECT
        toStartOfMinute(toDateTime(1775121779 + ((t.block_number - 24791000) * 12.0376))) AS minute_bucket,
        s.pool_address AS pool_address,
        t.trader_address AS trader_address,
        countState() AS trades_count,
        sumState(toFloat64(ifNull(s.usd_amount, 0))) AS total_volume,
        sumState(toFloat64(ifNull(t.bribe, 0))) AS total_bribe,
        minState(toFloat64(ifNull(s.usd_amount, 0))) AS min_volume,
        maxState(toFloat64(ifNull(s.usd_amount, 0))) AS max_volume,
        quantilesState(0.5)(toFloat64(ifNull(s.usd_amount, 0))) AS median_volume
    FROM swaps AS s
    INNER JOIN transactions AS t ON s.transaction_hash_id = t.hash_id
    GROUP BY minute_bucket, pool_address, trader_address;
"""

from __future__ import annotations

import pandas as pd

import config
from data import clickhouse
from data import stubs

# --- Выражения метрик над merged-состояниями представления ------------------
# Метрики анализа рынка (ключи config.MARKET_METRICS).
_MARKET_EXPR = {
    "trade_volume": "sumMerge(mv.total_volume)",
    "bribe_volume": "sumMerge(mv.total_bribe)",
    "avg_size": "sumMerge(mv.total_volume) / nullIf(countMerge(mv.trades_count), 0)",
    "median_size": "quantilesMerge(0.5)(mv.median_volume)[1]",
    "max_size": "maxMerge(mv.max_volume)",
    "min_size": "minMerge(mv.min_volume)",
    "trade_count": "countMerge(mv.trades_count)",
}

# Тренд-метрики графиков (config.TREND_METRICS): size = средний размер сделки.
_TREND_EXPR = {
    "volume": "sumMerge(mv.total_volume)",
    "bribe": "sumMerge(mv.total_bribe)",
    "size": "sumMerge(mv.total_volume) / nullIf(countMerge(mv.trades_count), 0)",
}

_MV = "mv_dex_analytics_data AS mv"
_DIM = "LEFT JOIN dim_pool_pair AS d ON mv.pool_address = d.pool_address"
_TRD = "LEFT JOIN traders AS tr ON mv.trader_address = tr.contract_address"

# Подпись пула, уникальная на адрес: «WETH/USDC (0x12ab34cd…)».
_POOL_LABEL = ("concat(coalesce(d.pair, 'unknown'), ' (', "
               "substring(mv.pool_address, 1, 8), '…)')")
# То же, но с ПОЛНЫМ адресом без обрезки — для таблицы Топ-50.
_POOL_LABEL_FULL = "concat(coalesce(d.pair, 'unknown'), ' (', mv.pool_address, ')')"
# Подпись игрока, уникальная на адрес: «whale (0x1234ab…)». Большинство адресов
# в traders помечены как «unknown», поэтому к ярлыку добавляем префикс адреса —
# иначе тысячи игроков схлопнулись бы в одну серию.
_SHARK_LABEL = ("concat(coalesce(tr.label, 'addr'), ' (', "
                "substring(mv.trader_address, 1, 8), '…)')")
# То же, но с ПОЛНЫМ адресом без обрезки — для таблицы Топ-50 игроков.
_SHARK_LABEL_FULL = "concat(coalesce(tr.label, 'addr'), ' (', mv.trader_address, ')')"


# --- Хелперы построения SQL -------------------------------------------------
def _now_sql() -> str:
    """SQL-выражение «текущего момента» — единая точка отсчёта всех окон времени.

    Режим задаётся config.TIME_ANCHOR:
      "now"  → серверное now() (живая БД);
      "data" → max(minute_bucket) представления (статический дамп).
    Везде ниже вместо now() подставляется это выражение, поэтому переключение
    режима — смена одной константы в config.
    """
    if config.TIME_ANCHOR == "data":
        return "(SELECT max(minute_bucket) FROM mv_dex_analytics_data)"
    return "now()"


def _time_where(time_range: str, col: str = "mv.minute_bucket") -> str:
    """Условие окна времени относительно _now_sql(). Ключи — из config.TIME_RANGES
    (белый список, не пользовательский ввод), поэтому инлайнятся безопасно."""
    now = _now_sql()
    if time_range == "last_hour":
        return f"{col} >= {now} - INTERVAL 1 HOUR"
    if time_range == "today":
        return f"{col} >= toStartOfDay({now})"
    if time_range == "yesterday":
        return (f"{col} >= toStartOfDay({now}) - INTERVAL 1 DAY "
                f"AND {col} < toStartOfDay({now})")
    if time_range == "week":
        return f"{col} >= {now} - INTERVAL 7 DAY"
    if time_range == "month":
        return f"{col} >= {now} - INTERVAL 30 DAY"
    return ""  # all — без ограничения по времени


def _reference_where(reference: str, col: str = "mv.minute_bucket") -> str:
    """Окно reference-дня для анализа тренда — сопоставимый день в прошлом."""
    offset = {"yesterday": 1, "week": 7, "month": 30}.get(reference, 1)
    now = _now_sql()
    return (f"{col} >= toStartOfDay({now}) - INTERVAL {offset} DAY "
            f"AND {col} < toStartOfDay({now}) - INTERVAL {offset - 1} DAY")


def _scope(filters: dict, *, time_sql: str | None = None,
           include_clause: str = "membership",
           apply_exclude: bool = True, apply_pools: bool = True):
    """Собрать WHERE по фильтрам игроков/пулов/времени и params для clickhouse-connect.

    Три независимых списка игроков:
      include_players       — поле «Включить», всегда include: «пулы, в которых
                              были эти игроки» (членство пула через подзапрос);
      exclude_pool_players  — поле «Исключить пулы игроков», глобальное вычитание:
                              убрать целиком пулы этих игроков;
      exclude_trade_players — поле «Исключить сделки игроков», глобальное
                              вычитание: убрать только их сделки (пулы остаются).
    Оба списка исключения применяются одновременно и независимо.
    Плюс фильтр пулов (`pools` / `pools_mode`: include = только, exclude = кроме).

    Членство пула (include и exclude_pools) считается в пределах того же окна
    времени, что и основной запрос: подзапрос несёт ту же `tw`-клаузулу, а его
    внутренний alias тоже `mv`, поэтому строка времени переиспользуется.

    Параметры тонкой настройки (по умолчанию — базовый скоуп):
      include_clause:
        "membership" — `pool IN (include-пулы)` если include_players непуст,
                       иначе traders-дефолт (как при пустом фильтре);
        "trades"     — `has(iplayers, trader)` если непуст, иначе traders-дефолт;
                       нужно для «микро» и «ушли/зашли» (именно их сделки);
        "off"        — клаузулу include не добавлять (обогащённые таблицы и
                       «макро по игрокам» считают объём отдельно через -MergeIf).
      apply_exclude=False — не добавлять вычитание обоих списков исключения.
      apply_pools=False   — не добавлять клаузулу пулов (например, чтобы «общий
                            объём игрока» считался по всему рынку).

    Адреса нормализуются в нижний регистр; параметры названы раздельно
    (`iplayers`/`xpoolplayers`/`xtradeplayers`/`pools`), чтобы не конфликтовать
    при совмещении.
    Возвращает (where_sql, params); where == "1", если ограничений нет.
    """
    clauses: list[str] = []
    params: dict = {}

    iplayers = [p.strip().lower() for p in (filters.get("include_players") or []) if p.strip()]
    xpool_players = [p.strip().lower() for p in (filters.get("exclude_pool_players") or []) if p.strip()]
    xtrade_players = [p.strip().lower() for p in (filters.get("exclude_trade_players") or []) if p.strip()]
    pools = [p.strip().lower() for p in (filters.get("pools") or []) if p.strip()]
    pools_mode = filters.get("pools_mode", config.DEFAULT_POOL_MODE)

    # Окно времени считаем заранее — оно же уходит внутрь подзапросов членства.
    tw = time_sql if time_sql is not None else _time_where(
        filters.get("time_range", "today"))

    def _membership(param: str) -> str:
        member = (f"SELECT DISTINCT pool_address FROM mv_dex_analytics_data AS mv "
                  f"WHERE has({{{param}:Array(String)}}, lower(mv.trader_address))")
        return member + (f" AND {tw}" if tw else "")

    # --- Включить игроков ---
    if include_clause != "off":
        if iplayers:
            params["iplayers"] = iplayers
            if include_clause == "trades":
                clauses.append("has({iplayers:Array(String)}, lower(mv.trader_address))")
            else:  # membership — пулы, в которых были эти игроки
                clauses.append(f"mv.pool_address IN ({_membership('iplayers')})")
        else:
            clauses.append(
                "lower(mv.trader_address) IN (SELECT lower(contract_address) FROM traders)"
            )

    # --- Исключить пулы игроков (убрать целиком их пулы) ---
    if apply_exclude and xpool_players:
        params["xpoolplayers"] = xpool_players
        clauses.append(f"mv.pool_address NOT IN ({_membership('xpoolplayers')})")

    # --- Исключить сделки игроков (убрать только их сделки) ---
    if apply_exclude and xtrade_players:
        params["xtradeplayers"] = xtrade_players
        clauses.append("NOT has({xtradeplayers:Array(String)}, lower(mv.trader_address))")

    # --- Пулы ---
    if apply_pools and pools:
        params["pools"] = pools
        if pools_mode == "exclude":
            clauses.append("NOT has({pools:Array(String)}, lower(mv.pool_address))")
        else:
            clauses.append("has({pools:Array(String)}, lower(mv.pool_address))")

    if tw:
        clauses.append(tw)

    return (" AND ".join(clauses) or "1"), params


def _pivot_wide(df: pd.DataFrame, idx: str) -> pd.DataFrame:
    """Длинный [idx, series, value] -> широкий [idx, <series...>], NaN -> 0.

    pivot_table(aggfunc=sum) вместо pivot — на случай совпадения подписей серий
    (две серии с одинаковым именем складываются, а не роняют reshape).
    """
    if df.empty:
        return pd.DataFrame({idx: []})
    wide = (df.pivot_table(index=idx, columns="series", values="value", aggfunc="sum")
              .fillna(0).reset_index())
    wide.columns.name = None
    return wide

def _bucket(time_range: str, col: str = "mv.minute_bucket") -> str:
    """Шаг округления времени: час для под-суточных окон, иначе день."""
    if time_range in ("last_hour"):
        return f"toStartOfMinute({col})"
    elif time_range in ("today", "yesterday"):
        return f"toStartOfTenMinutes({col})"
    elif time_range in ("week"):
        return f"toStartOfInterval({col}, INTERVAL 12 HOUR)"
    return f"toStartOfDay({col})"


def _hitmap_bucket(time_range: str, col: str = "mv.minute_bucket") -> str:
    """Шаг округления времени: час для под-суточных окон, иначе день."""
    if time_range in ("last_hour"):
        return f"toStartOfFiveMinutes({col})"
    elif time_range in ("today", "yesterday"):
        return f"toStartOfHour({col})"
    elif time_range in ("week"):
        return f"toStartOfDay({col})"
    return f"toStartOfWeek({col})"

# --- Кейс 1: Анализ рынка ---------------------------------------------------
def get_top_pools(filters: dict):
    """Топ-50 пулов. Обычно DataFrame[pool, volume], по убыванию.

    При ВКЛЮЧЕНИИ игроков (include_players непуст) возвращает обогащённый
    DataFrame[pool, player_vol, pool_total, share]: объём включённых игроков в
    пуле, общий объём пула (все трейдеры) и доля игроков в %. Оба числа считаются
    за один скан через комбинатор -MergeIf; HAVING оставляет только пулы, где эти
    игроки реально торговали. Исключения (оба списка) применяются и тут.
    """
    if clickhouse.USE_STUB:
        return stubs.top_pools(config.TOP_POOLS_LIMIT)

    iplayers = [p.strip().lower() for p in (filters.get("include_players") or []) if p.strip()]
    enriched = bool(iplayers)

    if enriched:
        # include_clause="off" → WHERE = исключения + (фильтр пулов) + время; сами
        # «их пулы» отбирает HAVING player_vol > 0. params["iplayers"] нужен для
        # -MergeIf, добавляем вручную.
        where, params = _scope(filters, include_clause="off")
        params["iplayers"] = iplayers
        sql = f"""
            SELECT {_POOL_LABEL_FULL} AS pool,
                   round(sumMergeIf(mv.total_volume,
                         has({{iplayers:Array(String)}}, lower(mv.trader_address))), 2) AS player_vol,
                   round(sumMerge(mv.total_volume), 2) AS pool_total,
                   round(100 * player_vol / nullIf(pool_total, 0), 2) AS share
            FROM {_MV}
            {_DIM}
            WHERE {where}
            GROUP BY mv.pool_address, d.pair
            HAVING player_vol > 0
            ORDER BY player_vol DESC
            LIMIT {int(config.TOP_POOLS_LIMIT)}
        """
        df = clickhouse.execute(sql, params)
        return df if not df.empty else pd.DataFrame(
            {"pool": [], "player_vol": [], "pool_total": [], "share": []})

    where, params = _scope(filters)
    sql = f"""
        SELECT {_POOL_LABEL_FULL} AS pool,
               round(sumMerge(mv.total_volume), 2) AS volume
        FROM {_MV}
        {_DIM}
        WHERE {where}
        GROUP BY mv.pool_address, d.pair
        ORDER BY volume DESC
        LIMIT {int(config.TOP_POOLS_LIMIT)}
    """
    df = clickhouse.execute(sql, params)
    return df if not df.empty else pd.DataFrame({"pool": [], "volume": []})


def get_top_players(filters: dict):
    """Топ-50 игроков. Обычно DataFrame[player, volume], по убыванию.

    При ВКЛЮЧЕНИИ пулов (pools непуст, pools_mode == include) возвращает
    обогащённый DataFrame[player, player_in_pool, player_total, share]: объём
    игрока в выбранных пулах, его ОБЩИЙ объём по всему рынку и долю выбранных
    пулов в %. Зеркало get_top_pools: объём в пулах — через -MergeIf, общий
    объём — обычным sumMerge без ограничения по пулам (потому WHERE без фильтра
    пулов и без фильтра игроков — таблица отвечает «кто торгует в этих пулах»).
    """
    if clickhouse.USE_STUB:
        return stubs.top_players(config.TOP_POOLS_LIMIT)

    pools = [p.strip().lower() for p in (filters.get("pools") or []) if p.strip()]
    enriched = bool(pools) and filters.get(
        "pools_mode", config.DEFAULT_POOL_MODE) == "include"

    if enriched:
        # WHERE = окно времени + исключения → player_total по всему рынку (без
        # фильтра пулов); объём в выбранных пулах берём через -MergeIf; HAVING
        # оставляет лишь игроков, торговавших в этих пулах. params["pools"] вручную.
        where, params = _scope(filters, include_clause="off", apply_pools=False)
        params["pools"] = pools
        sql = f"""
            SELECT {_SHARK_LABEL_FULL} AS player,
                   round(sumMergeIf(mv.total_volume,
                         has({{pools:Array(String)}}, lower(mv.pool_address))), 2) AS player_in_pool,
                   round(sumMerge(mv.total_volume), 2) AS player_total,
                   round(100 * player_in_pool / nullIf(player_total, 0), 2) AS share
            FROM {_MV}
            {_TRD}
            WHERE {where}
            GROUP BY mv.trader_address, tr.label
            HAVING player_in_pool > 0
            ORDER BY player_in_pool DESC
            LIMIT {int(config.TOP_POOLS_LIMIT)}
        """
        df = clickhouse.execute(sql, params)
        return df if not df.empty else pd.DataFrame(
            {"player": [], "player_in_pool": [], "player_total": [], "share": []})

    where, params = _scope(filters)
    sql = f"""
        SELECT {_SHARK_LABEL_FULL} AS player,
               round(sumMerge(mv.total_volume), 2) AS volume
        FROM {_MV}
        {_TRD}
        WHERE {where}
        GROUP BY mv.trader_address, tr.label
        ORDER BY volume DESC
        LIMIT {int(config.TOP_POOLS_LIMIT)}
    """
    df = clickhouse.execute(sql, params)
    return df if not df.empty else pd.DataFrame({"player": [], "volume": []})


def get_market_metrics(filters: dict, pair: str | None = None) -> dict:
    """7 метрик рынка: total + разбивка по паре. См. config.MARKET_METRICS.

    Если задана пара, и итоговые числа (total), и разбивка считаются в её
    разрезе. Совпадение по паре — регистронезависимое вхождение подстроки
    (ввод «usdc» подхватывает WETH/USDC, USDC/USDT и т.п.).
    """
    if clickhouse.USE_STUB:
        return stubs.market_metrics(pair)

    where, params = _scope(filters)
    total_cols = ", ".join(f"{expr} AS {key}" for key, expr in _MARKET_EXPR.items())

    # Фильтр по паре общий для total и by_pair: джойн к dim + вхождение подстроки.
    dim_join = "INNER JOIN dim_pool_pair AS d ON mv.pool_address = d.pool_address"
    if pair:
        where = f"{where} AND positionCaseInsensitive(d.pair, {{pair:String}}) > 0"
        params = dict(params)
        params["pair"] = pair

    # total — все метрики одним запросом без группировки (с парой — в её разрезе).
    total_join = dim_join if pair else ""
    tdf = clickhouse.execute(
        f"SELECT {total_cols} FROM {_MV} {total_join} WHERE {where}", params)

    # by_pair — те же метрики с группировкой по паре токенов.
    pdf = clickhouse.execute(
        f"""
        SELECT d.pair AS pair, {total_cols}
        FROM {_MV}
        {dim_join}
        WHERE {where}
        GROUP BY d.pair
        ORDER BY trade_volume DESC
        LIMIT {int(config.MARKET_PAIRS_LIMIT)}
        """,
        params,
    )

    result: dict = {}
    for key in config.MARKET_METRICS:
        total = tdf[key].iloc[0] if len(tdf) else 0
        if pd.isna(total):
            total = 0
        total = int(total) if key == "trade_count" else round(float(total), 2)

        if pdf.empty:
            by_pair = pd.DataFrame({"pair": [], "value": []})
        else:
            by_pair = pdf[["pair", key]].rename(columns={key: "value"}).copy()
            by_pair["value"] = by_pair["value"].fillna(0)
            if key != "trade_count":
                by_pair["value"] = by_pair["value"].round(2)
        result[key] = {"total": total, "by_pair": by_pair}
    return result


def get_metric_timeseries(filters: dict, metric: str, pair: str | None = None):
    """Динамика конкретной метрики по времени. DataFrame[time, value]."""
    if clickhouse.USE_STUB:
        return stubs.metric_timeseries(metric, filters.get("time_range", "today"), pair)

    tr = filters.get("time_range", "today")
    expr = _MARKET_EXPR.get(metric, _MARKET_EXPR["trade_volume"])
    val = expr if metric == "trade_count" else f"round({expr}, 2)"
    where, params = _scope(filters)

    join = ""
    if pair:
        join = "INNER JOIN dim_pool_pair AS d ON mv.pool_address = d.pool_address"
        where = f"{where} AND positionCaseInsensitive(d.pair, {{pair:String}}) > 0"
        params = dict(params)
        params["pair"] = pair

    sql = f"""
        SELECT {_bucket(tr)} AS time, {val} AS value
        FROM {_MV}
        {join}
        WHERE {where}
        GROUP BY time
        ORDER BY time
    """
    df = clickhouse.execute(sql, params)
    return df if not df.empty else pd.DataFrame({"time": [], "value": []})


# --- Кейс 2: Анализ тренда --------------------------------------------------
def _pools_in_window(filters: dict, time_sql: str, metric: str = "volume") -> pd.DataFrame:
    """Пулы со значением выбранной метрики за окно. DataFrame[pool, volume].

    Имя столбца оставляем `volume` (контракт стабилен) — подмену метрики делает
    выражение `_TREND_EXPR[metric]`. Презентационную подпись столбца под метрику
    навешивает слой колбэков.
    """
    # Членство пула в окне считаем по сделкам ВКЛЮЧЁННЫХ игроков (include_clause
    # = "trades"): таблицы «ушли/зашли» отвечают, где играли именно они.
    expr = _TREND_EXPR.get(metric, _TREND_EXPR["volume"])
    where, params = _scope(filters, time_sql=time_sql, include_clause="trades")
    sql = f"""
        SELECT {_POOL_LABEL} AS pool,
               round({expr}, 2) AS volume
        FROM {_MV}
        {_DIM}
        WHERE {where}
        GROUP BY mv.pool_address, d.pair
    """
    df = clickhouse.execute(sql, params)
    return df if not df.empty else pd.DataFrame({"pool": [], "volume": []})


def get_pools_delta(filters: dict, reference: str, metric: str = "volume") -> dict:
    """Ушедшие и зашедшие пулы за один проход. dict[left|entered -> DataFrame].

    Оба окна (`today` и reference) запрашиваются ОДИН раз, а set-difference в
    обе стороны считается в Python. Раньше get_pools_left и get_pools_entered
    вызывались по отдельности и каждая заново тянула оба окна — 4 запроса вместо
    нужных 2. refresh_all использует этот объединённый вызов.

    `metric` (volume/bribe/size) задаёт, какое значение показывать в столбце;
    членство пула в окне от метрики не зависит — set-difference идёт по `pool`.
    """
    if clickhouse.USE_STUB:
        return {"left": stubs.pools_left(reference, metric),
                "entered": stubs.pools_entered(reference, metric)}

    empty = pd.DataFrame({"pool": [], "volume": []})
    today = _pools_in_window(filters, _time_where("today"), metric)
    ref = _pools_in_window(filters, _reference_where(reference), metric)
    today_pools = set(today["pool"]) if len(today) else set()
    ref_pools = set(ref["pool"]) if len(ref) else set()

    left = (ref[~ref["pool"].isin(today_pools)].reset_index(drop=True)
            if len(ref) else empty)
    entered = (today[~today["pool"].isin(ref_pools)].reset_index(drop=True)
               if len(today) else empty)
    return {"left": left, "entered": entered}


def get_pools_left(filters: dict, reference: str, metric: str = "volume"):
    """Пулы, где играли в reference-окне, но не сегодня. DataFrame[pool, volume]."""
    if clickhouse.USE_STUB:
        return stubs.pools_left(reference, metric)
    return get_pools_delta(filters, reference, metric)["left"]


def get_pools_entered(filters: dict, reference: str, metric: str = "volume"):
    """Пулы, где появились сегодня, но не было в reference-окне."""
    if clickhouse.USE_STUB:
        return stubs.pools_entered(reference, metric)
    return get_pools_delta(filters, reference, metric)["entered"]


def _daily_grouped(where, params, expr, bucket, key, label, join, group_cols,
                   series_where=None, series_params=None):
    """Широкий df [time × топ-серии]. NaN -> 0.

    `bucket` — SQL-выражение временного бакета (из `_bucket(time_range)`): для
    под-суточных окон это 1-/10-минутки, для недели — 12 ч, иначе день. Колонку
    оси оставляем под именем `day` (контракт `_pivot_wide`/`grouped_lines`), но
    по сути это «время».

    Серии (топ-N по объёму) выбираются из `series_where` (по умолчанию = where).
    Разделение нужно, чтобы РАНЖИРОВАТЬ серии по одному скоупу (объём выбранных
    игроков), а ЗНАЧЕНИЯ показывать по другому (общий объём пула) — тогда микро- и
    макро-графики «по пулам» строятся по одному и тому же набору пулов и
    сопоставимы визуально.
    """
    sw = series_where if series_where is not None else where
    sp = series_params if series_params is not None else params
    merged = {**sp, **params}  # параметры обоих скоупов (имена не конфликтуют)
    sql = f"""
        WITH top_series AS (
            SELECT {key} AS k
            FROM {_MV}
            WHERE {sw}
            GROUP BY k
            ORDER BY sumMerge(mv.total_volume) DESC
            LIMIT {int(config.AREA_POOLS_LIMIT)}
        )
        SELECT {bucket} AS day,
               {label} AS series,
               round({expr}, 2) AS value
        FROM {_MV}
        {join}
        WHERE {where} AND {key} IN (SELECT k FROM top_series)
        GROUP BY day, {group_cols}
        ORDER BY day
    """
    return _pivot_wide(clickhouse.execute(sql, merged), "day")


def _daily_total(where, params, expr, bucket, series_name: str):
    """Широкий df [day, <series_name>] — одна серия (без группировки по сущности).

    `bucket` — выражение временного бакета (см. `_daily_grouped`)."""
    sql = f"""
        SELECT {bucket} AS day,
               round({expr}, 2) AS value
        FROM {_MV}
        WHERE {where}
        GROUP BY day
        ORDER BY day
    """
    df = clickhouse.execute(sql, params)
    if df.empty:
        return pd.DataFrame({"day": []})
    return df.rename(columns={"value": series_name})


def get_daily_micro_macro(filters: dict, metric: str, group_by: str) -> dict:
    """Динамика метрики по времени в разрезе микро/макро. dict[micro|macro -> wide df].

    Окно — ВЫБРАННЫЙ `time_range`, шаг бакета — `_bucket(time_range)` (как у
    area-графиков): «последний час» → минутки, «сегодня/вчера» → 10-минутки,
    «неделя» → 12 ч, «месяц/всё время» → день. Раньше под-недельные окна молча
    подменялись на «последние 14 дней по дням» — это путало (метки = чужие даты).

    group_by == "pool":
        micro — объём ВКЛЮЧЁННЫХ игроков в их пулах (серии = пулы);
        macro — общий объём ВСЕХ трейдеров в тех же пулах (серии = пулы).
    group_by == "player":
        micro — личные объёмы включённых игроков по всему рынку (серии = игроки);
        macro — общий объём всего рынка DEX, минус исключения (одна серия «Рынок»).

    Если игроки не включены — micro пустой (график рисуется без линий).
    """
    if clickhouse.USE_STUB:
        base = stubs.daily_changes(metric, group_by)
        return {"micro": base, "macro": base}

    tr = filters.get("time_range", "today")
    bucket = _bucket(tr)  # окно берёт сам _scope из filters["time_range"]
    expr = _TREND_EXPR.get(metric, _TREND_EXPR["volume"])
    iplayers = [p.strip().lower() for p in (filters.get("include_players") or []) if p.strip()]
    empty = pd.DataFrame({"day": []})

    if group_by == "player":
        # micro — включённые игроки по всему рынку (игнорируем фильтр пулов).
        if iplayers:
            w_micro, p_micro = _scope(filters, include_clause="trades", apply_pools=False)
            micro = _daily_grouped(w_micro, p_micro, expr, bucket, "mv.trader_address",
                                   _SHARK_LABEL, _TRD, "mv.trader_address, tr.label")
        else:
            micro = empty
        # macro — весь рынок одной линией (минус исключения).
        w_macro, p_macro = _scope(filters, include_clause="off", apply_pools=False)
        macro = _daily_total(w_macro, p_macro, expr, bucket, "Рынок")
        return {"micro": micro, "macro": macro}

    # group_by == "pool"
    # macro — вся активность в пулах текущего скоупа (при include — пулы игроков).
    w_macro, p_macro = _scope(filters)
    if iplayers:
        # micro — только сделки включённых игроков (их сделки и так в их пулах).
        # Набор пулов (топ по объёму игроков) общий для обоих графиков, чтобы
        # «когда заходил кит» и «рос ли пул» читались на одних и тех же пулах.
        w_micro, p_micro = _scope(filters, include_clause="trades")
        micro = _daily_grouped(w_micro, p_micro, expr, bucket, "mv.pool_address",
                               _POOL_LABEL, _DIM, "mv.pool_address, d.pair")
        macro = _daily_grouped(w_macro, p_macro, expr, bucket, "mv.pool_address",
                               _POOL_LABEL, _DIM, "mv.pool_address, d.pair",
                               series_where=w_micro, series_params=p_micro)
    else:
        micro = empty
        macro = _daily_grouped(w_macro, p_macro, expr, bucket, "mv.pool_address",
                               _POOL_LABEL, _DIM, "mv.pool_address, d.pair")
    return {"micro": micro, "macro": macro}


def get_heatmap_sharks_pools(filters: dict, metric: str):
    """Хитмап 1: строки=пулы, колонки=акулы, значения=metric."""
    if clickhouse.USE_STUB:
        return stubs.heatmap_sharks_pools(metric, config.HEATMAP_SHARKS_POOLS_LIMIT)

    expr = _TREND_EXPR.get(metric, _TREND_EXPR["volume"])
    where, params = _scope(filters)
    sql = f"""
        WITH top_pools AS (
            SELECT mv.pool_address AS pa
            FROM {_MV}
            WHERE {where}
            GROUP BY pa
            ORDER BY sumMerge(mv.total_volume) DESC
            LIMIT {int(config.HEATMAP_SHARKS_POOLS_LIMIT)}
        ),
        top_sharks AS (
            SELECT mv.trader_address AS ta
            FROM {_MV}
            WHERE {where}
            GROUP BY ta
            ORDER BY sumMerge(mv.total_volume) DESC
            LIMIT {int(config.HEATMAP_SHARKS_LIMIT)}
        )
        SELECT {_POOL_LABEL} AS pool,
               {_SHARK_LABEL} AS shark,
               round({expr}, 2) AS value
        FROM {_MV}
        {_DIM}
        {_TRD}
        WHERE {where}
          AND mv.pool_address IN (SELECT pa FROM top_pools)
          AND mv.trader_address IN (SELECT ta FROM top_sharks)
        GROUP BY mv.pool_address, d.pair, mv.trader_address, tr.label
        ORDER BY pool
    """
    df = clickhouse.execute(sql, params)
    if df.empty:
        return pd.DataFrame()
    mat = df.pivot_table(index="pool", columns="shark", values="value",
                         aggfunc="sum").fillna(0)
    mat.index.name = None
    mat.columns.name = None
    return mat


def get_heatmap_time_pools(filters: dict, metric: str):
    """Хитмап 2: строки=пулы, колонки=время (день), значения=metric."""
    if clickhouse.USE_STUB:
        return stubs.heatmap_time_pools(
            metric, filters.get("time_range", "today"), config.HEATMAP_POOLS_LIMIT)
    tc = filters.get("time_range", "today")
    expr = _TREND_EXPR.get(metric, _TREND_EXPR["volume"])
    where, params = _scope(filters)
    sql = f"""
        WITH top_pools AS (
            SELECT mv.pool_address AS pa
            FROM {_MV}
            WHERE {where}
            GROUP BY pa
            ORDER BY sumMerge(mv.total_volume) DESC
            LIMIT {int(config.HEATMAP_TIME_POOLS_LIMIT)}
        )
        SELECT {_POOL_LABEL} AS pool,
               {_hitmap_bucket(tc)} AS day,
               round({expr}, 2) AS value
        FROM {_MV}
        {_DIM}
        WHERE {where} AND mv.pool_address IN (SELECT pa FROM top_pools)
        GROUP BY mv.pool_address, d.pair, day
        ORDER BY pool, day
    """
    df = clickhouse.execute(sql, params)
    if df.empty:
        return pd.DataFrame()
    mat = df.pivot_table(index="pool", columns="day", values="value",
                         aggfunc="sum").fillna(0)
    # Колонки — реальные даты; сортируем по дате, затем подписываем «%m-%d».
    mat = mat.reindex(sorted(mat.columns), axis=1)
    if tc in ("last_hour", "today", "yesterday"):
        mat.columns = [pd.Timestamp(c).strftime("%H:%M") for c in mat.columns]
    else:
        mat.columns = [pd.Timestamp(c).strftime("%d %b") for c in mat.columns]
    mat.index.name = None
    mat.columns.name = None
    return mat


def get_area_by_pool(filters: dict, metric: str, limit: int = config.TOP_POOLS_LIMIT):
    """Filled area 1: время × объём, серии=пулы.

    Возвращает весь набор пулов (по умолчанию TOP_POOLS_LIMIT). Прореживание
    до нужного числа серий и сворачивание остатка в «Others» делает
    viz.filled_area по значению ползунка — данные при этом не перезапрашиваются.
    """
    if clickhouse.USE_STUB:
        return stubs.area_by_pool(metric, filters.get("time_range", "today"), limit)

    tr = filters.get("time_range", "today")
    expr = _TREND_EXPR.get(metric, _TREND_EXPR["volume"])
    where, params = _scope(filters)
    sql = f"""
        WITH top_pools AS (
            SELECT mv.pool_address AS pa
            FROM {_MV}
            WHERE {where}
            GROUP BY pa
            ORDER BY sumMerge(mv.total_volume) DESC
            LIMIT {int(limit)}
        )
        SELECT {_bucket(tr)} AS time,
               {_POOL_LABEL} AS series,
               round({expr}, 2) AS value
        FROM {_MV}
        {_DIM}
        WHERE {where} AND mv.pool_address IN (SELECT pa FROM top_pools)
        GROUP BY mv.pool_address, d.pair, time
        ORDER BY time
    """
    return _pivot_wide(clickhouse.execute(sql, params), "time")


def get_area_by_shark(filters: dict, metric: str, limit: int = config.AREA_SHARKS_LIMIT):
    """Filled area 2: время × объём, серии=акулы (ограничено limit)."""
    if clickhouse.USE_STUB:
        return stubs.area_by_shark(metric, filters.get("time_range", "today"), limit)

    tr = filters.get("time_range", "today")
    expr = _TREND_EXPR.get(metric, _TREND_EXPR["volume"])
    where, params = _scope(filters)
    sql = f"""
        WITH top_sharks AS (
            SELECT mv.trader_address AS ta
            FROM {_MV}
            WHERE {where}
            GROUP BY ta
            ORDER BY sumMerge(mv.total_volume) DESC
            LIMIT {int(limit)}
        )
        SELECT {_bucket(tr)} AS time,
               {_SHARK_LABEL} AS series,
               round({expr}, 2) AS value
        FROM {_MV}
        {_TRD}
        WHERE {where} AND mv.trader_address IN (SELECT ta FROM top_sharks)
        GROUP BY mv.trader_address, tr.label, time
        ORDER BY time
    """
    return _pivot_wide(clickhouse.execute(sql, params), "time")


def get_tokens_dict() -> dict:
    sql = """
        SELECT contract_address, label FROM tokens
    """
    df = clickhouse.execute(sql)
    return dict(zip(df["contract_address"].str.lower(), df["label"]))