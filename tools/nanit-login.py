#!/usr/bin/env python3
"""Authenticate with Nanit cloud and save session for local testing.

Saves tokens + baby info to .nanit-session (JSON) for use by other tools.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from getpass import getpass
from pathlib import Path

import aiohttp

from aionanit import NanitAuthError, NanitClient, NanitConnectionError, NanitMfaRequiredError

SESSION_FILE = Path(__file__).resolve().parents[1] / ".nanit-session"


async def async_main() -> int:
    parser = argparse.ArgumentParser(description="Login to Nanit cloud.")
    parser.add_argument("--email", help="Nanit account email")
    parser.add_argument("--password", help="Nanit account password")
    args = parser.parse_args()

    email = args.email or input("Email: ")
    password = args.password or getpass("Password: ")

    async with aiohttp.ClientSession() as session:
        try:
            client = NanitClient(session)
            try:
                result = await client.async_login(email, password)
            except NanitMfaRequiredError as err:
                code = getpass("MFA code: ")
                result = await client.async_verify_mfa(email, password, err.mfa_token, code)

            access_token = result["access_token"]
            refresh_token = result["refresh_token"]
            babies = await client.async_get_babies()

            if not babies:
                print("Error: no babies found on account", file=sys.stderr)
                return 1

            baby = babies[0]
            session_data = {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "baby_uid": baby.uid,
                "camera_uid": baby.camera_uid,
                "baby_name": baby.name,
            }

            SESSION_FILE.write_text(json.dumps(session_data, indent=2) + "\n")

            print(f"Logged in. Baby: {session_data['baby_name']} (uid={session_data['baby_uid']})")
            print(f"Session saved to {SESSION_FILE.name}")
            return 0

        except (NanitAuthError, NanitConnectionError) as err:
            print(f"Error: {err}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
