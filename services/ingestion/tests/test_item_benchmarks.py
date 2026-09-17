"""Tests for thaqip_ingestion.item_benchmarks against the real dev database
(PRD T-BENCH-01/T-BENCH-02, T-FAKE-01, T-BOQ-04, T-MATCH-01's "unconfirmed
never enters benchmarks" carried through here). Skips rather than fabricates
a pass when Postgres is unreachable.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from thaqip_ingestion.item_benchmarks import materialize_benchmarks

DSN = os.environ.get("DATABASE_URL", "postgres://thaqip:thaqip_dev@localhost:5433/thaqip")


@pytest.fixture
async def pool():
    try:
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    except OSError as exc:
        pytest.skip(f"database unavailable: {exc}")
    yield p
    await p.close()


@pytest.fixture
async def catalogue_item(pool):
    row = await pool.fetchrow(
        "INSERT INTO catalogue_items (name_ar, unit) VALUES ($1, 'EA') RETURNING id",
        f"بند اختبار المعايير {uuid.uuid4().hex[:8]}",
    )
    item_id = row["id"]
    yield item_id
    await pool.execute("DELETE FROM item_benchmarks WHERE catalogue_item_id=$1", item_id)
    await pool.execute(
        "UPDATE boq_lines SET catalogue_item_id=NULL WHERE catalogue_item_id=$1", item_id)
    await pool.execute("DELETE FROM catalogue_items WHERE id=$1", item_id)


class _Rig:
    """Builds throwaway (tenant, document, line) triples contributing to one
    catalogue item, with full teardown respecting the NO ACTION FKs already
    found the hard way in test_milestone_reminders.py (pursuits) — the same
    order discipline applies here for boq_documents/boq_consents."""

    def __init__(self, pool, catalogue_item_id):
        self.pool = pool
        self.catalogue_item_id = catalogue_item_id
        self.tenant_ids: list[int] = []
        self.document_ids: list[int] = []

    async def add_contributor(
        self, unit_price: float, *, confirmed: bool = True, consented: bool = True,
        withdrawn: bool = False, created_at: datetime | None = None,
    ) -> int:
        tenant = await self.pool.fetchrow(
            "INSERT INTO tenants (slug, name) VALUES ($1, 'Benchmark Test Tenant') RETURNING id",
            f"bench-test-{uuid.uuid4().hex[:12]}",
        )
        tenant_id = tenant["id"]
        self.tenant_ids.append(tenant_id)
        created_at = created_at or datetime.now(UTC)
        doc = await self.pool.fetchrow(
            """INSERT INTO boq_documents (tenant_id, filename, storage_key, scan_status,
                                          consent_pool, created_at)
               VALUES ($1, 'test.xlsx', $2, 'unscanned', $3, $4) RETURNING id""",
            tenant_id, f"bench-test/{uuid.uuid4()}.xlsx", consented, created_at,
        )
        doc_id = doc["id"]
        self.document_ids.append(doc_id)
        if consented:
            withdrawn_at = created_at if withdrawn else None
            await self.pool.execute(
                """INSERT INTO boq_consents (tenant_id, document_id, scope, granted_at, withdrawn_at)
                   VALUES ($1,$2,'item_pool',$3,$4)""",
                tenant_id, doc_id, created_at, withdrawn_at,
            )
        confirmed_by = None
        if confirmed:
            user = await self.pool.fetchrow(
                """INSERT INTO users (tenant_id, username, password_hash)
                   VALUES ($1, $2, 'x') RETURNING id""",
                tenant_id, f"bench-user-{uuid.uuid4().hex[:12]}",
            )
            confirmed_by = user["id"]
        confirmed_at = created_at if confirmed else None
        await self.pool.execute(
            """INSERT INTO boq_lines (document_id, line_no, description, unit, unit_price,
                                      catalogue_item_id, match_confirmed_by, match_confirmed_at)
               VALUES ($1,1,'test line','EA',$2,$3,$4,$5)""",
            doc_id, unit_price, self.catalogue_item_id, confirmed_by, confirmed_at,
        )
        return tenant_id

    async def teardown(self):
        for doc_id in self.document_ids:
            await self.pool.execute("DELETE FROM boq_documents WHERE id=$1", doc_id)
        for tenant_id in self.tenant_ids:
            # users.tenant_id is NO ACTION too — must go before the tenant.
            await self.pool.execute("DELETE FROM users WHERE tenant_id=$1", tenant_id)
            await self.pool.execute("DELETE FROM tenants WHERE id=$1", tenant_id)


@pytest.fixture
async def rig(pool, catalogue_item):
    r = _Rig(pool, catalogue_item)
    yield r
    await r.teardown()


async def test_five_contributors_materializes_a_benchmark(pool, rig, catalogue_item):
    for price in (100, 110, 120, 130, 140):
        await rig.add_contributor(price)
    n = await materialize_benchmarks(pool)
    assert n >= 1

    row = await pool.fetchrow(
        "SELECT * FROM item_benchmarks WHERE catalogue_item_id=$1", catalogue_item)
    assert row is not None
    assert row["n_contributors"] == 5
    assert row["n_lines"] == 5
    assert float(row["p50"]) == 120.0
    assert row["index_used"] is None


async def test_four_contributors_never_materializes(pool, rig, catalogue_item):
    for price in (100, 110, 120, 130):
        await rig.add_contributor(price)
    await materialize_benchmarks(pool)
    row = await pool.fetchrow(
        "SELECT * FROM item_benchmarks WHERE catalogue_item_id=$1", catalogue_item)
    assert row is None


async def test_unconfirmed_lines_excluded_even_with_five_tenants(pool, rig, catalogue_item):
    for price in (100, 110, 120, 130, 140):
        await rig.add_contributor(price, confirmed=False)
    await materialize_benchmarks(pool)
    row = await pool.fetchrow(
        "SELECT * FROM item_benchmarks WHERE catalogue_item_id=$1", catalogue_item)
    assert row is None


async def test_unconsented_lines_excluded(pool, rig, catalogue_item):
    for price in (100, 110, 120, 130, 140):
        await rig.add_contributor(price, consented=False)
    await materialize_benchmarks(pool)
    row = await pool.fetchrow(
        "SELECT * FROM item_benchmarks WHERE catalogue_item_id=$1", catalogue_item)
    assert row is None


async def test_withdrawn_consent_excluded(pool, rig, catalogue_item):
    # 5 total, but one withdrew consent -> only 4 active contributors.
    for price in (100, 110, 120, 130):
        await rig.add_contributor(price)
    await rig.add_contributor(150, withdrawn=True)
    await materialize_benchmarks(pool)
    row = await pool.fetchrow(
        "SELECT * FROM item_benchmarks WHERE catalogue_item_id=$1", catalogue_item)
    assert row is None


async def test_contributions_outside_lookback_window_excluded(pool, rig, catalogue_item):
    old = datetime.now(UTC) - timedelta(days=800)  # older than the 730-day window
    for price in (100, 110, 120, 130):
        await rig.add_contributor(price)
    await rig.add_contributor(150, created_at=old)
    await materialize_benchmarks(pool)
    row = await pool.fetchrow(
        "SELECT * FROM item_benchmarks WHERE catalogue_item_id=$1", catalogue_item)
    assert row is None  # only 4 within the window


async def test_same_tenant_multiple_lines_counts_as_one_contributor(pool, catalogue_item):
    """n_contributors counts DISTINCT tenants, not lines: five lines from
    the same tenant must never be able to fabricate a 5-tenant benchmark."""
    tenant = await pool.fetchrow(
        "INSERT INTO tenants (slug, name) VALUES ($1, 'Single Tenant Rig') RETURNING id",
        f"bench-single-{uuid.uuid4().hex[:12]}",
    )
    tenant_id = tenant["id"]
    doc = await pool.fetchrow(
        """INSERT INTO boq_documents (tenant_id, filename, storage_key, scan_status, consent_pool)
           VALUES ($1, 'test.xlsx', $2, 'unscanned', true) RETURNING id""",
        tenant_id, f"bench-test/{uuid.uuid4()}.xlsx",
    )
    doc_id = doc["id"]
    await pool.execute(
        "INSERT INTO boq_consents (tenant_id, document_id, scope) VALUES ($1,$2,'item_pool')",
        tenant_id, doc_id,
    )
    user = await pool.fetchrow(
        "INSERT INTO users (tenant_id, username, password_hash) VALUES ($1,$2,'x') RETURNING id",
        tenant_id, f"bench-single-user-{uuid.uuid4().hex[:12]}",
    )
    for line_no, price in enumerate((100, 110, 120, 130, 140), start=1):
        await pool.execute(
            """INSERT INTO boq_lines (document_id, line_no, description, unit, unit_price,
                                      catalogue_item_id, match_confirmed_by, match_confirmed_at)
               VALUES ($1,$2,'test line','EA',$3,$4,$5,now())""",
            doc_id, line_no, price, catalogue_item, user["id"],
        )
    try:
        await materialize_benchmarks(pool)
        row = await pool.fetchrow(
            "SELECT * FROM item_benchmarks WHERE catalogue_item_id=$1", catalogue_item)
        assert row is None
    finally:
        await pool.execute("DELETE FROM boq_documents WHERE id=$1", doc_id)
        await pool.execute("DELETE FROM users WHERE tenant_id=$1", tenant_id)
        await pool.execute("DELETE FROM tenants WHERE id=$1", tenant_id)


async def test_rerun_upserts_rather_than_duplicating(pool, rig, catalogue_item):
    for price in (100, 110, 120, 130, 140):
        await rig.add_contributor(price)
    await materialize_benchmarks(pool)
    await materialize_benchmarks(pool)
    rows = await pool.fetch(
        "SELECT * FROM item_benchmarks WHERE catalogue_item_id=$1", catalogue_item)
    assert len(rows) == 1
