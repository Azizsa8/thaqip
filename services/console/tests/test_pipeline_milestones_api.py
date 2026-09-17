"""Bid pipeline milestone tests (PRD §5.2, T-MILE-01) plus a regression test
for the pursuits multi-tenant uniqueness bug found while building this:
`pursuits` carried a tenant_id but kept its pre-multi-tenant
`UNIQUE (tender_id)` constraint, so a second tenant pursuing a tender another
tenant already pursues got a raw 500 instead of its own independent pursuit.

Needs the real dev database; skips rather than fails when unreachable, same
pattern as test_boq_api.py / test_eligibility_api.py.
"""
from __future__ import annotations

import os
import uuid

import pytest

from thaqip_console import auth as auth_mod
from thaqip_console.app import MILESTONE_SEQUENCE, TENANT_HEADER, app

DSN = os.environ.get("DATABASE_URL", "postgres://thaqip:thaqip_dev@localhost:5433/thaqip")
fastapi_testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture(scope="module")
def client():
    os.environ["DATABASE_URL"] = DSN
    auth_mod.SERVICE_TOKEN = "mile-api-test-token"
    try:
        with fastapi_testclient.TestClient(
                app, headers={"Authorization": "Bearer mile-api-test-token"}) as c:
            if c.get("/api/health").status_code != 200:
                pytest.skip("console database is not reachable")
            yield c
    except OSError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"database unavailable: {exc}")


@pytest.fixture(scope="module")
def other_tenant_slug(client):
    slug = f"mile-redteam-{uuid.uuid4().hex[:8]}"
    import asyncio

    import asyncpg

    async def _create():
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(
                "INSERT INTO tenants (slug, name) VALUES ($1, $2) ON CONFLICT (slug) DO NOTHING",
                slug, "Milestone Redteam Tenant",
            )
        finally:
            await conn.close()

    asyncio.run(_create())
    return slug


@pytest.fixture
def open_tender():
    """One throwaway 'etimad' tender with known enquiries/offer dates, so
    seeded milestones get real due_at values to assert on."""
    import asyncio

    import asyncpg

    async def _create():
        conn = await asyncpg.connect(DSN)
        try:
            row = await conn.fetchrow(
                """INSERT INTO tenders (source, source_tender_id, reference_number, name,
                                        activity_id, booklet_price, last_enquiries_date,
                                        last_offer_date, payload, content_hash)
                   VALUES ('etimad', -abs(('x' || md5(random()::text))::bit(32)::int),
                           'mile-api-test-ref', 'Milestone API test tender', 111, 0,
                           now() + interval '5 days', now() + interval '10 days',
                           '{}'::jsonb, md5(random()::text))
                   RETURNING id""",
            )
            return row["id"]
        finally:
            await conn.close()

    async def _delete(tender_id):
        conn = await asyncpg.connect(DSN)
        try:
            # pursuits.tender_id has no cascade; drop pursuits (and their
            # milestones, which DO cascade from pursuits) first.
            await conn.execute("DELETE FROM pursuits WHERE tender_id=$1", tender_id)
            await conn.execute("DELETE FROM tenders WHERE id=$1", tender_id)
        finally:
            await conn.close()

    tender_id = asyncio.run(_create())
    yield tender_id
    asyncio.run(_delete(tender_id))


def test_two_tenants_can_pursue_the_same_tender(client, other_tenant_slug, open_tender):
    """The core regression test for the uniqueness bug: tenant A and tenant
    B both pursuing the same shared tender must both succeed, as two
    independent pursuit rows."""
    r_a = client.post("/api/pursuits", json={"tender_id": open_tender})
    assert r_a.status_code == 200, r_a.text
    assert r_a.json()["created"] is True

    r_b = client.post("/api/pursuits", json={"tender_id": open_tender},
                       headers={TENANT_HEADER: other_tenant_slug})
    assert r_b.status_code == 200, r_b.text
    assert r_b.json()["created"] is True
    assert r_b.json()["id"] != r_a.json()["id"]


def test_pursuit_creation_seeds_the_full_milestone_sequence(client, open_tender):
    r = client.post("/api/pursuits", json={"tender_id": open_tender})
    pid = r.json()["id"]

    milestones = client.get(f"/api/pursuits/{pid}/milestones").json()
    assert [m["milestone"] for m in milestones] == list(MILESTONE_SEQUENCE)
    by_name = {m["milestone"]: m for m in milestones}
    assert by_name["enquiries_deadline"]["due_at"] is not None
    assert by_name["submitted"]["due_at"] is not None
    assert by_name["booklet_purchased"]["due_at"] is None  # no natural source date


def test_patch_milestone_sets_completion_and_data(client, open_tender):
    r = client.post("/api/pursuits", json={"tender_id": open_tender})
    pid = r.json()["id"]

    r2 = client.patch(f"/api/pursuits/{pid}/milestones/site_visit", json={
        "completed_at": "2026-09-20T10:00:00Z",
        "data": {"attendee": "أحمد"},
    })
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["completed_at"] is not None
    assert body["data"]["attendee"] == "أحمد"


def test_patch_milestone_rejects_unknown_milestone(client, open_tender):
    r = client.post("/api/pursuits", json={"tender_id": open_tender})
    pid = r.json()["id"]
    r2 = client.patch(f"/api/pursuits/{pid}/milestones/not_a_real_milestone", json={})
    assert r2.status_code == 422


def test_other_tenant_cannot_read_or_patch_milestones_idor(client, other_tenant_slug, open_tender):
    r = client.post("/api/pursuits", json={"tender_id": open_tender})
    pid = r.json()["id"]

    other_get = client.get(f"/api/pursuits/{pid}/milestones", headers={TENANT_HEADER: other_tenant_slug})
    assert other_get.status_code == 404

    other_patch = client.patch(
        f"/api/pursuits/{pid}/milestones/booklet_purchased",
        json={"completed_at": "2026-09-20T10:00:00Z"},
        headers={TENANT_HEADER: other_tenant_slug},
    )
    assert other_patch.status_code == 404
