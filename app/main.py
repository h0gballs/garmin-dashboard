import io
import math
import os
import sqlite3
from contextlib import closing
from datetime import date

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

DB_PATH = os.environ.get("GARMIN_DB_PATH", "/data/garmin.db")

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


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM daily_stats ORDER BY day DESC LIMIT 1")
        latest = cur.fetchone()

        cur.execute("SELECT AVG(steps) FROM daily_stats WHERE day >= date('now','-7 days')")
        avg_steps_7d = cur.fetchone()[0]

        cur.execute("SELECT AVG(resting_hr) FROM daily_stats WHERE day >= date('now','-7 days')")
        avg_rhr_7d = cur.fetchone()[0]

        cur.execute("SELECT AVG(sleep_seconds) FROM daily_stats WHERE day >= date('now','-7 days')")
        avg_sleep_7d = cur.fetchone()[0]

        cur.execute(
            """
            SELECT activity_id, day, name, activity_type, distance_meters,
                   duration_seconds, calories, avg_hr
            FROM activities ORDER BY day DESC, start_time DESC LIMIT 15
            """
        )
        activities = cur.fetchall()

        cur.execute("SELECT MAX(synced_at) FROM daily_stats")
        last_sync = cur.fetchone()[0]

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "latest": latest,
            "avg_steps_7d": avg_steps_7d,
            "avg_rhr_7d": avg_rhr_7d,
            "avg_sleep_7d": avg_sleep_7d,
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


def _png_response(fig) -> Response:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


def _style(ax):
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.grid(True, alpha=0.3)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)


def _line_chart(rows, field, label, color):
    dates = [date.fromisoformat(r["day"]) for r in rows if r[field] is not None]
    values = [r[field] for r in rows if r[field] is not None]
    fig, ax = plt.subplots(figsize=(7, 3))
    if dates:
        ax.plot(dates, values, color=color, linewidth=2, marker="o", markersize=3)
    ax.set_title(label)
    _style(ax)
    fig.autofmt_xdate()
    return fig


@app.get("/chart/steps.png")
def chart_steps():
    return _png_response(_line_chart(_fetch_series(30), "steps", "Daily Steps (30d)", "#2563eb"))


@app.get("/chart/resting_hr.png")
def chart_resting_hr():
    return _png_response(
        _line_chart(_fetch_series(30), "resting_hr", "Resting Heart Rate (30d)", "#dc2626")
    )


@app.get("/chart/stress.png")
def chart_stress():
    return _png_response(
        _line_chart(_fetch_series(30), "stress_avg", "Average Stress (30d)", "#0891b2")
    )


@app.get("/chart/sleep.png")
def chart_sleep():
    rows = _fetch_series(30)
    dates = [date.fromisoformat(r["day"]) for r in rows if r["sleep_seconds"] is not None]
    hours = [r["sleep_seconds"] / 3600 for r in rows if r["sleep_seconds"] is not None]
    fig, ax = plt.subplots(figsize=(7, 3))
    if dates:
        ax.bar(dates, hours, color="#7c3aed", width=0.7)
    ax.set_title("Sleep Duration (30d, hours)")
    _style(ax)
    fig.autofmt_xdate()
    return _png_response(fig)


@app.get("/chart/body_battery.png")
def chart_body_battery():
    rows = _fetch_series(30)
    dates = [date.fromisoformat(r["day"]) for r in rows]
    charged = [r["body_battery_charged"] if r["body_battery_charged"] is not None else math.nan for r in rows]
    drained = [r["body_battery_drained"] if r["body_battery_drained"] is not None else math.nan for r in rows]
    fig, ax = plt.subplots(figsize=(7, 3))
    if dates:
        ax.plot(dates, charged, color="#16a34a", label="Charged", marker="o", markersize=3)
        ax.plot(dates, drained, color="#ea580c", label="Drained", marker="o", markersize=3)
        ax.legend(loc="upper left", fontsize=8, frameon=False)
    ax.set_title("Body Battery (30d)")
    _style(ax)
    fig.autofmt_xdate()
    return _png_response(fig)
