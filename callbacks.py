"""Колбэки Taipy и логика обновления данных.

Здесь живёт refresh_all() — единственная точка, которая перечитывает слой
данных и переприсваивает все state.* переменные, привязанные к графикам и
таблицам. Её вызывают: on_init (при подключении клиента), любой колбэк
фильтра, ручная кнопка «Обновить» и фоновый поток автообновления.

Селекторы/тогглы хранят в state человекочитаемые подписи; здесь они
конвертируются обратно во внутренние ключи через реверс-словари.
"""

from __future__ import annotations

import pandas as pd
from taipy.gui import notify

import config
import viz
from data import queries, signals_service
from data.login_logic import (
    User as auth_user,
    check_password,
    Admin_panel,
)

# Реверс-словари: подпись -> внутренний ключ.
_TIME_KEY = {v: k for k, v in config.TIME_RANGES.items()}
_REF_KEY = {v: k for k, v in config.TREND_REFERENCES.items()}
_METRIC_KEY = {v: k for k, v in config.TREND_METRICS.items()}
_GROUP_KEY = {v: k for k, v in config.TREND_GROUP_BY.items()}
_DIM_KEY = {v: k for k, v in config.TOP_DIMENSION.items()}
_POOL_MODE_KEY = {v: k for k, v in config.POOL_MODES.items()}

# Заголовок графика filled area секции Топ-50 — по разрезу.
_AREA1_TITLE = {"pool": "Перетекание средств между пулами",
                "player": "Объёмы игроков во времени"}


def _fmt(value) -> str:
    """Форматирование числа метрики с разделителями разрядов."""
    try:
        if isinstance(value, int) or float(value).is_integer():
            return f"{int(value):,}".replace(",", " ")
        return f"{float(value):,.2f}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def get_filters(state) -> dict:
    """Собрать словарь фильтров из текущего состояния.

    Три отдельных списка игроков: `include_sharks` (поле «Включить», всегда
    include), `exclude_pool_sharks` (поле «Исключить пулы игроков») и
    `exclude_trade_sharks` (поле «Исключить сделки игроков»). Оба списка
    исключения применяются одновременно и независимо.
    """
    return {
        "include_players": list(state.include_sharks),
        "exclude_pool_players": list(state.exclude_pool_sharks),
        "exclude_trade_players": list(state.exclude_trade_sharks),
        "pools": list(state.pools),
        "pools_mode": _POOL_MODE_KEY.get(
            state.pools_mode, config.DEFAULT_POOL_MODE),
        "time_range": _TIME_KEY.get(state.time_range, config.DEFAULT_TIME_RANGE),
    }


def _build_top50(top, dim, entity_title, players, pools):
    """Собрать (data, columns, pie_df) для секции Топ-50 из df слоя данных.

    Обогащённый df (включение игроков → разрез «Пулы», или включение пулов →
    разрез «Игроки») даёт таблицу с тремя числовыми колонками (объём
    выбранных / общий / доля %); обычный df — прежнюю таблицу entity|volume.
    Пирог всегда кормим 2-колоночным df [entity, volume] (volume — основной
    объём: объём игроков / объём в пуле / просто объём).
    """
    cols = set(top.columns)
    if dim == "pool" and {"player_vol", "pool_total", "share"} <= cols:
        many = len(players) > 1
        columns = {
            "entity": {"index": 0, "title": entity_title},
            "player_vol": {"index": 1,
                           "title": "Объём выбранных игроков" if many else "Объём игрока"},
            "pool_total": {"index": 2, "title": "Общий объём пула"},
            "share": {"index": 3,
                      "title": "Доля выбранных игроков, %" if many else "Доля игрока, %"},
        }
        pie_df = top[["entity", "player_vol"]].rename(columns={"player_vol": "volume"})
        return top, columns, pie_df

    if dim == "player" and {"player_in_pool", "player_total", "share"} <= cols:
        many = len(pools) > 1
        columns = {
            "entity": {"index": 0, "title": entity_title},
            "player_in_pool": {"index": 1,
                               "title": "Объём в выбранных пулах" if many else "Объём в пуле"},
            "player_total": {"index": 2, "title": "Общий объём игрока"},
            "share": {"index": 3,
                      "title": "Доля выбранных пулов, %" if many else "Доля пула, %"},
        }
        pie_df = top[["entity", "player_in_pool"]].rename(
            columns={"player_in_pool": "volume"})
        return top, columns, pie_df

    columns = {
        "entity": {"index": 0, "title": entity_title},
        "volume": {"index": 1, "title": "Объём"},
    }
    pie_df = top if "volume" in cols else pd.DataFrame({"entity": [], "volume": []})
    return top, columns, pie_df


# --- Главное обновление -----------------------------------------------------
def refresh_all(state):
    """Перечитать весь слой данных и обновить все привязанные переменные."""
    f = get_filters(state)
    tmetric = _METRIC_KEY.get(state.trend_metric, config.DEFAULT_TREND_METRIC)

    # --- Топ-50: пулы или игроки (по тумблеру top_dimension) ---
    # Имя колонки сущности держим стабильным ("entity") — иначе Taipy-таблица,
    # запомнив колонки при первом рендере, покажет пустоту при смене разреза.
    # Подпись столбца меняем через bindable columns (top_cols) + rebuild=True.
    dim = _DIM_KEY.get(state.top_dimension, config.DEFAULT_TOP_DIMENSION)
    if dim == "player":
        top = queries.get_top_players(f).rename(columns={"player": "entity"})
        area = queries.get_area_by_shark(f, tmetric)
        entity_title = "Игрок"
    else:
        top = queries.get_top_pools(f).rename(columns={"pool": "entity"})
        area = queries.get_area_by_pool(f, tmetric)
        entity_title = "Пул"
    state.data_top50, state.top_cols, pie_df = _build_top50(
        top, dim, entity_title, f["include_players"], f["pools"])
    state.data_top50_pie = pie_df
    state.fig_pie = viz.pie_top_pools(pie_df, int(state.pie_parts))

    # --- Анализ рынка: 7 метрик (total) ---
    pair = (state.market_pair or "").strip() or None
    metrics = queries.get_market_metrics(f, pair)
    state.metric_values = {k: _fmt(metrics[k]["total"]) for k in config.MARKET_METRICS}

    # Раскрытый график метрики (если строка развёрнута).
    _refresh_expanded_metric(state, metrics)

    # --- Filled area 1 (по пулам/игрокам), секция Топ-50 ---
    # Тянем весь набор серий в state, чтобы ползунок мог пересобирать график
    # без повторного запроса к данным (см. rebuild_area1). Данные уже получены
    # выше в ветке разреза (area).
    state.data_area1 = area
    state.fig_area1 = viz.filled_area(
        state.data_area1, int(state.area1_parts), title=_AREA1_TITLE[dim],
    )

    # --- Анализ тренда ---
    ref = _REF_KEY.get(state.trend_reference, config.DEFAULT_TREND_REFERENCE)
    group_by = _GROUP_KEY.get(state.trend_group_by, config.DEFAULT_TREND_GROUP_BY)

    # Ушедшие/зашедшие пулы — одним вызовом (оба окна запрашиваются один раз).
    # Значения столбца считаются по выбранной метрике, но ИМЯ колонки в df
    # остаётся стабильным ("volume") — иначе Taipy-таблица, запомнив колонки при
    # первом рендере, не найдёт переименованную и покажет пустоту. Подпись же
    # столбца под метрику навешиваем через bindable-свойство columns (pools_cols).
    delta = queries.get_pools_delta(f, ref, tmetric)
    state.data_pools_left = delta["left"]
    state.data_pools_entered = delta["entered"]
    state.pools_cols = {
        "pool": {"index": 0, "title": "Пул"},
        "volume": {"index": 1, "title": config.TREND_METRICS[tmetric]},
    }
    # График «изменение по дням» разделён на микро (выбранные игроки) и макро
    # (контекст: пулы / весь рынок) — см. queries.get_daily_micro_macro.
    daily = queries.get_daily_micro_macro(f, tmetric, group_by)
    if group_by == "player":
        micro_title = f"Микро: объёмы выбранных игроков ({state.trend_metric})"
        macro_title = f"Макро: объёмы всех игроков ({state.trend_metric})"
    else:
        micro_title = f"Микро: объём выбранных игроков в их пулах ({state.trend_metric})"
        macro_title = f"Макро: общие объёмы пулов ({state.trend_metric})"
    state.fig_daily_micro = viz.grouped_lines(daily["micro"], title=micro_title)
    state.fig_daily_macro = viz.grouped_lines(daily["macro"], title=macro_title)
    state.fig_heatmap1 = viz.heatmap(
        queries.get_heatmap_sharks_pools(f, tmetric), title="Хитмап: топ-10 акул × топ-10 пулов"
    )
    state.fig_heatmap2 = viz.heatmap(
        queries.get_heatmap_time_pools(f, tmetric), title="Хитмап: время × топ-20 пулов"
    )
    state.fig_area2 = viz.filled_area(
        queries.get_area_by_shark(f, tmetric), title="Объёмы акул во времени"
    )


def _refresh_expanded_metric(state, metrics=None):
    """Пересчитать график динамики для развёрнутой строки метрики."""
    key = state.expanded_metric
    if not key:
        return
    f = get_filters(state)
    pair = (state.market_pair or "").strip() or None
    if metrics is None:
        metrics = queries.get_market_metrics(f, pair)
    label = config.MARKET_METRICS[key]
    state.fig_metric_ts = viz.timeseries(
        queries.get_metric_timeseries(f, key, pair), title=f"{label} — динамика по времени"
    )
    state.fig_metric_pair = viz.bar_by_pair(metrics[key]["by_pair"], title=f"{label} — по парам")


# --- Колбэки -----------------------------------------------------------------
def on_init(state):
    """Первичная загрузка данных при подключении клиента."""
    refresh_all(state)
    refresh_signals(state)


def on_change_refresh(state, var_name=None, value=None):
    """Универсальный колбэк фильтров: данные уже записаны в state биндингом."""
    refresh_all(state)


def rebuild_pie(state, var_name=None, value=None):
    """Ползунок круговой диаграммы: пересобрать её под новое число секторов.

    Данные не перезапрашиваем — режем уже загруженный state.data_top50_pie
    (2-колоночный срез [entity, volume], пригодный для pie_top_pools).
    """
    state.fig_pie = viz.pie_top_pools(state.data_top50_pie, int(state.pie_parts))


def rebuild_area1(state, var_name=None, value=None):
    """Ползунок filled area: пересобрать график под новое число серий.

    Данные не перезапрашиваем — режем уже загруженный state.data_area1.
    Заголовок выбираем по текущему разрезу (пулы/игроки).
    """
    dim = _DIM_KEY.get(state.top_dimension, config.DEFAULT_TOP_DIMENSION)
    state.fig_area1 = viz.filled_area(
        state.data_area1, int(state.area1_parts), title=_AREA1_TITLE[dim],
    )


def add_include_shark(state):
    val = (state.include_shark_input or "").strip()
    if val and val not in state.include_sharks:
        state.include_sharks = state.include_sharks + [val]
    state.include_shark_input = ""
    refresh_all(state)


def remove_include_shark(state, id):
    i = int(id.rsplit("_", 1)[1])
    lst = list(state.include_sharks)
    if 0 <= i < len(lst):
        del lst[i]
        state.include_sharks = lst
        refresh_all(state)


def add_exclude_pool_shark(state):
    val = (state.exclude_pool_shark_input or "").strip()
    if val and val not in state.exclude_pool_sharks:
        state.exclude_pool_sharks = state.exclude_pool_sharks + [val]
    state.exclude_pool_shark_input = ""
    refresh_all(state)


def remove_exclude_pool_shark(state, id):
    i = int(id.rsplit("_", 1)[1])
    lst = list(state.exclude_pool_sharks)
    if 0 <= i < len(lst):
        del lst[i]
        state.exclude_pool_sharks = lst
        refresh_all(state)


def add_exclude_trade_shark(state):
    val = (state.exclude_trade_shark_input or "").strip()
    if val and val not in state.exclude_trade_sharks:
        state.exclude_trade_sharks = state.exclude_trade_sharks + [val]
    state.exclude_trade_shark_input = ""
    refresh_all(state)


def remove_exclude_trade_shark(state, id):
    i = int(id.rsplit("_", 1)[1])
    lst = list(state.exclude_trade_sharks)
    if 0 <= i < len(lst):
        del lst[i]
        state.exclude_trade_sharks = lst
        refresh_all(state)


def add_pool(state):
    val = (state.pool_input or "").strip()
    if val and val not in state.pools:
        state.pools = state.pools + [val]
    state.pool_input = ""
    refresh_all(state)


def remove_pool(state, id):
    i = int(id.rsplit("_", 1)[1])
    lst = list(state.pools)
    if 0 <= i < len(lst):
        del lst[i]
        state.pools = lst
        refresh_all(state)


def toggle_sidebar(state):
    """Свернуть/развернуть боковую панель вручную (по стрелке)."""
    state.sidebar_open = not state.sidebar_open


def login(state):
    """Авторизация: проверить логин/пароль, при успехе наполнить сессию
    (logged_in / user_login / is_admin). Дашборд показывается реактивно
    (render="{logged_in}"), навигации нет; иначе — тост."""
    login_name = (state.username or "").strip()
    password = state.password or ""
    if not login_name or not password:
        notify(state, "warning", "Введите логин и пароль")
        return
    if check_password(login_name, password) == 1:
        state.logged_in = True
        state.user_login = login_name
        state.is_admin = bool(Admin_panel.check_is_admin(login_name))
        state.password = ""                # не держим пароль в состоянии
    else:
        state.password = ""
        notify(state, "error", "Неверный логин или пароль")


def logout(state):
    """Выход: очистить сессию. Карточка входа возвращается реактивно
    (render="{not logged_in}"), навигации нет."""
    state.logged_in = False
    state.is_admin = False
    state.user_login = ""
    state.username = ""
    state.password = ""


# --- Админ-панель (связана с бэкендом data/login_logic.py) -------------------
# Текущий логин берём из state.user_login; авторизацию операций обеспечивает сам
# бэкенд (методы admin_* возвращают None не-админу). Смены роли в бэкенде нет —
# «Роль» остаётся заглушкой.
def _current_user(state):
    """Бэкенд-объект текущего пользователя для админ-методов login_logic.

    Текущий логин — state.user_login (его выставит будущий логин-флоу).
    """
    return auth_user(state.user_login)


def _load_admin_users(state):
    """Перечитать список юзеров из БД в state.admin_users (DataFrame Логин|Роль)."""
    rows = _current_user(state).admin_get_users_list()  # None если не админ
    if not rows:
        state.admin_users = pd.DataFrame({"Логин": [], "Роль": []})
        return
    state.admin_users = pd.DataFrame({
        "Логин": [r[0] for r in rows],
        "Роль":  ["Админ" if r[1] else "Юзер" for r in rows],
    })


def open_admin_users(state):
    """Открыть модалку со списком пользователей (подгрузив актуальный список)."""
    _load_admin_users(state)
    state.show_admin_users = True


def open_admin_create(state):
    """Открыть модалку создания пользователя."""
    state.show_admin_create = True


def open_admin_delete(state):
    """Открыть модалку удаления пользователя."""
    state.show_admin_delete = True


def open_admin_role(state):
    """Открыть модалку управления ролью."""
    state.show_admin_role = True


def close_admin_dialog(state, id=None, payload=None):
    """Закрыть любую модалку админ-панели (вешается на on_action — крестик).

    Одновременно открыта максимум одна, поэтому просто гасим все флаги.
    """
    state.show_admin_users = False
    state.show_admin_create = False
    state.show_admin_delete = False
    state.show_admin_role = False


def admin_create(state):
    """Создать пользователя через бэкенд login_logic.admin_add_user."""
    login = (state.admin_create_login or "").strip()
    password = state.admin_create_password or ""
    is_admin = 1 if state.admin_create_role == "Админ" else 0
    if not login or not password:
        notify(state, "warning", "Укажите логин и пароль")
        return
    result = _current_user(state).admin_add_user(login, password, is_admin)
    if result is None:
        notify(state, "error", "Недостаточно прав (не админ)")
    elif result is False:
        notify(state, "error", f"Пользователь «{login}» уже существует")
    else:
        notify(state, "success", f"Пользователь «{login}» создан")
        _load_admin_users(state)
    state.admin_create_login = ""
    state.admin_create_password = ""
    state.show_admin_create = False


def admin_delete(state):
    """Удалить пользователя по логину через login_logic.admin_delete_user."""
    login = (state.admin_delete_login or "").strip()
    if not login:
        notify(state, "warning", "Укажите логин")
        return
    try:
        result = _current_user(state).admin_delete_user(login)
    except Exception as exc:  # на случай блокировки БД и т.п.
        notify(state, "error", f"Ошибка удаления: {exc}")
        return
    if result is None:
        notify(state, "error", "Недостаточно прав (не админ)")
    elif result:
        notify(state, "success", f"Удалено пользователей: {result}")
        _load_admin_users(state)
    else:
        notify(state, "warning", f"Пользователь «{login}» не найден")
    state.admin_delete_login = ""
    state.show_admin_delete = False


def admin_promote(state):
    """Назначение роли админа — заглушка (функции нет в бэкенде)."""
    # TODO: добавить смену роли в login_logic, затем связать.
    notify(state, "info", "Смена роли пока недоступна: нет функции в бэкенде")
    state.admin_role_login = ""
    state.show_admin_role = False


def admin_demote(state):
    """Снятие роли админа — заглушка (функции нет в бэкенде)."""
    # TODO: добавить смену роли в login_logic, затем связать.
    notify(state, "info", "Смена роли пока недоступна: нет функции в бэкенде")
    state.admin_role_login = ""
    state.show_admin_role = False


def toggle_metric(state, id):
    """Развернуть/свернуть строку метрики и пересчитать её график."""
    key = id.split("metric_", 1)[1]
    state.expanded_metric = None if state.expanded_metric == key else key
    if state.expanded_metric:
        _refresh_expanded_metric(state)

# --- Страница «Сигналы» (данные: data/signals_service.py) --------------------
# signals_full_data хранит ПОЛНЫЙ summary-df из БД (или стаба); фильтры,
# статистика и пагинация считаются на клиенте в _signals_view — без повторных
# запросов к Postgres/ClickHouse. Перезапрос из БД — только refresh_signals
# (вызывается из on_init).
def show_dashboard(state):
    state.current_page = "dashboard"


def show_signals(state):
    state.current_page = "signals"


def refresh_signals(state):
    """Перезапросить сигналы+трейды из БД (или стаба) и перерисовать таблицу."""
    # state.signals_full_data = signals_service.get_signal_matches(block_window=state.block_window)[0]
    state.signals_full_data = signals_service.get_signal_matches()[0]
    _signals_view(state)


def _to_float(text):
    """Число из текстового поля фильтра; None — пусто или не число."""
    try:
        return float(str(text).strip().replace(" ", ""))
    except (TypeError, ValueError):
        return None


def _filtered_signals(state) -> pd.DataFrame:
    """Применить клиентские фильтры страницы к полному summary."""
    df = state.signals_full_data
    if df.empty:
        return df
    if state.filter_status == "Покрытые":
        df = df[df["covered"]]
    elif state.filter_status == "Непокрытые":
        df = df[~df["covered"]]
    token = (state.filter_token or "").strip().lower()
    if token:
        cols = ["token_a", "token_b", "base_token", "quote_token"]
        mask = pd.concat(
            [df[c].astype(str).str.lower().str.contains(token, na=False)
             for c in cols], axis=1).any(axis=1)
        df = df[mask]
    lo = _to_float(state.filter_min_volume)
    if lo is not None:
        df = df[df["signal_amount"] >= lo]
    hi = _to_float(state.filter_max_volume)
    if hi is not None:
        df = df[df["signal_amount"] <= hi]
    # Окно «Дата» — от максимальной метки времени в данных (анкер по данным,
    # как TIME_ANCHOR="data"); "all" в SIGNALS_TIME_WINDOWS отсутствует.
    key = _TIME_KEY.get(state.filter_time_range, config.DEFAULT_TIME_RANGE)
    window = config.SIGNALS_TIME_WINDOWS.get(key)
    if window is not None and not df.empty:
        anchor = state.signals_full_data["signal_timestamp"].max()
        df = df[df["signal_timestamp"] >= anchor - window]
    return df


def _signals_view(state):
    """Фильтры → статистика → пагинация → видимая страница таблицы."""
    df = _filtered_signals(state)
    total = len(df)
    covered = int(df["covered"].sum()) if total else 0
    state.signals_total = total
    state.signals_covered = covered
    state.signals_uncovered = total - covered
    state.signals_coverage_rate = f"{covered / total * 100:.1f}%" if total else "0%"

    page_size = max(int(state.signals_page_size), 1)
    state.signals_total_pages = max((total + page_size - 1) // page_size, 1)
    if state.signals_current_page > state.signals_total_pages:
        state.signals_current_page = state.signals_total_pages
    start = (state.signals_current_page - 1) * page_size
    state.signals_display_data = df.iloc[start:start + page_size]


def next_signals_page(state):
    """Переход на следующую страницу."""
    if state.signals_current_page < state.signals_total_pages:
        state.signals_current_page += 1
        _signals_view(state)


def prev_signals_page(state):
    """Переход на предыдущую страницу."""
    if state.signals_current_page > 1:
        state.signals_current_page -= 1
        _signals_view(state)


def change_signals_page_size(state, var_name=None, value=None):
    """Селектор «Строк»: пересчитать пагинацию под новый размер страницы."""
    state.signals_current_page = 1
    _signals_view(state)


def apply_signals_filters(state, var_name=None, value=None):
    """Фильтры уже записаны в state биндингом — пересобрать вид без запроса к БД."""
    state.signals_current_page = 1
    _signals_view(state)


def reset_signals_filters(state):
    """Сбрасывает все фильтры."""
    state.filter_status = "Все"
    state.filter_token = ""
    state.filter_min_volume = ""
    state.filter_max_volume = ""
    state.filter_time_range = config.TIME_RANGES[config.DEFAULT_TIME_RANGE]
    state.signals_current_page = 1
    _signals_view(state)


def export_signals_csv(state):
    """Экспорт в CSV (заглушка)."""
    if state.signals_full_data.empty:
        return
    # TODO: реализовать экспорт CSV
    pass


def on_signal_row_click(state, action=None, info=None):
    """Обработка клика по строке (заглушка)."""
    pass


