#!/usr/bin/env python3
"""
Spotify Related Artists crawler.

IMPORTANT:
- This project DOES NOT use the Spotify Web API.
- It opens Spotify's public website with Playwright/Chromium.
- It inspects the rendered DOM/HTML of /related and /artist pages.
- It persists its queue and data to JSON so GitHub Actions can resume.
"""

import asyncio
import json
import os
import re
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
ARTISTS_FILE = DATA_DIR / "artists.json"
STATE_FILE = DATA_DIR / "state.json"

BASE = "https://open.spotify.com"
MAX_SECONDS = int(os.getenv("CRAWLER_MAX_SECONDS", "600"))
HEADLESS = os.getenv("HEADLESS", "1") != "0"
LANGUAGE = os.getenv("SPOTIFY_LANGUAGE", "intl-fr")
PAGE_WAIT_MS = int(os.getenv("PAGE_WAIT_MS", "2500"))
SCROLL_COUNT = int(os.getenv("SCROLL_COUNT", "5"))

ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json(path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def normalize_id(value):
    if not value:
        return None
    value = value.strip()
    if value.startswith("spotify:artist:"):
        value = value.rsplit(":", 1)[-1]
    if ID_RE.fullmatch(value):
        return value
    return None


def artist_id_from_href(href):
    if not href:
        return None
    href = href.split("?", 1)[0].split("#", 1)[0]
    m = re.search(r"/artist/([A-Za-z0-9]{22})(?:/|$)", href)
    return normalize_id(m.group(1)) if m else None


async def accept_cookies(page):
    # Spotify changes labels by locale; these are intentionally broad.
    labels = [
        "Accept cookies", "Accept Cookies", "Accepter les cookies",
        "Autoriser les cookies", "Allow all cookies"
    ]
    for label in labels:
        try:
            await page.get_by_role("button", name=re.compile(label, re.I)).first.click(timeout=1200)
            return
        except Exception:
            pass


async def prepare_page(page):
    await page.set_extra_http_headers({
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"
    })
    await page.set_viewport_size({"width": 1440, "height": 1000})


async def wait_for_spotify(page):
    await page.wait_for_load_state("domcontentloaded", timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=12000)
    except PlaywrightTimeoutError:
        pass
    await page.wait_for_timeout(PAGE_WAIT_MS)


async def scroll_related(page):
    for _ in range(SCROLL_COUNT):
        await page.mouse.wheel(0, 1600)
        await page.wait_for_timeout(700)


async def extract_artist_links(page):
    """
    Extract IDs from rendered DOM anchors.
    This is deliberately DOM-based rather than API-based.
    """
    links = await page.locator('a[href*="/artist/"]').evaluate_all(
        """els => els.map(a => ({
            href: a.href || a.getAttribute('href') || '',
            text: (a.innerText || a.textContent || '').trim()
        }))"""
    )

    found = {}
    for item in links:
        aid = artist_id_from_href(item.get("href", ""))
        if aid:
            found.setdefault(aid, {
                "id": aid,
                "href": item.get("href", ""),
                "text": item.get("text", "")
            })

    # Fallback: inspect complete rendered HTML too.
    html = await page.content()
    for match in re.finditer(r'/artist/([A-Za-z0-9]{22})(?:/related)?', html):
        aid = normalize_id(match.group(1))
        if aid:
            found.setdefault(aid, {
                "id": aid,
                "href": f"{BASE}/{LANGUAGE}/artist/{aid}",
                "text": ""
            })

    return found


async def extract_artist_profile(page, artist_id):
    """
    Extract information visible/embedded in the rendered Spotify artist page.
    No Spotify API is called.
    """
    await page.goto(
        f"{BASE}/{LANGUAGE}/artist/{artist_id}",
        wait_until="domcontentloaded",
        timeout=45000
    )
    await wait_for_spotify(page)

    # A short scroll can trigger lazy-loaded content/images.
    await page.mouse.wheel(0, 700)
    await page.wait_for_timeout(500)

    html = await page.content()

    # Collect rendered anchor/image information.
    data = await page.locator("body").inner_text(timeout=10000)
    links = await page.locator("a[href]").evaluate_all(
        """els => els.map(a => ({
            href: a.href || a.getAttribute('href') || '',
            text: (a.innerText || a.textContent || '').trim()
        }))"""
    )
    images = await page.locator("img").evaluate_all(
        """els => els.map(i => i.src || i.getAttribute('src') || '').filter(Boolean)"""
    )

    # JSON-LD is part of the website HTML and can provide useful structured data.
    jsonld = []
    for raw in await page.locator('script[type="application/ld+json"]').all_text_contents():
        try:
            jsonld.append(json.loads(raw))
        except Exception:
            pass

    name = None
    # Prefer visible heading, then JSON-LD, then document title.
    for selector in ["h1", '[data-testid="entityTitle"]']:
        try:
            txt = (await page.locator(selector).first.inner_text(timeout=1500)).strip()
            if txt:
                name = txt
                break
        except Exception:
            pass

    if not name:
        for item in jsonld:
            candidates = item if isinstance(item, list) else [item]
            for obj in candidates:
                if isinstance(obj, dict) and obj.get("@type") in ("MusicGroup", "Person"):
                    if obj.get("name"):
                        name = str(obj["name"]).strip()
                        break

    if not name:
        title = await page.title()
        name = re.sub(r"\s*[|–-]\s*Spotify.*$", "", title, flags=re.I).strip() or artist_id

    # Spotify's website can expose monthly listeners as visible text.
    monthly = None
    m = re.search(r'([\d\s.,]+)\s+auditeurs mensuels', data, flags=re.I)
    if not m:
        m = re.search(r'([\d\s.,]+)\s+monthly listeners', data, flags=re.I)
    if m:
        raw = m.group(1).replace(" ", "").replace("\u00a0", "")
        # Keep it as a number when unambiguous.
        try:
            monthly = int(raw.replace(".", "").replace(",", ""))
        except ValueError:
            monthly = None

    # Images are deduplicated and limited.
    clean_images = []
    for src in images:
        if src and src not in clean_images:
            clean_images.append(src)
    clean_images = clean_images[:10]

    url = f"{BASE}/{LANGUAGE}/artist/{artist_id}"
    return {
        "id": artist_id,
        "name": name,
        "url": url,
        "uri": f"spotify:artist:{artist_id}",
        "followers": None,
        "monthly_listeners": monthly,
        "popularity": None,
        "genres": [],
        "images": clean_images,
        "external_urls": {"spotify": url},
        "first_seen": now_iso(),
        "last_seen": now_iso()
    }


async def main():
    artists_doc = load_json(ARTISTS_FILE, {"artists": {}})
    if "artists" not in artists_doc or not isinstance(artists_doc["artists"], dict):
        artists_doc["artists"] = {}

    state = load_json(STATE_FILE, {})
    queue = deque(state.get("queue", []))
    processed = set(state.get("processed", []))
    stats = state.get("stats", {"discovered": 0, "added": 0, "processed": 0, "errors": 0})

    # Ensure every initial artist can be explored.
    for aid in artists_doc["artists"]:
        if aid not in processed and aid not in queue:
            queue.append(aid)

    started = time.monotonic()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        context = await browser.new_context(
            locale="fr-FR",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()
        await prepare_page(page)

        while queue and (time.monotonic() - started) < MAX_SECONDS:
            aid = queue.popleft()

            if aid in processed:
                continue

            try:
                related_url = f"{BASE}/{LANGUAGE}/artist/{aid}/related"
                print(f"[RELATED] {aid} -> {related_url}", flush=True)

                await page.goto(related_url, wait_until="domcontentloaded", timeout=45000)
                await wait_for_spotify(page)
                await accept_cookies(page)
                await scroll_related(page)

                found = await extract_artist_links(page)
                stats["discovered"] += len(found)

                for related_id, meta in found.items():
                    # Do not add the source artist as a new profile.
                    if related_id == aid:
                        continue

                    if related_id not in artists_doc["artists"]:
                        print(f"[NEW] {related_id} {meta.get('text','')}", flush=True)
                        profile = await extract_artist_profile(page, related_id)
                        artists_doc["artists"][related_id] = profile
                        stats["added"] += 1
                        save_json(ARTISTS_FILE, artists_doc)

                    if related_id not in processed and related_id not in queue:
                        queue.append(related_id)

                processed.add(aid)
                stats["processed"] += 1

                state["queue"] = list(queue)
                state["processed"] = sorted(processed)
                state["stats"] = stats
                state["last_run"] = now_iso()
                save_json(STATE_FILE, state)

                # Avoid hammering the site.
                await page.wait_for_timeout(1000)

            except Exception as exc:
                stats["errors"] += 1
                print(f"[ERROR] {aid}: {type(exc).__name__}: {exc}", flush=True)

                # Put it back once; a later run can retry.
                if aid not in queue:
                    queue.append(aid)

                state["queue"] = list(queue)
                state["processed"] = sorted(processed)
                state["stats"] = stats
                state["last_run"] = now_iso()
                save_json(STATE_FILE, state)

                await page.wait_for_timeout(3000)

        state["queue"] = list(queue)
        state["processed"] = sorted(processed)
        state["stats"] = stats
        state["last_run"] = now_iso()
        save_json(STATE_FILE, state)
        save_json(ARTISTS_FILE, artists_doc)

        await browser.close()

    print(
        f"Finished. artists={len(artists_doc['artists'])} "
        f"processed={len(processed)} queue={len(queue)} added={stats['added']} "
        f"errors={stats['errors']}",
        flush=True
    )


if __name__ == "__main__":
    asyncio.run(main())
