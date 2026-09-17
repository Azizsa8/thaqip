"""Tests for thaqip_ingestion.milestone_reminders against the real dev
database (PRD T-MILE-01: "e2e test creates a pursuit with a deadline in 2
days and asserts a message"). Skips rather than fabricates a pass when
Postgres is unreachable.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from thaqip_ingestion.milestone_reminders import send_due_reminders

DSN = os.environ.get("DATABASE_URL", "postgres://thaqip:thaqip_dev@localhost:5433/thaqip")


@pytest.fixture
async def pool():
    try:
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    except OSError as exc:
        pytest.skip(f"database unavailable: {exc}")
    yield p
    await p.close()


class _RecordingSender:
    """Stands in for alerts.Sender: records what it was asked to send,
    always reports success, no real Telegram/network call."""

    def __init__(self):
        self.sent: list[tuple[str, str, str, str]] = []

    async def send(self, channel, target, title, body):
        self.sent.append((channel, target, title, body))
        return "sent", None


@pytest.fixture
async def tenant_and_pursuit(pool):
    """A throwaway tenant with an active Telegram link, a throwaway tender,
    and a pursuit for it. Cleans up via cascade on tenant delete... except
    tenders has no tenant_id, so the tender is deleted explicitly too."""
    tenant = await pool.fetchrow(
        "INSERT INTO tenants (slug, name) VALUES ($1, 'Milestone Reminder Test') RETURNING id",
        f"mile-test-{uuid.uuid4().hex[:12]}",
    )
    tenant_id = tenant["id"]
    await pool.execute(
        """INSERT INTO telegram_links (tenant_id, chat_id, active)
           VALUES ($1, 'fake-chat-id', true)""",
        tenant_id,
    )
    tender = await pool.fetchrow(
        """INSERT INTO tenders (source, source_tender_id, reference_number, name,
                                activity_id, booklet_price, payload, content_hash)
           VALUES ('etimad', -abs(('x' || md5(random()::text))::bit(32)::int),
                   'mile-test-ref', 'Milestone reminder test tender', 111, 0,
                   '{}'::jsonb, md5(random()::text))
           RETURNING id""",
    )
    tender_id = tender["id"]
    pursuit = await pool.fetchrow(
        "INSERT INTO pursuits (tender_id, tenant_id) VALUES ($1,$2) RETURNING id",
        tender_id, tenant_id,
    )
    pursuit_id = pursuit["id"]
    yield tenant_id, pursuit_id, tender_id
    # pursuits.tenant_id/tender_id are NO ACTION (not cascading), so the
    # pursuit itself must go first, or these deletes raise a foreign key
    # violation — pursuit_milestones/notifications then cascade from it.
    await pool.execute("DELETE FROM pursuits WHERE id=$1", pursuit_id)
    await pool.execute("DELETE FROM telegram_links WHERE tenant_id=$1", tenant_id)
    await pool.execute("DELETE FROM tenants WHERE id=$1", tenant_id)
    await pool.execute("DELETE FROM tenders WHERE id=$1", tender_id)


async def _add_milestone(pool, pursuit_id, milestone, due_at):
    row = await pool.fetchrow(
        "INSERT INTO pursuit_milestones (pursuit_id, milestone, due_at) VALUES ($1,$2,$3) RETURNING id",
        pursuit_id, milestone, due_at,
    )
    return row["id"]


async def test_deadline_in_two_days_sends_a_t_minus_1_or_t_minus_3_message(pool, tenant_and_pursuit):
    # T-MILE-01's own example: a deadline 2 days out is inside the T-3
    # window (<= 3 days) but outside T-1 (> 1 day), so this must send T-3.
    _tenant_id, pursuit_id, _tender_id = tenant_and_pursuit
    await _add_milestone(pool, pursuit_id, "enquiries_deadline",
                          datetime.now(UTC) + timedelta(days=2))
    sender = _RecordingSender()
    sent = await send_due_reminders(pool, sender)
    assert sent >= 1
    assert len(sender.sent) >= 1
    channel, target, title, body = sender.sent[-1]
    assert channel == "telegram"
    assert target == "fake-chat-id"
    assert "استفسارات" in title or "استفسارات" in body or "T" in title

    row = await pool.fetchrow(
        """SELECT * FROM notifications
           WHERE pursuit_milestone_id = (SELECT id FROM pursuit_milestones
                                          WHERE pursuit_id=$1 AND milestone='enquiries_deadline')""",
        pursuit_id,
    )
    assert row is not None
    assert row["milestone_offset"] == "T-3"
    assert row["status"] == "sent"


async def test_deadline_in_twelve_hours_sends_t_minus_1(pool, tenant_and_pursuit):
    _tenant_id, pursuit_id, _tender_id = tenant_and_pursuit
    await _add_milestone(pool, pursuit_id, "submitted",
                          datetime.now(UTC) + timedelta(hours=12))
    sender = _RecordingSender()
    await send_due_reminders(pool, sender)

    row = await pool.fetchrow(
        """SELECT milestone_offset FROM notifications
           WHERE pursuit_milestone_id = (SELECT id FROM pursuit_milestones
                                          WHERE pursuit_id=$1 AND milestone='submitted')""",
        pursuit_id,
    )
    assert row["milestone_offset"] == "T-1"


async def test_completed_milestone_never_reminded(pool, tenant_and_pursuit):
    _tenant_id, pursuit_id, _tender_id = tenant_and_pursuit
    await pool.execute(
        """INSERT INTO pursuit_milestones (pursuit_id, milestone, due_at, completed_at)
           VALUES ($1, 'bond_issued', $2, now())""",
        pursuit_id, datetime.now(UTC) + timedelta(hours=12),
    )
    sender = _RecordingSender()
    await send_due_reminders(pool, sender)
    assert sender.sent == []


async def test_far_future_deadline_not_yet_reminded(pool, tenant_and_pursuit):
    _tenant_id, pursuit_id, _tender_id = tenant_and_pursuit
    await _add_milestone(pool, pursuit_id, "bond_issued",
                          datetime.now(UTC) + timedelta(days=30))
    sender = _RecordingSender()
    await send_due_reminders(pool, sender)
    assert sender.sent == []


async def test_no_double_send_across_two_job_runs(pool, tenant_and_pursuit):
    _tenant_id, pursuit_id, _tender_id = tenant_and_pursuit
    await _add_milestone(pool, pursuit_id, "opened",
                          datetime.now(UTC) + timedelta(hours=12))
    sender = _RecordingSender()
    first = await send_due_reminders(pool, sender)
    second = await send_due_reminders(pool, sender)
    assert first == 1
    assert second == 0
    assert len(sender.sent) == 1


async def test_no_linked_chat_skips_without_error(pool):
    tenant = await pool.fetchrow(
        "INSERT INTO tenants (slug, name) VALUES ($1, 'No Telegram Link Test') RETURNING id",
        f"mile-nolink-{uuid.uuid4().hex[:12]}",
    )
    tenant_id = tenant["id"]
    tender = await pool.fetchrow(
        """INSERT INTO tenders (source, source_tender_id, reference_number, name,
                                activity_id, booklet_price, payload, content_hash)
           VALUES ('etimad', -abs(('x' || md5(random()::text))::bit(32)::int),
                   'mile-nolink-ref', 'No-link test tender', 111, 0,
                   '{}'::jsonb, md5(random()::text))
           RETURNING id""",
    )
    tender_id = tender["id"]
    pursuit = await pool.fetchrow(
        "INSERT INTO pursuits (tender_id, tenant_id) VALUES ($1,$2) RETURNING id",
        tender_id, tenant_id,
    )
    try:
        await _add_milestone(pool, pursuit["id"], "awarded",
                              datetime.now(UTC) + timedelta(hours=6))
        sender = _RecordingSender()
        await send_due_reminders(pool, sender)
        assert sender.sent == []
    finally:
        await pool.execute("DELETE FROM pursuits WHERE id=$1", pursuit["id"])
        await pool.execute("DELETE FROM tenants WHERE id=$1", tenant_id)
        await pool.execute("DELETE FROM tenders WHERE id=$1", tender_id)
