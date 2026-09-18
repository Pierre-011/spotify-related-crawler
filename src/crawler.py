```python
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

from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

# Nom du fichier corrigé
ARTISTS_FILE = DATA_DIR / "artistes.json"

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
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = path.with_suffix(path.suffix + ".tmp")

    with tmp.open("w", encoding="utf-8") as f:
        json.dump(
            obj,
            f,
            ensure_ascii=False,
            indent=2
        )

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

    match = re.search(
        r"/artist/([A-Za-z0-9]{22})(?:/|$)",
        href
    )

    return normalize_id(match.group(1)) if match else None


async def accept_cookies(page):
    """
    Spotify changes cookie-button labels depending on locale.
    These labels are intentionally broad.
    """

    labels = [
        "Accept cookies",
        "Accept Cookies",
        "Accepter les cookies",
        "Autoriser les cookies",
        "Allow all cookies",
    ]

    for label in labels:
        try:
            await page.get_by_role(
                "button",
                name=re.compile(label, re.I)
            ).first.click(timeout=1200)

            return

        except Exception:
            pass


async def prepare_page(page):
    await page.set_extra_http_headers({
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"
    })

    await page.set_viewport_size({
        "width": 1440,
        "height": 1000
    })


async def wait_for_spotify(page):
    await page.wait_for_load_state(
        "domcontentloaded",
        timeout=30000
    )

    try:
        await page.wait_for_load_state(
            "networkidle",
            timeout=12000
        )

    except PlaywrightTimeoutError:
        pass

    await page.wait_for_timeout(PAGE_WAIT_MS)


async def scroll_related(page):
    for _ in range(SCROLL_COUNT):
        await page.mouse.wheel(0, 1600)
        await page.wait_for_timeout(700)


async def extract_artist_links(page):
    """
    Extract artist IDs from rendered DOM anchors.

    This is deliberately DOM-based rather than API-based.
    """

    links = await page.locator(
        'a[href*="/artist/"]'
    ).evaluate_all(
        """els => els.map(a => ({
            href: a.href || a.getAttribute('href') || '',
            text: (a.innerText || a.textContent || '').trim()
        }))"""
    )

    found = {}

    for item in links:
        aid = artist_id_from_href(
            item.get("href", "")
        )

        if aid:
            found.setdefault(
                aid,
                {
                    "id": aid,
                    "href": item.get("href", ""),
                    "text": item.get("text", "")
                }
            )

    # Fallback:
    # inspect complete rendered HTML too.
    html = await page.content()

    for match in re.finditer(
        r'/artist/([A-Za-z0-9]{22})(?:/related)?',
        html
    ):
        aid = normalize_id(match.group(1))

        if aid:
            found.setdefault(
                aid,
                {
                    "id": aid,
                    "href": f"{BASE}/{LANGUAGE}/artist/{aid}",
                    "text": ""
                }
            )

    return found


async def extract_artist_profile(page, artist_id):
    """
    Extract information visible/embedded
    in the rendered Spotify artist page.

    No Spotify API is called.
    """

    await page.goto(
        f"{BASE}/{LANGUAGE}/artist/{artist_id}",
        wait_until="domcontentloaded",
        timeout=45000
    )

    await wait_for_spotify(page)

    # Short scroll to trigger lazy-loaded content/images.
    await page.mouse.wheel(0, 700)
    await page.wait_for_timeout(500)

    html = await page.content()

    # Visible page text.
    data = await page.locator(
        "body"
    ).inner_text(timeout=10000)

    # Links.
    links = await page.locator(
        "a[href]"
    ).evaluate_all(
        """els => els.map(a => ({
            href: a.href || a.getAttribute('href') || '',
            text: (a.innerText || a.textContent || '').trim()
        }))"""
    )

    # Images.
    images = await page.locator(
        "img"
    ).evaluate_all(
        """els => els.map(i =>
            i.src || i.getAttribute('src') || ''
        ).filter(Boolean)"""
    )

    # JSON-LD.
    jsonld = []

    for raw in await page.locator(
        'script[type="application/ld+json"]'
    ).all_text_contents():

        try:
            jsonld.append(json.loads(raw))

        except Exception:
            pass

    name = None

    # Prefer visible heading.
    for selector in [
        "h1",
        '[data-testid="entityTitle"]'
    ]:
        try:
            txt = (
                await page.locator(selector)
                .first
                .inner_text(timeout=1500)
            ).strip()

            if txt:
                name = txt
                break

        except Exception:
            pass

    # Fallback to JSON-LD.
    if not name:

        for item in jsonld:

            candidates = (
                item
                if isinstance(item, list)
                else [item]
            )

            for obj in candidates:

                if (
                    isinstance(obj, dict)
                    and obj.get("@type")
                    in ("MusicGroup", "Person")
                ):

                    if obj.get("name"):
                        name = str(
                            obj["name"]
                        ).strip()

                        break

            if name:
                break

    # Final fallback: document title.
    if not name:

        title = await page.title()

        name = (
            re.sub(
                r"\s*[|–-]\s*Spotify.*$",
                "",
                title,
                flags=re.I
            ).strip()
            or artist_id
        )

    # Monthly listeners.
    monthly = None

    match = re.search(
        r'([\d\s.,]+)\s+auditeurs mensuels',
        data,
        flags=re.I
    )

    if not match:
        match = re.search(
            r'([\d\s.,]+)\s+monthly listeners',
            data,
            flags=re.I
        )

    if match:

        raw = (
            match.group(1)
            .replace(" ", "")
            .replace("\u00a0", "")
        )

        try:
            monthly = int(
                raw
                .replace(".", "")
                .replace(",", "")
            )

        except ValueError:
            monthly = None

    # Deduplicate images.
    clean_images = []

    for src in images:

        if src and src not in clean_images:
            clean_images.append(src)

    clean_images = clean_images[:10]

    url = (
        f"{BASE}/{LANGUAGE}/artist/{artist_id}"
    )

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
        "external_urls": {
            "spotify": url
        },
        "first_seen": now_iso(),
        "last_seen": now_iso()
    }


async def main():

    # ---------------------------------------------------------
    # Chargement de artistes.json
    # ---------------------------------------------------------

    artists_doc = load_json(
        ARTISTS_FILE,
        {"artists": {}}
    )

    if (
        "artists" not in artists_doc
        or not isinstance(
            artists_doc["artists"],
            dict
        )
    ):
        artists_doc["artists"] = {}

    # ---------------------------------------------------------
    # Chargement de state.json
    # ---------------------------------------------------------

    state = load_json(
        STATE_FILE,
        {}
    )

    queue = deque(
        state.get("queue", [])
    )

    processed = set(
        state.get("processed", [])
    )

    stats = state.get(
        "stats",
        {
            "discovered": 0,
            "added": 0,
            "processed": 0,
            "errors": 0
        }
    )

    # ---------------------------------------------------------
    # Ajouter les artistes existants à la queue
    # ---------------------------------------------------------

    for aid in artists_doc["artists"]:

        if (
            aid not in processed
            and aid not in queue
        ):
            queue.append(aid)

    started = time.monotonic()

    # ---------------------------------------------------------
    # Playwright
    # ---------------------------------------------------------

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=HEADLESS
        )

        context = await browser.new_context(
            locale="fr-FR",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            )
        )

        page = await context.new_page()

        await prepare_page(page)

        # -----------------------------------------------------
        # Boucle principale
        # -----------------------------------------------------

        while (
            queue
            and (
                time.monotonic() - started
            ) < MAX_SECONDS
        ):

            aid = queue.popleft()

            if aid in processed:
                continue

            try:

                related_url = (
                    f"{BASE}/{LANGUAGE}"
                    f"/artist/{aid}/related"
                )

                print(
                    f"[RELATED] {aid} -> {related_url}",
                    flush=True
                )

                # ---------------------------------------------
                # Page Related Artists
                # ---------------------------------------------

                await page.goto(
                    related_url,
                    wait_until="domcontentloaded",
                    timeout=45000
                )

                await wait_for_spotify(page)

                await accept_cookies(page)

                await scroll_related(page)

                # ---------------------------------------------
                # Extraction des artistes liés
                # ---------------------------------------------

                found = await extract_artist_links(
                    page
                )

                stats["discovered"] += len(found)

                # ---------------------------------------------
                # Traitement des artistes trouvés
                # ---------------------------------------------

                for related_id, meta in found.items():

                    # Ne pas ajouter l'artiste source.
                    if related_id == aid:
                        continue

                    # -----------------------------------------
                    # Nouvel artiste
                    # -----------------------------------------

                    if (
                        related_id
                        not in artists_doc["artists"]
                    ):

                        print(
                            f"[NEW] {related_id} "
                            f"{meta.get('text', '')}",
                            flush=True
                        )

                        profile = (
                            await extract_artist_profile(
                                page,
                                related_id
                            )
                        )

                        artists_doc[
                            "artists"
                        ][related_id] = profile

                        stats["added"] += 1

                        # Sauvegarde immédiate.
                        save_json(
                            ARTISTS_FILE,
                            artists_doc
                        )

                    # -----------------------------------------
                    # Ajouter à la queue
                    # -----------------------------------------

                    if (
                        related_id not in processed
                        and related_id not in queue
                    ):
                        queue.append(related_id)

                # ---------------------------------------------
                # Artiste traité
                # ---------------------------------------------

                processed.add(aid)

                stats["processed"] += 1

                state["queue"] = list(queue)

                state["processed"] = sorted(
                    processed
                )

                state["stats"] = stats

                state["last_run"] = now_iso()

                save_json(
                    STATE_FILE,
                    state
                )

                # Éviter de surcharger Spotify.
                await page.wait_for_timeout(
                    1000
                )

            except Exception as exc:

                stats["errors"] += 1

                print(
                    f"[ERROR] {aid}: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True
                )

                # Remettre l'artiste dans la queue
                # pour une tentative ultérieure.
                if aid not in queue:
                    queue.append(aid)

                state["queue"] = list(queue)

                state["processed"] = sorted(
                    processed
                )

                state["stats"] = stats

                state["last_run"] = now_iso()

                save_json(
                    STATE_FILE,
                    state
                )

                await page.wait_for_timeout(
                    3000
                )

        # -----------------------------------------------------
        # Sauvegarde finale
        # -----------------------------------------------------

        state["queue"] = list(queue)

        state["processed"] = sorted(
            processed
        )

        state["stats"] = stats

        state["last_run"] = now_iso()

        save_json(
            STATE_FILE,
            state
        )

        save_json(
            ARTISTS_FILE,
            artists_doc
        )

        await browser.close()

    # ---------------------------------------------------------
    # Résumé
    # ---------------------------------------------------------

    print(
        f"Finished. "
        f"artists={len(artists_doc['artists'])} "
        f"processed={len(processed)} "
        f"queue={len(queue)} "
        f"added={stats['added']} "
        f"errors={stats['errors']}",
        flush=True
    )


if __name__ == "__main__":
    asyncio.run(main())
```
