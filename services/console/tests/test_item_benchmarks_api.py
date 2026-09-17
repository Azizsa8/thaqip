"""Item price benchmark API tests (PRD §5.4, T-BENCH-01/T-BENCH-02, T-FAKE-01)
plus the consent-withdrawal endpoint the benchmark job's honesty story
depends on. Needs the real dev database; skips rather than fails when
unreachable, same pattern as the other BoQ test files.
"""
from __future__ import annotations

import os
import uuid

import pytest

from thaqip_console import auth as auth_mod
from thaqip_console.app import app

DSN = os.environ.get("DATABASE_URL", "postgres://thaqip:thaqip_dev@localhost:5433/thaqip")

pytest.importorskip("thaqip_ingestion.item_benchmarks")
fastapi_testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture(scope="module")
def client():
    os.environ["DATABASE_URL"] = DSN
    auth_mod.SERVICE_TOKEN = "bench-api-test-token"
    try:
        with fastapi_testclient.TestClient(
                app, headers={"Authorization": "Bearer bench-api-test-token"}) as c:
            if c.get("/api/health").status_code != 200:
                pytest.skip("console database is not reachable")
            yield c
    except OSError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"database unavailable: {exc}")


@pytest.fixture
def catalogue_item():
    import asyncio

    import asyncpg

    async def _create():
        conn = await asyncpg.connect(DSN)
        try:
            row = await conn.fetchrow(
                "INSERT INTO catalogue_items (name_ar, unit) VALUES ($1, 'EA') RETURNING id",
                f"بند اختبار API المعايير {uuid.uuid4().hex[:8]}",
            )
            return row["id"]
        finally:
            await conn.close()

    async def _delete(item_id):
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute("DELETE FROM item_benchmarks WHERE catalogue_item_id=$1", item_id)
            await conn.execute("DELETE FROM catalogue_items WHERE id=$1", item_id)
        finally:
            await conn.close()

    item_id = asyncio.run(_create())
    yield item_id
    asyncio.run(_delete(item_id))


def test_no_benchmark_yet_returns_unavailable_never_a_number(client, catalogue_item):
    r = client.get(f"/api/catalogue-items/{catalogue_item}/benchmark")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is False
    assert body["message_ar"] == "لا توجد بيانات كافية"
    assert "p50" not in body


def test_unknown_catalogue_item_404s(client):
    r = client.get("/api/catalogue-items/999999999/benchmark")
    assert r.status_code == 404


def test_materialized_benchmark_is_served_with_full_provenance(client, catalogue_item):
    import asyncio

    import asyncpg

    from thaqip_ingestion.item_benchmarks import materialize_benchmarks

    async def _seed_and_materialize():
        conn = await asyncpg.connect(DSN)
        tenant_ids, doc_ids, user_ids = [], [], []
        try:
            for price in (100, 110, 120, 130, 140):
                tenant = await conn.fetchrow(
                    "INSERT INTO tenants (slug, name) VALUES ($1, 'Bench API Test') RETURNING id",
                    f"bench-api-{uuid.uuid4().hex[:12]}",
                )
                tenant_ids.append(tenant["id"])
                doc = await conn.fetchrow(
                    """INSERT INTO boq_documents (tenant_id, filename, storage_key, scan_status, consent_pool)
                       VALUES ($1, 'x.xlsx', $2, 'unscanned', true) RETURNING id""",
                    tenant["id"], f"bench-api/{uuid.uuid4()}.xlsx",
                )
                doc_ids.append(doc["id"])
                await conn.execute(
                    "INSERT INTO boq_consents (tenant_id, document_id, scope) VALUES ($1,$2,'item_pool')",
                    tenant["id"], doc["id"],
                )
                user = await conn.fetchrow(
                    "INSERT INTO users (tenant_id, username, password_hash) VALUES ($1,$2,'x') RETURNING id",
                    tenant["id"], f"bench-api-user-{uuid.uuid4().hex[:12]}",
                )
                user_ids.append(user["id"])
                await conn.execute(
                    """INSERT INTO boq_lines (document_id, line_no, description, unit, unit_price,
                                              catalogue_item_id, match_confirmed_by, match_confirmed_at)
                       VALUES ($1,1,'x','EA',$2,$3,$4,now())""",
                    doc["id"], price, catalogue_item, user["id"],
                )
            pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
            try:
                await materialize_benchmarks(pool)
            finally:
                await pool.close()
        finally:
            for doc_id in doc_ids:
                await conn.execute("DELETE FROM boq_documents WHERE id=$1", doc_id)
            for tenant_id in tenant_ids:
                await conn.execute("DELETE FROM users WHERE tenant_id=$1", tenant_id)
                await conn.execute("DELETE FROM tenants WHERE id=$1", tenant_id)
            await conn.close()

    asyncio.run(_seed_and_materialize())

    r = client.get(f"/api/catalogue-items/{catalogue_item}/benchmark")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True
    assert body["n_contributors"] == 5
    assert body["p50"] == 120.0
    assert body["period_start"] is not None
    assert body["period_end"] is not None
    assert body["index_used"] is None


def test_withdraw_consent_stops_future_pooling(client):
    from thaqip_console.app import TENANT_HEADER

    import asyncio

    import asyncpg

    async def _setup():
        conn = await asyncpg.connect(DSN)
        try:
            slug = f"bench-withdraw-{uuid.uuid4().hex[:12]}"
            tenant = await conn.fetchrow(
                "INSERT INTO tenants (slug, name) VALUES ($1, 'Withdraw Test') RETURNING id",
                slug,
            )
            doc = await conn.fetchrow(
                """INSERT INTO boq_documents (tenant_id, filename, storage_key, scan_status, consent_pool)
                   VALUES ($1, 'x.xlsx', $2, 'unscanned', true) RETURNING id""",
                tenant["id"], f"bench-withdraw/{uuid.uuid4()}.xlsx",
            )
            await conn.execute(
                "INSERT INTO boq_consents (tenant_id, document_id, scope) VALUES ($1,$2,'item_pool')",
                tenant["id"], doc["id"],
            )
            return tenant["id"], slug, doc["id"]
        finally:
            await conn.close()

    async def _check_withdrawn(document_id):
        conn = await asyncpg.connect(DSN)
        try:
            return await conn.fetchval(
                "SELECT withdrawn_at FROM boq_consents WHERE document_id=$1", document_id)
        finally:
            await conn.close()

    async def _cleanup(tenant_id, doc_id):
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute("DELETE FROM boq_documents WHERE id=$1", doc_id)
            await conn.execute("DELETE FROM tenants WHERE id=$1", tenant_id)
        finally:
            await conn.close()

    tenant_id, slug, doc_id = asyncio.run(_setup())
    try:
        r = client.post(f"/api/boq/{doc_id}/consent/withdraw", headers={TENANT_HEADER: slug})
        assert r.status_code == 200, r.text
        assert r.json()["withdrawn"] is True
        assert asyncio.run(_check_withdrawn(doc_id)) is not None

        # Idempotent: withdrawing again finds nothing left to withdraw.
        r2 = client.post(f"/api/boq/{doc_id}/consent/withdraw", headers={TENANT_HEADER: slug})
        assert r2.json()["withdrawn"] is False
    finally:
        asyncio.run(_cleanup(tenant_id, doc_id))


def test_withdraw_consent_other_tenant_404s_idor(client):
    from thaqip_console.app import TENANT_HEADER

    import asyncio

    import asyncpg

    async def _setup():
        conn = await asyncpg.connect(DSN)
        try:
            owner_slug = f"bench-withdraw-owner-{uuid.uuid4().hex[:12]}"
            other_slug = f"bench-withdraw-redteam-{uuid.uuid4().hex[:12]}"
            owner = await conn.fetchrow(
                "INSERT INTO tenants (slug, name) VALUES ($1, 'Withdraw Owner') RETURNING id",
                owner_slug,
            )
            other = await conn.fetchrow(
                "INSERT INTO tenants (slug, name) VALUES ($1, 'Withdraw Redteam') RETURNING id",
                other_slug,
            )
            doc = await conn.fetchrow(
                """INSERT INTO boq_documents (tenant_id, filename, storage_key, scan_status, consent_pool)
                   VALUES ($1, 'x.xlsx', $2, 'unscanned', true) RETURNING id""",
                owner["id"], f"bench-withdraw/{uuid.uuid4()}.xlsx",
            )
            await conn.execute(
                "INSERT INTO boq_consents (tenant_id, document_id, scope) VALUES ($1,$2,'item_pool')",
                owner["id"], doc["id"],
            )
            return owner["id"], other["id"], other_slug, doc["id"]
        finally:
            await conn.close()

    async def _cleanup(owner_id, other_id, doc_id):
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute("DELETE FROM boq_documents WHERE id=$1", doc_id)
            await conn.execute("DELETE FROM tenants WHERE id=$1", owner_id)
            await conn.execute("DELETE FROM tenants WHERE id=$1", other_id)
        finally:
            await conn.close()

    owner_id, other_id, other_slug, doc_id = asyncio.run(_setup())
    try:
        r = client.post(f"/api/boq/{doc_id}/consent/withdraw", headers={TENANT_HEADER: other_slug})
        assert r.status_code == 404
    finally:
        asyncio.run(_cleanup(owner_id, other_id, doc_id))
