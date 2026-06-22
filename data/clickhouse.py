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

try:
    # Подхватываем переменные из файла `.env` (если он есть) до чтения os.getenv.
    # Отсутствие python-dotenv не критично — тогда берутся системные переменные.
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# Переключатель источника данных. False → реальная БД, True → заглушки.
USE_STUB = False

# Параметры подключения (можно переопределить через переменные окружения / .env).
CLICKHOUSE_HOST = os.getenv("CH_HOST", "localhost")
CLICKHOUSE_PORT = int(os.getenv("CH_PORT", "8123"))
CLICKHOUSE_USER = os.getenv("CH_USER", "default")
CLICKHOUSE_PASSWORD = os.getenv("CH_PASSWORD", "qwerty")
CLICKHOUSE_DB = os.getenv("CH_DB", "eywa")

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


def _ensure_dim_pool_pair(c) -> None:
    """Создать/мигрировать dim_pool_pair как refreshable MV и наполнить данными.

    Идемпотентно: если MV уже есть — не пересоздаём (CREATE ... IF NOT EXISTS).
    Если от прошлых версий остался объект другого типа (обычная VIEW или
    MergeTree-таблица) — снимаем его и создаём заново. После первого создания
    ждём окончания первичного refresh (SYSTEM WAIT VIEW), чтобы запросы сразу
    видели подписи пар, а не пустой словарь.
    """
    rows = c.query(
        "SELECT engine FROM system.tables "
        "WHERE database = currentDatabase() AND name = 'dim_pool_pair'"
    ).result_rows
    exists_as_mv = bool(rows) and rows[0][0] == "MaterializedView"
    if rows and not exists_as_mv:
        c.command("DROP TABLE IF EXISTS dim_pool_pair")
    c.command(_DIM_POOL_PAIR_DDL)
    if not exists_as_mv:
        # Дождаться первичного наполнения (запускается при создании без EMPTY).
        c.command("SYSTEM WAIT VIEW dim_pool_pair")


def _get_client():
    """Ленивая инициализация клиента + создание dim-сущности."""
    global _client
    if _client is None:
        import clickhouse_connect

        c = clickhouse_connect.get_client(
            host=CLICKHOUSE_HOST,
            port=CLICKHOUSE_PORT,
            username=CLICKHOUSE_USER,
            password=CLICKHOUSE_PASSWORD,
            database=CLICKHOUSE_DB,
        )
        _ensure_dim_pool_pair(c)
        _client = c
    return _client


def ensure_schema():
    """Принудительно подключиться и создать вспомогательные представления."""
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
