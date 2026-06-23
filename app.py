"""Точка входа дашборда ChainBI (Taipy GUI).

Запуск:  python app.py

Реализует автообновление: фоновый daemon-поток раз в config.REFRESH_SECONDS
вызывает refresh_all для каждого подключённого клиента через invoke_callback.
Состояние per-client, поэтому каждый клиент перечитывает данные со своими
фильтрами.
"""

from __future__ import annotations

import threading
import time

from taipy.gui import Gui, get_state_id, invoke_callback

import callbacks
import config
from data import clickhouse
# Импортируем всё пространство имён страницы в __main__: Taipy ищет
# привязываемые переменные (sharks, data_*, fig_* …), хелперы выражений
# (chip_label) и колбэки именно в модуле, где создаётся Gui. Поэтому имена
# должны существовать здесь, в __main__.
from pages.main_page import *  # noqa: F401,F403
from pages.main_page import page

# Множество идентификаторов подключённых клиентов (для автообновления).
_clients: set[str] = set()

gui = Gui(pages={"/": page}, css_file="assets/main.css")


def on_init(state):
    """Регистрируем клиента и делаем первичную загрузку данных."""
    _clients.add(get_state_id(state))
    callbacks.on_init(state)


def _auto_refresh_loop():
    """Фоновое автообновление всех подключённых клиентов."""
    while True:
        time.sleep(config.REFRESH_SECONDS)
        for client_id in list(_clients):
            try:
                invoke_callback(gui, client_id, callbacks.refresh_all, [])
            except Exception:
                # Клиент отключился — убираем из набора.
                _clients.discard(client_id)


if __name__ == "__main__":
    # Разовая сборка dim-таблицы заранее, чтобы её ~0.5 c не падали на первое
    # обновление пользователя (на заглушках подключение к БД не нужно).
    if not clickhouse.USE_STUB:
        clickhouse.ensure_schema()
    threading.Thread(target=_auto_refresh_loop, daemon=True).start()
    gui.run(
        title="ChainBI — DEX Analytics",
        dark_mode=False,
        use_reloader=False,
    )
