#-------------------------------------------------------------
import pandas as pd
import numpy as np
from datetime import datetime

from data import clickhouse


def get_trades(pair_blocks, use_stub: bool = False, window_size: int = 2) -> pd.DataFrame:
    """
    Извлекает сделки акул/китов из ClickHouse (БД eywa) в окне вокруг переданных блоков.

    :param pair_blocks: Набор кортежей (token_lo, token_hi, block_number) —
        выход matching.signal_pair_blocks: адреса пары в нижнем регистре,
        канонический порядок lo <= hi.
    :param use_stub: Флаг использования заглушки (генерация фейковых данных).
    :param window_size: Окно блоков (+- от искомого) для расширения диапазона поиска.
    :return: pd.DataFrame с нормализованными данными для маппинга.
    """

    # Если на входе пусто, возвращаем пустой DataFrame с нужной структурой
    columns = [
        'block_number', 'token_a', 'token_b', 'usd_amount',
        'trader_address', 'bribe', 'priority_fee', 'swap_timestamp'
    ]

    if not pair_blocks:
        return pd.DataFrame(columns=columns)

    # Диапазон блоков для bulk-запроса (расширен на window_size) + канонические
    # ключи пар "lo|hi" для пушдауна фильтра пар на сторону БД.
    blocks = [block for *_, block in pair_blocks]
    min_block = max(min(blocks) - window_size, 0)
    max_block = max(blocks) + window_size
    pair_keys = sorted({f"{lo}|{hi}" for lo, hi, _ in pair_blocks})

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
    # SQL запрос составлен с учетом требований:
    # 1. Тянем из сырых swaps JOIN transactions (НЕ mv)
    # 2. Фильтр по traders (label IN 'shark', 'whale') через подзапрос для скорости
    # 3. Приведение адресов к нижнему регистру (lower)
    # 4. Каст bribe и priority_fee в String (toString) для защиты от переполнения
    # 5. Вычисление swap_timestamp прямо на стороне БД
    # 6. Диапазон блоков и пары — серверными параметрами (никаких f-строк);
    #    пара свопа канонизируется как least|greatest и сверяется с pair_keys,
    #    чтобы не тянуть чужие пары из того же диапазона блоков.
    query = """
        SELECT
            t.block_number AS block_number,
            lower(s.token_a_address) AS token_a,
            lower(s.token_b_address) AS token_b,
            s.usd_amount AS usd_amount,
            lower(t.trader_address) AS trader_address,
            toString(t.bribe) AS bribe,
            toString(t.priority_fee) AS priority_fee,
            toDateTime(1775121779 + (t.block_number - 24791000) * 12.0376) AS swap_timestamp
        FROM swaps s
        JOIN transactions t ON s.transaction_hash_id = t.hash_id
        WHERE t.block_number >= {minb:UInt64} AND t.block_number <= {maxb:UInt64}
          AND s.token_a_address IS NOT NULL
          AND s.token_b_address IS NOT NULL
          AND has({pairs:Array(String)},
                  concat(least(lower(s.token_a_address), lower(s.token_b_address)), '|',
                         greatest(lower(s.token_a_address), lower(s.token_b_address))))
          AND lower(t.trader_address) IN (
              SELECT lower(contract_address) FROM traders
              WHERE label IN ('shark', 'whale')
          )
    """

    # clickhouse-connect на пустом результате отдаёт DataFrame без единой
    # колонки (shape (0, 0)) — приводим к контракту (те же именованные columns,
    # что и в guard-ветке "пустой pair_blocks" выше), иначе matching.build_matches
    # падает на t["token_a"] с KeyError.
    result = clickhouse.execute(
        query, {"minb": min_block, "maxb": max_block, "pairs": pair_keys})
    return result if not result.empty else pd.DataFrame(columns=columns)
