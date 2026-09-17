"""Eligibility/fit scoring API tests (PRD §5.1, T-ELIG-01/T-ELIG-02).

Needs the real dev database; skips rather than fails when unreachable, same
pattern as test_boq_api.py.
"""
from __future__ import annotations

import os
import uuid

import pytest

from thaqip_console import auth as auth_mod
from thaqip_console.app import TENANT_HEADER, app

DSN = os.environ.get("DATABASE_URL", "postgres://thaqip:thaqip_dev@localhost:5433/thaqip")

pytest.importorskip("thaqip_ingestion.fit_score")
fastapi_testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture(scope="module")
def client():
    os.environ["DATABASE_URL"] = DSN
    auth_mod.SERVICE_TOKEN = "elig-api-test-token"
    try:
        with fastapi_testclient.TestClient(
                app, headers={"Authorization": "Bearer elig-api-test-token"}) as c:
            if c.get("/api/health").status_code != 200:
                pytest.skip("console database is not reachable")
            yield c
    except OSError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"database unavailable: {exc}")


@pytest.fixture(scope="module")
def other_tenant_slug(client):
    slug = f"elig-redteam-{uuid.uuid4().hex[:8]}"
    import asyncio

    import asyncpg

    async def _create():
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(
                "INSERT INTO tenants (slug, name) VALUES ($1, $2) ON CONFLICT (slug) DO NOTHING",
                slug, "Eligibility Redteam Tenant",
            )
        finally:
            await conn.close()

    asyncio.run(_create())
    return slug


@pytest.fixture
def open_tender():
    """One throwaway 'etimad' tender, cleaned up after the test. Yields
    (tender_id,)."""
    import asyncio

    import asyncpg

    async def _create():
        conn = await asyncpg.connect(DSN)
        try:
            row = await conn.fetchrow(
                """INSERT INTO tenders (source, source_tender_id, reference_number, name,
                                        activity_id, booklet_price, payload, content_hash)
                   VALUES ('etimad', -abs(('x' || md5(random()::text))::bit(32)::int),
                           'elig-api-test-ref', 'Eligibility API test tender', 111, 0,
                           '{}'::jsonb, md5(random()::text))
                   RETURNING id""",
            )
            return row["id"]
        finally:
            await conn.close()

    async def _delete(tender_id):
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute("DELETE FROM tenders WHERE id=$1", tender_id)
        finally:
            await conn.close()

    tender_id = asyncio.run(_create())
    yield tender_id
    asyncio.run(_delete(tender_id))


def test_company_profile_defaults_to_empty(client):
    # A brand-new tenant, never PUT to before, so this is independent of
    # whatever other tests in this run have done to the default tenant's row.
    fresh_slug = f"elig-fresh-{uuid.uuid4().hex[:8]}"
    import asyncio

    import asyncpg

    async def _create():
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(
                "INSERT INTO tenants (slug, name) VALUES ($1, $2) ON CONFLICT (slug) DO NOTHING",
                fresh_slug, "Eligibility Fresh Tenant",
            )
        finally:
            await conn.close()

    asyncio.run(_create())

    r = client.get("/api/company-profile", headers={TENANT_HEADER: fresh_slug})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["activity_ids"] == []
    assert body["regions"] == []


def test_company_profile_put_then_get_roundtrips(client):
    payload = {
        "activity_ids": [111, 222],
        "regions": ["الرياض"],
        "classification_grades": ["التكييف المركزي"],
        "certifications": ["ISO 9001"],
        "min_project_value": 100000,
        "max_project_value": 5000000,
    }
    r = client.put("/api/company-profile", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["activity_ids"] == [111, 222]
    assert body["regions"] == ["الرياض"]

    r2 = client.get("/api/company-profile")
    assert r2.json()["activity_ids"] == [111, 222]


def test_other_tenant_has_its_own_isolated_profile(client, other_tenant_slug):
    """T-BOQ-03-style IDOR check for the company profile: setting tenant A's
    profile must never be visible to tenant B."""
    client.put("/api/company-profile", json={"activity_ids": [999], "regions": [],
                                              "classification_grades": [], "certifications": []})
    other = client.get("/api/company-profile", headers={TENANT_HEADER: other_tenant_slug})
    assert other.status_code == 200
    assert other.json()["activity_ids"] != [999]


def test_fit_unconfirmed_when_details_not_fetched(client, open_tender):
    # T-ELIG-02: no tender_details row at all yet -> must never claim eligible.
    client.put("/api/company-profile", json={"activity_ids": [111], "regions": [],
                                              "classification_grades": [], "certifications": []})
    r = client.get(f"/api/tenders/{open_tender}/fit")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["details_fetched"] is False
    assert body["classification_status"] == "unconfirmed"
    assert body["label"] == "غير مؤكد التصنيف"


def test_fit_matches_when_details_present_and_requirement_met(client, open_tender):
    import asyncio

    import asyncpg

    async def _insert_details():
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(
                """INSERT INTO tender_details
                     (tender_id, classification_required, classification_text, execution_location)
                   VALUES ($1, true, 'أعمال الميكانيكية، نظام التكييف المركزي', 'منطقة الرياض')""",
                open_tender,
            )
        finally:
            await conn.close()

    asyncio.run(_insert_details())

    client.put("/api/company-profile", json={
        "activity_ids": [111], "regions": ["الرياض"],
        "classification_grades": ["التكييف المركزي"], "certifications": [],
    })
    r = client.get(f"/api/tenders/{open_tender}/fit")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["classification_status"] == "matched"
    assert body["label"] == "متوافق مع المتطلبات المنشورة"
    assert body["score"] == 100


def test_fit_404_for_unknown_tender(client):
    r = client.get("/api/tenders/999999999/fit")
    assert r.status_code == 404
