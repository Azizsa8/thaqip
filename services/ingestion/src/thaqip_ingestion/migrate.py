"""Forward-only SQL migration runner.

Why this exists: db/migrations is mounted into the postgres container at
/docker-entrypoint-initdb.d, which the postgres image executes **only on first
database initialisation**. Every migration authored after the volume was first
created therefore never runs automatically — the schema only stayed correct
because someone piped the files through psql by hand. This module makes that
explicit and repeatable.

Contract:
  * schema_migrations(filename primary key, applied_at) records what ran.
  * db/migrations/*.sql is applied in lexical filename order.
  * Each file runs inside its own transaction together with the bookkeeping
    INSERT, so a file is recorded iff its statements committed.
  * Re-running is a no-op (idempotent): recorded files are skipped.
  * Files already applied out-of-band are tolerated because every migration in
    this repo is written with IF NOT EXISTS / ON CONFLICT DO NOTHING semantics;
    re-executing one is harmless and simply records it. `--baseline` is provided
    for the case where a file is NOT safely re-runnable: it records pending
    files as applied without executing them.

Usage:
    uv run --extra db python -m thaqip_ingestion.migrate [--dry-run] [--baseline]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DATABASE_URL = "postgres://thaqip:thaqip_dev@localhost:5433/thaqip"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
)
"""


def repo_root() -> Path:
    """Repo root inferred from this file's location.

    .../services/ingestion/src/thaqip_ingestion/migrate.py -> repo root
    """
    return Path(__file__).resolve().parents[4]


def migrations_dir() -> Path:
    override = os.environ.get("THAQIP_MIGRATIONS_DIR")
    if override:
        return Path(override)
    return repo_root() / "db" / "migrations"


def discover_migrations(directory: Path | str) -> list[Path]:
    """All *.sql files in `directory`, in lexical filename order.

    Lexical order is the ordering contract: filenames are zero-padded numeric
    prefixes, so lexical == numeric. Non-.sql files are ignored.
    """
    d = Path(directory)
    if not d.is_dir():
        return []
    return sorted((p for p in d.iterdir() if p.is_file() and p.suffix == ".sql"),
                  key=lambda p: p.name)


def pending_migrations(files: list[Path], applied: set[str]) -> list[Path]:
    """Files whose basename has not been recorded yet, order preserved."""
    return [f for f in files if f.name not in applied]


@dataclass
class MigrationResult:
    """Outcome of one runner invocation."""

    applied: list[str]
    skipped: list[str]
    baselined: list[str]
    pending: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.applied or self.baselined)

    def summary(self) -> str:
        parts = [f"applied={len(self.applied)}", f"skipped={len(self.skipped)}"]
        if self.baselined:
            parts.append(f"baselined={len(self.baselined)}")
        if self.pending:
            parts.append(f"pending={len(self.pending)}")
        return " ".join(parts)


class MigrationError(RuntimeError):
    """A migration file failed to apply; carries the offending filename."""

    def __init__(self, filename: str, cause: Exception) -> None:
        super().__init__(f"migration {filename} failed: {cause}")
        self.filename = filename
        self.cause = cause


async def fetch_applied(conn) -> set[str]:
    rows = await conn.fetch("SELECT filename FROM schema_migrations")
    return {r["filename"] for r in rows}


async def run_migrations(
    dsn: str,
    directory: Path | str | None = None,
    *,
    dry_run: bool = False,
    baseline: bool = False,
) -> MigrationResult:
    """Apply every unrecorded migration file. Safe to call repeatedly."""
    import asyncpg  # imported lazily so the module is importable without the db extra

    directory = Path(directory) if directory is not None else migrations_dir()
    files = discover_migrations(directory)

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(CREATE_TABLE_SQL)
        applied_before = await fetch_applied(conn)
        todo = pending_migrations(files, applied_before)
        skipped = [f.name for f in files if f.name in applied_before]

        if dry_run:
            return MigrationResult(applied=[], skipped=skipped, baselined=[],
                                   pending=[f.name for f in todo])

        applied: list[str] = []
        baselined: list[str] = []
        for path in todo:
            if baseline:
                await conn.execute(
                    "INSERT INTO schema_migrations (filename) VALUES ($1) "
                    "ON CONFLICT (filename) DO NOTHING",
                    path.name,
                )
                baselined.append(path.name)
                continue
            sql = path.read_text(encoding="utf-8")
            try:
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (filename) VALUES ($1) "
                        "ON CONFLICT (filename) DO NOTHING",
                        path.name,
                    )
            except Exception as exc:
                raise MigrationError(path.name, exc) from exc
            applied.append(path.name)
        return MigrationResult(applied=applied, skipped=skipped, baselined=baselined)
    finally:
        await conn.close()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Apply pending SQL migrations.")
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))
    ap.add_argument("--dir", dest="directory", default=None,
                    help="migrations directory (default: <repo>/db/migrations)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be applied, change nothing")
    ap.add_argument("--baseline", action="store_true",
                    help="record pending files as applied WITHOUT executing them")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = asyncio.run(
            run_migrations(args.dsn, args.directory,
                           dry_run=args.dry_run, baseline=args.baseline)
        )
    except MigrationError as exc:
        print(f"FAILED {exc}", file=sys.stderr)
        return 1
    for name in result.applied:
        print(f"applied  {name}")
    for name in result.baselined:
        print(f"baseline {name}")
    for name in result.pending:
        print(f"pending  {name}")
    for name in result.skipped:
        print(f"skipped  {name}")
    print(result.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
