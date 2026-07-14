"""Сигналы и их покрытие сделками — целиком на стороне ClickHouse.

Раньше модуль ходил в Postgres напрямую (psycopg2), тянул сигналы в pandas, а
сопоставление со сделками считал Python. Так больше нельзя по двум причинам:

*   в новой схеме torch у сигнала НЕТ адресов токенов — только имена ног вида
    «WETH/USDC Uv3 0.01%» (`opportunities.metadata`), поэтому сшивка требует
    словарей, которые живут в ClickHouse (`dim_token_canon`, `dim_pool_meta`);
*   джойн 100k+ сигналов с десятками миллионов свопов в pandas не помещался в
    память — ради этого и существовала прогрессивная загрузка батчами.

Теперь сигналы материализует сам ClickHouse (`signals_legs`, см. data/clickhouse),
он же считает покрытие, а в Python приезжает ПО СТРОКЕ НА СИГНАЛ. Postgres из
дашборда не читается вообще — единственное соединение теперь в ClickHouse.

Публичный интерфейс:
    get_max_timestamp()                   -- анкер окна «Дата»
    get_signal_ids(limit, min_ts)         -- id сигналов окна (свежие первыми)
    get_signal_summary(ids, block_window) -- готовый summary по этим сигналам
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

import config
from data import clickhouse

# Контракт колонок summary. На него садятся UI (pages/main_page.signals_columns),
# фильтры (callbacks._filtered_signals) и статистика покрытия.
SUMMARY_COLS = [
    "request_id", "signal_timestamp", "found_block",
    "base_token", "quote_token", "token_a", "token_b",
    "signal_amount", "profit", "route", "signal_bribe", "signal_fee",
    "n_hops", "covered", "coverage_kind", "covered_legs", "coverage_ratio",
    "covering_volume", "n_trades",
    "swap_timestamp", "swap_block", "swap_route_str", "swap_amount",
    "swap_user_id", "swap_bribe", "swap_fee",
    "competitor_bribe", "bribe_edge",
]


def empty_summary() -> pd.DataFrame:
    """Пустой summary по контракту (стартовое значение аккумулятора в callbacks)."""
    return pd.DataFrame(columns=SUMMARY_COLS)


# Время сделки восстанавливаем по номеру блока — метки времени в swaps/transactions
# нет. То же выражение, что в data/clickhouse._block_bucket (~12.0376 c на блок).
_BLOCK_TO_TIME = "toDateTime(1775121779 + (({block}) - 24791000) * 12.0376)"

# --- Покрытие сигнала --------------------------------------------------------
# Наивное «есть сделка по паре в ±N блоков» здесь бесполезно: в самом активном
# пуле (USDC/WETH) сделка есть в 77% ВСЕХ блоков, так что покрытым оказался бы
# почти каждый сигнал. Поэтому покрывающей считается только MEV-транзакция
# (bribe > 0), и требуется одно из двух (см. config.SIGNAL_MIN_ATOMIC_LEGS):
#
#   atomic — ОДНА транзакция задела >= 2 разных (нога, хоп) сигнала. Настоящий
#            арбитражник исполняет ноги атомарно, случайный шум — нет;
#   volume — фолбэк для сигналов с единственной DEX-ногой (42% выборки): там
#            атомарность проверить не на чем, поэтому смотрим на объём — сделка
#            должна укладываться в коридор [1/k, k] от объёма ноги.
#
# Какой критерий сработал, видно в колонке coverage_kind.
_SUMMARY_SQL = f"""
WITH
-- Батч выбирается ЗДЕСЬ, а не списком id из Python: 5000 uuid — это ~190 КБ в
-- HTTP-параметре, и ClickHouse такой запрос отвергает («Field value too long»).
-- Сортировка с добором по request_id детерминирована, поэтому соседние батчи не
-- пересекаются и не теряют сигналы (signals_legs меняется только при refresh).
sel AS (
    SELECT request_id
    FROM signals_legs
    WHERE ts >= {{min_ts:DateTime64(3)}}
    GROUP BY request_id
    ORDER BY max(ts) DESC, request_id
    LIMIT {{size:UInt64}} OFFSET {{offset:UInt64}}
),
legs AS (
    SELECT * FROM signals_legs WHERE request_id IN (SELECT request_id FROM sel)
),
-- Ноги, которые вообще можно сопоставлять: DEX и с разрешёнными адресами токенов.
dex AS (
    SELECT * FROM legs WHERE is_dex = 1 AND token_lo != ''
),
bounds AS (
    SELECT min(found_block) AS lo, max(found_block) AS hi FROM legs
),
-- Нога -> пулы-кандидаты. Если протокол ноги распознан (Uv2/Uv3/Sv3) — сверяем
-- ещё фабрику и комиссию (матч по конкретному пулу); если нет (Curve, маршруты
-- без протокола в имени) — матчим по паре токенов.
cand AS (
    SELECT d.request_id     AS request_id,
           d.leg_index      AS leg_index,
           d.hop_index      AS hop_index,
           d.leg_name       AS leg_name,
           d.leg_volume_usd AS leg_volume_usd,
           d.found_block    AS found_block,
           p.pool_address   AS pool_address
    FROM dex AS d
    INNER JOIN dim_pool_meta AS p
        ON p.token_lo = d.token_lo AND p.token_hi = d.token_hi
    WHERE d.pool_bound = 0
       OR (p.protocol = d.protocol AND ifNull(p.fee_tier, 0) = ifNull(d.fee_tier, 0))
),
-- Окно блоков разворачиваем в РАВЕНСТВО (pool_address, block_number): так это
-- хеш-джойн по двум ключам, а не range-join через всю таблицу свопов.
keys AS (
    SELECT request_id, leg_index, hop_index, leg_name, leg_volume_usd, pool_address,
           -- toUInt64: вычитание UInt64 даёт Int64, а джойн ниже идёт по
           -- transactions.block_number (UInt64) — общий тип не вывелся бы.
           toUInt64(found_block - {{w:UInt64}} + arrayJoin(range(2 * {{w:UInt64}} + 1))) AS block
    FROM cand
),
-- Только MEV-транзакции (bribe > 0) и только в блоках выбранного окна: диапазон
-- прунится по block_number — ключу сортировки transactions.
trades AS (
    SELECT s.pool_address              AS pool_address,
           t.block_number              AS block_number,
           s.transaction_hash_id       AS tx,
           toFloat64(ifNull(s.usd_amount, 0)) AS usd,
           lower(t.trader_address)     AS trader,
           -- bribe/priority_fee — Nullable(UInt256); NULL трактуем как 0, иначе
           -- сумма схлопнется в NULL и транзакция потеряет «цену» в сравнении.
           ifNull(t.bribe, 0) + ifNull(t.priority_fee, 0) AS paid,
           toFloat64(ifNull(t.priority_fee, 0))           AS priority_fee
    FROM swaps AS s
    INNER JOIN transactions AS t ON s.transaction_hash_id = t.hash_id
    WHERE t.bribe > 0
      AND t.block_number >= (SELECT lo FROM bounds) - {{w:UInt64}}
      AND t.block_number <= (SELECT hi FROM bounds) + {{w:UInt64}}
),
hits AS (
    SELECT k.request_id     AS request_id,
           k.leg_index      AS leg_index,
           k.hop_index      AS hop_index,
           k.leg_name       AS leg_name,
           k.leg_volume_usd AS leg_volume_usd,
           x.tx             AS tx,
           x.usd            AS usd,
           x.trader         AS trader,
           x.paid           AS paid,
           x.priority_fee   AS priority_fee,
           x.block_number   AS block_number
    FROM keys AS k
    INNER JOIN trades AS x
        ON x.pool_address = k.pool_address AND x.block_number = k.block
),
-- Кандидаты в покрытие: агрегат по (сигнал, транзакция). units — сколько разных
-- ног/хопов сигнала задела ЭТА транзакция; на этом и стоит критерий атомарности.
tx_cov AS (
    SELECT request_id,
           tx,
           uniqExact((leg_index, hop_index))         AS units,
           sum(usd)                                  AS usd_total,
           count()                                   AS n_trades,
           argMax(trader, usd)                       AS trader,
           argMax(block_number, usd)                 AS block_number,
           argMax(paid, usd)                         AS paid,
           argMax(priority_fee, usd)                 AS priority_fee,
           arrayStringConcat(arraySort(groupUniqArray(leg_name)), ' | ') AS legs_hit,
           -- фолбэк по объёму: сделка соразмерна ноге (коридор [1/k, k])
           maxIf(1, usd >= leg_volume_usd / {{k:Float64}}
                    AND usd <= leg_volume_usd * {{k:Float64}}) AS volume_fit
    FROM hits
    GROUP BY request_id, tx
),
cov_atomic AS (
    SELECT * FROM tx_cov
    WHERE units >= {{min_atomic:UInt16}}
    ORDER BY request_id, units DESC, usd_total DESC
    LIMIT 1 BY request_id
),
cov_volume AS (
    SELECT * FROM tx_cov
    WHERE volume_fit = 1
    ORDER BY request_id, usd_total DESC
    LIMIT 1 BY request_id
),
-- Сильнейший конкурент в окне: максимум (bribe + priority_fee) среди ВСЕХ
-- покрывающих транзакций сигнала — считаем независимо от того, какая из них
-- в итоге признана покрытием.
comp AS (
    SELECT request_id, max(paid) AS competitor_paid FROM hits GROUP BY request_id
),
-- Печатный маршрут сигнала: все ноги, включая CEX (сделок по ним в ClickHouse
-- нет, но без них маршрут нечитаем).
route AS (
    SELECT request_id,
           arrayStringConcat(
               arrayMap(x -> x.2,
                        arraySort(groupArray((leg_index, concat(leg_name, ' ', leg_side))))),
               ' | ') AS route
    FROM (SELECT DISTINCT request_id, leg_index, leg_name, leg_side FROM legs)
    GROUP BY request_id
),
-- Комиссия сигнала = комиссия первой DEX-ноги (3000 -> 0.003), как раньше.
fee AS (
    SELECT request_id, argMin(ifNull(fee_tier, 0), leg_index) / 1000000 AS signal_fee
    FROM legs WHERE is_dex = 1 GROUP BY request_id
),
sig AS (
    SELECT request_id,
           any(ts)          AS ts,
           any(niche)       AS niche,
           any(volume_usd)  AS volume_usd,
           any(profit)      AS profit,
           any(found_block) AS found_block,
           any(bribe)       AS bribe,
           countIf(is_dex = 1) AS n_units
    FROM legs GROUP BY request_id
)
SELECT
    s.request_id                                   AS request_id,
    s.ts                                           AS signal_timestamp,
    s.found_block                                  AS found_block,
    splitByChar('/', s.niche)[1]                   AS base_token,
    splitByChar('/', s.niche)[2]                   AS quote_token,
    splitByChar('/', s.niche)[1]                   AS token_a,
    splitByChar('/', s.niche)[2]                   AS token_b,
    s.volume_usd                                   AS signal_amount,
    s.profit                                       AS profit,
    ifNull(r.route, '')                            AS route,
    toFloat64(s.bribe) / 1e18                      AS signal_bribe,
    ifNull(f.signal_fee, 0)                        AS signal_fee,
    s.n_units                                      AS n_hops,
    -- Атомарное покрытие сильнее объёмного, поэтому проверяется первым. Объёмный
    -- фолбэк применим ТОЛЬКО к сигналам с одной DEX-ногой: там атомарность
    -- принципиально непроверяема (задеть две ноги одной транзакцией нечем).
    multiIf(a.tx != '', 'atomic',
            v.tx != '' AND s.n_units = 1, 'volume',
            '')                                    AS coverage_kind,
    coverage_kind != ''                            AS covered,
    multiIf(coverage_kind = 'atomic', a.units,
            coverage_kind = 'volume', toUInt64(1),
            toUInt64(0))                           AS covered_legs,
    if(s.n_units = 0, 0, covered_legs / s.n_units) AS coverage_ratio,
    multiIf(coverage_kind = 'atomic', a.usd_total,
            coverage_kind = 'volume', v.usd_total,
            0.)                                    AS covering_volume,
    multiIf(coverage_kind = 'atomic', a.n_trades,
            coverage_kind = 'volume', v.n_trades,
            toUInt64(0))                           AS n_trades,
    multiIf(coverage_kind = 'atomic', a.legs_hit,
            coverage_kind = 'volume', v.legs_hit,
            '')                                    AS swap_route_str,
    multiIf(coverage_kind = 'atomic', a.trader,
            coverage_kind = 'volume', v.trader,
            '')                                    AS swap_user_id,
    multiIf(coverage_kind = 'atomic', a.block_number,
            coverage_kind = 'volume', v.block_number,
            toUInt64(0))                           AS swap_block_raw,
    if(covered, {_BLOCK_TO_TIME.format(block='swap_block_raw')},
       toDateTime(0))                              AS swap_timestamp,
    if(covered, swap_block_raw, NULL)              AS swap_block,
    if(covered, covering_volume, NULL)             AS swap_amount,
    if(covered,
       toFloat64(multiIf(coverage_kind = 'atomic', a.paid,
                         coverage_kind = 'volume', v.paid,
                         toUInt256(0))) / 1e18,
       NULL)                                       AS swap_bribe,
    if(covered,
       multiIf(coverage_kind = 'atomic', a.priority_fee,
               coverage_kind = 'volume', v.priority_fee,
               0.) / 1e18,
       NULL)                                       AS swap_fee,
    if(c.request_id != '', toFloat64(c.competitor_paid) / 1e18, NULL) AS competitor_bribe,
    if(c.request_id != '', signal_bribe - competitor_bribe, NULL)     AS bribe_edge
FROM sig AS s
LEFT JOIN route      AS r ON r.request_id = s.request_id
LEFT JOIN fee        AS f ON f.request_id = s.request_id
LEFT JOIN cov_atomic AS a ON a.request_id = s.request_id
LEFT JOIN cov_volume AS v ON v.request_id = s.request_id
LEFT JOIN comp       AS c ON c.request_id = s.request_id
ORDER BY signal_timestamp DESC
"""


def get_max_timestamp() -> datetime | None:
    """Максимальная метка времени материализованных сигналов (анкер окна «Дата»)."""
    rows = clickhouse.execute("SELECT max(ts) AS ts FROM signals_legs")
    if rows.empty or pd.isna(rows["ts"].iloc[0]):
        return None
    return rows["ts"].iloc[0].to_pydatetime()


# Отсутствие нижней границы времени («Всё время») — это заведомо древняя дата, а
# не отдельная ветка SQL: так запрос один и параметры всегда одинаковые. Не 1970:
# драйвер переводит naive datetime в таймзону сервера, а на Windows такой перевод
# нулевой эпохи падает с OSError.
_EPOCH = datetime(2000, 1, 1)


def get_signal_count(min_ts: datetime | None = None) -> int:
    """Сколько сигналов попадает в окно «Дата» (нужно для числа батчей)."""
    df = clickhouse.execute(
        "SELECT uniqExact(request_id) AS n FROM signals_legs "
        "WHERE ts >= {min_ts:DateTime64(3)}",
        {"min_ts": min_ts or _EPOCH})
    return 0 if df.empty else int(df["n"].iloc[0])


def get_signal_summary(offset: int, size: int, block_window: int,
                       min_ts: datetime | None = None) -> pd.DataFrame:
    """Покрытие для одного батча сигналов: строка на сигнал, колонки SUMMARY_COLS.

    Батч — это срез `[offset, offset + size)` сигналов окна «Дата», отсортированных
    свежими вперёд (см. CTE `sel`). Считать покрытие сразу по всему окну нельзя:
    джойн ног со свопами — это пик памяти в ClickHouse, а база живая и растёт.
    """
    if size <= 0:
        return empty_summary()

    df = clickhouse.execute(_SUMMARY_SQL, {
        "min_ts": min_ts or _EPOCH,
        "offset": int(offset),
        "size": int(size),
        "w": int(block_window),
        "k": float(config.SIGNAL_VOLUME_TOLERANCE),
        "min_atomic": int(config.SIGNAL_MIN_ATOMIC_LEGS),
    })
    if df.empty:
        return empty_summary()

    # covered приезжает UInt8 — на нём стоят фильтр «Покрытые/Непокрытые» и
    # подсчёт статистики, поэтому приводим к настоящему bool.
    df["covered"] = df["covered"].astype(bool)
    # У непокрытых сигналов время сделки — эпоха; в таблице должен быть пробел.
    df.loc[~df["covered"], "swap_timestamp"] = pd.NaT
    return df[SUMMARY_COLS]


# --------------------------------------------------------------------------- #
# Self-test инвариантов покрытия (python -m data.signals_queries).
#
# Логика покрытия живёт в SQL, поэтому и проверять её надо в ClickHouse — но НЕ на
# боевых данных: тест поднимает свою базу с синтетическими swaps/transactions/
# dim_pool_meta/signals_legs, гоняет по ним _SUMMARY_SQL и в конце базу сносит.
# Синхронность дампов не нужна: каждый сценарий сконструирован руками.
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import clickhouse_connect

    A, B, C = ("0x" + ch * 40 for ch in "abc")     # a < b < c: канонический порядок
    P1, P2, P3, P4 = ("0x" + f"{i}" * 40 for i in range(1, 5))
    T0 = datetime(2026, 7, 14, 9, 0, 0)

    # Пулы: пара адресов + протокол + комиссия. P4 — «как Curve»: фабрики нет,
    # поэтому протокол пустой и нога может привязаться к нему только по паре.
    POOLS = [
        (P1, A, B, "0xf1", 500, "Uv3"),
        (P2, A, C, "0xf1", 3000, "Uv3"),
        (P3, A, B, "0xf2", 3000, "Uv2"),
        (P4, B, C, "__unknown__", None, ""),
    ]

    def leg(rid, block, idx, lo, hi, proto, fee, vol, is_dex=1, hop=1):
        """Строка signals_legs (сигнал-уровневые поля дублируются на каждой ноге)."""
        return (rid, T0, "X/Y", "SELL", 10_000.0, 5.0, block, 1_000_000_000_000_000,
                idx, f"leg{idx}", "SELL", vol, is_dex, hop, "", "",
                proto, fee, lo if is_dex else "", hi if is_dex else "",
                1 if proto else 0)

    # Блоки у сигналов разные, чтобы окна ±2 не пересекались и сценарии не влияли
    # друг на друга через чужие сделки.
    LEGS = [
        # s1: две ноги, обе задеты ОДНОЙ транзакцией -> atomic. Плюс CEX-нога:
        # она не должна попасть ни в n_hops, ни в матчинг.
        leg("s1", 100, 1, A, B, "Uv3", 500, 1000.0),
        leg("s1", 100, 2, A, C, "Uv3", 3000, 1000.0),
        leg("s1", 100, 3, "", "", "", None, 2000.0, is_dex=0),
        # s2: те же две ноги, но задеты РАЗНЫМИ транзакциями -> атомарности нет.
        leg("s2", 200, 1, A, B, "Uv3", 500, 1000.0),
        leg("s2", 200, 2, A, C, "Uv3", 3000, 1000.0),
        # s3: одна нога, объём сделки в коридоре [1/3, 3] -> volume.
        leg("s3", 300, 1, A, B, "Uv3", 500, 1000.0),
        # s4: одна нога, объём сделки в 100 раз больше -> не покрыт.
        leg("s4", 400, 1, A, B, "Uv3", 500, 1000.0),
        # s5: одна нога, сделка есть, но bribe = 0 (не MEV) -> не покрыт.
        leg("s5", 500, 1, A, B, "Uv3", 500, 1000.0),
        # s6: две ноги, атомарная транзакция есть, но вне окна ±2 -> не покрыт.
        leg("s6", 600, 1, A, B, "Uv3", 500, 1000.0),
        leg("s6", 600, 2, A, C, "Uv3", 3000, 1000.0),
        # s7: нога требует Uv3 c fee 100 — такого пула нет -> кандидатов нет.
        leg("s7", 700, 1, A, B, "Uv3", 100, 1000.0),
        # s8: нога без протокола (Curve) -> матч по ПАРЕ, пул P4 подходит.
        leg("s8", 800, 1, B, C, "", None, 1000.0),
    ]

    def tx(h, block, bribe, priority=0):
        return (h, block, "0xtrader", bribe, priority)

    TXS = [
        tx("tx1", 100, 10),          # s1: атомарная (две ноги в одной транзакции)
        tx("tx2", 200, 10),          # s2: только нога 1
        tx("tx3", 200, 10),          # s2: только нога 2 (другая транзакция)
        tx("tx4", 301, 5, 2),        # s3: соседний блок, объём в коридоре
        tx("tx5", 400, 5),           # s4: объём вне коридора
        tx("tx6", 500, 0),           # s5: без брайба — не MEV
        tx("tx7", 610, 10),          # s6: вне окна ±2 от 600
        tx("tx8", 801, 7),           # s8: Curve-нога, матч по паре
    ]

    SWAPS = [
        ("tx1", P1, 1000.0), ("tx1", P2, 1000.0),   # одна транзакция — две ноги
        ("tx2", P1, 1000.0),
        ("tx3", P2, 1000.0),
        ("tx4", P1, 1200.0),                        # 1.2x от объёма ноги — в коридоре
        ("tx5", P1, 100000.0),                      # 100x — вне коридора
        ("tx6", P1, 1000.0),
        ("tx7", P1, 1000.0), ("tx7", P2, 1000.0),
        ("tx8", P4, 900.0),
    ]

    db = f"{clickhouse.CLICKHOUSE_DB}_selftest"
    admin = clickhouse_connect.get_client(
        host=clickhouse.CLICKHOUSE_HOST, port=clickhouse.CLICKHOUSE_PORT,
        username=clickhouse.CLICKHOUSE_USER, password=clickhouse.CLICKHOUSE_PASSWORD)
    admin.command(f"DROP DATABASE IF EXISTS {db}")
    admin.command(f"CREATE DATABASE {db}")

    c = clickhouse_connect.get_client(
        host=clickhouse.CLICKHOUSE_HOST, port=clickhouse.CLICKHOUSE_PORT,
        username=clickhouse.CLICKHOUSE_USER, password=clickhouse.CLICKHOUSE_PASSWORD,
        database=db)
    try:
        c.command("""CREATE TABLE signals_legs (
            request_id String, ts DateTime64(3), niche String, direction String,
            volume_usd Float64, profit Float64, found_block UInt64, bribe Int64,
            leg_index UInt16, leg_name String, leg_side String, leg_volume_usd Float64,
            is_dex UInt8, hop_index UInt16, sym_in String, sym_out String,
            protocol String, fee_tier Nullable(UInt32),
            token_lo String, token_hi String, pool_bound UInt8
        ) ENGINE = MergeTree ORDER BY (request_id, leg_index, hop_index)""")
        c.command("""CREATE TABLE dim_pool_meta (
            pool_address String, token_lo String, token_hi String,
            dex_factory String, fee_tier Nullable(UInt32), protocol String
        ) ENGINE = MergeTree ORDER BY (token_lo, token_hi, pool_address)""")
        c.command("""CREATE TABLE transactions (
            hash_id String, block_number UInt64, trader_address String,
            bribe Nullable(UInt256), priority_fee Nullable(UInt256)
        ) ENGINE = MergeTree ORDER BY block_number""")
        c.command("""CREATE TABLE swaps (
            transaction_hash_id String, pool_address String,
            usd_amount Nullable(Float64)
        ) ENGINE = MergeTree ORDER BY transaction_hash_id""")

        c.insert("signals_legs", LEGS, column_names=[
            "request_id", "ts", "niche", "direction", "volume_usd", "profit",
            "found_block", "bribe", "leg_index", "leg_name", "leg_side",
            "leg_volume_usd", "is_dex", "hop_index", "sym_in", "sym_out",
            "protocol", "fee_tier", "token_lo", "token_hi", "pool_bound"])
        c.insert("dim_pool_meta", POOLS, column_names=[
            "pool_address", "token_lo", "token_hi", "dex_factory", "fee_tier", "protocol"])
        c.insert("transactions", TXS, column_names=[
            "hash_id", "block_number", "trader_address", "bribe", "priority_fee"])
        c.insert("swaps", SWAPS, column_names=[
            "transaction_hash_id", "pool_address", "usd_amount"])

        df = c.query_df(_SUMMARY_SQL, parameters={
            "min_ts": _EPOCH, "offset": 0, "size": 100,
            "w": 2, "k": 3.0, "min_atomic": 2,
        }).set_index("request_id")
        assert len(df) == 8, f"ожидалось 8 сигналов, пришло {len(df)}"

        kind = df["coverage_kind"].to_dict()
        assert kind["s1"] == "atomic", kind["s1"]   # две ноги — одной транзакцией
        assert kind["s2"] == "", kind["s2"]         # те же ноги, но разными транзакциями
        assert kind["s3"] == "volume", kind["s3"]   # одноногий, объём в коридоре
        assert kind["s4"] == "", kind["s4"]         # объём вне коридора
        assert kind["s5"] == "", kind["s5"]         # сделка без брайба — не MEV
        assert kind["s6"] == "", kind["s6"]         # атомарная, но вне окна блоков
        assert kind["s7"] == "", kind["s7"]         # нет пула с такой комиссией
        assert kind["s8"] == "volume", kind["s8"]   # Curve: матч по паре, без протокола

        assert bool(df.loc["s1", "covered"]) is True
        assert bool(df.loc["s2", "covered"]) is False

        # CEX-нога не считается ногой маршрута и не ищется в сделках.
        assert int(df.loc["s1", "n_hops"]) == 2, df.loc["s1", "n_hops"]
        assert int(df.loc["s1", "covered_legs"]) == 2
        assert int(df.loc["s3", "covered_legs"]) == 1
        assert int(df.loc["s2", "covered_legs"]) == 0
        assert float(df.loc["s1", "coverage_ratio"]) == 1.0

        # Объём покрытия = сумма сделок покрывающей транзакции; сделки считаются.
        assert float(df.loc["s1", "covering_volume"]) == 2000.0
        assert int(df.loc["s1", "n_trades"]) == 2
        assert float(df.loc["s3", "covering_volume"]) == 1200.0

        # Брайб сильнейшего конкурента — по ВСЕМ покрывающим транзакциям сигнала,
        # даже если покрытием признана не она; перевес = наш брайб минус его.
        assert float(df.loc["s1", "competitor_bribe"]) == 10 / 1e18
        assert float(df.loc["s3", "competitor_bribe"]) == 7 / 1e18   # bribe 5 + priority 2
        assert abs(float(df.loc["s1", "bribe_edge"])
                   - (1e15 - 10) / 1e18) < 1e-12
        # У сигнала без единой MEV-сделки конкурента нет.
        assert pd.isna(df.loc["s5", "competitor_bribe"])

        # Непокрытые не должны тащить в таблицу нули вместо пропусков.
        assert pd.isna(df.loc["s2", "swap_block"])
        assert pd.isna(df.loc["s2", "swap_amount"])

        print("OK: все проверки покрытия пройдены")
        print(df[["coverage_kind", "n_hops", "covered_legs", "covering_volume",
                  "n_trades", "competitor_bribe"]].to_string())
    finally:
        admin.command(f"DROP DATABASE IF EXISTS {db}")
