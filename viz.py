"""Построение plotly-фигур из DataFrame'ов слоя данных.

Для сложных графиков (несколько динамических серий, хитмапы, stacked area)
надёжнее всего отдавать в Taipy готовую plotly-фигуру через свойство
`figure="{...}"`, чем полагаться на автоопределение серий. Эти функции
вызываются из refresh_all() и складывают результат в state.fig_*.
"""

from __future__ import annotations

import plotly.graph_objects as go

import config

# Единая палитра, чтобы графики выглядели согласованно.
_COLORS = [
    "#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#76b7b2",
    "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac",
]
_LAYOUT = dict(
    margin=dict(l=40, r=20, t=30, b=40),
    template="plotly_white",
    legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
    height=320,
)


def pie_top_pools(df, others_limit: int = config.PIE_OTHERS_LIMIT):
    """Круговая диаграмма топ-пулов; мелкие объединяются в «Others»."""
    head = df.head(others_limit)
    labels = list(head["pool"])
    values = list(head["volume"])
    rest = df["volume"].iloc[others_limit:].sum()
    if rest > 0:
        labels.append("Others")
        values.append(round(float(rest), 2))
    fig = go.Figure(go.Pie(labels=labels, values=values, hole=0.35, textinfo="percent"))
    fig.update_layout(**_LAYOUT, showlegend=False)
    return fig


def filled_area(df, x_col: str = "time", title: str = ""):
    """Stacked filled area: x = время, по серии на каждую колонку (кроме x)."""
    fig = go.Figure()
    series_cols = [c for c in df.columns if c != x_col]
    for i, col in enumerate(series_cols):
        fig.add_trace(
            go.Scatter(
                x=df[x_col], y=df[col], name=col, mode="lines",
                stackgroup="one", line=dict(width=0.5, color=_COLORS[i % len(_COLORS)]),
            )
        )
    fig.update_layout(**_LAYOUT, showlegend=False)
    return fig


def heatmap(df, title: str = ""):
    """Хитмап из матрицы: индекс=ось Y, колонки=ось X, значения=цвет."""
    fig = go.Figure(
        go.Heatmap(
            z=df.values, x=list(df.columns), y=list(df.index),
            colorscale="Viridis", colorbar=dict(title="Объём"),
        )
    )
    fig.update_layout(title=title, **{**_LAYOUT, "height": 420})
    return fig


def grouped_lines(df, x_col: str = "day", title: str = ""):
    """Линии изменения по дням: серия на каждую колонку (кроме x)."""
    fig = go.Figure()
    series_cols = [c for c in df.columns if c != x_col]
    for i, col in enumerate(series_cols):
        fig.add_trace(
            go.Scatter(
                x=df[x_col], y=df[col], name=col, mode="lines+markers",
                line=dict(color=_COLORS[i % len(_COLORS)]),
            )
        )
    fig.update_layout(title=title, **_LAYOUT)
    return fig


def timeseries(df, title: str = ""):
    """Простой график динамики метрики по времени (одна линия)."""
    fig = go.Figure(
        go.Scatter(x=df["time"], y=df["value"], mode="lines", fill="tozeroy",
                   line=dict(color=_COLORS[0]))
    )
    fig.update_layout(title=title, **{**_LAYOUT, "height": 280})
    return fig


def bar_by_pair(df, title: str = ""):
    """Столбчатая диаграмма метрики в разбивке по парам."""
    fig = go.Figure(go.Bar(x=df["pair"], y=df["value"], marker_color=_COLORS[1]))
    fig.update_layout(title=title, **{**_LAYOUT, "height": 280})
    return fig
