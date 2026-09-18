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

ARTISTS_FILE = DATA_DIR / "artistes.json"
STATE_FILE = DATA_DIR / "state.json"

BASE = "https://open.spotify.com"

MAX_SECONDS = int(os.getenv("CRAWLER_MAX_SECONDS", "600"))
HEADLESS = os.getenv("HEADLESS", "1") != "0"
LANGUAGE = os.getenv("SPOTIFY_LANGUAGE", "intl-fr")
PAGE_WAIT_MS = int(os.getenv("PAGE_WAIT_MS", "2500"))
SCROLL_COUNT = int(os.getenv("SCROLL_COUNT", "5"))

ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")


# ============================================================
# UTILITAIRES
# ============================================================

def now_iso():
    return datetime.now(
        timezone.utc
    ).replace(
        microsecond=0
    ).isoformat()


def load_json(path, default):
    if not path.exists():
        return default

    try:
        with path.open(
            "r",
            encoding="utf-8"
        ) as f:
            return json.load(f)

    except Exception as exc:
        print(
            f"[WARNING] Impossible de lire {path}: {exc}",
            flush=True
        )
        return default


def save_json(path, obj):
    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    tmp = path.with_suffix(
        path.suffix + ".tmp"
    )

    with tmp.open(
        "w",
        encoding="utf-8"
    ) as f:
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

    value = str(value).strip()

    if value.startswith(
        "spotify:artist:"
    ):
        value = value.rsplit(
            ":",
            1
        )[-1]

    if ID_RE.fullmatch(value):
        return value

    return None


def artist_id_from_href(href):
    if not href:
        return None

    href = (
        href
        .split("?", 1)[0]
        .split("#", 1)[0]
    )

    match = re.search(
        r"/artist/([A-Za-z0-9]{22})(?:/|$)",
        href
    )

    return (
        normalize_id(match.group(1))
        if match
        else None
    )


# ============================================================
# EXTRACTION DU NOM DE L'ARTISTE
# ============================================================

def clean_artist_name(value):
    """
    Nettoie un nom récupéré depuis Spotify.
    """

    if not value:
        return None

    value = str(value)

    # Remplace les espaces Unicode multiples.
    value = re.sub(
        r"\s+",
        " ",
        value
    ).strip()

    # Retire les suffixes habituels du titre Spotify.
    value = re.sub(
        r"\s*[|–—-]\s*Spotify(?:\s*.*)?$",
        "",
        value,
        flags=re.I
    ).strip()

    # Spotify peut parfois retourner des titres génériques.
    invalid_names = {
        "spotify",
        "web player",
        "bibliothèque",
        "accueil",
        "recherche",
        "home",
        "search",
        "library",
        "your library",
    }

    if value.lower() in invalid_names:
        return None

    return value or None


async def extract_artist_name(page, jsonld):
    """
    Spotify peut modifier régulièrement son DOM.

    On utilise plusieurs sources dans cet ordre :

    1. meta og:title
    2. document.title
    3. JSON-LD
    4. data-testid="entityTitle"
    5. h1

    Cela évite notamment de récupérer "Bibliothèque"
    comme nom d'artiste.
    """

    # --------------------------------------------------------
    # 1. OpenGraph title
    # --------------------------------------------------------

    try:
        og_title = await page.locator(
            'meta[property="og:title"]'
        ).get_attribute(
            "content"
        )

        name = clean_artist_name(
            og_title
        )

        if name:
            return name

    except Exception:
        pass


    # --------------------------------------------------------
    # 2. Titre du document
    # --------------------------------------------------------

    try:
        title = await page.title()

        name = clean_artist_name(
            title
        )

        if name:
            return name

    except Exception:
        pass


    # --------------------------------------------------------
    # 3. JSON-LD
    # --------------------------------------------------------

    for item in jsonld:

        candidates = (
            item
            if isinstance(item, list)
            else [item]
        )

        for obj in candidates:

            if not isinstance(
                obj,
                dict
            ):
                continue

            obj_type = obj.get(
                "@type"
            )

            if obj_type in (
                "MusicGroup",
                "Person"
            ):

                name = clean_artist_name(
                    obj.get("name")
                )

                if name:
                    return name


    # --------------------------------------------------------
    # 4. data-testid entityTitle
    # --------------------------------------------------------

    selectors = [
        '[data-testid="entityTitle"]',
        '[data-testid="entityTitle"] h1',
    ]

    for selector in selectors:

        try:

            locator = page.locator(
                selector
            ).first

            if await locator.count():

                text = await locator.inner_text(
                    timeout=1500
                )

                name = clean_artist_name(
                    text
                )

                if name:
                    return name

        except Exception:
            pass


    # --------------------------------------------------------
    # 5. h1
    # --------------------------------------------------------

    try:

        headings = await page.locator(
            "h1"
        ).all_inner_texts()

        for text in headings:

            name = clean_artist_name(
                text
            )

            if name:
                return name

    except Exception:
        pass


    return None


# ============================================================
# EXTRACTION DU NOMBRE D'AUDITEURS
# ============================================================

def extract_monthly_listeners(text):
    """
    Extrait le nombre d'auditeurs mensuels.

    Fonctionne avec notamment :

    266 auditeurs mensuels
    1 234 auditeurs mensuels
    1 234 auditeurs mensuels
    1 234 auditeurs mensuels
    181,571 monthly listeners
    1,234,567 monthly listeners
    1.234.567 auditeurs mensuels

    On conserve uniquement les chiffres afin de ne pas
    dépendre du séparateur utilisé par Spotify.
    """

    if not text:
        return None


    patterns = [

        # Français
        r"([\d\s.,\u00a0\u202f]+)\s+auditeurs\s+mensuels",

        # Anglais
        r"([\d\s.,\u00a0\u202f]+)\s+monthly\s+listeners",

    ]


    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            flags=re.I
        )

        if not match:
            continue


        raw = match.group(1)

        # ----------------------------------------------------
        # IMPORTANT :
        #
        # On supprime TOUT sauf les chiffres.
        #
        # Cela gère :
        #
        # 1 234
        # 1 234
        # 1 234
        # 1,234
        # 1.234
        # 181,571
        # ----------------------------------------------------

        digits = re.sub(
            r"[^\d]",
            "",
            raw
        )


        if not digits:
            continue


        try:
            return int(digits)

        except ValueError:
            continue


    return None


# ============================================================
# COOKIES
# ============================================================

async def accept_cookies(page):
    """
    Spotify changes cookie-button labels depending on locale.
    """

    labels = [
        "Accept cookies",
        "Accept Cookies",
        "Accepter les cookies",
        "Autoriser les cookies",
        "Allow all cookies",
        "Tout accepter",
        "Accepter tout",
    ]

    for label in labels:

        try:

            await page.get_by_role(
                "button",
                name=re.compile(
                    label,
                    re.I
                )
            ).first.click(
                timeout=1200
            )

            print(
                "[COOKIES] Cookies accepted",
                flush=True
            )

            return

        except Exception:
            pass


# ============================================================
# PREPARATION PAGE
# ============================================================

async def prepare_page(page):

    await page.set_extra_http_headers({
        "Accept-Language":
            "fr-FR,fr;q=0.9,en;q=0.8"
    })

    await page.set_viewport_size({
        "width": 1440,
        "height": 1000
    })


# ============================================================
# ATTENTE SPOTIFY
# ============================================================

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

    await page.wait_for_timeout(
        PAGE_WAIT_MS
    )


# ============================================================
# SCROLL RELATED
# ============================================================

async def scroll_related(page):

    for _ in range(
        SCROLL_COUNT
    ):

        await page.mouse.wheel(
            0,
            1600
        )

        await page.wait_for_timeout(
            700
        )


# ============================================================
# EXTRACTION DES ARTISTES LIÉS
# ============================================================

async def extract_artist_links(page):
    """
    Extract artist IDs from rendered DOM anchors.
    """

    links = await page.locator(
        'a[href*="/artist/"]'
    ).evaluate_all(
        """els => els.map(a => ({
            href: a.href ||
                  a.getAttribute('href') ||
                  '',
            text: (
                a.innerText ||
                a.textContent ||
                ''
            ).trim()
        }))"""
    )

    found = {}


    # --------------------------------------------------------
    # Extraction des liens
    # --------------------------------------------------------

    for item in links:

        aid = artist_id_from_href(
            item.get(
                "href",
                ""
            )
        )

        if aid:

            found.setdefault(
                aid,
                {
                    "id": aid,
                    "href": item.get(
                        "href",
                        ""
                    ),
                    "text": item.get(
                        "text",
                        ""
                    )
                }
            )


    # --------------------------------------------------------
    # Fallback HTML complet
    # --------------------------------------------------------

    html = await page.content()

    for match in re.finditer(
        r'/artist/([A-Za-z0-9]{22})(?:/related)?',
        html
    ):

        aid = normalize_id(
            match.group(1)
        )

        if aid:

            found.setdefault(
                aid,
                {
                    "id": aid,
                    "href":
                        f"{BASE}/{LANGUAGE}"
                        f"/artist/{aid}",
                    "text": ""
                }
            )


    return found


# ============================================================
# EXTRACTION PROFIL ARTISTE
# ============================================================

async def extract_artist_profile(
    page,
    artist_id
):
    """
    Extract information from the rendered Spotify page.

    No Spotify API is called.
    """

    url = (
        f"{BASE}/{LANGUAGE}"
        f"/artist/{artist_id}"
    )

    print(
        f"[PROFILE] {artist_id} -> {url}",
        flush=True
    )


    await page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=45000
    )

    await wait_for_spotify(
        page
    )

    # --------------------------------------------------------
    # Cookies
    # --------------------------------------------------------

    await accept_cookies(
        page
    )


    # --------------------------------------------------------
    # Scroll léger
    # --------------------------------------------------------

    await page.mouse.wheel(
        0,
        700
    )

    await page.wait_for_timeout(
        500
    )


    # --------------------------------------------------------
    # HTML
    # --------------------------------------------------------

    html = await page.content()


    # --------------------------------------------------------
    # Texte visible
    # --------------------------------------------------------

    data = await page.locator(
        "body"
    ).inner_text(
        timeout=10000
    )


    # --------------------------------------------------------
    # JSON-LD
    # --------------------------------------------------------

    jsonld = []

    for raw in await page.locator(
        'script[type="application/ld+json"]'
    ).all_text_contents():

        try:

            jsonld.append(
                json.loads(raw)
            )

        except Exception:
            pass


    # --------------------------------------------------------
    # NOM
    # --------------------------------------------------------

    name = await extract_artist_name(
        page,
        jsonld
    )


    if not name:
        name = artist_id


    # --------------------------------------------------------
    # AUDITEURS MENSUELS
    # --------------------------------------------------------

    monthly = extract_monthly_listeners(
        data
    )


    # --------------------------------------------------------
    # DEBUG
    # --------------------------------------------------------

    print(
        f"[PROFILE DATA] "
        f"id={artist_id} "
        f"name={name!r} "
        f"monthly_listeners={monthly}",
        flush=True
    )


    # --------------------------------------------------------
    # Images
    # --------------------------------------------------------

    images = await page.locator(
        "img"
    ).evaluate_all(
        """els => els.map(i =>
            i.src ||
            i.getAttribute('src') ||
            ''
        ).filter(Boolean)"""
    )


    clean_images = []

    for src in images:

        if (
            src
            and src not in clean_images
        ):
            clean_images.append(
                src
            )


    clean_images = clean_images[:10]


    # --------------------------------------------------------
    # Profil final
    # --------------------------------------------------------

    return {

        "id": artist_id,

        "name": name,

        "url": url,

        "uri":
            f"spotify:artist:{artist_id}",

        "followers": None,

        "monthly_listeners":
            monthly,

        "popularity": None,

        "genres": [],

        "images":
            clean_images,

        "external_urls": {
            "spotify": url
        },

        "first_seen":
            now_iso(),

        "last_seen":
            now_iso()
    }


# ============================================================
# MAIN
# ============================================================

async def main():

    # --------------------------------------------------------
    # artistes.json
    # --------------------------------------------------------

    artists_doc = load_json(
        ARTISTS_FILE,
        {
            "artists": {}
        }
    )


    if (
        "artists" not in artists_doc
        or not isinstance(
            artists_doc["artists"],
            dict
        )
    ):

        artists_doc["artists"] = {}


    # --------------------------------------------------------
    # state.json
    # --------------------------------------------------------

    state = load_json(
        STATE_FILE,
        {}
    )


    queue = deque(
        state.get(
            "queue",
            []
        )
    )


    processed = set(
        state.get(
            "processed",
            []
        )
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


    # --------------------------------------------------------
    # Ajouter les artistes existants à la queue
    # --------------------------------------------------------

    for aid in artists_doc[
        "artists"
    ]:

        if (
            aid not in processed
            and aid not in queue
        ):

            queue.append(
                aid
            )


    started = time.monotonic()


    # --------------------------------------------------------
    # Playwright
    # --------------------------------------------------------

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=HEADLESS
        )


        context = await browser.new_context(

            locale="fr-FR",

            user_agent=(
                "Mozilla/5.0 "
                "(X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/128.0.0.0 "
                "Safari/537.36"
            )
        )


        page = await context.new_page()


        await prepare_page(
            page
        )


        # ----------------------------------------------------
        # BOUCLE
        # ----------------------------------------------------

        while (
            queue
            and (
                time.monotonic()
                - started
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
                    f"[RELATED] "
                    f"{aid} -> "
                    f"{related_url}",
                    flush=True
                )


                # ------------------------------------------------
                # Related page
                # ------------------------------------------------

                await page.goto(
                    related_url,
                    wait_until="domcontentloaded",
                    timeout=45000
                )


                await wait_for_spotify(
                    page
                )


                await accept_cookies(
                    page
                )


                await scroll_related(
                    page
                )


                # ------------------------------------------------
                # Artistes liés
                # ------------------------------------------------

                found = (
                    await extract_artist_links(
                        page
                    )
                )


                stats[
                    "discovered"
                ] += len(found)


                print(
                    f"[FOUND] "
                    f"{len(found)} artistes liés",
                    flush=True
                )


                # ------------------------------------------------
                # Traitement
                # ------------------------------------------------

                for (
                    related_id,
                    meta
                ) in found.items():


                    # Ne pas ajouter la source.
                    if related_id == aid:
                        continue


                    # ------------------------------------------------
                    # NOUVEL ARTISTE
                    # ------------------------------------------------

                    if (
                        related_id
                        not in artists_doc[
                            "artists"
                        ]
                    ):

                        print(
                            f"[NEW] "
                            f"{related_id} "
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
                        ][
                            related_id
                        ] = profile


                        stats[
                            "added"
                        ] += 1


                        # Sauvegarde immédiate.
                        save_json(
                            ARTISTS_FILE,
                            artists_doc
                        )


                    # ------------------------------------------------
                    # Queue
                    # ------------------------------------------------

                    if (
                        related_id
                        not in processed
                        and related_id
                        not in queue
                    ):

                        queue.append(
                            related_id
                        )


                # ------------------------------------------------
                # Artiste traité
                # ------------------------------------------------

                processed.add(
                    aid
                )


                stats[
                    "processed"
                ] += 1


                state[
                    "queue"
                ] = list(
                    queue
                )


                state[
                    "processed"
                ] = sorted(
                    processed
                )


                state[
                    "stats"
                ] = stats


                state[
                    "last_run"
                ] = now_iso()


                save_json(
                    STATE_FILE,
                    state
                )


                # ------------------------------------------------
                # Pause
                # ------------------------------------------------

                await page.wait_for_timeout(
                    1000
                )


            except Exception as exc:

                stats[
                    "errors"
                ] += 1


                print(
                    f"[ERROR] "
                    f"{aid}: "
                    f"{type(exc).__name__}: "
                    f"{exc}",
                    flush=True
                )


                # Remettre dans la queue.
                if aid not in queue:

                    queue.append(
                        aid
                    )


                state[
                    "queue"
                ] = list(
                    queue
                )


                state[
                    "processed"
                ] = sorted(
                    processed
                )


                state[
                    "stats"
                ] = stats


                state[
                    "last_run"
                ] = now_iso()


                save_json(
                    STATE_FILE,
                    state
                )


                await page.wait_for_timeout(
                    3000
                )


        # --------------------------------------------------------
        # SAUVEGARDE FINALE
        # --------------------------------------------------------

        state[
            "queue"
        ] = list(
            queue
        )


        state[
            "processed"
        ] = sorted(
            processed
        )


        state[
            "stats"
        ] = stats


        state[
            "last_run"
        ] = now_iso()


        save_json(
            STATE_FILE,
            state
        )


        save_json(
            ARTISTS_FILE,
            artists_doc
        )


        await browser.close()


    # --------------------------------------------------------
    # Résumé
    # --------------------------------------------------------

    print(
        f"Finished. "
        f"artists="
        f"{len(artists_doc['artists'])} "
        f"processed="
        f"{len(processed)} "
        f"queue="
        f"{len(queue)} "
        f"added="
        f"{stats['added']} "
        f"errors="
        f"{stats['errors']}",
        flush=True
    )


if __name__ == "__main__":
    asyncio.run(main())
