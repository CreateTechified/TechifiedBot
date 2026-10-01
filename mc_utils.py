import asyncio
import os
import uuid

import requests

PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile/lookup/name/{name}"
WHITELIST_URL = "https://whitelistsync.com/api/whitelist"


def get_api_key():
    return os.getenv("WLS_API_KEY") or None


def _headers():
    return {"X-API-KEY": get_api_key() or ""}


async def lookup_profile(name: str):
    """Checks a Minecraft username with Mojang.

    Returns (state, canonical_name, uuid):
      ("ok", "Steve", "xxxxxxxx-xxxx-...")  - the account exists
      ("not_found", None, None)             - no such account
      ("error", None, None)                 - couldn't reach Mojang / unexpected response

    This means no cracked accounts :D.. Sorry Tlauncher/Cracked users"""
    try:
        resp = await asyncio.to_thread(requests.get, PROFILE_URL.format(name=name), timeout=10)
    except requests.RequestException:
        return "error", None, None

    if resp.status_code == 200:
        try:
            data = resp.json()
            return "ok", data.get("name", name), str(uuid.UUID(data["id"]))
        except (ValueError, KeyError):
            return "error", None, None

    if resp.status_code in (204, 400, 404):
        return "not_found", None, None

    return "error", None, None


async def whitelist_add(player_uuid: str):
    """Returns (ok, status_code). status_code is None if the request itself failed."""
    try:
        resp = await asyncio.to_thread(
            requests.post, WHITELIST_URL, headers=_headers(), json={"uuid": player_uuid}, timeout=15
        )
    except requests.RequestException:
        return False, None
    return resp.status_code < 400, resp.status_code


async def whitelist_remove(player_uuid: str):
    """Returns (ok, status_code). status_code is None if the request itself failed."""
    try:
        resp = await asyncio.to_thread(
            requests.delete, f"{WHITELIST_URL}/{player_uuid}", headers=_headers(), timeout=15
        )
    except requests.RequestException:
        return False, None
    return resp.status_code < 400, resp.status_code


async def whitelist_names():
    """Returns a set of lowercase whitelisted names, or None if the API couldn't be queried."""
    try:
        resp = await asyncio.to_thread(requests.get, WHITELIST_URL, headers=_headers(), timeout=15)
    except requests.RequestException:
        return None

    if resp.status_code != 200:
        return None

    try:
        data = resp.json()
    except ValueError:
        return None

    return {
        player["name"].lower()
        for player in data
        if isinstance(player, dict) and player.get("name")
    }