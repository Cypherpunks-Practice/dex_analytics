"""Колбэки Taipy и логика обновления данных.

Здесь живёт refresh_all() — единственная точка, которая перечитывает слой
данных и переприсваивает все state.* переменные, привязанные к графикам и
таблицам. Её вызывают: on_init (при подключении клиента), любой колбэк
фильтра, ручная кнопка «Обновить» и фоновый поток автообновления.

Селекторы/тогглы хранят в state человекочитаемые подписи; здесь они
конвертируются обратно во внутренние ключи через реверс-словари.
"""

from __future__ import annotations

import config
import viz
from data import queries

# Реверс-словари: подпись -> внутренний ключ.
_TIME_KEY = {v: k for k, v in config.TIME_RANGES.items()}
_REF_KEY = {v: k for k, v in config.TREND_REFERENCES.items()}
_METRIC_KEY = {v: k for k, v in config.TREND_METRICS.items()}
_GROUP_KEY = {v: k for k, v in config.TREND_GROUP_BY.items()}
_DIM_KEY = {v: k for k, v in config.TOP_DIMENSION.items()}

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
    """Собрать словарь фильтров из текущего состояния."""
    return {
        "players": list(state.sharks),
        "pools": list(state.pools),
        "time_range": _TIME_KEY.get(state.time_range, config.DEFAULT_TIME_RANGE),
    }


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
    state.data_top50 = top
    state.top_cols = {
        "entity": {"index": 0, "title": entity_title},
        "volume": {"index": 1, "title": "Объём"},
    }
    state.fig_pie = viz.pie_top_pools(top, int(state.pie_parts))

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
    state.fig_daily = viz.grouped_lines(
        queries.get_daily_changes(f, tmetric, group_by),
        title=f"Изменение по дням ({state.trend_metric}, {state.trend_group_by.lower()})",
    )
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


def on_change_refresh(state, var_name=None, value=None):
    """Универсальный колбэк фильтров: данные уже записаны в state биндингом."""
    refresh_all(state)


def rebuild_pie(state, var_name=None, value=None):
    """Ползунок круговой диаграммы: пересобрать её под новое число секторов.

    Данные не перезапрашиваем — режем уже загруженный state.data_top50.
    """
    state.fig_pie = viz.pie_top_pools(state.data_top50, int(state.pie_parts))


def rebuild_area1(state, var_name=None, value=None):
    """Ползунок filled area: пересобрать график под новое число серий.

    Данные не перезапрашиваем — режем уже загруженный state.data_area1.
    Заголовок выбираем по текущему разрезу (пулы/игроки).
    """
    dim = _DIM_KEY.get(state.top_dimension, config.DEFAULT_TOP_DIMENSION)
    state.fig_area1 = viz.filled_area(
        state.data_area1, int(state.area1_parts), title=_AREA1_TITLE[dim],
    )


def add_shark(state):
    val = (state.shark_input or "").strip()
    if val and val not in state.sharks:
        state.sharks = state.sharks + [val]
    state.shark_input = ""
    refresh_all(state)


def remove_shark(state, id):
    i = int(id.rsplit("_", 1)[1])
    lst = list(state.sharks)
    if 0 <= i < len(lst):
        del lst[i]
        state.sharks = lst
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


def toggle_metric(state, id):
    """Развернуть/свернуть строку метрики и пересчитать её график."""
    key = id.split("metric_", 1)[1]
    state.expanded_metric = None if state.expanded_metric == key else key
    if state.expanded_metric:
        _refresh_expanded_metric(state)
