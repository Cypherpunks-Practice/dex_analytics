"""Запросы сигналов (swaps_request) из Postgres.

`get_signals_df` возвращает сырые кортежи из БД (исходный интерфейс);
`get_signals` — обёртка под контракт `data/matching.py`: pandas DataFrame
с колонками [request_id, ts, base_token, quote_token, quote_amount, bribe,
found_block, route, profit].

Подключение ленивое: создаётся при первом запросе (и пересоздаётся, если
соединение закрыто), поэтому импорт модуля без живого Postgres безопасен.
Все значения фильтров передаются параметрами %s, не f-строками.
"""

import json
import os
import threading

import pandas as pd
import psycopg2

_pg_connection = None
# Единственное соединение psycopg2 не потокобезопасно для параллельных курсоров.
# Прогрессивная загрузка идёт в фоновом потоке и при смене фильтров может на миг
# наложиться на прежнюю — сериализуем доступ к соединению этим локом.
_pg_lock = threading.Lock()


def _get_connection():
    """Ленивое подключение к Postgres (+реконнект, если соединение закрыто).

    Если сервер локализован (напр. русский Windows-инсталлятор PG шлёт
    ошибки в cp1251), psycopg2 падает сырым UnicodeDecodeError вместо
    OperationalError с текстом — здесь перехватываем это и перевыбрасываем
    как OperationalError с читаемым сообщением (utf-8 -> cp1251 -> lossy).
    """
    global _pg_connection
    if _pg_connection is None or _pg_connection.closed:
        try:
            _pg_connection = psycopg2.connect(
                host=os.getenv("PG_HOST", "localhost"),
                port=int(os.getenv("PG_PORT", "5432")),
                database=os.getenv("PG_DB", "mydb"),
                user=os.getenv("PG_USER", "postgres"),
                password=os.getenv("PG_PASSWORD", "121205"),
            )
            # Читаем только SELECT-ами, транзакции не нужны. Без autocommit первый
            # же упавший запрос оставляет соединение в INFAILEDTRANSACTION (при этом
            # closed == 0, т.е. реконнекта не будет), и ВСЕ следующие запросы отвечают
            # «current transaction is aborted», маскируя исходную ошибку. Заодно
            # соединение не висит в «idle in transaction».
            _pg_connection.autocommit = True
        except UnicodeDecodeError as exc:
            msg = exc.object.decode("cp1251", errors="replace")
            raise psycopg2.OperationalError(msg) from exc
    return _pg_connection


def get_signals_df(limit=50, min_timestamp=0, max_timestamp=0xffffffffffffffff,
                   timestamp_increase=None, tokens_a_list=None, tokens_b_list=None,
                   min_amount=0, max_amount=0xffffffffffffffff, amount_increase=None,
                   min_profit=0, max_profit=0xffffffffffffffff, profit_increase=None):
    orderby = "ORDER BY"
    if timestamp_increase == True:
        orderby += " timestamp asc"
    elif timestamp_increase == False:
        orderby += " timestamp desc"
    elif amount_increase == True:
        orderby += " quote_amount asc"
    elif amount_increase == False:
        orderby += " quote_amount desc"
    elif profit_increase == True:
        orderby += " potential_profit asc"
    elif profit_increase == False:
        orderby += " potential_profit desc"
    else:
        orderby = ""

    params = [min_timestamp, max_timestamp, min_amount, max_amount,
              min_profit, max_profit]
    tokens_a = ""
    if tokens_a_list:
        tokens_a = "AND base_token IN (" + ", ".join(["%s"] * len(tokens_a_list)) + ") "
        params.extend(tokens_a_list)
    tokens_b = ""
    if tokens_b_list:
        tokens_b = "AND quote_token IN (" + ", ".join(["%s"] * len(tokens_b_list)) + ") "
        params.extend(tokens_b_list)
    params.append(limit)

    with _pg_lock:
        cursor = _get_connection().cursor()
        cursor.execute(f'''SELECT swaps_request.id, swaps_request.timestamp, base_token,
                        quote_token, quote_amount, bribe, found_block, route, potential_profit
                        FROM arbitrages JOIN swaps_request ON arbitrages.id=swaps_request.arbitrage_id
                        JOIN swaps_request_dex ON swaps_request_dex.id = swaps_request.id
                        WHERE type = 'DECENTRALIZED' AND
                        swaps_request.timestamp > %s AND swaps_request.timestamp < %s AND
                        quote_amount > %s AND quote_amount < %s AND
                        potential_profit > %s AND potential_profit < %s
                        {tokens_a}{tokens_b}
                        {orderby}
                        LIMIT %s;''', params)
        result = cursor.fetchall()
        cursor.close()
    return result


def get_max_timestamp():
    """Максимальная метка времени DEX-сигналов — анкер окон фильтра «Дата».

    Возвращает сырое число в единицах БД (сек/мс, как колонка `timestamp`) или
    None, если сигналов нет. FROM/WHERE повторяют `get_signals_df`, чтобы анкер
    считался только по тем же строкам (type='DECENTRALIZED'), от которых потом
    отсчитывается окно в `signals_service`.
    """
    with _pg_lock:
        cursor = _get_connection().cursor()
        cursor.execute('''SELECT max(swaps_request.timestamp)
                        FROM arbitrages JOIN swaps_request ON arbitrages.id=swaps_request.arbitrage_id
                        JOIN swaps_request_dex ON swaps_request_dex.id = swaps_request.id
                        WHERE type = 'DECENTRALIZED';''')
        row = cursor.fetchone()
        cursor.close()
    return row[0] if row and row[0] is not None else None


# Порядок колонок соответствует SELECT в get_signals_df.
_SIGNAL_COLS = ["request_id", "ts", "base_token", "quote_token", "quote_amount",
                "bribe", "found_block", "route", "profit"]


def get_signals(n=50, **kwargs) -> pd.DataFrame:
    """Сигналы из Postgres в формате контракта `data/matching.py`.

    Нормализация: `ts` → datetime (числовой epoch: секунды/миллисекунды
    определяются по величине), `route` → list[dict] (json.loads, если БД отдала
    строку), числовые типы found_block/quote_amount фиксируются.
    """
    df = pd.DataFrame(get_signals_df(limit=n, **kwargs), columns=_SIGNAL_COLS)
    if df.empty:
        return df
    if not pd.api.types.is_datetime64_any_dtype(df["ts"]):
        ts = pd.to_numeric(df["ts"])
        unit = "ms" if float(ts.max()) > 1e12 else "s"
        df["ts"] = pd.to_datetime(ts, unit=unit)
    df["route"] = df["route"].apply(
        lambda r: json.loads(r) if isinstance(r, str) else r)
    df["found_block"] = df["found_block"].astype("int64")
    df["quote_amount"] = df["quote_amount"].astype(float)

    return df
