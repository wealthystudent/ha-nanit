#!/usr/bin/env python3
"""Fetch activity events directly from the Nanit cloud API.

Reads session from .nanit-session (created by nanit-login.py).
Calls: GET https://api.nanit.com/babies/{baby_uid}/messages?limit=N
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import aiohttp

from aionanit.rest import NANIT_API_HEADERS

SESSION_FILE = Path(__file__).resolve().parents[1] / ".nanit-session"


async def async_main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Nanit activity events.")
    parser.add_argument("--limit", type=int, default=10, help="Number of events (default: 10)")
    args = parser.parse_args()

    if not SESSION_FILE.exists():
        print("No session found. Run: just login", file=sys.stderr)
        return 1

    data = json.loads(SESSION_FILE.read_text())
    token = data["access_token"]
    baby_uid = data["baby_uid"]

    url = f"https://api.nanit.com/babies/{baby_uid}/messages"
    headers = {**NANIT_API_HEADERS, "Authorization": token}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params={"limit": args.limit}, headers=headers) as resp:
            if resp.status == 401:
                print("Token expired. Run: just login", file=sys.stderr)
                return 1
            if resp.status != 200:
                text = await resp.text()
                print(f"Error: HTTP {resp.status}\n{text}", file=sys.stderr)
                return 1
            body = await resp.json()
            print(json.dumps(body, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
