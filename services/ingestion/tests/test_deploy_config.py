"""The production compose config, rendered exactly as the server will see it.

Pins the properties whose failure is silent until it is expensive: a stateful
service without a named volume (lost on the first recreate; this happened
with `volumes: !reset`), a port published beyond loopback, migrations
re-running as init scripts on a production volume, or the console losing its
proxy-trust settings.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


PROD_ENV = {"POSTGRES_PASSWORD": "pg-secret-x", "MINIO_ROOT_USER": "mu", "MINIO_ROOT_PASSWORD": "mp",
            "TYPESENSE_API_KEY": "tk", "CLOUDFLARE_TUNNEL_TOKEN": "t"}


def _render(*files: str, env: dict | None = None, expect_ok: bool = True):
    if not shutil.which("docker"):
        pytest.skip("docker CLI not available")
    args = ["docker", "compose"]
    for f in files:
        args += ["-f", str(REPO / f)]
    out = subprocess.run(
        [*args, "config", "--format", "json"], cwd=REPO, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(Path.home()), **(env or PROD_ENV)},
        timeout=60, check=False)
    if not expect_ok:
        return out
    if out.returncode != 0:
        pytest.fail(f"compose config failed: {out.stderr[:400]}")
    return json.loads(out.stdout)


@pytest.fixture(scope="module")
def prod() -> dict:
    return _render("docker-compose.yml", "docker-compose.prod.yml")


@pytest.mark.parametrize("service,target", [
    ("postgres", "/var/lib/postgresql/data"), ("redis", "/data"),
    ("minio", "/data"), ("typesense", "/data")])
def test_stateful_services_keep_data_on_named_volumes(prod, service, target):
    mounts = prod["services"][service].get("volumes") or []
    named = [m for m in mounts if m["type"] == "volume" and m["target"] == target and m.get("source")]
    assert named, f"{service} has no named volume at {target}: data would die with the container"
    assert named[0]["source"] in prod["volumes"]


def test_production_publishes_nothing_beyond_loopback(prod):
    for name, svc in prod["services"].items():
        for port in svc.get("ports") or []:
            assert port.get("host_ip") == "127.0.0.1", f"{name} publishes {port} beyond loopback"
    published = {n for n, s in prod["services"].items() if s.get("ports")}
    assert published <= {"postgres"}, f"unexpected published services: {published}"


def test_production_postgres_does_not_run_migrations_as_init_scripts(prod):
    targets = [m["target"] for m in prod["services"]["postgres"].get("volumes") or []]
    assert "/docker-entrypoint-initdb.d" not in targets


def test_console_behind_tunnel_trusts_cloudflare_and_forces_secure_cookies(prod):
    env = prod["services"]["console"]["environment"]
    assert env.get("THAQIP_TRUSTED_PROXY") == "cloudflare"
    assert env.get("THAQIP_COOKIE_SECURE") == "1"
    assert prod["services"]["superset"]["environment"].get("SUPERSET_COOKIE_SECURE") == "1"


def test_every_service_rotates_its_logs(prod):
    for name, svc in prod["services"].items():
        opts = (svc.get("logging") or {}).get("options") or {}
        assert opts.get("max-size"), f"{name} logs are unbounded"


def test_no_dev_default_reaches_production_services(prod):
    assert "thaqip_dev" not in json.dumps(prod), "a dev default leaked into the production config"


@pytest.mark.parametrize("missing", sorted(PROD_ENV))
def test_production_refuses_to_start_without_each_secret(missing):
    env = {k: v for k, v in PROD_ENV.items() if k != missing}
    out = _render("docker-compose.yml", "docker-compose.prod.yml", env=env, expect_ok=False)
    assert out.returncode != 0 and missing in out.stderr, f"rendered without {missing}"
