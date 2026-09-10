"""Unit tests for the migration runner's pure logic and CLI wiring.

The DB-touching path (`run_migrations`) is exercised against the live database
separately; what is tested here is everything that decides *what* runs and in
*what order*, which is where a runner actually goes wrong.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from thaqip_ingestion.migrate import (
    DEFAULT_DATABASE_URL,
    MigrationError,
    MigrationResult,
    _parse_args,
    discover_migrations,
    migrations_dir,
    pending_migrations,
    repo_root,
)


@pytest.fixture()
def migdir(tmp_path: Path) -> Path:
    for name in ("0002_b.sql", "0001_a.sql", "0010_j.sql", "0009_i.sql"):
        (tmp_path / name).write_text(f"-- {name}\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("not a migration\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("also not\n", encoding="utf-8")
    return tmp_path


def test_discover_returns_only_sql_in_lexical_order(migdir: Path):
    assert [p.name for p in discover_migrations(migdir)] == [
        "0001_a.sql", "0002_b.sql", "0009_i.sql", "0010_j.sql",
    ]


def test_discover_zero_padding_makes_lexical_order_numeric(tmp_path: Path):
    # The padding is what stops 0010 sorting before 0002.
    for name in ("0002_x.sql", "0010_x.sql"):
        (tmp_path / name).write_text("", encoding="utf-8")
    assert [p.name for p in discover_migrations(tmp_path)] == ["0002_x.sql", "0010_x.sql"]


def test_discover_ignores_directories(tmp_path: Path):
    (tmp_path / "0001_a.sql").write_text("", encoding="utf-8")
    (tmp_path / "sub.sql").mkdir()
    assert [p.name for p in discover_migrations(tmp_path)] == ["0001_a.sql"]


def test_discover_missing_directory_is_empty_not_an_error(tmp_path: Path):
    assert discover_migrations(tmp_path / "nope") == []


def test_discover_accepts_a_string_path(migdir: Path):
    assert len(discover_migrations(str(migdir))) == 4


def test_pending_excludes_recorded_files_and_preserves_order(migdir: Path):
    files = discover_migrations(migdir)
    pending = pending_migrations(files, {"0001_a.sql", "0009_i.sql"})
    assert [p.name for p in pending] == ["0002_b.sql", "0010_j.sql"]


def test_pending_is_empty_when_everything_is_recorded(migdir: Path):
    files = discover_migrations(migdir)
    assert pending_migrations(files, {p.name for p in files}) == []


def test_pending_matches_on_basename_not_full_path(migdir: Path):
    # schema_migrations stores basenames, so a moved repo must not re-run files.
    files = discover_migrations(migdir)
    assert pending_migrations(files, {"0001_a.sql"})[0].name == "0002_b.sql"


def test_pending_ignores_recorded_names_that_no_longer_exist(migdir: Path):
    files = discover_migrations(migdir)
    pending = pending_migrations(files, {"0001_a.sql", "9999_deleted.sql"})
    assert [p.name for p in pending] == ["0002_b.sql", "0009_i.sql", "0010_j.sql"]


def test_repo_root_contains_the_real_migrations_directory():
    root = repo_root()
    assert (root / "db" / "migrations").is_dir()
    assert (root / "services" / "ingestion" / "pyproject.toml").is_file()


def test_migrations_dir_env_override(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("THAQIP_MIGRATIONS_DIR", str(tmp_path))
    assert migrations_dir() == tmp_path
    monkeypatch.delenv("THAQIP_MIGRATIONS_DIR")
    assert migrations_dir().name == "migrations"


def test_the_p2w_canonical_migration_is_discoverable_and_last():
    names = [p.name for p in discover_migrations(migrations_dir())]
    assert "0015_p2w_canonical.sql" in names
    assert names[-1] == "0015_p2w_canonical.sql"


def test_every_repo_migration_is_idempotent_by_construction():
    """The runner's tolerance of out-of-band application rests on this property.

    Any CREATE TABLE / CREATE INDEX in this repo must be IF NOT EXISTS, and any
    seed INSERT must be ON CONFLICT ... DO NOTHING, otherwise re-running a file
    that someone already piped through psql would fail.
    """
    offenders: list[str] = []
    for path in discover_migrations(migrations_dir()):
        text = path.read_text(encoding="utf-8")
        lowered = " ".join(text.lower().split())
        for stmt in ("create table ", "create index ", "create unique index "):
            start = 0
            while (idx := lowered.find(stmt, start)) != -1:
                if "if not exists" not in lowered[idx:idx + len(stmt) + 20]:
                    offenders.append(f"{path.name}: {stmt.strip()}")
                start = idx + len(stmt)
        if "insert into" in lowered and "on conflict" not in lowered:
            offenders.append(f"{path.name}: INSERT without ON CONFLICT")
    assert offenders == []


def test_migration_result_summary_and_changed_flag():
    empty = MigrationResult(applied=[], skipped=["0001_a.sql"], baselined=[])
    assert not empty.changed
    assert empty.summary() == "applied=0 skipped=1"

    did = MigrationResult(applied=["0015_p2w_canonical.sql"], skipped=[], baselined=[])
    assert did.changed
    assert "applied=1" in did.summary()

    base = MigrationResult(applied=[], skipped=[], baselined=["0007_x.sql"])
    assert base.changed
    assert "baselined=1" in base.summary()

    dry = MigrationResult(applied=[], skipped=[], baselined=[], pending=["0015_x.sql"])
    assert not dry.changed  # a dry run changes nothing
    assert "pending=1" in dry.summary()


def test_migration_error_names_the_offending_file():
    err = MigrationError("0015_p2w_canonical.sql", RuntimeError("syntax error"))
    assert err.filename == "0015_p2w_canonical.sql"
    assert "0015_p2w_canonical.sql" in str(err)
    assert "syntax error" in str(err)


def test_cli_defaults_and_flags(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    args = _parse_args([])
    assert args.dsn == DEFAULT_DATABASE_URL
    assert args.directory is None
    assert not args.dry_run and not args.baseline

    args = _parse_args(["--dry-run", "--baseline", "--dir", "/tmp/m", "--dsn", "postgres://x/y"])
    assert (args.dry_run, args.baseline, args.directory, args.dsn) == (
        True, True, "/tmp/m", "postgres://x/y")


def test_cli_reads_database_url_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://env/db")
    assert _parse_args([]).dsn == "postgres://env/db"
