"""Коннектор к ClickHouse.

ЗАГЛУШКА. Сейчас данные берутся из `data/stubs.py`, реального подключения нет.
Когда дойдём до интеграции с БД — здесь появится пул соединений и исполнитель
запросов, а `data/queries.py` переключится с заглушек на `execute()`.
"""

from __future__ import annotations

# Пока работаем на заглушках. Переключение на реальную БД — смена флага
# и реализация execute().
USE_STUB = True

# Параметры подключения (заполнить при интеграции).
CLICKHOUSE_HOST = "localhost"
CLICKHOUSE_PORT = 9000
CLICKHOUSE_DB = "dex"


def execute(query: str, params: dict | None = None):
    """Выполнить параметризованный запрос к ClickHouse.

    ВАЖНО (на будущее): списки адресов передавать ТОЛЬКО через params,
    например `WHERE player IN (%(players)s)`, и никогда не интерполировать
    адреса в текст SQL.
    """
    raise NotImplementedError(
        "Подключение к ClickHouse ещё не реализовано. "
        "Сейчас используются заглушки из data/stubs.py (USE_STUB=True)."
    )
