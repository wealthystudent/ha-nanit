#!/usr/bin/env python3
"""Fetch camera network diagnostics from the Nanit cloud API.

Reads session from .nanit-session (created by nanit-login.py).
Calls: GET https://api.nanit.com/babies and extracts camera.network info.

Usage:
    just network              # single fetch
    just network --watch 60   # repeat every 60s
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import aiohttp

from aionanit.rest import NANIT_API_HEADERS

SESSION_FILE = Path(__file__).resolve().parents[1] / ".nanit-session"


def _print_network(babies: list[dict], *, request_num: int = 0) -> None:
    """Print network info for each baby/camera."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    header = f"[{timestamp}]"
    if request_num:
        header += f" Request #{request_num}"
    print(f"\n{header}")
    print("-" * 50)

    for baby in babies:
        name = baby.get("name", "?")
        camera = baby.get("camera") or {}
        network = camera.get("network")

        if network is None:
            print(f"  {name}: no network data")
            continue

        ssid = network.get("ssid", "?")
        freq = network.get("freq")
        level = network.get("level")
        print(f"  {name}:")
        print(f"    SSID:      {ssid}")
        print(f"    Signal:    {level} dBm" if level is not None else "    Signal:    n/a")
        print(f"    Frequency: {freq} MHz" if freq is not None else "    Frequency: n/a")

        # Show any extra fields in camera.network we might not know about
        known = {"ssid", "freq", "level"}
        extra = {k: v for k, v in network.items() if k not in known}
        if extra:
            print(f"    Extra:     {extra}")


async def _fetch_babies(
    session: aiohttp.ClientSession,
    token: str,
) -> list[dict] | None:
    """Fetch /babies and return the raw list, or None on auth failure."""
    async with session.get(
        "https://api.nanit.com/babies",
        headers={**NANIT_API_HEADERS, "Authorization": token},
    ) as resp:
        if resp.status == 401:
            print("Token expired. Run: just login", file=sys.stderr)
            return None
        if resp.status != 200:
            text = await resp.text()
            print(f"Error: HTTP {resp.status}\n{text}", file=sys.stderr)
            return None
        body = await resp.json()
        return body.get("babies", [])


async def async_main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Nanit camera network info.")
    parser.add_argument(
        "--watch",
        type=int,
        metavar="SECONDS",
        help="Repeat every N seconds (e.g. --watch 60)",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print full raw JSON response",
    )
    args = parser.parse_args()

    if not SESSION_FILE.exists():
        print("No session found. Run: just login", file=sys.stderr)
        return 1

    data = json.loads(SESSION_FILE.read_text())
    token = data["access_token"]

    async with aiohttp.ClientSession() as session:
        request_num = 0
        while True:
            request_num += 1
            babies = await _fetch_babies(session, token)
            if babies is None:
                return 1

            if args.raw:
                for baby in babies:
                    network = (baby.get("camera") or {}).get("network")
                    print(json.dumps({"name": baby.get("name"), "network": network}, indent=2))
            else:
                _print_network(babies, request_num=request_num)

            if not args.watch:
                break

            try:
                print(f"\nNext request in {args.watch}s (Ctrl+C to stop)")
                await asyncio.sleep(args.watch)
            except (KeyboardInterrupt, asyncio.CancelledError):
                print("\nStopped.")
                break

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
