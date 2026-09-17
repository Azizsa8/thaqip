"""BoQ catalogue matching API tests (PRD §5.3, T-MATCH-01).

Needs the real dev database and MinIO; skips rather than fails when
unreachable, same pattern as test_boq_api.py.
"""
from __future__ import annotations

import io
import os
import uuid

import pytest

from thaqip_console import auth as auth_mod
from thaqip_console.app import TENANT_HEADER, app

DSN = os.environ.get("DATABASE_URL", "postgres://thaqip:thaqip_dev@localhost:5433/thaqip")

pytest.importorskip("thaqip_ingestion.boq")
pytest.importorskip("thaqip_ingestion.boq_review")
pytest.importorskip("thaqip_ingestion.catalogue_match")
openpyxl = pytest.importorskip("openpyxl")
fastapi_testclient = pytest.importorskip("fastapi.testclient")

_HEADER = ["م", "وصف البند", "المواصفات", "الوحدة", "الكمية", "سعر الوحدة", "الإجمالي"]


def _build_boq_xlsx() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "BOQ"
    ws.append(_HEADER)
    ws.append([1, "توريد وتركيب باب حديد مقاوم للحريق ضلفة واحدة", "90 دقيقة", "EA", 2, 500, 1000])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture(scope="module")
def client():
    os.environ["DATABASE_URL"] = DSN
    os.environ.setdefault("MINIO_ENDPOINT", "localhost:9002")
    os.environ.setdefault("MINIO_ROOT_USER", "thaqip")
    os.environ.setdefault("MINIO_ROOT_PASSWORD", "thaqip_dev_secret")
    auth_mod.SERVICE_TOKEN = "catalogue-api-test-token"
    try:
        with fastapi_testclient.TestClient(
                app, headers={"Authorization": "Bearer catalogue-api-test-token"}) as c:
            if c.get("/api/health").status_code != 200:
                pytest.skip("console database is not reachable")
            yield c
    except OSError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"database unavailable: {exc}")


@pytest.fixture(scope="module")
def other_tenant_slug(client):
    slug = f"catalogue-redteam-{uuid.uuid4().hex[:8]}"
    import asyncio

    import asyncpg

    async def _create():
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(
                "INSERT INTO tenants (slug, name) VALUES ($1, $2) ON CONFLICT (slug) DO NOTHING",
                slug, "Catalogue Redteam Tenant",
            )
        finally:
            await conn.close()

    asyncio.run(_create())
    return slug


@pytest.fixture
def two_door_catalogue_items(client):
    """Two EA-unit fire-door catalogue items, distinct capacities, real
    text taken from the PRD's own example item (§5.3), so the deterministic
    matcher has >=2 unit-compatible candidates to rank for the synthetic
    upload's "90 دقيقة" line.

    catalogue_items is shared, global data (no tenant_id) with no per-test
    isolation, so these must be cleaned up afterwards — an earlier version
    of this fixture left rows behind across runs, and once several runs'
    worth of identically-worded "90 دقيقة" items piled up, the matcher's
    top pick became whichever one happened to be inserted first, not this
    run's fixture rows, breaking the id-equality assertions below.
    """
    ids = []
    for name, code in (
        ("باب حديد مقاوم للحريق ضلفة واحدة 90 دقيقة", f"door-90-{uuid.uuid4().hex[:6]}"),
        ("باب حديد مقاوم للحريق ضلفة واحدة 60 دقيقة", f"door-60-{uuid.uuid4().hex[:6]}"),
    ):
        r = client.post("/api/catalogue-items", json={"name_ar": name, "unit": "EA", "code": code})
        assert r.status_code == 200, r.text
        ids.append(r.json()["id"])
    yield ids

    import asyncio

    import asyncpg

    async def _cleanup():
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(
                "UPDATE boq_lines SET catalogue_item_id=NULL, match_confirmed_by=NULL, "
                "match_confirmed_at=NULL WHERE catalogue_item_id = ANY($1::bigint[])", ids)
            await conn.execute("DELETE FROM catalogue_items WHERE id = ANY($1::bigint[])", ids)
        finally:
            await conn.close()

    asyncio.run(_cleanup())


def _upload(client, *, data: bytes):
    return client.post(
        "/api/boq/upload",
        files={"file": ("boq.xlsx", data,
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"consent_pool": "false"},
    )


def test_catalogue_item_search_and_create(client):
    name = f"بند اختبار فريد {uuid.uuid4().hex[:8]}"
    r = client.post("/api/catalogue-items", json={"name_ar": name, "unit": "M2"})
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]
    try:
        r2 = client.get("/api/catalogue-items", params={"q": name})
        assert r2.status_code == 200
        assert any(row["id"] == item_id for row in r2.json())

        r3 = client.get("/api/catalogue-items", params={"unit": "M2", "q": name})
        assert all(row["unit"] == "M2" for row in r3.json())
    finally:
        import asyncio

        import asyncpg

        async def _cleanup():
            conn = await asyncpg.connect(DSN)
            try:
                await conn.execute("DELETE FROM catalogue_items WHERE id=$1", item_id)
            finally:
                await conn.close()

        asyncio.run(_cleanup())


def test_upload_suggests_a_match_with_two_candidates(client, two_door_catalogue_items):
    data = _build_boq_xlsx()
    r = _upload(client, data=data)
    assert r.status_code == 200, r.text
    doc_id = r.json()["document_id"]

    got = client.get(f"/api/boq/{doc_id}")
    line = got.json()["lines"][0]
    assert line["catalogue_item_id"] == two_door_catalogue_items[0]  # 90-min door ranks first
    assert line["match_confidence"] is not None
    assert line["match_confirmed_by"] is None  # suggestion only, not yet confirmed
    candidates = line["match_candidates"]
    assert len(candidates) == 2
    assert all(c["kind"] == "suggested" for c in candidates)


def test_confirming_a_suggested_match_sets_confirmed_by(client, two_door_catalogue_items):
    data = _build_boq_xlsx()
    r = _upload(client, data=data)
    doc_id = r.json()["document_id"]
    line_id = client.get(f"/api/boq/{doc_id}").json()["lines"][0]["id"]

    r2 = client.patch(f"/api/boq/{doc_id}/lines/{line_id}/match",
                       json={"catalogue_item_id": two_door_catalogue_items[0]})
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["catalogue_item_id"] == two_door_catalogue_items[0]
    # match_confirmed_by is whoever's user_id the caller resolves to; the
    # service token these tests authenticate with has none (see auth.py's
    # principal() — "service" callers get user_id: None, same as
    # boq_documents.uploaded_by elsewhere), so match_confirmed_at is the
    # meaningful, always-real signal that a confirmation actually happened.
    assert body["match_confirmed_at"] is not None


def test_editing_to_a_different_candidate_reconfirms(client, two_door_catalogue_items):
    data = _build_boq_xlsx()
    r = _upload(client, data=data)
    doc_id = r.json()["document_id"]
    line_id = client.get(f"/api/boq/{doc_id}").json()["lines"][0]["id"]

    # Estimator disagrees with the suggestion and picks the 60-minute door instead.
    r2 = client.patch(f"/api/boq/{doc_id}/lines/{line_id}/match",
                       json={"catalogue_item_id": two_door_catalogue_items[1]})
    assert r2.status_code == 200, r2.text
    assert r2.json()["catalogue_item_id"] == two_door_catalogue_items[1]
    assert r2.json()["match_confirmed_at"] is not None


def test_clearing_a_match_unconfirms_it(client, two_door_catalogue_items):
    data = _build_boq_xlsx()
    r = _upload(client, data=data)
    doc_id = r.json()["document_id"]
    line_id = client.get(f"/api/boq/{doc_id}").json()["lines"][0]["id"]

    client.patch(f"/api/boq/{doc_id}/lines/{line_id}/match",
                 json={"catalogue_item_id": two_door_catalogue_items[0]})
    r2 = client.patch(f"/api/boq/{doc_id}/lines/{line_id}/match", json={"catalogue_item_id": None})
    assert r2.status_code == 200, r2.text
    assert r2.json()["catalogue_item_id"] is None
    assert r2.json()["match_confirmed_by"] is None
    assert r2.json()["match_confirmed_at"] is None


def test_unknown_catalogue_item_rejected(client, two_door_catalogue_items):
    data = _build_boq_xlsx()
    r = _upload(client, data=data)
    doc_id = r.json()["document_id"]
    line_id = client.get(f"/api/boq/{doc_id}").json()["lines"][0]["id"]

    r2 = client.patch(f"/api/boq/{doc_id}/lines/{line_id}/match",
                       json={"catalogue_item_id": 999999999})
    assert r2.status_code == 422


def test_other_tenant_cannot_confirm_a_match_idor(client, other_tenant_slug, two_door_catalogue_items):
    data = _build_boq_xlsx()
    r = _upload(client, data=data)
    doc_id = r.json()["document_id"]
    line_id = client.get(f"/api/boq/{doc_id}").json()["lines"][0]["id"]

    other = client.patch(
        f"/api/boq/{doc_id}/lines/{line_id}/match",
        json={"catalogue_item_id": two_door_catalogue_items[0]},
        headers={TENANT_HEADER: other_tenant_slug},
    )
    assert other.status_code == 404

    # Still unconfirmed for the real owner afterwards.
    line = client.get(f"/api/boq/{doc_id}").json()["lines"][0]
    assert line["match_confirmed_by"] is None
