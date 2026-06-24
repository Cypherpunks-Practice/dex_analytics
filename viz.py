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
    "#2563cf", "#eb761c", "#3fbd19", "#e32447", "#27b092",
    "#f5bb1b", "#b63db8", "#c72031", "#5d5e99", "#afc995",
]
_LAYOUT = dict(
    margin=dict(l=40, r=20, t=95, b=40), 
    template="plotly_white",
    title_y=0.96,                        
    title_yanchor="top",
    legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),  
    height=370,
    
    hovermode="x unified",  Добавлена spike line
    
    xaxis=dict(
        showspikes=True,
        spikemode="across",
        spikesnap="cursor",
        spikecolor="#94a3b8",
        spikethickness=1,
        spikedash="dash",
    )
)


def pie_top_pools(df, parts: int = config.PIE_PARTS_DEFAULT):
    """Круговая диаграмма топ-пулов, поделённая на `parts` секторов.

    parts >= числа пулов → каждый пул отдельным сектором (все пулы);
    иначе (parts-1) крупнейших пулов по отдельности + объединённый сектор
    «Others» из оставшихся — итого ровно `parts` секторов.
    """
    n = len(df)
    if n == 0:
        return go.Figure()
    # Колонка-подпись — первая не-volume (pool или player в зависимости от разреза).
    label_col = next((c for c in df.columns if c != "volume"), "entity")
    if parts >= n:
        labels = list(df[label_col])
        values = [round(float(v), 2) for v in df["volume"]]
    else:
        head = df.head(parts - 1)
        labels = list(head[label_col])
        values = [round(float(v), 2) for v in head["volume"]]
        rest = float(df["volume"].iloc[parts - 1:].sum())
        if rest > 0:
            labels.append("Others")
            values.append(round(rest, 2))
    fig = go.Figure(go.Pie(labels=labels, values=values, hole=0.35, textinfo="percent"))
    fig.update_layout(**_LAYOUT, showlegend=False)
    return fig


def filled_area(df, parts: int | None = None, x_col: str = "time", title: str = ""):
    """Stacked filled area: x = время, серия на каждую колонку (кроме x).

    Если задан `parts` и он меньше числа серий — оставляем (parts-1)
    крупнейших по суммарному объёму пулов, остальные сворачиваем в одну
    серию «Others» (итого `parts` серий). parts=None или parts >= числа
    серий → показываем все серии как есть.
    """
    fig = go.Figure()
    series_cols = [c for c in df.columns if c != x_col]
    if not series_cols:
        fig.update_layout(**_LAYOUT, showlegend=False)
        return fig
    if parts is not None and parts < len(series_cols):
        ranked = sorted(series_cols, key=lambda c: float(df[c].sum()), reverse=True)
        keep = ranked[: parts - 1]
        others = [c for c in series_cols if c not in keep]
        plot_df = df[[x_col] + keep].copy()
        plot_df["Others"] = df[others].sum(axis=1)
        cols = keep + ["Others"]
    else:
        plot_df = df
        cols = series_cols
    for i, col in enumerate(cols):
        fig.add_trace(
            go.Scatter(
                x=plot_df[x_col], y=plot_df[col], name=col, mode="lines",
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
