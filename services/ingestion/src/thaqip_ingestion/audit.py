"""Capture-rate audit harness (ticket D4).

Weekly audit script: samples random tenders across multiple pages from the live
Etimad visitor API and checks presence in the local corpus to compute capture rate.
Phase exit criterion: >= 99% capture rate on two consecutive audits.

Run:
  DATABASE_URL=... uv run --extra db python -m thaqip_ingestion.audit [--sample-size 200]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
from datetime import UTC, datetime

import asyncpg

from . import db
from .etimad.client import EtimadClient

log = logging.getLogger("thaqip.audit")


async def run_audit(
    pool: asyncpg.Pool,
    client: EtimadClient,
    *,
    sample_size: int = 200,
    page_size: int = 50,
) -> dict:
    """Audit capture rate against live Etimad listing."""
    log.info("starting capture-rate audit (target sample: %d tenders)", sample_size)

    # 1. Fetch first page to learn total pages available
    first_page = await client.fetch_listing_page(1, page_size)
    total_count = first_page.totalCount
    if total_count <= 0:
        raise RuntimeError("source listing returned 0 tenders")

    total_pages = (total_count + page_size - 1) // page_size
    # Focus sample on newer-to-mid range of corpus (e.g. within top 100 pages or all)
    max_audit_page = min(total_pages, 100)
    pages_to_sample = max(1, sample_size // page_size + 2)
    selected_pages = sorted(random.sample(range(1, max_audit_page + 1), min(pages_to_sample, max_audit_page)))

    sampled_tenders = []
    for page in selected_pages:
        listing = await client.fetch_listing_page(page, page_size)
        sampled_tenders.extend(listing.data)
        if len(sampled_tenders) >= sample_size:
            break

    if len(sampled_tenders) > sample_size:
        sampled_tenders = random.sample(sampled_tenders, sample_size)

    total_sampled = len(sampled_tenders)
    if total_sampled == 0:
        raise RuntimeError("failed to sample any tenders from source")

    tender_ids = [t.tender_id for t in sampled_tenders]

    rows = await pool.fetch(
        """SELECT source_tender_id FROM tenders
           WHERE source = 'etimad' AND source_tender_id = ANY($1::bigint[])""",
        tender_ids,
    )
    found_ids = {r["source_tender_id"] for r in rows}
    missing = [t for t in sampled_tenders if t.tender_id not in found_ids]

    captured_count = len(found_ids)
    capture_rate = round(captured_count / total_sampled, 4)

    report = {
        "audited_at": datetime.now(UTC).isoformat(),
        "total_source_tenders": total_count,
        "sampled_pages": selected_pages,
        "sample_size": total_sampled,
        "captured": captured_count,
        "missing": len(missing),
        "capture_rate": capture_rate,
        "criterion_passed": capture_rate >= 0.99,
        "missing_samples": [
            {
                "tender_id": m.tender_id,
                "name": m.tender_name,
                "ref": m.reference_number,
                "submission_date": m.submition_date.isoformat() if m.submition_date else None,
            }
            for m in missing[:10]
        ],
    }

    # Record run in ingest_runs
    await pool.execute(
        """INSERT INTO ingest_runs (connector, finished_at, ok, pages, items_seen, items_new, items_changed, checkpoint)
           VALUES ('etimad.audit', now(), $1, $2, $3, $4, 0, $5::jsonb)""",
        report["criterion_passed"],
        len(selected_pages),
        total_sampled,
        captured_count,
        json.dumps(report, ensure_ascii=False),
    )

    return report


async def main() -> None:
    parser = argparse.ArgumentParser(description="Thaqip capture-rate audit harness")
    parser.add_argument("--sample-size", type=int, default=200, help="number of tenders to sample")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise ValueError("DATABASE_URL environment variable is required")

    pool = await db.connect(dsn)
    client = EtimadClient(rate_limit_per_sec=1.0)
    try:
        report = await run_audit(pool, client, sample_size=args.sample_size)
        print("\n=== THAQIP CAPTURE-RATE AUDIT REPORT ===")
        print(f"Audited At   : {report['audited_at']}")
        print(f"Sample Size  : {report['sample_size']}")
        print(f"Captured     : {report['captured']}")
        print(f"Missing      : {report['missing']}")
        print(f"Capture Rate : {report['capture_rate'] * 100:.2f}%")
        print(f"SLO Target   : >= 99.00% -> {'PASS' if report['criterion_passed'] else 'FAIL'}")
        if report["missing_samples"]:
            print("\nMissing Samples:")
            for s in report["missing_samples"]:
                print(f"  ID {s['tender_id']}: {s['name'][:60]} (ref: {s['ref']})")
        print("=========================================\n")
    finally:
        await client.aclose()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
