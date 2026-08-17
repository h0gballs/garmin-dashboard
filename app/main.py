import io
import math
import os
import sqlite3
from contextlib import closing
from datetime import date, timedelta

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from fastapi import FastAPI, Query, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

DB_PATH = os.environ.get("GARMIN_DB_PATH", "/data/garmin.db")

ALLOWED_DAYS = (1, 7, 30, 90, 120)
DEFAULT_DAYS = 30

CARD_BG = "#1c2839"
GRID_COLOR = "#33415580"
TEXT_COLOR = "#cbd5e1"
MUTED_COLOR = "#7c8ba1"

plt.rcParams.update(
    {
        "figure.facecolor": CARD_BG,
        "axes.facecolor": CARD_BG,
        "savefig.facecolor": CARD_BG,
        "axes.edgecolor": "#33415580",
        "axes.labelcolor": TEXT_COLOR,
        "text.color": TEXT_COLOR,
        "xtick.color": MUTED_COLOR,
        "ytick.color": MUTED_COLOR,
        "grid.color": GRID_COLOR,
        "font.size": 10.5,
        "font.family": ["-apple-system", "Segoe UI", "DejaVu Sans", "sans-serif"],
        "axes.titlesize": 11,
        "axes.titleweight": "semibold",
        "axes.titlecolor": "#e2e8f0",
        "axes.titlepad": 10,
    }
)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self'; style-src 'self'; "
        "script-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        response.headers[k] = v
    return response


def get_conn() -> sqlite3.Connection:
    # Read-only connection: the app has no write path even if the mount
    # somehow became writable.
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def clamp_days(days: int) -> int:
    return days if days in ALLOWED_DAYS else DEFAULT_DAYS


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, days: int = Query(DEFAULT_DAYS)):
    days = clamp_days(days)

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT AVG(steps) AS steps, AVG(resting_hr) AS resting_hr,
                   AVG(sleep_seconds) AS sleep_seconds, AVG(stress_avg) AS stress_avg,
                   AVG(body_battery_charged) AS body_battery_charged
            FROM daily_stats WHERE day >= date('now', ?)
            """,
            (f"-{days} days",),
        )
        avg = cur.fetchone()

        cur.execute(
            """
            SELECT activity_id, day, name, activity_type, distance_meters,
                   duration_seconds, calories, avg_hr
            FROM activities WHERE day >= date('now', ?)
            ORDER BY day DESC, start_time DESC LIMIT 20
            """,
            (f"-{days} days",),
        )
        activities = []
        for a in cur.fetchall():
            d = dict(a)
            d["day_label"] = date.fromisoformat(a["day"]).strftime("%b %-d")
            activities.append(d)

        cur.execute("SELECT MAX(synced_at) FROM daily_stats")
        last_sync = cur.fetchone()[0]

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "days": days,
            "allowed_days": ALLOWED_DAYS,
            "avg": avg,
            "activities": activities,
            "last_sync": last_sync,
        },
    )


def _fetch_series(days: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM daily_stats WHERE day >= date('now', ?) ORDER BY day ASC",
            (f"-{days} days",),
        )
        return cur.fetchall()


def _svg_response(fig) -> Response:
    buf = io.BytesIO()
    fig.savefig(buf, format="svg", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


def _style(ax, dates):
    if not dates:
        # No data at all (vs. a single point) leaves the axis with no
        # data-derived limits, and matplotlib falls back to the epoch --
        # producing a nonsensical 1970 x-axis. Show a placeholder instead.
        today = date.today()
        ax.set_xlim(today - timedelta(days=1), today + timedelta(days=1))
        ax.set_xticks([])
        ax.set_yticks([])
        ax.text(
            0.5, 0.5, "No data", transform=ax.transAxes,
            ha="center", va="center", color=MUTED_COLOR, fontsize=10,
        )
        for spine in ax.spines.values():
            spine.set_visible(False)
        return

    # AutoDateLocator + ConciseDateFormatter adapt tick density/format to
    # whatever span the data actually covers, instead of a fixed format
    # keyed to the requested range -- which breaks badly on a single point
    # (matplotlib's default autoscale picks a nonsensical multi-year span).
    lo, hi = min(dates), max(dates)
    span_days = (hi - lo).days
    pad = timedelta(days=max(1, int(span_days * 0.04)))
    ax.set_xlim(lo - pad, hi + pad)
    locator = mdates.AutoDateLocator(minticks=3, maxticks=7)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    ax.grid(True, alpha=0.6, linewidth=0.6)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_alpha(0.4)


def _line_chart(rows, field, label, color, days: int):
    dates = [date.fromisoformat(r["day"]) for r in rows if r[field] is not None]
    values = [r[field] for r in rows if r[field] is not None]
    fig, ax = plt.subplots(figsize=(6.8, 2.9))
    if dates:
        marker_size = 5 if len(dates) <= 30 else 0
        ax.plot(dates, values, color=color, linewidth=2, marker="o", markersize=marker_size)
        ax.fill_between(dates, values, min(values), color=color, alpha=0.08)
    ax.set_title(label)
    _style(ax, dates)
    return fig


@app.get("/chart/steps.svg")
def chart_steps(days: int = Query(DEFAULT_DAYS)):
    days = clamp_days(days)
    return _svg_response(
        _line_chart(_fetch_series(days), "steps", f"Daily Steps ({days}d)", "#3b82f6", days)
    )


@app.get("/chart/resting_hr.svg")
def chart_resting_hr(days: int = Query(DEFAULT_DAYS)):
    days = clamp_days(days)
    return _svg_response(
        _line_chart(
            _fetch_series(days), "resting_hr", f"Resting Heart Rate ({days}d)", "#f43f5e", days
        )
    )


@app.get("/chart/stress.svg")
def chart_stress(days: int = Query(DEFAULT_DAYS)):
    days = clamp_days(days)
    return _svg_response(
        _line_chart(_fetch_series(days), "stress_avg", f"Average Stress ({days}d)", "#06b6d4", days)
    )


@app.get("/chart/sleep.svg")
def chart_sleep(days: int = Query(DEFAULT_DAYS)):
    days = clamp_days(days)
    rows = _fetch_series(days)
    dates = [date.fromisoformat(r["day"]) for r in rows if r["sleep_seconds"] is not None]
    hours = [r["sleep_seconds"] / 3600 for r in rows if r["sleep_seconds"] is not None]
    fig, ax = plt.subplots(figsize=(6.8, 2.9))
    if dates:
        width = 0.7 if len(dates) <= 30 else (0.9 if len(dates) <= 90 else 1.0)
        ax.bar(dates, hours, color="#a78bfa", width=width)
    ax.set_title(f"Sleep Duration ({days}d, hours)")
    _style(ax, dates)
    return _svg_response(fig)


@app.get("/chart/body_battery.svg")
def chart_body_battery(days: int = Query(DEFAULT_DAYS)):
    days = clamp_days(days)
    rows = _fetch_series(days)
    dates = [date.fromisoformat(r["day"]) for r in rows]
    charged = [
        r["body_battery_charged"] if r["body_battery_charged"] is not None else math.nan
        for r in rows
    ]
    drained = [
        r["body_battery_drained"] if r["body_battery_drained"] is not None else math.nan
        for r in rows
    ]
    fig, ax = plt.subplots(figsize=(6.8, 2.9))
    if dates:
        marker_size = 5 if len(dates) <= 30 else 0
        ax.plot(dates, charged, color="#22c55e", label="Charged", marker="o", markersize=marker_size)
        ax.plot(dates, drained, color="#fb923c", label="Drained", marker="o", markersize=marker_size)
        legend = ax.legend(loc="best", fontsize=8.5, frameon=False)
        for text in legend.get_texts():
            text.set_color(TEXT_COLOR)
    ax.set_title(f"Body Battery ({days}d)")
    _style(ax, dates)
    return _svg_response(fig)
