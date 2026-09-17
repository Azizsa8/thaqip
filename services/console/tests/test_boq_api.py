"""BoQ workbench API contract tests (PRD §5.3, ticket for db/migrations/0022).

Needs the real dev database and MinIO (both already running via
docker-compose) and the ingestion source tree for the parser/review engine;
skips rather than fails when either is unreachable so this file is safe to
collect anywhere.

Run:
    cd services/console
    DATABASE_URL=postgres://thaqip:thaqip_dev@localhost:5433/thaqip \
        uv run pytest tests/test_boq_api.py -q
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
openpyxl = pytest.importorskip("openpyxl")
fastapi_testclient = pytest.importorskip("fastapi.testclient")

# Arabic header row the parser recognizes (see thaqip_ingestion.boq._COLUMN_PATTERNS):
# item_no, description, unit, qty, unit_price, total.
_HEADER = ["م", "وصف البند", "المواصفات", "الوحدة", "الكمية", "سعر الوحدة", "الإجمالي"]


def _build_boq_xlsx() -> bytes:
    """A tiny synthetic BoQ workbook: one clean line, one arithmetic error."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "BOQ"
    ws.append(_HEADER)
    # item_no, description, spec, unit, qty, unit_price, total
    ws.append([1, "توريد وتركيب باب حديد مقاوم للحريق", "90 دقيقة", "EA", 2, 500, 1000])
    # Deliberately wrong total (2 * 300 = 600, not 999) -> arithmetic_mismatch finding.
    ws.append([2, "توريد وتركيب لوحة كهربائية رئيسية", "400 أمبير", "EA", 1, 300, 999])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture(scope="module")
def client():
    os.environ["DATABASE_URL"] = DSN
    os.environ.setdefault("MINIO_ENDPOINT", "localhost:9002")
    os.environ.setdefault("MINIO_ROOT_USER", "thaqip")
    os.environ.setdefault("MINIO_ROOT_PASSWORD", "thaqip_dev_secret")
    auth_mod.SERVICE_TOKEN = "boq-api-test-token"
    try:
        with fastapi_testclient.TestClient(
                app, headers={"Authorization": "Bearer boq-api-test-token"}) as c:
            if c.get("/api/health").status_code != 200:
                pytest.skip("console database is not reachable")
            yield c
    except OSError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"database unavailable: {exc}")


@pytest.fixture(scope="module")
def other_tenant_slug(client):
    """A second tenant, created directly in the DB (same pattern as
    services/ingestion/tests/test_battery_security.py's `redteam` fixture),
    used only to prove tenant B cannot read tenant A's BoQ document.
    """
    slug = f"boq-redteam-{uuid.uuid4().hex[:8]}"
    import asyncio

    import asyncpg

    async def _create():
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(
                "INSERT INTO tenants (slug, name) VALUES ($1, $2) ON CONFLICT (slug) DO NOTHING",
                slug, "BoQ Redteam Tenant",
            )
        finally:
            await conn.close()

    asyncio.run(_create())
    return slug


def _upload(client, *, data: bytes, filename: str = "boq.xlsx", consent_pool: bool = False):
    return client.post(
        "/api/boq/upload",
        files={"file": (filename, data,
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"consent_pool": "true" if consent_pool else "false"},
    )


def test_upload_parses_lines_and_flags_arithmetic_mismatch(client):
    data = _build_boq_xlsx()
    r = _upload(client, data=data)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["line_count"] == 2
    assert body["scan_status"] == "unscanned"
    assert body["document_id"] > 0
    assert body["total_computed"] is not None

    rules = {f["rule"] for f in body["findings"]}
    assert "arithmetic_mismatch" in rules, body["findings"]


def test_get_document_returns_lines_and_findings(client):
    data = _build_boq_xlsx()
    uploaded = _upload(client, data=data)
    assert uploaded.status_code == 200, uploaded.text
    doc_id = uploaded.json()["document_id"]

    r = client.get(f"/api/boq/{doc_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["document"]["id"] == doc_id
    assert len(body["lines"]) == 2
    assert len(body["findings"]) >= 1


def test_list_documents_includes_the_upload(client):
    data = _build_boq_xlsx()
    uploaded = _upload(client, data=data)
    doc_id = uploaded.json()["document_id"]

    r = client.get("/api/boq", params={"limit": 200})
    assert r.status_code == 200, r.text
    ids = {row["id"] for row in r.json()}
    assert doc_id in ids


def test_other_tenant_cannot_read_document_idor(client, other_tenant_slug):
    """The core IDOR requirement: a document belongs to the uploading tenant
    and 404s for everyone else, never a 403 that would confirm existence."""
    data = _build_boq_xlsx()
    uploaded = _upload(client, data=data)
    assert uploaded.status_code == 200, uploaded.text
    doc_id = uploaded.json()["document_id"]

    # Sanity: the owning tenant (default, via the service token) can read it.
    own = client.get(f"/api/boq/{doc_id}")
    assert own.status_code == 200

    other = client.get(f"/api/boq/{doc_id}", headers={TENANT_HEADER: other_tenant_slug})
    assert other.status_code == 404

    other_list = client.get("/api/boq", headers={TENANT_HEADER: other_tenant_slug})
    assert other_list.status_code == 200
    assert doc_id not in {row["id"] for row in other_list.json()}

    other_delete = client.delete(f"/api/boq/{doc_id}", headers={TENANT_HEADER: other_tenant_slug})
    assert other_delete.status_code == 404

    # And the document must still exist for its real owner afterwards.
    still_there = client.get(f"/api/boq/{doc_id}")
    assert still_there.status_code == 200


def test_non_xlsx_extension_is_rejected(client):
    r = _upload(client, data=b"just some text", filename="boq.txt")
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_file_type"
    assert "message_ar" in r.json()["detail"]


def test_xlsx_extension_with_wrong_magic_bytes_is_rejected(client):
    """T-UPL-01: extension alone is never trusted, only the real magic bytes."""
    fake = b"not a real zip/xlsx file, just text pretending to be one"
    r = _upload(client, data=fake, filename="fake.xlsx")
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_file_signature"
    assert "message_ar" in r.json()["detail"]


def test_delete_removes_the_document(client):
    data = _build_boq_xlsx()
    uploaded = _upload(client, data=data)
    doc_id = uploaded.json()["document_id"]

    r = client.delete(f"/api/boq/{doc_id}")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True

    gone = client.get(f"/api/boq/{doc_id}")
    assert gone.status_code == 404


def test_delete_removes_a_consented_document(client):
    """A document uploaded with consent_pool=True also writes a boq_consents
    row (see the upload handler). boq_consents.document_id must cascade on
    delete (db/migrations/0022_boq_workbench.sql) or this 404s with a foreign
    key violation instead of succeeding — this exact bug was found by manual
    review and fixed by adding ON DELETE CASCADE to that column.
    """
    data = _build_boq_xlsx()
    uploaded = _upload(client, data=data, consent_pool=True)
    assert uploaded.status_code == 200, uploaded.text
    doc_id = uploaded.json()["document_id"]

    r = client.delete(f"/api/boq/{doc_id}")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True

    gone = client.get(f"/api/boq/{doc_id}")
    assert gone.status_code == 404
