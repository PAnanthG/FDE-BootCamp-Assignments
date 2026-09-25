#!/usr/bin/env python3
"""Preflight check for .env before `docker compose up`.

Stdlib only, deliberately: this has to run before any dependency is installed,
and its whole job is to fail fast with a clear message instead of letting the
compose stack die three minutes into a CLIP model download.

Never prints a secret. Values are reported as set/unset and, where a shape is
checkable, as well-formed or not.

    python3 scripts/check-env.py
"""

from __future__ import annotations

import os
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(ROOT, ".env")

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = DIM = RESET = ""

failures = 0
warnings = 0


def ok(msg: str) -> None:
    print(f"  {GREEN}[ok]{RESET}   {msg}")


def bad(msg: str) -> None:
    global failures
    failures += 1
    print(f"  {RED}[FAIL]{RESET} {msg}")


def warn(msg: str) -> None:
    global warnings
    warnings += 1
    print(f"  {YELLOW}[warn]{RESET} {msg}")


def load_env(path: str) -> dict[str, str]:
    """Minimal .env reader - no dependency on python-dotenv."""
    env: dict[str, str] = {}
    if not os.path.exists(path):
        return env
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            # Strip trailing "# TODO ..." markers and surrounding quotes.
            val = val.split("#", 1)[0].strip().strip("'\"")
            env[key.strip()] = val
    return env


def host_reachable(host: str, port: int, timeout: float = 6.0) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "TCP connect ok"
    except socket.gaierror as exc:
        return False, f"DNS lookup failed ({exc.strerror or exc})"
    except OSError as exc:
        return False, f"{exc.__class__.__name__}: {exc}"


def _ssl_context() -> ssl.SSLContext:
    """A context that can actually verify certificates on macOS.

    python.org builds ship without a CA bundle - `ssl.get_default_verify_paths()
    .cafile` is None until someone runs Install Certificates.command - so every
    HTTPS check here fails with CERTIFICATE_VERIFY_FAILED and looks exactly like
    an unreachable service. certifi is present in the project venv (httpx and
    qdrant-client both depend on it); fall back to it when the system store is
    empty rather than reporting a false outage.
    """
    ctx = ssl.create_default_context()
    if ssl.get_default_verify_paths().cafile is None:
        try:
            import certifi

            ctx.load_verify_locations(certifi.where())
        except ImportError:
            warn("no system CA bundle and certifi is not installed - HTTPS "
                 "checks below may report a false failure. On macOS run "
                 "'Install Certificates.command' from your Python folder, or "
                 "run this script with the project venv.")
    return ctx


def http_status(url: str, headers: dict[str, str], timeout: float = 10.0) -> tuple[int | None, str]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    ctx = _ssl_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, "reachable"
    except urllib.error.HTTPError as exc:
        return exc.code, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return None, f"{exc.reason}"
    except OSError as exc:
        return None, f"{exc.__class__.__name__}: {exc}"


def main() -> int:
    if not os.path.exists(ENV_PATH):
        print(f"{RED}No .env at {ENV_PATH}{RESET}")
        print("Copy .env.example to .env and fill it in.")
        return 1

    env = load_env(ENV_PATH)
    print(f"{DIM}Reading {ENV_PATH} - {len(env)} keys. Secret values are never printed.{RESET}\n")

    # --- required, no default -----------------------------------------------
    print("=== required credentials present ===")
    required = {
        "DATABASE_URL": "Neon Postgres pooled connection string",
        "PREFECT_API_URL": "Prefect Cloud workspace API URL",
        "PREFECT_API_KEY": "Prefect Cloud API key",
        "QDRANT_URL": "Qdrant Cloud cluster URL",
        "QDRANT_API_KEY": "Qdrant Cloud API key",
        "LLM_API_KEY": "vision-capable LLM provider key",
        "ADMIN_TOKEN": "admin bearer token for mutating endpoints",
    }
    for key, what in required.items():
        val = env.get(key, "")
        if not val:
            bad(f"{key} is unset - {what}")
        elif val in {"change-me", "TODO"}:
            bad(f"{key} still holds a placeholder value")
        else:
            ok(f"{key} set ({len(val)} chars)")

    # ADMIN_TOKEN weakness is a real finding, not a style note: an empty or
    # guessable token disables auth on every mutating endpoint.
    admin = env.get("ADMIN_TOKEN", "")
    if admin and len(admin) < 20:
        warn(f"ADMIN_TOKEN is short ({len(admin)} chars) - generate a longer one")

    # --- shape checks --------------------------------------------------------
    print("\n=== value shapes ===")
    db = env.get("DATABASE_URL", "")
    if db:
        if not db.startswith(("postgres://", "postgresql://")):
            bad("DATABASE_URL does not look like a postgres URL")
        elif "-pooler." not in db:
            warn("DATABASE_URL is not Neon's POOLED endpoint - the example asks for "
                 "the pooled connection; direct connections exhaust under worker load")
        else:
            ok("DATABASE_URL looks like a Neon pooled URL")
        if db and "sslmode=" not in db:
            warn("DATABASE_URL has no sslmode - Neon expects sslmode=require")

    prefect_url = env.get("PREFECT_API_URL", "")
    if prefect_url and "/accounts/" not in prefect_url:
        warn("PREFECT_API_URL usually contains /accounts/<id>/workspaces/<id>")
    elif prefect_url:
        ok("PREFECT_API_URL has the expected workspace shape")

    qurl = env.get("QDRANT_URL", "")
    if qurl and not qurl.startswith("http"):
        bad("QDRANT_URL must include the scheme (https://...)")
    elif qurl:
        ok("QDRANT_URL has a scheme")

    # --- reachability --------------------------------------------------------
    print("\n=== service reachability ===")
    if db.startswith(("postgres://", "postgresql://")):
        parsed = urllib.parse.urlparse(db)
        if parsed.hostname:
            good, detail = host_reachable(parsed.hostname, parsed.port or 5432)
            (ok if good else bad)(f"Neon {parsed.hostname}:{parsed.port or 5432} - {detail}")

    if qurl.startswith("http"):
        key = env.get("QDRANT_API_KEY", "")
        status, detail = http_status(
            qurl.rstrip("/") + "/collections", {"api-key": key} if key else {}
        )
        if status == 200:
            ok("Qdrant reachable and API key accepted")
        elif status in (401, 403):
            bad(f"Qdrant reachable but rejected the API key (HTTP {status})")
        elif status is None:
            bad(f"Qdrant unreachable - {detail}")
        else:
            warn(f"Qdrant returned HTTP {status}")

    if prefect_url.startswith("http"):
        key = env.get("PREFECT_API_KEY", "")
        status, detail = http_status(
            prefect_url.rstrip("/") + "/health",
            {"Authorization": f"Bearer {key}"} if key else {},
        )
        if status in (200, 204):
            ok("Prefect Cloud reachable and API key accepted")
        elif status in (401, 403):
            bad(f"Prefect Cloud rejected the API key (HTTP {status})")
        elif status is None:
            bad(f"Prefect Cloud unreachable - {detail}")
        else:
            warn(f"Prefect Cloud returned HTTP {status}")

    # --- settings that matter for the benchmark ------------------------------
    print("\n=== settings relevant to later stages ===")
    provider = env.get("STORAGE_PROVIDER", "local")
    if provider == "local":
        ok("STORAGE_PROVIDER=local - fine for Stage 2, switch before the Fly deploy")
    else:
        ok(f"STORAGE_PROVIDER={provider}")

    model = env.get("LLM_MODEL", "")
    if model:
        ok(f"LLM_MODEL={model} (must be VISION-capable - it is shown video frames)")
    else:
        warn("LLM_MODEL unset - falls back to the app default")

    if env.get("SEED_SAMPLE_VIDEOS", "true").lower() == "true":
        ok("SEED_SAMPLE_VIDEOS=true - compose blocks the API until 4 talks are indexed")
        warn("first run takes several minutes; measure idle p95 only AFTER seeding ends")

    print()
    if failures:
        print(f"{RED}NOT READY: {failures} failure(s), {warnings} warning(s){RESET}")
        return 1
    print(f"{GREEN}READY{RESET}" + (f" - {warnings} warning(s)" if warnings else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
