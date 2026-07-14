"""Коннектор к ClickHouse.

Реальное подключение через `clickhouse-connect` (HTTP). Слой `data/queries.py`
вызывает только `execute()` и получает готовый `pandas.DataFrame`.

ВАЖНО: списки адресов передаются ТОЛЬКО через `params` (серверные параметры
ClickHouse вида `{name:Type}`), их нельзя интерполировать в текст SQL. Пример:
    execute("... WHERE has({players:Array(String)}, lower(trader_address))",
            {"players": [...]})

`USE_STUB = True` переключает весь слой данных обратно на заглушки
(`data/stubs.py`) для офлайн-разработки без БД.
"""

from __future__ import annotations

import os
import threading
import time

import config

try:
    # Подхватываем переменные из файла `.env` (если он есть) до чтения os.getenv.
    # Отсутствие python-dotenv не критично — тогда берутся системные переменные.
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# Переключатель источника данных. False → реальная БД, True → заглушки.
# Можно переопределить через переменную окружения USE_STUB (напр. для смоук-теста
# контейнера без доступа к БД): USE_STUB=true. По умолчанию — реальная БД.
USE_STUB = os.getenv("USE_STUB", "false").lower() in ("1", "true", "yes")

# Параметры подключения (можно переопределить через переменные окружения / .env).
CLICKHOUSE_HOST = os.getenv("CH_HOST", "localhost")
CLICKHOUSE_PORT = int(os.getenv("CH_PORT", "8123"))
CLICKHOUSE_USER = os.getenv("CH_USER", "default")
CLICKHOUSE_PASSWORD = os.getenv("CH_PASSWORD", "qwerty")
CLICKHOUSE_DB = os.getenv("CH_DB", "eywa")

# --- Аналитическое представление mv_dex_analytics ----------------------------
# Основа всего дашборда: агрегатные состояния по (минута, пул, игрок), из которых
# `data/queries.py` -Merge-ом собирает все метрики. Состоит из ДВУХ объектов:
#   mv_dex_analytics_data — таблица-приёмник (AggregatingMergeTree);
#   mv_dex_analytics      — MV-триггер: агрегирует КАЖДУЮ новую пачку строк swaps
#                           (джойн к transactions за номером блока/трейдером/брайбом)
#                           и дописывает состояния в таблицу-приёмник.
#
# MV-триггер срабатывает только на вставки ПОСЛЕ своего создания — уже лежащие в
# БД данные он не «затягивает». Поэтому при старте (_ensure_analytics) историю
# досыпаем вручную (INSERT ... SELECT тем же телом), см. _backfill_analytics.
#
# Время считаем по номеру блока (в swaps/transactions нет метки времени):
# toDateTime(1775121779 + (block_number - 24791000) * 12.0376) — линейная
# аппроксимация «блок → utc» (~12.0376 c на блок). Функция монотонна по блоку —
# на этом держится и досыпка по диапазону блоков, и отсечка по минутам.
def _block_bucket(block_expr: str) -> str:
    """SQL «номер блока → минутный бакет» (то же выражение, что в теле MV)."""
    return (f"toStartOfMinute(toDateTime(1775121779 + "
            f"(({block_expr} - 24791000) * 12.0376)))")


# Список агрегатов тела представления. Один на два потребителя (DDL самой MV и
# ручной бэкофилл), чтобы триггер и досыпка считали ОДНО И ТО ЖЕ.
_ANALYTICS_AGG = f"""
    {_block_bucket('t.block_number')} AS minute_bucket,
    s.pool_address AS pool_address,
    t.trader_address AS trader_address,
    countState() AS trades_count,
    sumState(toFloat64(ifNull(s.usd_amount, 0))) AS total_volume,
    sumState(toFloat64(ifNull(t.bribe, 0))) AS total_bribe,
    minState(toFloat64(ifNull(s.usd_amount, 0))) AS min_volume,
    maxState(toFloat64(ifNull(s.usd_amount, 0))) AS max_volume,
    quantilesState(0.5)(toFloat64(ifNull(s.usd_amount, 0))) AS median_volume
"""
_ANALYTICS_GROUP_BY = "GROUP BY minute_bucket, pool_address, trader_address"

_ANALYTICS_TABLE_DDL = """
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
ORDER BY (minute_bucket, pool_address, trader_address)
"""

_ANALYTICS_MV_DDL = f"""
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_dex_analytics TO mv_dex_analytics_data
AS SELECT {_ANALYTICS_AGG}
FROM swaps AS s
INNER JOIN transactions AS t ON s.transaction_hash_id = t.hash_id
{_ANALYTICS_GROUP_BY}
"""

# Ручная досыпка истории: то же тело, но по ДИАПАЗОНУ БЛОКОВ. transactions взята
# подзапросом, а не таблицей — так фильтр по блоку гарантированно применяется ДО
# джойна (правая часть = только чанк, хеш-таблица маленькая) на любой версии CH,
# без надежды на пушдаун предиката. Левая (swaps) при этом сканируется на каждый
# чанк целиком, поэтому чанк берём крупным: config.CH_BACKFILL_BLOCK_CHUNK — это
# ручка «память чанка ↔ число проходов по swaps».
_ANALYTICS_BACKFILL = f"""
INSERT INTO mv_dex_analytics_data
SELECT {_ANALYTICS_AGG}
FROM swaps AS s
INNER JOIN (
    SELECT hash_id, block_number, trader_address, bribe
    FROM transactions
    WHERE block_number >= {{lo:UInt64}} AND block_number <= {{hi:UInt64}}
) AS t ON s.transaction_hash_id = t.hash_id
{_ANALYTICS_GROUP_BY}
"""

# Вспомогательная dim-сущность: pool_address -> подпись пары токенов
# (`WETH/USDC`). Самого агрегирующего представления mv_dex_analytics_data в БД
# для аналитики рынка достаточно, но в нём нет читаемых подписей пар — их даёт
# эта сущность. Почти каждый запрос слоя данных джойнит её (`_DIM` в queries.py).
#
# Реализована как REFRESHABLE MATERIALIZED VIEW: создаётся один раз, живёт в БД и
# сама пересчитывается по расписанию (REFRESH EVERY 1 DAY), подхватывая новые пулы
# и заполненные подписи токенов. Под капотом это обычная таблица MergeTree, поэтому
# джойн к ней быстрый (десятки мс на запрос). Обычная VIEW так не годится — она
# пересчитывала бы тело (полный скан swaps + GROUP BY + два JOIN к tokens) на
# КАЖДЫЙ джойн (~0.5 c на запрос). Обычная (insert-триггерная) MV тоже не подходит:
# тело агрегирует ВЕСЬ swaps и джойнит tokens — это полный пересчёт, а не инкремент
# по новым строкам, и существующие данные она бы не «затянула».
#
# Создаётся без EMPTY → первичный refresh наполняет её имеющимися данными сразу.
# Период пересчёта (1 DAY) — единственный тюнинг: пара у пула неизменна, словарь
# только пополняется новыми пулами, так что суточного цикла с запасом достаточно.
_DIM_POOL_PAIR_DDL = """
CREATE MATERIALIZED VIEW IF NOT EXISTS dim_pool_pair
REFRESH EVERY 1 DAY
ENGINE = MergeTree ORDER BY pool_address AS
SELECT
    p.pool_address AS pool_address,
    concat(
        coalesce(ta.label, substring(p.ta, 1, 8)),
        '/',
        coalesce(tb.label, substring(p.tb, 1, 8))
    ) AS pair
FROM (
    SELECT pool_address,
           any(token_a_address) AS ta,
           any(token_b_address) AS tb
    FROM swaps
    GROUP BY pool_address
) AS p
LEFT JOIN tokens AS ta ON p.ta = ta.contract_address
LEFT JOIN tokens AS tb ON p.tb = tb.contract_address
"""

# --- Словарь канонических адресов токенов (dim_token_canon) ------------------
# Сигналы из Postgres знают токены только по СИМВОЛУ («WETH/USDC Uv3 0.01%»), а
# сделки — только по адресу. Символ же ничем не защищён: в tokens 130 разных
# контрактов зовутся PEPE, 64 — DOGE, 49 — USDT, 6 — WETH. Матчинг «по символу»
# засчитал бы сделку в пуле поддельного USDC как покрытие сигнала.
#
# Канонический адрес символа = адрес с НАИБОЛЬШИМ оборотом: настоящий токен
# опережает подделки на 3-9 порядков. Исключения (где оборот не даёт однозначного
# ответа) перечислены в config.CANONICAL_TOKEN_OVERRIDES и применяются поверх.
#
# Это обычная таблица, а не MV: оверрайды удобнее применять в Python (и покрыть
# тестом). Таблица крошечная, пересобирается на старте одним запросом.
_DIM_TOKEN_CANON_DDL = """
CREATE TABLE IF NOT EXISTS dim_token_canon (
    symbol  String,
    address String
) ENGINE = MergeTree ORDER BY symbol
"""

_TOKEN_CANON_RANKING = """
SELECT symbol, argMax(address, total) AS address
FROM (
    SELECT upper(t.label) AS symbol,
           legs.addr      AS address,
           sum(legs.vol)  AS total
    FROM (
        SELECT arrayJoin([lower(token_a_address), lower(token_b_address)]) AS addr,
               toFloat64(ifNull(usd_amount, 0)) AS vol
        FROM swaps
        WHERE token_a_address IS NOT NULL AND token_b_address IS NOT NULL
    ) AS legs
    INNER JOIN tokens AS t ON lower(t.contract_address) = legs.addr
    WHERE t.label != ''
    GROUP BY symbol, address
)
GROUP BY symbol
"""


def _protocol_case(column: str) -> str:
    """SQL «адрес фабрики -> код протокола» по config.DEX_FACTORIES.

    Справочника протоколов нет ни в ClickHouse (dexes.label — заглушки
    'unknown_dex_1f98431c'), ни в eywa, поэтому фабрики перечислены в конфиге.
    Нераспознанная фабрика (в т.ч. пулы Curve, у которых её нет) → пустая строка:
    такая нога матчится по паре токенов, без привязки к конкретному пулу.
    """
    branches = "".join(
        f"    WHEN {factory!r} THEN {protocol!r}\n"
        for factory, protocol in config.DEX_FACTORIES.items()
    )
    return f"CASE lower({column})\n{branches}    ELSE ''\nEND"


# --- Метаданные пулов (dim_pool_meta) ----------------------------------------
# Ключ джойна «нога сигнала -> пул»: канонически отсортированная пара АДРЕСОВ +
# протокол + fee_tier. Собирается из двух источников, потому что ни один не
# самодостаточен: liquidity_pools знает фабрику и комиссию, но НЕ знает токенов
# пула; токены есть только в swaps.
#
# Отдельная сущность, а не расширение dim_pool_pair: та отдаёт подписи-символы и
# джойнится всеми страницами дашборда (queries.py), менять её контракт рискованно.
_DIM_POOL_META_DDL = f"""
CREATE MATERIALIZED VIEW IF NOT EXISTS dim_pool_meta
REFRESH EVERY 1 HOUR
ENGINE = MergeTree ORDER BY (token_lo, token_hi, pool_address) AS
SELECT
    p.pool_address           AS pool_address,
    least(p.ta, p.tb)        AS token_lo,
    greatest(p.ta, p.tb)     AS token_hi,
    lp.dex_factory           AS dex_factory,
    lp.fee_tier              AS fee_tier,
    {_protocol_case('lp.dex_factory')} AS protocol
FROM (
    -- ifNull, хотя NULL отфильтрован выше: any() над Nullable-колонкой остаётся
    -- Nullable по типу, а MergeTree не пускает Nullable в ключ сортировки.
    SELECT pool_address,
           lower(ifNull(any(token_a_address), '')) AS ta,
           lower(ifNull(any(token_b_address), '')) AS tb
    FROM swaps
    WHERE token_a_address IS NOT NULL AND token_b_address IS NOT NULL
    GROUP BY pool_address
) AS p
LEFT JOIN liquidity_pools AS lp ON lp.contract_address = p.pool_address
"""


def _pg_source(table: str) -> str:
    """Табличная функция postgresql() к базе torch (см. config.CH_PG_*).

    Ходит в Postgres САМ ClickHouse, поэтому адрес — тот, по которому torch виден
    контейнеру CH. Пароль попадает в текст запроса (так устроена postgresql());
    в SHOW CREATE ClickHouse его маскирует.
    """
    def q(value) -> str:          # кавычки внутри значения экранируем удвоением
        return "'" + str(value).replace("'", "''") + "'"

    endpoint = f"{config.CH_PG_HOST}:{config.CH_PG_PORT}"
    return (f"postgresql({q(endpoint)}, {q(config.CH_PG_DB)}, {q(table)}, "
            f"{q(config.CH_PG_USER)}, {q(config.CH_PG_PASSWORD)})")


# --- Сигналы из Postgres (signals_legs) --------------------------------------
# Материализация сигналов на стороне ClickHouse: одна строка на ХОП ноги.
#
# Зачем МАТЕРИАЛИЗАЦИЯ, а не запрос к Postgres на каждый клик: metadata — это
# jsonb на 15-17 ног, ~3 КБ на сигнал; читать и разбирать сотни мегабайт на каждое
# открытие страницы нельзя. Refresh раз в час (torch и так обновляется репликацией
# раз в 4 часа), и он атомарен: если Postgres недоступен (окно репликации, когда
# базу дропают и заливают заново), в таблице остаются прежние данные, а дашборд
# продолжает работать.
#
# Разбор имени ноги («WETH/USDC Uv3 0.01%») проверен на всех 52 реальных именах:
#   parts[1] — пара, parts[2] — протокол, parts[3] — комиссия;
#   «0.05%» -> fee_tier 500, у Uv2 комиссия в имени не пишется (= 3000),
#   у Curve её нет вовсе (NULL) и фабрики у её пулов тоже нет -> матч по паре;
#   «DOGE/WETH/USDT» — три символа, то есть ДВА хопа (отсюда hop_index).
# CEX-ноги (Perp OKX/Binance) сохраняем с is_dex = 0: сделок по ним в ClickHouse
# нет и быть не может, но они нужны для печатного маршрута сигнала.
_SIGNALS_LEGS_DDL = f"""
CREATE MATERIALIZED VIEW IF NOT EXISTS signals_legs
REFRESH EVERY {config.SIGNALS_REFRESH}
ENGINE = MergeTree ORDER BY (request_id, leg_index, hop_index) AS
WITH
opps AS (
    SELECT
        toString(o.id)                                 AS request_id,
        fromUnixTimestamp64Milli(toInt64(o.timestamp)) AS ts,
        o.niche                                        AS niche,
        o.direction                                    AS direction,
        toFloat64(o.volume_usd)                        AS volume_usd,
        toFloat64(o.potential_profit)                  AS profit,
        toUInt64(oc.found_block)                       AS found_block,
        toInt64(oc.bribe)                              AS bribe,
        o.metadata                                     AS meta
    FROM {_pg_source('opportunities')} AS o
    INNER JOIN {_pg_source('opportunities_cross')} AS oc ON o.id = oc.id
    WHERE o.timestamp >= (toUnixTimestamp(now()) - {config.SIGNALS_RETENTION_DAYS} * 86400) * 1000
),
legs AS (
    SELECT
        request_id, ts, niche, direction, volume_usd, profit, found_block, bribe,
        leg.1                                              AS leg_index,
        JSONExtractString(leg.2, 'name')                   AS leg_name,
        JSONExtractString(leg.2, 'side')                   AS leg_side,
        JSONExtractFloat(leg.2, 'volume_usd')              AS leg_volume_usd,
        JSONExtractString(leg.2, 'type') = 'DECENTRALIZED' AS is_dex
    FROM (
        SELECT *, arrayJoin(arrayZip(arrayEnumerate(paths), paths)) AS leg
        FROM (SELECT *, JSONExtractArrayRaw(meta, 'paths') AS paths FROM opps)
    )
),
parsed AS (
    SELECT
        *,
        splitByChar(' ', leg_name)           AS parts,
        splitByChar('/', parts[1])           AS syms,
        if(length(parts) >= 2, parts[2], '') AS proto_raw,
        if(length(parts) >= 3, parts[3], '') AS fee_raw,
        if(proto_raw IN ({', '.join(repr(p) for p in sorted(set(config.DEX_FACTORIES.values())))}),
           proto_raw, '')                    AS protocol,
        multiIf(
            endsWith(fee_raw, '%'),
                toNullable(toUInt32(round(toFloat64(replaceAll(fee_raw, '%', '')) * 10000))),
            proto_raw = 'Uv2',
                toNullable(toUInt32(3000)),
            NULL
        )                                    AS fee_tier
    FROM legs
),
hops AS (
    SELECT
        request_id, ts, niche, direction, volume_usd, profit, found_block, bribe,
        leg_index, leg_name, leg_side, leg_volume_usd, is_dex, protocol, fee_tier,
        hop.1 AS hop_index, hop.2 AS sym_in, hop.3 AS sym_out
    FROM (
        SELECT *,
               arrayJoin(if(length(syms) >= 2,
                            arrayMap(i -> (i, syms[i], syms[i + 1]), range(1, length(syms))),
                            [(toUInt64(0), '', '')])) AS hop
        FROM parsed
    )
)
SELECT
    h.request_id                  AS request_id,
    h.ts                          AS ts,
    h.niche                       AS niche,
    h.direction                   AS direction,
    h.volume_usd                  AS volume_usd,
    h.profit                      AS profit,
    h.found_block                 AS found_block,
    h.bribe                       AS bribe,
    toUInt16(h.leg_index)         AS leg_index,
    h.leg_name                    AS leg_name,
    h.leg_side                    AS leg_side,
    h.leg_volume_usd              AS leg_volume_usd,
    toUInt8(h.is_dex)             AS is_dex,
    toUInt16(h.hop_index)         AS hop_index,
    h.sym_in                      AS sym_in,
    h.sym_out                     AS sym_out,
    h.protocol                    AS protocol,
    h.fee_tier                    AS fee_tier,
    if(ifNull(ta.address, '') != '' AND ifNull(tb.address, '') != '',
       least(ta.address, tb.address), '')    AS token_lo,
    if(ifNull(ta.address, '') != '' AND ifNull(tb.address, '') != '',
       greatest(ta.address, tb.address), '') AS token_hi,
    toUInt8(h.protocol != '')     AS pool_bound
FROM hops AS h
LEFT JOIN dim_token_canon AS ta ON ta.symbol = upper(h.sym_in)
LEFT JOIN dim_token_canon AS tb ON tb.symbol = upper(h.sym_out)
"""


def _ensure_dim_token_canon(c) -> None:
    """Пересобрать словарь «символ -> канонический адрес» (см. _DIM_TOKEN_CANON_DDL)."""
    c.command(_DIM_TOKEN_CANON_DDL)
    if not c.query("SELECT count() FROM tokens").result_rows[0][0]:
        return                    # источник пуст — наполнять словарь нечем

    canon = {sym: addr for sym, addr in c.query(_TOKEN_CANON_RANKING).result_rows}
    canon.update({sym.upper(): addr.lower()
                  for sym, addr in config.CANONICAL_TOKEN_OVERRIDES.items()})

    # Пересобираем целиком: словарь маленький, а инкрементальное обновление
    # MergeTree потребовало бы дедупликации на чтении.
    c.command("TRUNCATE TABLE dim_token_canon")
    c.insert("dim_token_canon", list(canon.items()), column_names=["symbol", "address"])


def _ensure_dim_pool_meta(c) -> None:
    """Метаданные пулов для матчинга ног сигнала (см. _DIM_POOL_META_DDL)."""
    if not c.query("SELECT count() FROM swaps").result_rows[0][0]:
        return
    _ensure_refreshable_mv(
        c, "dim_pool_meta", _DIM_POOL_META_DDL,
        "Сигналы не с чем будет сопоставлять до следующего refresh.")


def _ensure_signals_legs(c) -> None:
    """Материализация сигналов из Postgres (см. _SIGNALS_LEGS_DDL)."""
    _ensure_refreshable_mv(
        c, "signals_legs", _SIGNALS_LEGS_DDL,
        "Страница «Сигналы» будет пуста до следующего refresh.")


_client = None
_lock = threading.Lock()
# Схему (представления + досыпка истории) поднимаем один раз за процесс.
_schema_ready = False


def _ensure_analytics(c) -> None:
    """Создать mv_dex_analytics(_data) и догнать данные, отставшие от источника.

    Идемпотентно, дёшево при актуальной таблице:
      1. таблица-приёмник и MV-триггер — CREATE ... IF NOT EXISTS;
      2. сравниваем «докуда посчитано» (max(minute_bucket) приёмника) с «докуда
         есть исходник» (минута максимального block_number в transactions).
         Отставания нет — выходим, ничего не трогая;
      3. иначе досыпаем INSERT ... SELECT чанками по блокам.

    Досыпка начинается с ГРАНИЧНОЙ минуты (max(minute_bucket)), а не со следующей:
    эта минута могла быть посчитана наполовину (её блоки дописались позже). Её
    строки сначала удаляем, потом пересобираем целиком, — так досыпка идемпотентна
    и не задваивает состояния (AggregatingMergeTree повторный INSERT не схлопнул бы,
    а СЛОЖИЛ бы с уже лежащим).
    """
    c.command(_ANALYTICS_TABLE_DDL)

    # Докуда посчитано. count() отдельно: max() по пустой таблице вернёт не NULL,
    # а дефолт DateTime (1970-01-01), и «пусто» было бы не отличить от «древнее».
    done_rows, done_bucket = c.query(
        "SELECT count(), max(minute_bucket) FROM mv_dex_analytics_data"
    ).result_rows[0]

    # Границы источника. Читаем ДО создания MV-триггера: всё, что вставят в swaps
    # после его создания, посчитает сам триггер, и в бэкофилл (block <= src_max)
    # оно уже не попадёт — иначе строки на стыке были бы учтены дважды.
    src_rows, src_min, src_max, src_bucket = c.query(
        f"SELECT count(), min(block_number), max(block_number), "
        f"{_block_bucket('max(block_number)')} "
        f"FROM transactions"
    ).result_rows[0]

    mv_exists = bool(c.query(
        "SELECT 1 FROM system.tables "
        "WHERE database = currentDatabase() AND name = 'mv_dex_analytics'"
    ).result_rows)
    if not mv_exists:
        c.command(_ANALYTICS_MV_DDL)

    if not src_rows:
        return
    if not done_rows:
        # Представления не было (или его чистили) — считаем всю историю.
        lo = src_min
    elif src_bucket > done_bucket:
        # Источник ушёл вперёд: пересобираем с граничной (возможно, неполной) минуты.
        lo = c.query(
            f"SELECT min(block_number) FROM transactions "
            f"WHERE {_block_bucket('block_number')} >= {{b:DateTime}}",
            parameters={"b": done_bucket},
        ).result_rows[0][0]
        if lo is None:
            return
        c.command(
            "ALTER TABLE mv_dex_analytics_data DELETE "
            "WHERE minute_bucket >= {b:DateTime} SETTINGS mutations_sync = 2",
            parameters={"b": done_bucket},
        )
    else:
        return  # представление актуально — источник не ушёл дальше посчитанной минуты

    _backfill_analytics(c, lo, src_max)


def _backfill_analytics(c, lo: int, hi: int) -> None:
    """Посчитать состояния для блоков [lo, hi] чанками по CH_BACKFILL_BLOCK_CHUNK."""
    chunk = max(int(config.CH_BACKFILL_BLOCK_CHUNK), 1)
    total = (hi - lo) // chunk + 1
    print(f"[ClickHouse] mv_dex_analytics: досыпаю блоки {lo}-{hi} "
          f"({total} чанк(ов) по {chunk})")
    for i, start in enumerate(range(lo, hi + 1, chunk), start=1):
        end = min(start + chunk - 1, hi)
        c.command(_ANALYTICS_BACKFILL, parameters={"lo": start, "hi": end})
        print(f"[ClickHouse] mv_dex_analytics: чанк {i}/{total} "
              f"(блоки {start}-{end}) готов")


def _ensure_refreshable_mv(c, name: str, ddl: str, consequence: str) -> None:
    """Создать/мигрировать refreshable MV `name` и дождаться первичного наполнения.

    Идемпотентно: если MV уже есть — не пересоздаём (CREATE ... IF NOT EXISTS).
    Если от прошлых версий остался объект другого типа (обычная VIEW или
    MergeTree-таблица) — снимаем его и создаём заново.

    `consequence` — что будет, если наполнить не удалось; попадёт в предупреждение.
    """
    rows = c.query(
        "SELECT engine FROM system.tables "
        "WHERE database = currentDatabase() AND name = {n:String}",
        parameters={"n": name},
    ).result_rows
    exists_as_mv = bool(rows) and rows[0][0] == "MaterializedView"
    if rows and not exists_as_mv:
        c.command(f"DROP TABLE IF EXISTS {name}")
    c.command(ddl)
    _wait_refreshable(c, name, consequence)


def _wait_refreshable(c, name: str, consequence: str) -> None:
    """Дождаться первичного наполнения refreshable MV (refresh идёт на сервере).

    Ждём ПО ФАКТУ наполненности, а не по факту «MV только что создали»: вьюха
    может существовать с прошлого запуска и быть пустой (первичный refresh упал
    или был прерван) — тогда дашборд молча работал бы на пустом словаре.
    Признак готовности — непустая таблица: refresh атомарен (таблица заменяется
    целиком), полузаполненного состояния не бывает.

    Опрашиваем таблицу, а не `SYSTEM WAIT VIEW`: этой команды нет в ClickHouse
    24.8 (на котором крутится eywa) — там она даёт SYNTAX_ERROR.

    Не падаем ни по таймауту, ни по ошибке refresh: дашборд должен подниматься и
    без словаря, а следующий refresh наполнит его сам. Ошибку (например, «Postgres
    недоступен» в окно репликации torch) печатаем и выходим сразу, не выжидая
    таймаут впустую.
    """
    deadline = time.monotonic() + config.CH_DIM_WAIT_TIMEOUT
    while time.monotonic() < deadline:
        if c.query(f"SELECT count() FROM {name}").result_rows[0][0]:
            return
        failure = _refresh_failure(c, name)
        if failure:
            print(f"[ClickHouse] {name}: refresh не удался — {failure}. {consequence}")
            return
        time.sleep(1)

    print(f"[ClickHouse] {name}: пуста спустя {config.CH_DIM_WAIT_TIMEOUT} c. {consequence}")
    _report_refresh_state(c, name)


def _refresh_failure(c, name: str) -> str | None:
    """Текст ошибки последнего refresh (или None, если ошибки нет / статус неизвестен).

    Набор колонок system.view_refreshes разнится между версиями ClickHouse, поэтому
    ищем поле с 'exception' в имени, а не жёсткое имя колонки.
    """
    try:
        res = c.query(
            "SELECT * FROM system.view_refreshes "
            "WHERE database = currentDatabase() AND view = {n:String}",
            parameters={"n": name},
        )
    except Exception:             # noqa: BLE001 — диагностика не должна ронять старт
        return None
    for row in res.result_rows:
        state = dict(zip(res.column_names, row))
        for key, value in state.items():
            if "exception" in key.lower() and value:
                return str(value)
    return None


def _report_refresh_state(c, name: str) -> None:
    """Напечатать строку system.view_refreshes по вьюхе (диагностика при таймауте)."""
    try:
        res = c.query(
            "SELECT * FROM system.view_refreshes "
            "WHERE database = currentDatabase() AND view = {n:String}",
            parameters={"n": name},
        )
        for row in res.result_rows:
            print(f"[ClickHouse] system.view_refreshes: "
                  f"{dict(zip(res.column_names, row))}")
    except Exception as exc:      # noqa: BLE001 — диагностика не должна ронять старт
        print(f"[ClickHouse] system.view_refreshes недоступна: {exc}")


def _ensure_dim_pool_pair(c) -> None:
    """Словарь подписей пар (pool_address -> «WETH/USDC»)."""
    if not c.query("SELECT count() FROM swaps").result_rows[0][0]:
        return                    # источник пуст — наполнять словарь нечем
    _ensure_refreshable_mv(
        c, "dim_pool_pair", _DIM_POOL_PAIR_DDL,
        "Пулы подпишутся 'unknown' до следующего refresh.")


def _new_client(send_receive_timeout: int | None = None):
    """Новое HTTP-подключение к ClickHouse."""
    import clickhouse_connect

    kwargs = {}
    if send_receive_timeout is not None:
        kwargs["send_receive_timeout"] = send_receive_timeout
    return clickhouse_connect.get_client(
        host=CLICKHOUSE_HOST,
        port=CLICKHOUSE_PORT,
        username=CLICKHOUSE_USER,
        password=CLICKHOUSE_PASSWORD,
        database=CLICKHOUSE_DB,
        **kwargs,
    )


def _bootstrap_schema() -> None:
    """Поднять схему один раз за процесс: аналитическое представление + dim.

    Идёт по ОТДЕЛЬНОМУ подключению с длинным таймаутом (config.CH_SCHEMA_TIMEOUT):
    первичная досыпка истории и первичный refresh dim-таблицы — минуты, а рабочему
    клиенту такой таймаут не нужен (там короткие запросы дашборда).
    """
    global _schema_ready
    c = _new_client(send_receive_timeout=config.CH_SCHEMA_TIMEOUT)
    try:
        _ensure_analytics(c)
        _ensure_dim_pool_pair(c)
        # Порядок важен: signals_legs резолвит символы токенов через
        # dim_token_canon, а матчится потом на dim_pool_meta.
        _ensure_dim_token_canon(c)
        _ensure_dim_pool_meta(c)
        _ensure_signals_legs(c)
        _schema_ready = True
    finally:
        c.close()


def _get_client():
    """Ленивая инициализация клиента (схема к этому моменту уже поднята)."""
    global _client
    if _client is None:
        if not _schema_ready:
            _bootstrap_schema()
        _client = _new_client()
    return _client


def ensure_schema():
    """Принудительно создать представления и догнать отставшие данные.

    Вызывается на старте приложения (app.py), чтобы стоимость первичной сборки не
    легла на первое обновление пользователя. На заглушках (USE_STUB) — no-op.
    """
    if USE_STUB:
        return
    with _lock:
        _get_client()


def execute(query: str, params: dict | None = None):
    """Выполнить параметризованный запрос и вернуть `pandas.DataFrame`.

    Параметры подставляются на стороне сервера (`{name:Type}`), что исключает
    SQL-инъекции при передаче списков адресов.
    """
    with _lock:
        client = _get_client()
        return client.query_df(query, parameters=params or {})
