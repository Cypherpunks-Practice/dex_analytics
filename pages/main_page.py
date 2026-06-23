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
# доступны напрямую (а не как callbacks.add_shark).
from callbacks import (
    add_pool,
    add_shark,
    on_change_refresh,
    rebuild_area1,
    rebuild_pie,
    remove_pool,
    remove_shark,
    toggle_metric,
    toggle_sidebar,
)
import config

# ---------------------------------------------------------------------------
# Привязываемые переменные состояния (начальные значения)
# ---------------------------------------------------------------------------
# Фильтры
time_range = config.TIME_RANGES[config.DEFAULT_TIME_RANGE]
sharks: list[str] = []
shark_input = ""
pools: list[str] = []
pool_input = ""

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
data_area1 = _empty_df          # широкий df filled area 1 — режется ползунком
data_pools_left = _empty_df
data_pools_entered = _empty_df

# Фигуры (пустые до on_init)
fig_pie = go.Figure()
fig_area1 = go.Figure()
fig_area2 = go.Figure()
fig_heatmap1 = go.Figure()
fig_heatmap2 = go.Figure()
fig_daily = go.Figure()
fig_metric_ts = go.Figure()
fig_metric_pair = go.Figure()

# Списки значений для селекторов/тогглов (подписи)
time_lov = list(config.TIME_RANGES.values())
ref_lov = list(config.TREND_REFERENCES.values())
metric_lov = list(config.TREND_METRICS.values())
group_lov = list(config.TREND_GROUP_BY.values())
dimension_lov = list(config.TOP_DIMENSION.values())

# Максимум чипсов-слотов, отрисовываемых для каждого фильтра.
MAX_CHIPS = 15

card1_class = "top_card"
card2_class = "main_card"
card3_class = "bottom_card"   

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
    # Гибкая «оболочка»: первый столбец — панель (узкая полоска ИЛИ полная),
    # второй — контент, который сам подстраивается под свободное место.
    with tgb.part(class_name="shell"):

        # ---------- Свёрнутая панель: узкая полоска со стрелкой > ----------
        with tgb.part(render="{not sidebar_open}", class_name="rail"):
            tgb.button("❯", on_action=toggle_sidebar, class_name="rail-btn")

        # ---------- Развёрнутая панель: фильтры + стрелка < ----------------
        with tgb.part(render="{sidebar_open}", class_name="sidebar"):
            with tgb.part(class_name="sidebar-head"):
                tgb.button("❮", on_action=toggle_sidebar, class_name="collapse-btn")
            tgb.text("# ChainBI", mode="md")
            tgb.text("Аналитика DEX: акулы и кит", mode="md", class_name="subtitle")

            tgb.text("#### Навигация", mode="md")
            tgb.html("a", "Топ-50", href="#sec-top")
            tgb.html("a", "Анализ рынка", href="#sec-market")
            tgb.html("a", "Анализ тренда", href="#sec-trend")

            tgb.text("#### Топ-50: разрез", mode="md")
            tgb.toggle(value="{top_dimension}", lov=dimension_lov, on_change=on_change_refresh)

            tgb.text("#### Временной диапазон", mode="md")
            tgb.selector(
                value="{time_range}", lov=time_lov, dropdown=True,
                on_change=on_change_refresh,
            )

            # --- Фильтр: акулы ---
            tgb.text("#### Адреса акул / кита", mode="md")
            with tgb.layout("1fr auto", class_name="add-row"):
                tgb.input(value="{shark_input}", label="0x… адрес", on_action=add_shark)
                tgb.button("＋", on_action=add_shark, class_name="add-btn")
            # Чип-слоты обёрнуты в part с render: у button нет свойства render,
            # поэтому пустые слоты гасятся именно на уровне part.
            with tgb.part(class_name="chips"):
                for _i in range(MAX_CHIPS):
                    with tgb.part(render="{len(sharks) > %d}" % _i, class_name="chip"):
                        tgb.button(
                            "{chip_label(sharks, %d)}" % _i,
                            id="shark_chip_%d" % _i,
                            on_action=remove_shark,
                        )

            # --- Фильтр: пулы ---
            tgb.text("#### Адреса пулов", mode="md")
            tgb.text("_пусто = весь рынок_", mode="md", class_name="hint")
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

            tgb.button("⟳ Обновить", on_action=on_change_refresh, class_name="refresh-btn")

        # ========================= Основной контент =========================
        with tgb.part(class_name="content"):

            # --------------------------- Топ-50 ---------------------------
            with tgb.part(id="sec-top"):
                tgb.text("## Топ-50 — {top_dimension}", mode="md")
                with tgb.layout("1fr 2fr", class_name="cards2"):
                    with tgb.part(class_name="{card1_class}"):
                        tgb.button(label="", class_name="click-layer", on_action="card_1_pressed")
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
                        tgb.chart(figure="{fig_pie}", class_name="chart", style={"width": "100%", "height": "100%"})
                    with tgb.part(class_name="{card2_class}"):
                        tgb.button(label="", class_name="click-layer", on_action="card_2_pressed")
                        with tgb.part(class_name="parts-ctl"):
                            tgb.text(
                                "Серий на графике: **{area1_parts}** _(50 = все по отдельности)_",
                                mode="md", class_name="hint",
                            )
                            tgb.slider(
                                value="{area1_parts}",
                                min=config.PARTS_MIN, max=config.PARTS_MAX,
                                step=1, continuous=False, on_change=rebuild_area1,
                            )
                        tgb.chart(figure="{fig_area1}", class_name="chart", style={"width": "100%", "height": "100%"})
                    with tgb.part(class_name="{card3_class}"):
                        tgb.button(label="", class_name="click-layer", on_action="card_3_pressed")
                        tgb.table(data="{data_top50}", columns="{top_cols}", rebuild=True, page_size=10, page_size_options=[10, 25, 50])

            # ----------------------- Анализ рынка -----------------------
            with tgb.part(id="sec-market"):
                tgb.text("## Анализ рынка", mode="md")
                tgb.input(
                    value="{market_pair}", label="Пара для группировки (пусто = общие данные)",
                    on_change=on_change_refresh, change_delay=600,
                )
                tgb.text("_Клик по строке — график динамики по времени._", mode="md", class_name="hint")

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

            # ----------------------- Анализ тренда -----------------------
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
                    tgb.chart(figure="{fig_daily}")
                with tgb.layout("1fr 1fr", class_name="cards"):
                    with tgb.part(class_name="card"):
                        tgb.chart(figure="{fig_heatmap1}")
                    with tgb.part(class_name="card"):
                        tgb.chart(figure="{fig_heatmap2}")
                with tgb.part(class_name="card"):
                    tgb.chart(figure="{fig_area2}")