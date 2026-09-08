#!/usr/bin/env python3
"""Local readiness checks for Thaqip's Modal 24/7 runtime.

This script intentionally avoids importing ``modal_app``. Modal may not be
installed on the operator machine yet, and importing the app can trigger SDK
side effects. Instead, it parses the deployment module and validates the lanes
that must exist before a real ``modal deploy`` attempt.
"""

from __future__ import annotations

import ast
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODAL_APP = ROOT / "deploy" / "modal" / "modal_app.py"
REQUIRED_SECRET_NAME = "thaqip-runtime"
REQUIRED_ENV = ("DATABASE_URL",)
OPTIONAL_ENV = (
    "REDIS_URL",
    "TYPESENSE_URL",
    "TYPESENSE_KEY",
    "TELEGRAM_BOT_TOKEN",
    "THAQIP_ANTHROPIC_API_KEY",
)
EXPECTED_SCHEDULED = {
    "delta_poller": "Every 5 minutes Etimad newest-first pass",
    "awards_harvest": "Every 6 hours award/offers corpus growth",
    "award_watch": "Every 4 hours pursued-award watcher",
    "pricing_seed": "Every 6 hours seed price hypotheses for active pursuits",
    "forsah_pull": "Hourly Forsah opportunity pull",
    "reminders": "Hourly pursuit deadline reminders",
    "reconcile": "Daily corpus gap reconciliation",
    "daily_digest": "Daily KSA digest",
}
EXPECTED_MANUAL = {
    "backfill": "Manual historical corpus backfill",
    "bulk_index": "Manual Typesense rebuild",
}


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def _decorator_has_schedule(decorator: ast.AST) -> bool:
    return isinstance(decorator, ast.Call) and any(
        kw.arg == "schedule" for kw in decorator.keywords
    )


def _function_inventory() -> tuple[set[str], set[str]]:
    tree = ast.parse(MODAL_APP.read_text(encoding="utf-8"))
    scheduled: set[str] = set()
    manual: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        modal_function = False
        has_schedule = False
        for deco in node.decorator_list:
            if (
                isinstance(deco, ast.Call)
                and isinstance(deco.func, ast.Attribute)
                and deco.func.attr == "function"
            ):
                modal_function = True
                has_schedule = has_schedule or _decorator_has_schedule(deco)
        if modal_function and has_schedule:
            scheduled.add(node.name)
        elif modal_function:
            manual.add(node.name)
    return scheduled, manual


def _source_contains_secret_reference() -> bool:
    return f'from_name("{REQUIRED_SECRET_NAME}")' in MODAL_APP.read_text(encoding="utf-8")


def run_checks() -> list[Check]:
    scheduled, manual = _function_inventory()
    checks: list[Check] = []

    missing_scheduled = sorted(set(EXPECTED_SCHEDULED) - scheduled)
    checks.append(Check(
        "scheduled lanes",
        not missing_scheduled,
        "all required scheduled lanes present" if not missing_scheduled else "missing: " + ", ".join(missing_scheduled),
    ))

    missing_manual = sorted(set(EXPECTED_MANUAL) - manual)
    checks.append(Check(
        "manual lanes",
        not missing_manual,
        "all manual lanes present" if not missing_manual else "missing: " + ", ".join(missing_manual),
    ))

    checks.append(Check(
        "Modal secret reference",
        _source_contains_secret_reference(),
        f"uses Modal secret {REQUIRED_SECRET_NAME!r}",
    ))

    checks.append(Check(
        "Modal CLI",
        shutil.which("modal") is not None,
        "modal CLI found" if shutil.which("modal") else "install with: pipx install modal or uv tool install modal",
        required=False,
    ))

    missing_env = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    checks.append(Check(
        "local required env",
        not missing_env,
        "required env present locally" if not missing_env else "missing locally: " + ", ".join(missing_env),
        required=False,
    ))

    present_optional = [name for name in OPTIONAL_ENV if os.environ.get(name)]
    checks.append(Check(
        "local optional env",
        True,
        "present: " + ", ".join(present_optional) if present_optional else "none present locally; acceptable before Modal secret creation",
        required=False,
    ))

    return checks


def main() -> int:
    checks = run_checks()
    for check in checks:
        status = "PASS" if check.ok else ("WARN" if not check.required else "FAIL")
        print(f"[{status}] {check.name}: {check.detail}")
    failed_required = [c for c in checks if c.required and not c.ok]
    return 1 if failed_required else 0


if __name__ == "__main__":
    raise SystemExit(main())
