"""Credentials for the batteries that drive the running console.

The console refuses every data endpoint without a credential, so these tests
authenticate the same way a worker would: the service bearer token. It is read
from the environment first, then from var/credentials.env (written by
bin/bootstrap-auth.sh, gitignored, mode 600). Nothing here has a fallback
secret — with no token the live layers get 401 and fail loudly, which is the
correct outcome for a security gate.
"""
from __future__ import annotations

import os
from functools import cache
from pathlib import Path

CREDENTIALS_FILE = Path(__file__).resolve().parents[3] / "var" / "credentials.env"


@cache
def _file_values() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = CREDENTIALS_FILE.read_text()
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip("'\"")
    return values


def credential(name: str) -> str:
    return os.environ.get(name) or _file_values().get(name, "")


def service_token() -> str:
    return credential("THAQIP_API_TOKEN")


def auth_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {service_token()}"}
    if extra:
        headers.update(extra)
    return headers
