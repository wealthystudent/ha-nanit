#!/usr/bin/env python3
"""Probe Nanit cloud API for activity/feeding/care log endpoints.

This is a discovery tool for issue #49 (Feeding and Activity Data).
It tries speculative API endpoints to determine which ones exist and
whether they return data, are gated behind a subscription, or don't exist.

Reads session from .nanit-session (created by nanit-login.py).

Interpretation guide:
  - HTTP 200 + data  → Endpoint exists and works! Capture the response.
  - HTTP 200 + empty → Endpoint exists but no data (maybe no logs entered).
  - HTTP 403         → Endpoint exists but requires a higher subscription tier.
  - HTTP 401         → Token expired. Run: just login
  - HTTP 404         → Endpoint does not exist at this path.
  - HTTP 5xx         → Server error (endpoint may exist but is broken).

Usage:
    python3 tools/nanit-activities.py              # probe all endpoints
    python3 tools/nanit-activities.py --verbose    # include full response bodies
    python3 tools/nanit-activities.py --endpoint activities  # probe one endpoint
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import aiohttp

from aionanit.rest import NANIT_API_HEADERS

SESSION_FILE = Path(__file__).resolve().parents[1] / ".nanit-session"
BASE_URL = "https://api.nanit.com"

# Speculative endpoints to probe, based on Nanit URL patterns.
# Format: (name, method, path_template, params, description)
ENDPOINTS: list[tuple[str, str, str, dict[str, Any], str]] = [
    (
        "activities",
        "GET",
        "/babies/{baby_uid}/activities",
        {"limit": 20},
        "Speculative: activity/care log feed",
    ),
    (
        "care_logs",
        "GET",
        "/babies/{baby_uid}/care_logs",
        {"limit": 20},
        "Speculative: care logs (feeding, diaper)",
    ),
    (
        "logs",
        "GET",
        "/babies/{baby_uid}/logs",
        {"limit": 20},
        "Speculative: generic logs endpoint",
    ),
    (
        "feedings",
        "GET",
        "/babies/{baby_uid}/feedings",
        {"limit": 20},
        "Speculative: feeding-specific endpoint",
    ),
    (
        "diapers",
        "GET",
        "/babies/{baby_uid}/diapers",
        {"limit": 20},
        "Speculative: diaper-specific endpoint",
    ),
    (
        "insights",
        "GET",
        "/babies/{baby_uid}/insights",
        {},
        "Speculative: insights/analytics data",
    ),
    (
        "sleep",
        "GET",
        "/babies/{baby_uid}/sleep",
        {},
        "Speculative: sleep sessions/analysis",
    ),
    (
        "sleep_summary",
        "GET",
        "/babies/{baby_uid}/sleep_summary",
        {},
        "Speculative: sleep summary/stats",
    ),
    (
        "timeline",
        "GET",
        "/babies/{baby_uid}/timeline",
        {"limit": 20},
        "Speculative: activity timeline",
    ),
    (
        "events",
        "GET",
        "/babies/{baby_uid}/events",
        {"limit": 20},
        "Known older endpoint (may return more event types than /messages)",
    ),
    (
        "messages_extended",
        "GET",
        "/babies/{baby_uid}/messages",
        {"limit": 50},
        "Known endpoint with larger limit (check for non-MOTION/SOUND types)",
    ),
    (
        "subscriptions",
        "GET",
        "/babies/{baby_uid}/subscriptions",
        {},
        "Subscription/plan info (reveals your tier)",
    ),
    (
        "permissions",
        "GET",
        "/babies/{baby_uid}/permissions",
        {},
        "Baby permissions (may show feature gates)",
    ),
    (
        "user",
        "GET",
        "/user",
        {},
        "Current user info (may show subscription tier)",
    ),
]


def _format_status(status: int) -> str:
    """Return a colored/annotated status interpretation."""
    if status == 200:
        return f"{status} OK (endpoint exists!)"
    if status == 401:
        return f"{status} UNAUTHORIZED (token expired — run: just login)"
    if status == 403:
        return f"{status} FORBIDDEN (endpoint exists but subscription-gated!)"
    if status == 404:
        return f"{status} NOT FOUND (endpoint does not exist)"
    if status == 405:
        return f"{status} METHOD NOT ALLOWED (endpoint exists, wrong HTTP method)"
    if status >= 500:
        return f"{status} SERVER ERROR (endpoint may exist but is broken)"
    return f"{status} (unexpected)"


async def _probe_endpoint(
    session: aiohttp.ClientSession,
    token: str,
    baby_uid: str,
    name: str,
    method: str,
    path_template: str,
    params: dict[str, Any],
    description: str,
    verbose: bool,
) -> dict[str, Any]:
    """Probe a single endpoint and return the result."""
    path = path_template.format(baby_uid=baby_uid)
    url = f"{BASE_URL}{path}"
    headers = {**NANIT_API_HEADERS, "Authorization": token}

    result: dict[str, Any] = {
        "name": name,
        "method": method,
        "url": url,
        "params": params,
        "description": description,
    }

    try:
        if method == "GET":
            resp = await session.get(
                url,
                params=params if params else None,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            )
        else:
            resp = await session.request(
                method,
                url,
                params=params if params else None,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            )

        result["status"] = resp.status
        result["status_text"] = _format_status(resp.status)

        # Try to parse response body
        content_type = resp.headers.get("Content-Type", "")
        if "json" in content_type or resp.status in (200, 403):
            try:
                body = await resp.json(content_type=None)
                result["body"] = body
                if isinstance(body, dict):
                    result["keys"] = list(body.keys())
            except (json.JSONDecodeError, aiohttp.ContentTypeError):
                text = await resp.text()
                result["body_text"] = text[:500] if text else "(empty)"
        else:
            text = await resp.text()
            result["body_text"] = text[:200] if text else "(empty)"

    except aiohttp.ClientError as err:
        result["error"] = str(err)

    return result


async def async_main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe Nanit API for activity/feeding/care log endpoints.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Interpretation:
  200 + data  = Endpoint works! We found it.
  200 + empty = Endpoint exists but no data logged yet.
  403         = Endpoint exists but needs higher subscription (Insights plan).
  404         = Endpoint does not exist at this path.
  401         = Token expired. Run: just login
""",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Show full response bodies")
    parser.add_argument(
        "--endpoint",
        "-e",
        help="Probe only this endpoint (by name)",
        choices=[ep[0] for ep in ENDPOINTS],
    )
    parser.add_argument("--json", action="store_true", help="Output raw JSON results")
    args = parser.parse_args()

    if not SESSION_FILE.exists():
        print("No session found. Run: just login", file=sys.stderr)
        return 1

    data = json.loads(SESSION_FILE.read_text())
    token = data["access_token"]
    baby_uid = data["baby_uid"]
    baby_name = data.get("baby_name", "unknown")

    endpoints_to_probe = ENDPOINTS
    if args.endpoint:
        endpoints_to_probe = [ep for ep in ENDPOINTS if ep[0] == args.endpoint]

    print("Probing Nanit API for activity endpoints...")
    print(f"Baby: {baby_name} (uid={baby_uid})")
    print(f"Endpoints to probe: {len(endpoints_to_probe)}")
    print("=" * 70)

    results: list[dict[str, Any]] = []

    async with aiohttp.ClientSession() as session:
        for name, method, path_template, params, description in endpoints_to_probe:
            result = await _probe_endpoint(
                session,
                token,
                baby_uid,
                name,
                method,
                path_template,
                params,
                description,
                args.verbose,
            )
            results.append(result)

            if not args.json:
                print(f"\n  [{name}] {method} {result['url']}")
                print(f"  {description}")
                if params:
                    print(f"  Params: {params}")
                print(f"  Result: {result.get('status_text', result.get('error', 'unknown'))}")

                if result.get("keys"):
                    print(f"  Response keys: {result['keys']}")

                if args.verbose and result.get("body"):
                    print(f"  Body: {json.dumps(result['body'], indent=2)[:2000]}")
                elif args.verbose and result.get("body_text"):
                    print(f"  Body: {result['body_text']}")

                # Highlight interesting findings
                status = result.get("status", 0)
                if status == 200:
                    body = result.get("body")
                    if isinstance(body, dict) and body:
                        print("  >>> FOUND DATA! This endpoint works.")
                    elif isinstance(body, list) and body:
                        print(f"  >>> FOUND {len(body)} items! This endpoint works.")
                    elif isinstance(body, dict) and not body:
                        print("  >>> Endpoint exists but returned empty object.")
                    elif isinstance(body, list) and not body:
                        print("  >>> Endpoint exists but returned empty list (no data logged?).")
                elif status == 403:
                    print("  >>> ENDPOINT EXISTS but is subscription-gated (needs Insights plan).")

            # Stop early if token is expired
            if result.get("status") == 401:
                if not args.json:
                    print("\n  Token expired. Run: just login")
                break

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        # Summary
        print("\n" + "=" * 70)
        print("SUMMARY")
        print("=" * 70)

        found = [r for r in results if r.get("status") == 200]
        gated = [r for r in results if r.get("status") == 403]
        missing = [r for r in results if r.get("status") == 404]
        method_wrong = [r for r in results if r.get("status") == 405]

        if found:
            print(f"\n  WORKING ({len(found)}):")
            for r in found:
                keys = r.get("keys", [])
                print(f"    - {r['name']}: {r['url']} (keys: {keys})")

        if gated:
            print(f"\n  SUBSCRIPTION-GATED ({len(gated)}):")
            for r in gated:
                print(f"    - {r['name']}: {r['url']}")
            print("    These endpoints EXIST but need a higher plan (Insights).")
            print("    A tester with Insights can confirm they return data.")

        if method_wrong:
            print(f"\n  WRONG METHOD ({len(method_wrong)}):")
            for r in method_wrong:
                print(f"    - {r['name']}: {r['url']} (try POST?)")

        if missing:
            print(f"\n  NOT FOUND ({len(missing)}):")
            for r in missing:
                print(f"    - {r['name']}: {r['url']}")

        # Actionable next steps
        print("\n" + "-" * 70)
        print("NEXT STEPS")
        print("-" * 70)
        if gated:
            print("  Subscription-gated endpoints were found! A tester with the")
            print("  Insights plan should run this script to confirm they get data.")
        elif found:
            print("  Working endpoints found! Run with --verbose to see full data.")
            print("  Share the output in the GitHub issue.")
        else:
            print("  No activity endpoints found with these guesses.")
            print("  A tester with Insights should use mitmproxy to capture the")
            print("  actual API calls made by the Nanit app. See the tester guide.")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
