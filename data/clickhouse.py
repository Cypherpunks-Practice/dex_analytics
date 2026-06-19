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

# Переключатель источника данных. False → реальная БД, True → заглушки.
USE_STUB = False

# Параметры подключения (можно переопределить через переменные окружения).
CLICKHOUSE_HOST = os.getenv("CH_HOST", "localhost")
CLICKHOUSE_PORT = int(os.getenv("CH_PORT", "8123"))
CLICKHOUSE_USER = os.getenv("CH_USER", "default")
CLICKHOUSE_PASSWORD = os.getenv("CH_PASSWORD", "qwerty")
CLICKHOUSE_DB = os.getenv("CH_DB", "eywa")

# Вспомогательное представление: pool_address -> подпись пары токенов
# (`WETH/USDC`). Самого агрегирующего представления mv_dex_analytics_data в БД
# для аналитики рынка достаточно, но в нём нет читаемых подписей пар — их даёт
# этот dim-вьюшка. Создаётся идемпотентно при первом подключении.
_DIM_POOL_PAIR_DDL = """
CREATE VIEW IF NOT EXISTS dim_pool_pair AS
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


def _get_client():
    """Ленивая инициализация клиента + создание dim-представления."""
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
        # Идемпотентно — представление создаётся только если его ещё нет.
        c.command(_DIM_POOL_PAIR_DDL)
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
