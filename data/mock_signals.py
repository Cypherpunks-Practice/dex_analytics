import pandas as pd
import random
from datetime import datetime, timedelta

def get_mock_signals(count=50):
    """Генерирует тестовые данные для таблицы сигналов."""
    
    tokens = ['ETH', 'BTC', 'USDC', 'DAI', 'WBTC', 'UNI', 'LINK', 'AAVE']
    users = ['0x1234...5678', '0x9abc...def0', '0x1111...2222', '0x3333...4444']
    
    data = []
    now = datetime.now()
    
    for i in range(count):
        signal_amount = random.randint(100, 10000)
        num_swaps = random.randint(0, 3)
        
        if num_swaps == 0:
            # Сигнал без сделок
            data.append({
                'signal_timestamp': (now - timedelta(hours=i * 2)).isoformat(),
                'token_a': random.choice(tokens),
                'token_b': random.choice(tokens),
                'signal_amount': signal_amount,
                'signal_bribe': random.randint(0, 50) if random.random() > 0.7 else None,
                'signal_fee': random.randint(1, 20),
                'swap_timestamp': None,
                'swap_amount': None,
                'swap_user_id': None,
                'swap_bribe': None,
                'swap_fee': None,
            })
        else:
            # Сигнал с несколькими сделками
            for j in range(num_swaps):
                swap_amount = random.randint(50, signal_amount)
                data.append({
                    'signal_timestamp': (now - timedelta(hours=i * 2)).isoformat(),
                    'token_a': random.choice(tokens),
                    'token_b': random.choice(tokens),
                    'signal_amount': signal_amount,
                    'signal_bribe': random.randint(0, 50) if random.random() > 0.7 else None,
                    'signal_fee': random.randint(1, 20),
                    'swap_timestamp': (now - timedelta(hours=i * 2 + random.randint(0, 10))).isoformat(),
                    'swap_amount': swap_amount,
                    'swap_user_id': random.choice(users),
                    'swap_bribe': random.randint(0, 30) if random.random() > 0.8 else None,
                    'swap_fee': random.randint(1, 10),
                })
    
    return pd.DataFrame(data)