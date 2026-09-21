#!/usr/bin/env python3

"""
Spotify Related Artists crawler.

IMPORTANT:
- This project DOES NOT use the Spotify Web API.
- It opens Spotify's public website with Playwright/Chromium.
- It inspects the rendered DOM/HTML of /related and /artist pages.
- It persists its queue and data to JSON so GitHub Actions can resume.

MULTI-WORKER:
- WORKER_ID identifies the current worker.
- WORKER_COUNT defines the total number of workers.
- Each Spotify artist is deterministically assigned to exactly one worker.
- Workers therefore never intentionally process the same artist.
"""

import asyncio
import hashlib
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


# ============================================================
# CONFIGURATION
# ============================================================

ROOT = Path(__file__).resolve().parents[1]

DATA_DIR = ROOT / "data"

ARTISTS_FILE = DATA_DIR / "artistes.json"
STATE_FILE = DATA_DIR / "state.json"

BASE = "https://open.spotify.com"


MAX_SECONDS = int(
    os.getenv(
        "CRAWLER_MAX_SECONDS",
        "3600"
    )
)

HEADLESS = (
    os.getenv(
        "HEADLESS",
        "1"
    ) != "0"
)

LANGUAGE = os.getenv(
    "SPOTIFY_LANGUAGE",
    "intl-fr"
)

PAGE_WAIT_MS = int(
    os.getenv(
        "PAGE_WAIT_MS",
        "2500"
    )
)

SCROLL_COUNT = int(
    os.getenv(
        "SCROLL_COUNT",
        "5"
    )
)


# ============================================================
# WORKERS
# ============================================================

WORKER_ID = int(
    os.getenv(
        "WORKER_ID",
        "1"
    )
)

WORKER_COUNT = int(
    os.getenv(
        "WORKER_COUNT",
        "1"
    )
)

if WORKER_COUNT < 1:
    raise ValueError(
        f"WORKER_COUNT must be >= 1, got {WORKER_COUNT}"
    )

if WORKER_ID < 1 or WORKER_ID > WORKER_COUNT:
    raise ValueError(
        "Invalid worker configuration: "
        f"WORKER_ID={WORKER_ID}, "
        f"WORKER_COUNT={WORKER_COUNT}"
    )


# ============================================================
# CONSTANTES
# ============================================================

ID_RE = re.compile(
    r"^[A-Za-z0-9]{22}$"
)


# ============================================================
# UTILITAIRES
# ============================================================

def now_iso():
    return (
        datetime.now(
            timezone.utc
        )
        .replace(
            microsecond=0
        )
        .isoformat()
    )


def load_json(
    path,
    default
):
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
            f"[WARNING] Impossible de lire "
            f"{path}: {exc}",
            flush=True
        )

        return default


def save_json(
    path,
    obj
):
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

    tmp.replace(
        path
    )


def normalize_id(
    value
):
    if not value:
        return None

    value = str(
        value
    ).strip()

    if value.startswith(
        "spotify:artist:"
    ):
        value = value.rsplit(
            ":",
            1
        )[-1]

    if ID_RE.fullmatch(
        value
    ):
        return value

    return None


def artist_id_from_href(
    href
):
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
        normalize_id(
            match.group(1)
        )
        if match
        else None
    )


# ============================================================
# WORKER ASSIGNMENT
# ============================================================

def worker_for_artist(
    artist_id
):
    """
    Retourne le numéro du worker responsable
    de cet artiste.

    Le résultat est déterministe :
    un même artist_id sera toujours attribué
    au même worker tant que WORKER_COUNT
    reste identique.

    Retour:
        1..WORKER_COUNT
    """

    if not artist_id:
        return None

    digest = hashlib.sha256(
        artist_id.encode(
            "utf-8"
        )
    ).digest()

    number = int.from_bytes(
        digest[:8],
        "big"
    )

    return (
        number % WORKER_COUNT
    ) + 1


def belongs_to_worker(
    artist_id
):
    """
    True si l'artiste appartient
    au worker actuel.
    """

    return (
        worker_for_artist(
            artist_id
        )
        == WORKER_ID
    )


# ============================================================
# EXTRACTION DU NOM DE L'ARTISTE
# ============================================================

def clean_artist_name(
    value
):
    """
    Nettoie un nom récupéré depuis Spotify.
    """

    if not value:
        return None

    value = str(
        value
    )

    value = re.sub(
        r"\s+",
        " ",
        value
    ).strip()

    value = re.sub(
        r"\s*[|–—-]\s*Spotify(?:\s*.*)?$",
        "",
        value,
        flags=re.I
    ).strip()

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


async def extract_artist_name(
    page,
    jsonld
):
    """
    Spotify peut modifier régulièrement son DOM.

    Sources utilisées :
    1. meta og:title
    2. document.title
    3. JSON-LD
    4. data-testid="entityTitle"
    5. h1
    """

    # --------------------------------------------------------
    # 1. OpenGraph
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
    # 2. document.title
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
            if isinstance(
                item,
                list
            )
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
                    obj.get(
                        "name"
                    )
                )

                if name:
                    return name

    # --------------------------------------------------------
    # 4. entityTitle
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
# EXTRACTION AUDITEURS
# ============================================================

def extract_monthly_listeners(
    text
):
    """
    Extrait le nombre d'auditeurs mensuels.
    """

    if not text:
        return None

    patterns = [

        r"([\d\s.,\u00a0\u202f]+)\s+auditeurs\s+mensuels",

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

        raw = match.group(
            1
        )

        digits = re.sub(
            r"[^\d]",
            "",
            raw
        )

        if not digits:
            continue

        try:
            return int(
                digits
            )

        except ValueError:
            continue

    return None


# ============================================================
# COOKIES
# ============================================================

async def accept_cookies(
    page
):
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

async def prepare_page(
    page
):

    await page.set_extra_http_headers(
        {
            "Accept-Language":
                "fr-FR,fr;q=0.9,en;q=0.8"
        }
    )

    await page.set_viewport_size(
        {
            "width": 1440,
            "height": 1000
        }
    )


# ============================================================
# ATTENTE SPOTIFY
# ============================================================

async def wait_for_spotify(
    page
):

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
# SCROLL
# ============================================================

async def scroll_related(
    page
):

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
# EXTRACTION ARTISTES LIÉS
# ============================================================

async def extract_artist_links(
    page
):
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
    # DOM
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
    # HTML fallback
    # --------------------------------------------------------

    html = await page.content()

    for match in re.finditer(
        r'/artist/([A-Za-z0-9]{22})(?:/related)?',
        html
    ):

        aid = normalize_id(
            match.group(
                1
            )
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

    url = (
        f"{BASE}/{LANGUAGE}"
        f"/artist/{artist_id}"
    )

    print(
        f"[WORKER {WORKER_ID}] "
        f"[PROFILE] "
        f"{artist_id} -> {url}",
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

    await accept_cookies(
        page
    )

    await page.mouse.wheel(
        0,
        700
    )

    await page.wait_for_timeout(
        500
    )

    html = await page.content()

    data = await page.locator(
        "body"
    ).inner_text(
        timeout=10000
    )

    jsonld = []

    for raw in await page.locator(
        'script[type="application/ld+json"]'
    ).all_text_contents():

        try:

            jsonld.append(
                json.loads(
                    raw
                )
            )

        except Exception:
            pass

    name = await extract_artist_name(
        page,
        jsonld
    )

    if not name:
        name = artist_id

    monthly = extract_monthly_listeners(
        data
    )

    print(
        f"[WORKER {WORKER_ID}] "
        f"[PROFILE DATA] "
        f"id={artist_id} "
        f"name={name!r} "
        f"monthly_listeners={monthly}",
        flush=True
    )

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
# PREPARATION STATE
# ============================================================

def prepare_worker_state(
    artists_doc,
    state
):
    """
    Construit la queue du worker à partir de l'état global.

    Important :
    - les artistes sont filtrés par worker ;
    - les artistes appartenant aux autres workers
      sont ignorés ;
    - les artistes déjà traités sont ignorés.
    """

    global_queue = state.get(
        "queue",
        []
    )

    global_processed = set(
        state.get(
            "processed",
            []
        )
    )

    processed = {
        aid
        for aid in global_processed
        if belongs_to_worker(
            aid
        )
    }

    queue = deque()

    # --------------------------------------------------------
    # Queue provenant du state
    # --------------------------------------------------------

    for aid in global_queue:

        if not belongs_to_worker(
            aid
        ):
            continue

        if aid in processed:
            continue

        if aid not in queue:
            queue.append(
                aid
            )

    # --------------------------------------------------------
    # Tous les artistes existants
    # --------------------------------------------------------

    for aid in artists_doc[
        "artists"
    ]:

        if not belongs_to_worker(
            aid
        ):
            continue

        if aid in processed:
            continue

        if aid not in queue:
            queue.append(
                aid
            )

    return (
        queue,
        processed
    )


# ============================================================
# SAUVEGARDE STATE WORKER
# ============================================================

def save_worker_state(
    state,
    queue,
    processed,
    stats
):
    """
    Sauvegarde uniquement l'état du worker actuel.

    Le merge final du workflow fusionnera
    les états des 10 workers.
    """

    state[
        "worker_id"
    ] = WORKER_ID

    state[
        "worker_count"
    ] = WORKER_COUNT

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


# ============================================================
# MAIN
# ============================================================

async def main():

    print(
        "============================================================",
        flush=True
    )

    print(
        f"[WORKER] "
        f"Starting worker "
        f"{WORKER_ID}/{WORKER_COUNT}",
        flush=True
    )

    print(
        f"[WORKER] "
        f"MAX_SECONDS={MAX_SECONDS}",
        flush=True
    )

    print(
        "============================================================",
        flush=True
    )

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
            artists_doc[
                "artists"
            ],
            dict
        )
    ):
        artists_doc[
            "artists"
        ] = {}

    # --------------------------------------------------------
    # state.json
    # --------------------------------------------------------

    state = load_json(
        STATE_FILE,
        {}
    )

    queue, processed = prepare_worker_state(
        artists_doc,
        state
    )

    # --------------------------------------------------------
    # Stats
    # --------------------------------------------------------

    old_stats = state.get(
        "stats",
        {}
    )

    stats = {
        "discovered": 0,
        "added": 0,
        "processed": 0,
        "errors": 0
    }

    for key in stats:

        try:
            stats[key] = int(
                old_stats.get(
                    key,
                    0
                )
            )

        except Exception:
            pass

    # --------------------------------------------------------
    # Reset stats processed pour ce lancement ?
    #
    # On conserve les statistiques cumulées.
    # --------------------------------------------------------

    print(
        f"[WORKER {WORKER_ID}] "
        f"Initial queue: {len(queue)}",
        flush=True
    )

    print(
        f"[WORKER {WORKER_ID}] "
        f"Initial processed: {len(processed)}",
        flush=True
    )

    # --------------------------------------------------------
    # Distribution
    # --------------------------------------------------------

    assigned_count = 0

    for aid in artists_doc[
        "artists"
    ]:

        if belongs_to_worker(
            aid
        ):
            assigned_count += 1

    print(
        f"[WORKER {WORKER_ID}] "
        f"Assigned artists currently in database: "
        f"{assigned_count}",
        flush=True
    )

    started = time.monotonic()

    # ========================================================
    # PLAYWRIGHT
    # ========================================================

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

        # ====================================================
        # BOUCLE
        # ====================================================

        while (
            queue
            and (
                time.monotonic()
                - started
            ) < MAX_SECONDS
        ):

            aid = queue.popleft()

            # ------------------------------------------------
            # Sécurité worker
            # ------------------------------------------------

            if not belongs_to_worker(
                aid
            ):

                print(
                    f"[WORKER {WORKER_ID}] "
                    f"[SKIP] {aid} "
                    f"belongs to worker "
                    f"{worker_for_artist(aid)}",
                    flush=True
                )

                continue

            # ------------------------------------------------
            # Déjà traité
            # ------------------------------------------------

            if aid in processed:

                continue

            try:

                # ============================================
                # RELATED
                # ============================================

                related_url = (
                    f"{BASE}/{LANGUAGE}"
                    f"/artist/{aid}/related"
                )

                print(
                    f"[WORKER {WORKER_ID}] "
                    f"[RELATED] "
                    f"{aid} -> "
                    f"{related_url}",
                    flush=True
                )

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

                # ============================================
                # EXTRACTION
                # ============================================

                found = await extract_artist_links(
                    page
                )

                stats[
                    "discovered"
                ] += len(
                    found
                )

                print(
                    f"[WORKER {WORKER_ID}] "
                    f"[FOUND] "
                    f"{len(found)} artistes liés",
                    flush=True
                )

                # ============================================
                # TRAITEMENT
                # ============================================

                for (
                    related_id,
                    meta
                ) in found.items():

                    # ----------------------------------------
                    # Ne pas ajouter la source
                    # ----------------------------------------

                    if related_id == aid:
                        continue

                    # ----------------------------------------
                    # Déterminer le worker responsable
                    # ----------------------------------------

                    target_worker = worker_for_artist(
                        related_id
                    )

                    print(
                        f"[WORKER {WORKER_ID}] "
                        f"[DISCOVERED] "
                        f"{related_id} "
                        f"-> worker "
                        f"{target_worker}",
                        flush=True
                    )

                    # ----------------------------------------
                    # NOUVEL ARTISTE
                    # ----------------------------------------

                    if (
                        related_id
                        not in artists_doc[
                            "artists"
                        ]
                    ):

                        print(
                            f"[WORKER {WORKER_ID}] "
                            f"[NEW] "
                            f"{related_id} "
                            f"-> worker "
                            f"{target_worker}",
                            flush=True
                        )

                        # ------------------------------------
                        # IMPORTANT :
                        #
                        # On ne scrape le profil que si
                        # cet artiste appartient au worker
                        # actuel.
                        # ------------------------------------

                        if belongs_to_worker(
                            related_id
                        ):

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

                            save_json(
                                ARTISTS_FILE,
                                artists_doc
                            )

                    # ----------------------------------------
                    # QUEUE
                    # ----------------------------------------

                    if (
                        belongs_to_worker(
                            related_id
                        )
                        and related_id
                        not in processed
                        and related_id
                        not in queue
                    ):

                        queue.append(
                            related_id
                        )

                        print(
                            f"[WORKER {WORKER_ID}] "
                            f"[QUEUE] "
                            f"{related_id}",
                            flush=True
                        )

                # ============================================
                # ARTISTE TRAITÉ
                # ============================================

                processed.add(
                    aid
                )

                stats[
                    "processed"
                ] += 1

                save_worker_state(
                    state,
                    queue,
                    processed,
                    stats
                )

                print(
                    f"[WORKER {WORKER_ID}] "
                    f"[PROCESSED] "
                    f"{aid} "
                    f"| queue={len(queue)} "
                    f"| processed={len(processed)}",
                    flush=True
                )

                # ============================================
                # PAUSE
                # ============================================

                await page.wait_for_timeout(
                    1000
                )

            except Exception as exc:

                stats[
                    "errors"
                ] += 1

                print(
                    f"[WORKER {WORKER_ID}] "
                    f"[ERROR] "
                    f"{aid}: "
                    f"{type(exc).__name__}: "
                    f"{exc}",
                    flush=True
                )

                # --------------------------------------------
                # Remettre dans la queue
                # --------------------------------------------

                if (
                    belongs_to_worker(
                        aid
                    )
                    and aid not in queue
                ):

                    queue.append(
                        aid
                    )

                save_worker_state(
                    state,
                    queue,
                    processed,
                    stats
                )

                await page.wait_for_timeout(
                    3000
                )

        # ====================================================
        # SAUVEGARDE FINALE
        # ====================================================

        save_worker_state(
            state,
            queue,
            processed,
            stats
        )

        save_json(
            ARTISTS_FILE,
            artists_doc
        )

        await browser.close()

    # ========================================================
    # RÉSUMÉ
    # ========================================================

    elapsed = (
        time.monotonic()
        - started
    )

    print(
        "============================================================",
        flush=True
    )

    print(
        f"[WORKER {WORKER_ID}] FINISHED",
        flush=True
    )

    print(
        f"[WORKER {WORKER_ID}] "
        f"elapsed={elapsed:.0f}s",
        flush=True
    )

    print(
        f"[WORKER {WORKER_ID}] "
        f"artists={len(artists_doc['artists'])}",
        flush=True
    )

    print(
        f"[WORKER {WORKER_ID}] "
        f"processed={len(processed)}",
        flush=True
    )

    print(
        f"[WORKER {WORKER_ID}] "
        f"queue={len(queue)}",
        flush=True
    )

    print(
        f"[WORKER {WORKER_ID}] "
        f"discovered={stats['discovered']}",
        flush=True
    )

    print(
        f"[WORKER {WORKER_ID}] "
        f"added={stats['added']}",
        flush=True
    )

    print(
        f"[WORKER {WORKER_ID}] "
        f"errors={stats['errors']}",
        flush=True
    )

    print(
        "============================================================",
        flush=True
    )


# ============================================================
# ENTRYPOINT
# ============================================================

if __name__ == "__main__":
    asyncio.run(
        main()
    )
