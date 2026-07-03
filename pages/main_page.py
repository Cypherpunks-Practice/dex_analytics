"""Главная страница дашборда (Taipy Python Builder, tgb).

Содержит:
- объявления всех привязываемых state-переменных с начальными значениями
  (Taipy биндит переменные из модуля, где определена страница);
- вспомогательные функции, используемые в выражениях шаблона (chip_label);
- сборку страницы: сворачиваемый сайдбар с фильтрами + три секции контента.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from taipy.gui import builder as tgb

# Импортируем колбэки голыми именами: Taipy резолвит on_action/on_change по
# имени функции в пространстве имён модуля страницы, поэтому имена должны быть
# доступны напрямую (а не как callbacks.add_include_shark).
from callbacks import (
    add_exclude_pool_shark,
    add_exclude_trade_shark,
    add_include_shark,
    add_pool,
    admin_create,
    admin_delete,
    admin_demote,
    admin_promote,
    apply_signals_filters,
    change_signals_page_size,
    close_admin_dialog,
    export_signals_csv,
    login,
    logout,
    next_signals_page,
    on_change_refresh,
    on_signal_row_click,
    open_admin_create,
    open_admin_delete,
    open_admin_role,
    open_admin_users,
    prev_signals_page,
    rebuild_area1,
    rebuild_pie,
    remove_exclude_pool_shark,
    remove_exclude_trade_shark,
    remove_include_shark,
    remove_pool,
    reset_signals_filters,
    show_dashboard,
    show_signals,
    toggle_metric,
    toggle_sidebar,
)
import config


# ---------------------------------------------------------------------------
# Привязываемые переменные состояния (начальные значения)
# ---------------------------------------------------------------------------
# Фильтры
time_range = config.TIME_RANGES[config.DEFAULT_TIME_RANGE]
# Три отдельных поля игроков: «Включить» (без режима) и два поля исключения
# без тоггла — «Исключить пулы игроков» и «Исключить сделки игроков».
include_sharks: list[str] = []
include_shark_input = ""
exclude_pool_sharks: list[str] = []
exclude_pool_shark_input = ""
exclude_trade_sharks: list[str] = []
exclude_trade_shark_input = ""
pools: list[str] = []
pool_input = ""
pools_mode = config.POOL_MODES[config.DEFAULT_POOL_MODE]

# dashboard in current page
current_page = "dashboard"


# --- Сессия / авторизация ---------------------------------------------------
# Одна страница: карточка входа (render="{not logged_in}") и дашборд
# (render="{logged_in}") взаимно скрыты. callbacks.login переключает logged_in и
# наполняет user_login/is_admin; навигации нет.
logged_in = False
username = ""        # поле «Логин» карточки входа
password = ""        # поле «Пароль» карточки входа
# Логин вошедшего пользователя (показывается в правом верхнем углу). Пусто до входа.
user_login = ""

# --- Админ-панель (связана с бэкендом data/login_logic.py) -------------------
# Флаг видимости панели. Начально False — выставляется при входе из роли
# вошедшего (callbacks.login → Admin_panel.check_is_admin). Сами операции дополнительно
# авторизует бэкенд (admin_* → None не-админу).
is_admin = False
# Видимость модалок админ-панели.
show_admin_users = False
show_admin_create = False
show_admin_delete = False
show_admin_role = False
# Поля форм админ-панели.
admin_create_login = ""
admin_create_password = ""
admin_create_role = "Юзер"
admin_delete_login = ""
admin_role_login = ""
admin_role_lov = ["Юзер", "Админ"]
# Список юзеров: пустой до открытия модалки — наполняется из БД
# (callbacks._load_admin_users при open_admin_users).
admin_users = pd.DataFrame({"Логин": [], "Роль": []})

# Топ-50: разрез (пулы/игроки) + на сколько частей делить графики (2..50).
top_dimension = config.TOP_DIMENSION[config.DEFAULT_TOP_DIMENSION]
# Колонки таблицы Топ-50: имя колонки сущности стабильно ("entity"), заголовок
# меняется под разрез (см. refresh_all). rebuild=True — иначе Taipy не обновит
# заголовок при переключении пул↔игрок.
top_cols = {
    "entity": {"index": 0, "title": "Пул"},
    "volume": {"index": 1, "title": "Объём"},
}
pie_parts = config.PIE_PARTS_DEFAULT
area1_parts = config.AREA_PARTS_DEFAULT

# Анализ рынка
market_pair = ""
expanded_metric: str | None = None
metric_values: dict[str, str] = {k: "—" for k in config.MARKET_METRICS}

# Анализ тренда
trend_reference = config.TREND_REFERENCES[config.DEFAULT_TREND_REFERENCE]
trend_metric = config.TREND_METRICS[config.DEFAULT_TREND_METRIC]
trend_group_by = config.TREND_GROUP_BY[config.DEFAULT_TREND_GROUP_BY]

# Колонки таблиц «ушли/зашли»: имя колонки данных стабильно ("volume"), а
# заголовок столбца значений меняется под выбранную метрику (см. refresh_all).
# Таблицы заданы с rebuild=True — иначе Taipy считает columns статичным и не
# обновляет заголовок при смене метрики.
pools_cols = {
    "pool": {"index": 0, "title": "Пул"},
    "volume": {"index": 1, "title": config.TREND_METRICS[config.DEFAULT_TREND_METRIC]},
}

# Боковая панель: True — развёрнута, False — свёрнута в узкую полоску.
sidebar_open = True

# Данные таблиц (пустые до on_init)
_empty_df = pd.DataFrame()
data_top50 = _empty_df
data_top50_pie = _empty_df       # 2-колоночный срез [entity, volume] для пирога
data_area1 = _empty_df          # широкий df filled area 1 — режется ползунком
data_pools_left = _empty_df
data_pools_entered = _empty_df

# Фигуры (пустые до on_init)
fig_pie = go.Figure()
fig_area1 = go.Figure()
fig_area2 = go.Figure()
fig_heatmap1 = go.Figure()
fig_heatmap2 = go.Figure()
fig_daily_micro = go.Figure()
fig_daily_macro = go.Figure()
fig_metric_ts = go.Figure()
fig_metric_pair = go.Figure()

# Списки значений для селекторов/тогглов (подписи)
time_lov = list(config.TIME_RANGES.values())
ref_lov = list(config.TREND_REFERENCES.values())
metric_lov = list(config.TREND_METRICS.values())
group_lov = list(config.TREND_GROUP_BY.values())
dimension_lov = list(config.TOP_DIMENSION.values())
pools_mode_lov = list(config.POOL_MODES.values())

# Максимум чипсов-слотов, отрисовываемых для каждого фильтра.
MAX_CHIPS = 15

card1_class = "top_card"
card2_class = "main_card"
card3_class = "bottom_card"

# ---- Переменные для страницы Сигналы ----
signals_full_data = _empty_df
signals_display_data = _empty_df

# Фильтры
filter_status = "Все"
filter_token = ""
filter_min_volume = ""
filter_max_volume = ""
filter_block_window = 0
filter_time_range = config.TIME_RANGES[config.DEFAULT_TIME_RANGE]

# Пагинация
signals_page_size = 20
signals_current_page = 1
signals_total_pages = 1

# Статистика
signals_total = 0
signals_covered = 0
signals_uncovered = 0
signals_coverage_rate = "0%"

# ---- Колонки для таблицы сигналов ----
signals_columns = {
    "signal_timestamp": {"index": 0, "title": "Время сигнала"},
    "token_a": {"index": 1, "title": "Токен A"},
    "token_b": {"index": 2, "title": "Токен B"},
    "signal_amount": {"index": 3, "title": "Объём сигнала"},
    "signal_bribe": {"index": 4, "title": "Bribe сигнала"},
    "signal_fee": {"index": 5, "title": "Fee сигнала"},
    "swap_timestamp": {"index": 6, "title": "Время сделки"},
    "swap_amount": {"index": 7, "title": "Объём сделки"},
    "swap_user_id": {"index": 8, "title": "ID пользователя"},
    "swap_bribe": {"index": 9, "title": "Bribe сделки"},
    "swap_fee": {"index": 10, "title": "Fee сделки"},
}
# ---------------------------------------------------------------------------
# Хелперы для выражений шаблона
# ---------------------------------------------------------------------------
def short_addr(addr: str) -> str:
    """0x1234…abcd — сокращённое представление адреса."""
    if addr and len(addr) > 12:
        return f"{addr[:6]}…{addr[-4:]}"
    return addr or ""


def chip_label(lst, i: int) -> str:
    """Подпись чипса по индексу: «0x1234…abcd ✕» или пусто (слот скрыт)."""
    if i < len(lst):
        return f"{short_addr(lst[i])}  ✕"
    return ""


def card_1_pressed(state):
    if(state.card1_class != "main_card"):
        buf = state.card1_class
        state.card1_class = "main_card"
        if(state.card2_class == "main_card"):
            state.card2_class = buf
        else:
            state.card3_class = buf


def card_2_pressed(state):
    if(state.card2_class != "main_card"):
        buf = state.card2_class
        state.card2_class = "main_card"
        if(state.card1_class == "main_card"):
            state.card1_class = buf
        else:
            state.card3_class = buf


def card_3_pressed(state):
    if(state.card3_class != "main_card"):
        buf = state.card3_class
        state.card3_class = "main_card"
        if(state.card2_class == "main_card"):
            state.card2_class = buf
        else:
            state.card1_class = buf

# ---------------------------------------------------------------------------
# Сборка страницы
# ---------------------------------------------------------------------------
with tgb.Page() as page:

    # ---------- Карточка входа (видна, пока не авторизован) ----------
    with tgb.part(render="{not logged_in}", class_name="login-page"):
        with tgb.part(class_name="login-card"):
            tgb.text("# ChainBI", mode="md")
            tgb.input(value="{username}", label="Логин", on_action=login)
            tgb.input(value="{password}", label="Пароль", password=True,
                      on_action=login)
            tgb.button("Войти", on_action=login)

    # ================================================================
    # PAGE DASHBOARD (avec sidebar)
    # ================================================================
    with tgb.part(render="{logged_in and current_page == 'dashboard'}", class_name="shell"):

        # ---------- Свёрнутая панель: узкая полоска со стрелкой > ----------
        with tgb.part(render="{not sidebar_open}", class_name="rail"):
            tgb.button("❯", on_action=toggle_sidebar, class_name="rail-btn")

        # ---------- Развёрнутая панель: фильтры + стрелка < ----------------
        with tgb.part(render="{sidebar_open}", class_name="sidebar"):
            with tgb.part(class_name="sidebar-head"):
                tgb.button("❮", on_action=toggle_sidebar, class_name="collapse-btn")

            tgb.text("#### Навигация", mode="md")
            tgb.html("a", "Топ-50", href="#sec-top")
            tgb.html("a", "Анализ рынка", href="#sec-market")
            tgb.html("a", "Анализ тренда", href="#sec-trend")

            tgb.text("#### Топ-50", mode="md")
            tgb.toggle(value="{top_dimension}", lov=dimension_lov, on_change=on_change_refresh)

            tgb.text("#### Временной диапазон", mode="md")
            tgb.selector(
                value="{time_range}", lov=time_lov, dropdown=True,
                on_change=on_change_refresh,
            )

            # --- Фильтр: ВКЛЮЧИТЬ игроков (без режима — всегда «их пулы») ---
            tgb.text("#### Включить игроков", mode="md")
            with tgb.layout("1fr auto", class_name="add-row"):
                tgb.input(value="{include_shark_input}", label="0x… адрес",
                          on_action=add_include_shark)
                tgb.button("＋", on_action=add_include_shark, class_name="add-btn")
            with tgb.part(class_name="chips"):
                for _i in range(MAX_CHIPS):
                    with tgb.part(render="{len(include_sharks) > %d}" % _i, class_name="chip"):
                        tgb.button(
                            "{chip_label(include_sharks, %d)}" % _i,
                            id="inc_chip_%d" % _i,
                            on_action=remove_include_shark,
                        )

            # --- Фильтр: ИСКЛЮЧИТЬ ПУЛЫ игроков (убрать целиком их пулы) ---
            tgb.text("#### Исключить пулы игроков", mode="md")
            with tgb.layout("1fr auto", class_name="add-row"):
                tgb.input(value="{exclude_pool_shark_input}", label="0x… адрес",
                          on_action=add_exclude_pool_shark)
                tgb.button("＋", on_action=add_exclude_pool_shark, class_name="add-btn")
            with tgb.part(class_name="chips"):
                for _i in range(MAX_CHIPS):
                    with tgb.part(render="{len(exclude_pool_sharks) > %d}" % _i, class_name="chip"):
                        tgb.button(
                            "{chip_label(exclude_pool_sharks, %d)}" % _i,
                            id="excp_chip_%d" % _i,
                            on_action=remove_exclude_pool_shark,
                        )

            # --- Фильтр: ИСКЛЮЧИТЬ СДЕЛКИ игроков (убрать только их сделки) ---
            tgb.text("#### Исключить сделки игроков", mode="md")
            with tgb.layout("1fr auto", class_name="add-row"):
                tgb.input(value="{exclude_trade_shark_input}", label="0x… адрес",
                          on_action=add_exclude_trade_shark)
                tgb.button("＋", on_action=add_exclude_trade_shark, class_name="add-btn")
            with tgb.part(class_name="chips"):
                for _i in range(MAX_CHIPS):
                    with tgb.part(render="{len(exclude_trade_sharks) > %d}" % _i, class_name="chip"):
                        tgb.button(
                            "{chip_label(exclude_trade_sharks, %d)}" % _i,
                            id="exct_chip_%d" % _i,
                            on_action=remove_exclude_trade_shark,
                        )

            # --- Фильтр: пулы ---
            tgb.text("#### Адреса пулов", mode="md")
            tgb.toggle(value="{pools_mode}", lov=pools_mode_lov,
                       on_change=on_change_refresh, class_name="mode-toggle")
            with tgb.layout("1fr auto", class_name="add-row"):
                tgb.input(value="{pool_input}", label="0x… адрес пула", on_action=add_pool)
                tgb.button("＋", on_action=add_pool, class_name="add-btn")
            with tgb.part(class_name="chips"):
                for _i in range(MAX_CHIPS):
                    with tgb.part(render="{len(pools) > %d}" % _i, class_name="chip"):
                        tgb.button(
                            "{chip_label(pools, %d)}" % _i,
                            id="pool_chip_%d" % _i,
                            on_action=remove_pool,
                        )

        # ========================= Основной контент Dashboard =========================
        with tgb.part(class_name="content"):

            # ---- Верхняя строка: админ-панель (слева) + логин/выход (справа) ----
            with tgb.part(class_name="topbar"):
                with tgb.part(class_name="nav-bar"):
                    tgb.button("Dashboard", on_action=show_dashboard, class_name="nav-btn active")
                    tgb.button("Signals", on_action=show_signals, class_name="nav-btn")

                with tgb.part(render="{is_admin}", class_name="admin-bar"):
                    tgb.button("Список", on_action=open_admin_users, class_name="admin-btn")
                    tgb.button("Создать", on_action=open_admin_create, class_name="admin-btn")
                    tgb.button("Удалить", on_action=open_admin_delete, class_name="admin-btn")
                    
                with tgb.part(class_name="user-box"):
                    tgb.text("{user_login}", class_name="user-login")
                    tgb.button("Выйти", on_action=logout, class_name="logout-btn")

            # ---- Модалки админ-панели (поверх контента; только админам) ----
            with tgb.part(render="{is_admin}"):
                with tgb.dialog(open="{show_admin_users}", on_action=close_admin_dialog,
                                close_label="Закрыть", width="440px"):
                    with tgb.part(class_name="admin-dialog"):
                        tgb.text("### Пользователи", mode="md")
                        tgb.table(data="{admin_users}", page_size=10)
                with tgb.dialog(open="{show_admin_create}", on_action=close_admin_dialog,
                                close_label="Закрыть", width="360px"):
                    with tgb.part(class_name="admin-dialog"):
                        tgb.text("### Создать пользователя", mode="md")
                        tgb.input(value="{admin_create_login}", label="Логин")
                        tgb.input(value="{admin_create_password}", label="Пароль", password=True)
                        tgb.toggle(value="{admin_create_role}", lov=admin_role_lov)
                        tgb.button("Создать", on_action=admin_create, class_name="admin-action-btn")
                with tgb.dialog(open="{show_admin_delete}", on_action=close_admin_dialog,
                                close_label="Закрыть", width="360px"):
                    with tgb.part(class_name="admin-dialog"):
                        tgb.text("### Удалить пользователя", mode="md")
                        tgb.input(value="{admin_delete_login}", label="Логин")
                        tgb.button("Удалить", on_action=admin_delete,
                                   class_name="admin-action-btn danger")

            # ---- Top-50 ----
            with tgb.part(id="sec-top"):
                tgb.text("## Топ-50 — {top_dimension}", mode="md")
                with tgb.layout("1fr 1fr", class_name="cards"):
                    with tgb.part(class_name="card"):
                        with tgb.part(class_name="parts-ctl"):
                            tgb.text(
                                "Секторов на диаграмме: **{pie_parts}** _(50 = все по отдельности)_",
                                mode="md", class_name="hint",
                            )
                            tgb.slider(
                                value="{pie_parts}",
                                min=config.PARTS_MIN, max=config.PARTS_MAX,
                                step=1, continuous=False, on_change=rebuild_pie,
                            )
                        tgb.chart(figure="{fig_pie}")

                    with tgb.part(class_name="card"):
                        with tgb.part(class_name="parts-ctl"):
                            tgb.text(
                                "Серий на графике: **{area1_parts}** _(50 = все пулы)_",
                                mode="md", class_name="hint",
                            )
                            tgb.slider(
                                value="{area1_parts}",
                                min=config.PARTS_MIN, max=config.PARTS_MAX,
                                step=1, continuous=False, on_change=rebuild_area1,
                            )
                        tgb.chart(figure="{fig_area1}")

                with tgb.part(class_name="card"):
                    tgb.table(data="{data_top50}", columns="{top_cols}", rebuild=True,
                              page_size=10, page_size_options=[10, 25, 50])

            # ---- Анализ рынка ----
            with tgb.part(id="sec-market"):
                tgb.text("## Анализ рынка", mode="md")
                tgb.input(
                    value="{market_pair}", label="Пара для группировки (пусто = общие данные)",
                    on_change=on_change_refresh, change_delay=600,
                )

                with tgb.part(class_name="card"):
                    for _key, _label in config.MARKET_METRICS.items():
                        with tgb.layout("1fr auto", class_name="metric-row"):
                            tgb.button(
                                "%s ▸" % _label, id="metric_%s" % _key,
                                on_action=toggle_metric, class_name="metric-btn",
                            )
                            tgb.text("{metric_values['%s']}" % _key, class_name="metric-val")
                        with tgb.part(render="{expanded_metric == '%s'}" % _key, class_name="metric-detail"):
                            with tgb.layout("1fr 1fr"):
                                tgb.chart(figure="{fig_metric_ts}")
                                tgb.chart(figure="{fig_metric_pair}")

            # ---- Анализ тренда ----
            with tgb.part(id="sec-trend"):
                tgb.text("## Анализ тренда", mode="md")
                with tgb.layout("1fr 1fr 1fr", class_name="trend-controls"):
                    with tgb.part():
                        tgb.text("Сравнить с", mode="md", class_name="hint")
                        tgb.toggle(value="{trend_reference}", lov=ref_lov, on_change=on_change_refresh)
                    with tgb.part():
                        tgb.text("Метрика", mode="md", class_name="hint")
                        tgb.toggle(value="{trend_metric}", lov=metric_lov, on_change=on_change_refresh)
                    with tgb.part():
                        tgb.text("Группировка", mode="md", class_name="hint")
                        tgb.toggle(value="{trend_group_by}", lov=group_lov, on_change=on_change_refresh)

                with tgb.layout("1fr 1fr", class_name="cards"):
                    with tgb.part(class_name="card"):
                        tgb.text("### Ушли из пулов", mode="md")
                        tgb.text("_были в reference-окне, но не сегодня_", mode="md", class_name="hint")
                        tgb.table(data="{data_pools_left}", columns="{pools_cols}",
                                  rebuild=True, page_size=7)
                    with tgb.part(class_name="card"):
                        tgb.text("### Зашли в пулы", mode="md")
                        tgb.text("_есть сегодня, но не было в reference-окне_", mode="md", class_name="hint")
                        tgb.table(data="{data_pools_entered}", columns="{pools_cols}",
                                  rebuild=True, page_size=7)

                with tgb.part(class_name="card"):
                    tgb.chart(figure="{fig_daily_micro}")
                with tgb.part(class_name="card"):
                    tgb.chart(figure="{fig_daily_macro}")
                with tgb.layout("1fr 1fr", class_name="cards"):
                    with tgb.part(class_name="card"):
                        tgb.chart(figure="{fig_heatmap1}")
                    with tgb.part(class_name="card"):
                        tgb.chart(figure="{fig_heatmap2}")
                with tgb.part(class_name="card"):
                    tgb.chart(figure="{fig_area2}")

    # ================================================================
    # PAGE SIGNALS (SANS sidebar - pleine largeur)
    # ================================================================
    with tgb.part(render="{logged_in and current_page == 'signals'}", class_name="signals-shell"):

        # ---- Topbar spécifique Signals ----
        with tgb.part(class_name="topbar"):

            with tgb.part(class_name="nav-bar"):
                tgb.button("Dashboard", on_action=show_dashboard, class_name="admin-btn")
                tgb.button("Signals", on_action=show_signals, class_name="admin-btn active")
        
            with tgb.part(render="{is_admin}", class_name="admin-bar"):
                tgb.button("Список", on_action=open_admin_users, class_name="admin-btn")
                tgb.button("Создать", on_action=open_admin_create, class_name="admin-btn")
                tgb.button("Удалить", on_action=open_admin_delete, class_name="admin-btn")
                
            with tgb.part(class_name="user-box"):
                tgb.text("{user_login}", class_name="user-login")
                tgb.button("Выйти", on_action=logout, class_name="logout-btn")

        # ---- Signals content ----
        with tgb.part(class_name="signals-content"):
    
            # Заголовок
            tgb.text("# Сопоставление сигналов и сделок", mode="md", class_name="signals-title")
            
            # Статистика
            with tgb.layout("1fr 1fr 1fr 1fr", class_name="signals-stats"):
                with tgb.part(class_name="stat-card"):
                    tgb.text("### Всего сигналов", mode="md")
                    tgb.text("{signals_total}", class_name="stat-number")
                with tgb.part(class_name="stat-card"):
                    tgb.text("### Покрытые", mode="md")
                    tgb.text("{signals_covered}", class_name="stat-number covered")
                with tgb.part(class_name="stat-card"):
                    tgb.text("### Непокрытые", mode="md")
                    tgb.text("{signals_uncovered}", class_name="stat-number uncovered")
                with tgb.part(class_name="stat-card"):
                    tgb.text("### Процент покрытия", mode="md")
                    tgb.text("{signals_coverage_rate}", class_name="stat-number")
            
            # Фильтры
            with tgb.part(class_name="signals-filters"):
                tgb.text("## Фильтры", mode="md")
                
                # Изменили разметку на 7 колонок, чтобы блоки поместились в один ряд
                with tgb.layout("15% 20% 15% 15% 18% 17%", class_name="filters-grid"):
                    
                    with tgb.part():
                        tgb.text("Статус", mode="md", class_name="filter-label")
                        tgb.selector(
                            value="{filter_status}",
                            lov=["Все", "Покрытые", "Непокрытые"],
                            dropdown=True,
                            on_change=apply_signals_filters,
                            class_name="filter-select"
                        )
                    with tgb.part():
                        tgb.text("Токен", mode="md", class_name="filter-label")
                        tgb.input(
                            value="{filter_token}",
                            label="ETH, BTC, USDC...",
                            on_change=apply_signals_filters,
                            change_delay=300,
                            class_name="filter-input"
                        )
                    with tgb.part():
                        tgb.text("Диапазон блоков", mode="md", class_name="filter-label")
                        tgb.input(
                            value="{filter_block_window}",
                            label="Диап. блоков",
                            on_change=apply_signals_filters,
                            change_delay=300,
                            class_name="filter-input"
                        )
                        
                    with tgb.part():
                        tgb.text("Объём мин.", mode="md", class_name="filter-label")
                        tgb.input(
                            value="{filter_min_volume}",
                            label="0",
                            on_change=apply_signals_filters,
                            change_delay=300,
                            class_name="filter-input"
                        )
                    with tgb.part():
                        tgb.text("Объём макс.", mode="md", class_name="filter-label")
                        tgb.input(
                            value="{filter_max_volume}",
                            label="1000000",
                            on_change=apply_signals_filters,
                            change_delay=300,
                            class_name="filter-input"
                        )
                    
                    # ---- НОВЫЕ ПОЛЯ: ДИАПАЗОН БЛОКОВ ----
                    
                    
                    # -------------------------------------

                    with tgb.part():
                        tgb.text("Дата", mode="md", class_name="filter-label")
                        tgb.selector(
                            value="{filter_time_range}",
                            lov=time_lov,
                            dropdown=True,
                            on_change=apply_signals_filters,
                            class_name="filter-select"
                        )
                
                with tgb.layout("auto auto", class_name="filter-actions"):
                    tgb.button("Сбросить", on_action=reset_signals_filters, class_name="reset-btn")
                    tgb.button("Экспорт CSV", on_action=export_signals_csv, class_name="export-btn")

            # Таблица
            with tgb.part(class_name="signals-table-section"):
                with tgb.layout("1fr auto", class_name="table-header"):
                    tgb.text("## Таблица сигналов", mode="md")
                    with tgb.part(class_name="table-controls"):
                        tgb.text("Строк:", class_name="label")
                        tgb.selector(
                            value="{signals_page_size}",
                            lov=[500, 1000, 5000, 10000, 50000, 100000],
                            dropdown=True,
                            on_change=change_signals_page_size,
                            class_name="page-size-select"
                        )
                
                tgb.table(
                    data="{signals_display_data}",
                    columns="{signals_columns}",
                    rebuild=True,
                    page_size=0, 
   
                    page_size_options=[10, 50, 100, 500],
                    on_action=on_signal_row_click
                )
                
                
                    