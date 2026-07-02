#-------------------------------------------------------------
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

def get_trades(pair_blocks: set[tuple[str, int]], client=None, use_stub: bool = False, window_size: int = 2) -> pd.DataFrame:
    """
    Извлекает сделки акул/китов из ClickHouse (БД eywa) в окне вокруг переданных блоков.
    
    :param pair_blocks: Набор кортежей вида (pair_key, block_number).
    :param client: Экземпляр клиента clickhouse_connect.
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

    # Определяем минимальный и максимальный блок для bulk-запроса, расширяем на window_size
    blocks = [block for _, block in pair_blocks]
    min_block = min(blocks) - window_size
    max_block = max(blocks) + window_size

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
    if not client:
        raise ValueError("Не передан клиент ClickHouse, а use_stub=False")

    # SQL запрос составлен с учетом требований:
    # 1. Тянем из сырых swaps JOIN transactions (НЕ mv)
    # 2. Фильтр по traders (label IN 'shark', 'whale') через подзапрос для скорости
    # 3. Приведение адресов к нижнему регистру (lower)
    # 4. Каст bribe и priority_fee в String (toString) для защиты от переполнения
    # 5. Вычисление swap_timestamp прямо на стороне БД
    query = f"""
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
        WHERE t.block_number >= {min_block} AND t.block_number <= {max_block}
          AND s.token_a_address IS NOT NULL 
          AND s.token_b_address IS NOT NULL
          AND t.trader_address IN (
              SELECT address FROM traders WHERE label IN ('shark', 'whale')
          )
    """
    
    # Выполняем запрос. Метод query_df автоматически возвращает pandas DataFrame
    trades_df = client.query_df(query)
    
    return trades_df