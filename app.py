from __future__ import annotations
import threading
import time
from taipy.gui import Gui, get_state_id, invoke_callback, navigate
import callbacks
import config
from data import clickhouse
from pages.main_page import *
from pages.main_page import page
from pages.login_page import *
from pages.login_page import login_page

_clients: set[str] = set()

def on_navigate(state, page_name, params):
    if page_name == "dashboard" and not state.logged_in:
        return "/"
    return page_name

def on_init(state):
    _clients.add(get_state_id(state))
    callbacks.on_init(state)

def _auto_refresh_loop():
    while True:
        time.sleep(config.REFRESH_SECONDS)
        for client_id in list(_clients):
            try:
                invoke_callback(gui, client_id, callbacks.refresh_all, [])
            except Exception:
                _clients.discard(client_id)

gui = Gui(
    pages={
        "/": login_page,
        "dashboard": page,
    },
    css_file="assets/main.css",
)

if __name__ == "__main__":
    if not clickhouse.USE_STUB:
        clickhouse.ensure_schema()
    threading.Thread(target=_auto_refresh_loop, daemon=True).start()
    gui.run(
        title="ChainBI — DEX Analytics",
        dark_mode=False,
        use_reloader=False,
        on_navigate=on_navigate,
        on_init=on_init,
    )