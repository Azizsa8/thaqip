import pytest

from thaqip_ingestion.ops_health import close_stalled_runs


class _Pool:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    async def fetch(self, query):
        assert "finished_at IS NULL" in query
        return self.rows

    async def execute(self, query, *args):
        self.executed.append((query, args))


@pytest.mark.asyncio
async def test_close_stalled_runs_marks_only_old_unfinished_rows():
    pool = _Pool([
        {"id": 1, "connector": "etimad.awards_harvest", "started_at": object(), "age_minutes": 91.2},
        {"id": 2, "connector": "pricing.seed", "started_at": object(), "age_minutes": 2.0},
    ])

    closed = await close_stalled_runs(pool)

    assert closed == 1
    assert pool.executed[0][1][0] == 1
    assert '"stalled": true' in pool.executed[0][1][1]


@pytest.mark.asyncio
async def test_close_stalled_runs_respects_custom_threshold():
    pool = _Pool([
        {"id": 1, "connector": "unknown", "started_at": object(), "age_minutes": 45.0},
    ])

    closed = await close_stalled_runs(pool, older_than_minutes=90)

    assert closed == 0
    assert pool.executed == []
