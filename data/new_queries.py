#-------------------------------------------------------------
import pandas as pd
import numpy as np
from datetime import datetime

from data import clickhouse


def get_trades(pair_blocks, use_stub: bool = False, window_size: int = 2) -> pd.DataFrame:
    """
    Извлекает сделки ВСЕХ трейдеров и ВСЕХ пар из ClickHouse (БД eywa) в нужных блоках.

    Фильтров по traders/парам НЕТ — чтобы матчинг мог находить произвольные
    альтернативные маршруты через любые токены. Память держит батчинг сверху
    (см. signals_service.iter_signal_matches): набор блоков ограничен размером батча.

    :param pair_blocks: Набор кортежей (token_lo, token_hi, block_number) —
        выход matching.signal_pair_blocks; здесь используются только блоки (пары
        больше не фильтруются, но интерфейс сохранён).
    :param use_stub: Флаг использования заглушки (генерация фейковых данных).
    :param window_size: Окно блоков (± от искомого) для набора нужных блоков.
    :return: pd.DataFrame с нормализованными данными для маппинга.
    """

    # Если на входе пусто, возвращаем пустой DataFrame с нужной структурой.
    # side — направление свопа (sell: token_a→token_b, buy: token_b→token_a);
    # matching строит по нему ОРИЕНТИРОВАННЫЙ граф токенов при поиске покрытия.
    columns = [
        'block_number', 'token_a', 'token_b', 'usd_amount',
        'trader_address', 'bribe', 'priority_fee', 'side', 'swap_timestamp'
    ]

    if not pair_blocks:
        return pd.DataFrame(columns=columns)

    # Точный набор нужных блоков: для каждого блока сигнала разворачиваем окно
    # [block-window, block+window]. Именно этот НАБОР (а не сплошной диапазон
    # min..max в сотни тысяч блоков) уходит в БД — прунится по PK transactions,
    # так выборка ограничена реально нужными блоками (защита от OOM без trader-фильтра).
    blocks = [block for *_, block in pair_blocks]
    block_set = sorted({b for base in blocks
                        for b in range(max(base - window_size, 0), base + window_size + 1)})
    # min/max — только для генерации фейков в stub-ветке ниже.
    min_block = block_set[0]
    max_block = block_set[-1]

    if use_stub:
        # --- ЗАГЛУШКА (MOCK DATA) ---
        print(f"[STUB] Генерация фейковых сделок для блоков {min_block} - {max_block}")

        # Генерируем случайное количество сделок
        num_mock_trades = np.random.randint(10, 50)

        mock_data = {
            'block_number': np.random.randint(min_block, max_block + 1, num_mock_trades),
            'token_a': ['0x' + ''.join(np.random.choice(list('0123456789abcdef'), 40)) for _ in range(num_mock_trades)],
            'token_b': ['0x' + ''.join(np.random.choice(list('0123456789abcdef'), 40)) for _ in range(num_mock_trades)],
            'usd_amount': np.random.uniform(1000.0, 500000.0, num_mock_trades),
            'trader_address': ['0x' + ''.join(np.random.choice(list('0123456789abcdef'), 40)) for _ in range(num_mock_trades)],
            'bribe': [str(np.random.randint(10**15, 10**18)) for _ in range(num_mock_trades)],
            'priority_fee': [str(np.random.randint(10**14, 10**16)) for _ in range(num_mock_trades)],
            'side': list(np.random.choice(['sell', 'buy'], num_mock_trades)),
        }

        df = pd.DataFrame(mock_data)

        # Эмуляция расчета timestamp по формуле из queries.py
        base_ts = 1775121779
        base_block = 24791000
        df['swap_timestamp'] = df['block_number'].apply(
            lambda b: datetime.fromtimestamp(base_ts + (b - base_block) * 12.0376)
        )
        return df

    # --- БОЕВОЙ ЗАПРОС К CLICKHOUSE ---
    # Тянем сделки ВСЕХ трейдеров и ВСЕХ пар в нужных блоках (без фильтров по
    # traders/парам) — чтобы матчинг мог находить произвольные альтернативные
    # маршруты через любые токены. Память держит батчинг сверху (см. signals_service):
    # набор блоков ограничен, поэтому и выборка ограничена.
    #  - block_number IN {blocks} — точечный набор, прунится по PK transactions;
    #  - адреса → lower; bribe/priority_fee → toString (защита от переполнения);
    #  - side: Enum8('BUY'=1,'SELL'=2) → toString → lower ('buy'/'sell');
    #  - swap_timestamp считается на стороне БД по номеру блока.
    query = """
        SELECT
            t.block_number AS block_number,
            lower(s.token_a_address) AS token_a,
            lower(s.token_b_address) AS token_b,
            s.usd_amount AS usd_amount,
            lower(t.trader_address) AS trader_address,
            toString(t.bribe) AS bribe,
            toString(t.priority_fee) AS priority_fee,
            lower(toString(s.side)) AS side,
            toDateTime(1775121779 + (t.block_number - 24791000) * 12.0376) AS swap_timestamp
        FROM swaps s
        JOIN transactions t ON s.transaction_hash_id = t.hash_id
        WHERE t.block_number IN {blocks:Array(UInt64)}
          AND s.token_a_address IS NOT NULL
          AND s.token_b_address IS NOT NULL
    """

    # clickhouse-connect на пустом результате отдаёт DataFrame без единой
    # колонки (shape (0, 0)) — приводим к контракту (те же именованные columns,
    # что и в guard-ветке "пустой pair_blocks" выше), иначе matching.build_matches
    # падает на t["token_a"] с KeyError.
    result = clickhouse.execute(query, {"blocks": block_set})
    return result if not result.empty else pd.DataFrame(columns=columns)
