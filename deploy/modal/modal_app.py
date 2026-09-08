"""Modal schedules for Thaqip's 24/7 data runtime.

Deploy:
  modal deploy deploy/modal/modal_app.py

Required Modal secret:
  thaqip-runtime

The secret must provide DATABASE_URL and any optional integration variables
used by the selected lanes, such as REDIS_URL, TYPESENSE_URL, TYPESENSE_KEY,
TELEGRAM_BOT_TOKEN, and THAQIP_ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import modal

app = modal.App("thaqip-runtime")
ROOT = Path(__file__).resolve().parents[2]
INGESTION_SRC = ROOT / "services" / "ingestion" / "src"


BASE_IMAGE = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "asyncpg>=0.29",
        "httpx[http2]>=0.27",
        "pydantic>=2.8",
        "pydantic-settings>=2.4",
        "redis>=5.0",
    )
    .add_local_dir(str(INGESTION_SRC), remote_path="/app/src")
    .env({"PYTHONPATH": "/app/src"})
)

BROWSER_IMAGE = BASE_IMAGE.pip_install("playwright>=1.62.0").run_commands(
    "python -m playwright install --with-deps chromium"
)

SECRET = modal.Secret.from_name("thaqip-runtime")


def _run(module: str, *args: str) -> None:
    subprocess.run(["python", "-m", module, *args], check=True)


@app.function(image=BASE_IMAGE, secrets=[SECRET], schedule=modal.Period(minutes=5), timeout=900)
def delta_poller() -> None:
    """Newest-first Etimad delta lane. One pass per schedule, no local loop."""
    _run("thaqip_ingestion.main", "--pages", "6")


@app.function(image=BROWSER_IMAGE, secrets=[SECRET], schedule=modal.Period(hours=6), timeout=7200)
def awards_harvest() -> None:
    """Award and offer corpus growth lane."""
    _run("thaqip_ingestion.awards_harvest", "--pages", "4")


@app.function(image=BASE_IMAGE, secrets=[SECRET], schedule=modal.Period(hours=1), timeout=900)
def forsah_pull() -> None:
    """Forsah public opportunity and competition-intensity lane."""
    _run("thaqip_ingestion.forsah", "--pages", "3")


@app.function(image=BASE_IMAGE, secrets=[SECRET], schedule=modal.Cron("15 2 * * *"), timeout=3600)
def reconcile() -> None:
    """Daily corpus census and healing sample."""
    _run("thaqip_ingestion.reconcile", "--pages", "8")


@app.function(image=BASE_IMAGE, secrets=[SECRET], schedule=modal.Cron("0 4 * * *"), timeout=900)
def daily_digest() -> None:
    """07:00 Asia/Riyadh digest while Modal cron runs on UTC."""
    _run("thaqip_ingestion.digest")


@app.function(image=BASE_IMAGE, secrets=[SECRET], timeout=10800)
def backfill(category: str = "all", max_pages: int = 250) -> None:
    """Manual backfill lane for controlled corpus expansion."""
    _run(
        "thaqip_ingestion.backfill",
        "--category",
        category,
        "--max-pages",
        str(max_pages),
        "--rate",
        "1.0",
    )


@app.function(image=BASE_IMAGE, secrets=[SECRET], timeout=7200)
def bulk_index() -> None:
    """Manual Typesense rebuild after schema or ranking changes."""
    _run("thaqip_ingestion.indexer", "--bulk")
