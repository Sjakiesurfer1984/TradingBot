from __future__ import annotations

"""
src/backtest/plot_results.py
-----------------------------
Generates a two-panel chart from backtest results:

  Top panel:   Equity curve (line) with entry/exit arrows
               ↑ green  = BTO  (long LEAP entry)
               ↓ red    = STO  (short NEAR sold)
               ↑ orange = BTC  (near bought back / closed)
               ↓ grey   = STC  (long closed)

  Bottom panel: Drawdown curve (filled area, red)

The chart is saved as a PNG to output_dir/backtest_chart.png.
Requires matplotlib — install with: pip install matplotlib
"""

from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

# We import matplotlib lazily so that the rest of the backtest can run even
# if matplotlib isn't installed — the chart step simply logs a warning.
def plot_backtest(
    *,
    day_results: list,        # List[DayResult] from backtest_runner
    trade_log:   List[Dict[str, Any]],
    output_dir:  Path,
) -> Path:
    try:
        import matplotlib
        matplotlib.use("Agg")   # headless — no display needed
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        import matplotlib.dates as mdates
        from matplotlib.lines import Line2D
    except ImportError:
        raise ImportError(
            "matplotlib is required for chart generation. "
            "Install it with: pip install matplotlib"
        )

    from datetime import datetime

    # ------------------------------------------------------------------
    # Build equity + drawdown series
    # ------------------------------------------------------------------
    dates    = [datetime.fromisoformat(r.date) for r in day_results]
    equities = [r.equity                        for r in day_results]

    peak     = equities[0]
    drawdowns = []
    for eq in equities:
        peak = max(peak, eq)
        drawdowns.append((peak - eq) / peak * 100 if peak > 0 else 0.0)

    # ------------------------------------------------------------------
    # Map trade log entries to (date, action, symbol, equity_at_date)
    # ------------------------------------------------------------------
    # Build a quick lookup: date_str -> equity on that day
    equity_by_date = {r.date: r.equity for r in day_results}

    # Categorise each trade into one of four visual groups
    # BTO = bought to open (entry of LEAP)     → green up arrow
    # STO = sold to open   (entry of NEAR)     → red down arrow
    # BTC = bought to close (near roll/close)  → orange up arrow
    # STC = sold to close  (LEAP exit)         → grey down arrow

    _ACTION_STYLE = {
        "BTO": dict(color="#00c853", marker="^", zorder=5, label="BTO (long)"),
        "STO": dict(color="#d32f2f", marker="v", zorder=5, label="STO (short NEAR)"),
        "BTC": dict(color="#ff6f00", marker="^", zorder=5, label="BTC (close short)"),
        "STC": dict(color="#757575", marker="v", zorder=5, label="STC (close long)"),
    }

    trade_points: Dict[str, List] = {a: {"x": [], "y": [], "labels": []}
                                     for a in _ACTION_STYLE}

    for trade in trade_log:
        action  = str(trade.get("action", "")).upper()
        t_date  = str(trade.get("date", ""))
        symbol  = str(trade.get("symbol", ""))
        eq      = equity_by_date.get(t_date)
        if action not in trade_points or eq is None:
            continue
        dt = datetime.fromisoformat(t_date)
        trade_points[action]["x"].append(dt)
        # Offset arrow above/below the equity line so it doesn't overlap
        offset = eq * 0.003
        trade_points[action]["y"].append(
            eq + offset if "^" in _ACTION_STYLE[action]["marker"] else eq - offset
        )
        trade_points[action]["labels"].append(symbol)

    # ------------------------------------------------------------------
    # Plot
    # ------------------------------------------------------------------
    fig, (ax1, ax2) = plt.subplots(
        2, 1,
        figsize=(16, 9),
        gridspec_kw={"height_ratios": [3, 1]},
        sharex=True,
    )
    fig.patch.set_facecolor("#1a1a2e")
    for ax in (ax1, ax2):
        ax.set_facecolor("#16213e")
        ax.tick_params(colors="#cccccc")
        ax.xaxis.label.set_color("#cccccc")
        ax.yaxis.label.set_color("#cccccc")
        for spine in ax.spines.values():
            spine.set_edgecolor("#333355")

    # --- equity curve ---
    ax1.plot(dates, equities, color="#4fc3f7", linewidth=1.5, zorder=3)
    ax1.fill_between(dates, equities, min(equities), alpha=0.08, color="#4fc3f7")

    # initial capital reference line
    initial = equities[0]
    ax1.axhline(initial, color="#ffffff", linewidth=0.6, linestyle="--", alpha=0.3)
    ax1.text(
        dates[0], initial * 1.001,
        f"  Start ${initial:,.0f}",
        color="#aaaaaa", fontsize=7, va="bottom",
    )

    # --- trade arrows ---
    for action, style in _ACTION_STYLE.items():
        pts = trade_points[action]
        if not pts["x"]:
            continue
        ax1.scatter(
            pts["x"], pts["y"],
            color=style["color"],
            marker=style["marker"],
            s=70, zorder=style["zorder"], alpha=0.85,
        )
        # Annotate with option symbol — small text, rotated so it doesn't clobber
        for x, y, lbl in zip(pts["x"], pts["y"], pts["labels"]):
            # Strip the OSI symbol to just ticker + expiry for brevity
            short_lbl = lbl[:12] if len(lbl) > 12 else lbl
            ax1.annotate(
                short_lbl,
                xy=(x, y),
                xytext=(4, 6),
                textcoords="offset points",
                fontsize=5.5,
                color=style["color"],
                alpha=0.75,
                rotation=60,
            )

    ax1.set_ylabel("Portfolio Equity ($)", color="#cccccc", fontsize=10)
    ax1.yaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(lambda v, _: f"${v:,.0f}")
    )
    ax1.grid(color="#2a2a4a", linewidth=0.5, alpha=0.6)

    # Legend
    legend_handles = [
        Line2D([0], [0], color=s["color"], marker=s["marker"],
               linestyle="None", markersize=7, label=s["label"])
        for s in _ACTION_STYLE.values()
    ] + [Line2D([0], [0], color="#4fc3f7", linewidth=1.5, label="Equity")]
    ax1.legend(
        handles=legend_handles,
        loc="upper left",
        framealpha=0.3,
        facecolor="#1a1a2e",
        edgecolor="#333355",
        labelcolor="#cccccc",
        fontsize=8,
    )

    # --- drawdown ---
    ax2.fill_between(dates, drawdowns, 0, color="#d32f2f", alpha=0.55)
    ax2.plot(dates, drawdowns, color="#d32f2f", linewidth=0.8)
    ax2.set_ylabel("Drawdown (%)", color="#cccccc", fontsize=9)
    ax2.yaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:.1f}%")
    )
    ax2.invert_yaxis()
    ax2.grid(color="#2a2a4a", linewidth=0.5, alpha=0.6)

    # --- x-axis formatting ---
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
    ax2.xaxis.set_major_locator(mdates.MonthLocator(interval=1))
    plt.setp(ax2.xaxis.get_majorticklabels(), rotation=35, ha="right", fontsize=8)

    # --- title & stats box ---
    if day_results:
        first_eq = day_results[0].equity
        last_eq  = day_results[-1].equity
        ret_pct  = (last_eq - first_eq) / first_eq * 100
        n_days   = len(day_results)
        ann_ret  = ret_pct * 252 / n_days if n_days else 0
        max_dd   = max(drawdowns) if drawdowns else 0
        n_trades = len(trade_log)

        stats = (
            f"Return {ret_pct:+.1f}%   "
            f"Ann. {ann_ret:+.1f}%   "
            f"Max DD {max_dd:.1f}%   "
            f"Trades {n_trades}"
        )
        fig.suptitle(
            f"PMCC Backtest  ·  {day_results[0].date} → {day_results[-1].date}\n"
            f"{stats}",
            color="#eeeeee", fontsize=11, y=0.98,
        )

    plt.tight_layout(rect=[0, 0, 1, 0.96])

    output_dir.mkdir(parents=True, exist_ok=True)
    chart_path = output_dir / "backtest_chart.png"
    plt.savefig(chart_path, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return chart_path