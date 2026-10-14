"""Steam's public store APIs (no account, no key): search by name and artwork/release info by App ID.

Used to identify any game for the wishlist (one Steam App ID per game, whatever the download sites call it) and
for big artwork (portrait capsule, hero banner) in the Library and couch mode.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import quote

log = logging.getLogger(__name__)

SEARCH = "https://store.steampowered.com/api/storesearch/?term={term}&l=english&cc=US"
ITEMS = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/?input_json={payload}"
ASSET_BASE = "https://shared.akamai.steamstatic.com/store_item_assets/"
STORE_PAGE = "https://store.steampowered.com/app/{appid}/"


def store_url(appid: int) -> str:
    return STORE_PAGE.format(appid=int(appid))


async def search(client, term: str, limit: int = 12) -> list[dict]:
    """Steam store search: [{appid, name, image, price, platforms}] (games only)."""
    term = " ".join((term or "").split())[:80]
    if not term:
        return []
    res = await client.fetch(SEARCH.format(term=quote(term)), max_bytes=512 * 1024,
                             headers={"Accept": "application/json"})
    if res.status != 200:
        raise RuntimeError(f"Steam search answered HTTP {res.status}")
    try:
        items = json.loads(res.content).get("items") or []
    except (ValueError, AttributeError):
        items = []
    out = []
    for i in items:
        if not isinstance(i, dict) or i.get("type") != "app" or not isinstance(i.get("id"), int):
            continue
        price = i.get("price") or {}
        out.append({"appid": i["id"], "name": str(i.get("name") or ""),
                    "image": i.get("tiny_image"),
                    "price": (price.get("final") / 100) if isinstance(price.get("final"), int) else None,
                    "currency": price.get("currency"),
                    "platforms": [k for k, v in (i.get("platforms") or {}).items() if v]})
        if len(out) >= limit:
            break
    return out


def _asset(fmt: str | None, name: str | None) -> str | None:
    if not fmt or not name:
        return None
    return ASSET_BASE + fmt.replace("${FILENAME}", name)


async def items(client, appids: list[int]) -> dict[int, dict]:
    """{appid: {name, capsule, hero, header, released, coming_soon, early_access}} for up to 50 App IDs."""
    ids = [int(a) for a in appids if a][:50]
    if not ids:
        return {}
    payload = {"ids": [{"appid": a} for a in ids], "context": {"language": "english", "country_code": "US"},
               "data_request": {"include_assets": True, "include_release": True}}
    url = ITEMS.format(payload=quote(json.dumps(payload, separators=(",", ":"))))
    res = await client.fetch(url, max_bytes=2 * 1024 * 1024, headers={"Accept": "application/json"})
    if res.status != 200:
        raise RuntimeError(f"Steam answered HTTP {res.status}")
    try:
        data = json.loads(res.content).get("response", {}).get("store_items") or []
    except (ValueError, AttributeError):
        data = []
    out: dict[int, dict] = {}
    for it in data:
        if not isinstance(it, dict) or it.get("success") != 1 or not isinstance(it.get("appid"), int):
            continue
        a = it.get("assets") or {}
        fmt = a.get("asset_url_format")
        rel = it.get("release") or {}
        out[it["appid"]] = {
            "name": str(it.get("name") or ""),
            "capsule": _asset(fmt, a.get("library_capsule_2x") or a.get("library_capsule")),
            "hero": _asset(fmt, a.get("library_hero")),
            "header": _asset(fmt, a.get("header")),
            "wide": _asset(fmt, a.get("main_capsule")),
            "released": rel.get("steam_release_date") or rel.get("original_release_date"),
            "coming_soon": bool(rel.get("is_coming_soon") or it.get("is_coming_soon")),
            "release_text": rel.get("custom_release_date_message") or rel.get("coming_soon_display"),
            "early_access": bool(rel.get("is_early_access")),
        }
    return out
