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


def _ensure_dim_pool_pair(c) -> None:
    """Создать/мигрировать dim_pool_pair как refreshable MV и наполнить данными.

    Идемпотентно: если MV уже есть — не пересоздаём (CREATE ... IF NOT EXISTS).
    Если от прошлых версий остался объект другого типа (обычная VIEW или
    MergeTree-таблица) — снимаем его и создаём заново. Затем ждём, пока refresh
    наполнит словарь, чтобы запросы сразу видели подписи пар (см. _wait_dim_pool_pair).
    """
    rows = c.query(
        "SELECT engine FROM system.tables "
        "WHERE database = currentDatabase() AND name = 'dim_pool_pair'"
    ).result_rows
    exists_as_mv = bool(rows) and rows[0][0] == "MaterializedView"
    if rows and not exists_as_mv:
        c.command("DROP TABLE IF EXISTS dim_pool_pair")
    c.command(_DIM_POOL_PAIR_DDL)
    _wait_dim_pool_pair(c)


def _wait_dim_pool_pair(c) -> None:
    """Дождаться первичного наполнения dim_pool_pair (refresh идёт на сервере).

    Ждём ПО ФАКТУ наполненности, а не по факту «MV только что создали»: вьюха
    может существовать с прошлого запуска и быть пустой (первичный refresh упал
    или был прерван) — тогда дашборд молча подписал бы все пулы как «unknown».
    Признак готовности — непустая таблица: refresh у refreshable MV атомарен
    (таблица заменяется целиком), полузаполненного состояния не бывает.

    Опрашиваем таблицу, а не `SYSTEM WAIT VIEW`: этой команды нет в ClickHouse
    24.8 (на котором крутится eywa) — там она даёт SYNTAX_ERROR.

    По таймауту НЕ падаем: дашборд поднимется без словаря (пулы — «unknown»,
    метрики «по парам» пустые), а следующий refresh наполнит его сам.
    """
    if not c.query("SELECT count() FROM swaps").result_rows[0][0]:
        return                    # источник пуст — наполнять словарь нечем

    deadline = time.monotonic() + config.CH_DIM_WAIT_TIMEOUT
    while time.monotonic() < deadline:
        if c.query("SELECT count() FROM dim_pool_pair").result_rows[0][0]:
            return
        time.sleep(1)

    print(f"[ClickHouse] dim_pool_pair: пуста спустя {config.CH_DIM_WAIT_TIMEOUT} c "
          f"— подписи пар будут 'unknown' до следующего refresh")
    try:
        refresh = c.query(
            "SELECT * FROM system.view_refreshes "
            "WHERE database = currentDatabase() AND view = 'dim_pool_pair'"
        )
        for row in refresh.result_rows:
            print(f"[ClickHouse] system.view_refreshes: "
                  f"{dict(zip(refresh.column_names, row))}")
    except Exception as exc:      # noqa: BLE001 — диагностика не должна ронять старт
        print(f"[ClickHouse] system.view_refreshes недоступна: {exc}")


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
