#!/usr/bin/env python3

"""
Spotify Related Artists crawler.

IMPORTANT:
- Ce projet N'UTILISE PAS l'API Web Spotify.
- Il ouvre le site public Spotify avec Playwright/Chromium.
- Il inspecte le DOM/HTML rendu des pages /related et /artist.
- Les données sont persistées dans plusieurs fichiers JSON.
- Chaque fichier contient au maximum 45 000 artistes.
- Le crawler vérifie les doublons dans TOUS les fichiers artistes*.json.

MULTI-WORKER:
- WORKER_ID identifie le worker actuel.
- WORKER_COUNT définit le nombre total de workers.
- Chaque artiste est déterministiquement affecté à un seul worker.
- Les workers ne traitent donc volontairement pas le même artiste.
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

# ------------------------------------------------------------
# FICHIERS ARTISTES
# ------------------------------------------------------------

ARTISTS_PREFIX = "artistes"

# Nombre maximum d'artistes par fichier.
ARTISTS_MAX_PER_FILE = 45000

ARTISTS_FILE = DATA_DIR / "artistes.json"

STATE_FILE = DATA_DIR / "state.json"

BASE = "https://open.spotify.com"


MAX_SECONDS = int(
    os.getenv(
        "CRAWLER_MAX_SECONDS",
        "3000"
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


# ============================================================
# GESTION DES FICHIERS ARTISTES
# ============================================================

def get_artist_files():
    """
    Retourne tous les fichiers artistes*.json dans l'ordre.

    Ordre:
        artistes.json
        artistes_1.json
        artistes_2.json
        artistes_3.json
        ...
    """

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    files = []

    main_file = DATA_DIR / "artistes.json"

    if main_file.exists():
        files.append(
            main_file
        )

    numbered = []

    for path in DATA_DIR.glob(
        "artistes_*.json"
    ):

        match = re.fullmatch(
            r"artistes_(\d+)\.json",
            path.name
        )

        if match:
            numbered.append(
                (
                    int(match.group(1)),
                    path
                )
            )

    numbered.sort(
        key=lambda x: x[0]
    )

    files.extend(
        path
        for _, path in numbered
    )

    # Si aucun fichier n'existe encore,
    # le premier sera artistes.json.
    if not files:
        files.append(
            main_file
        )

    return files


def load_all_artists():
    """
    Charge TOUS les fichiers artistes*.json.

    Retourne:

        artists_docs:
            {
                Path: {
                    "artists": {...}
                }
            }

        artists_index:
            {
                artist_id: Path
            }

    L'index permet de savoir dans quel fichier
    se trouve déjà un artiste.
    """

    artists_docs = {}

    artists_index = {}

    files = get_artist_files()

    for path in files:

        doc = load_json(
            path,
            {
                "artists": {}
            }
        )

        if (
            "artists" not in doc
            or not isinstance(
                doc["artists"],
                dict
            )
        ):
            doc["artists"] = {}

        artists_docs[path] = doc

        for artist_id in doc["artists"]:

            # Si un doublon existe déjà dans plusieurs
            # fichiers, on conserve la première occurrence.
            if artist_id not in artists_index:
                artists_index[artist_id] = path

    total = len(
        artists_index
    )

    print(
        f"[ARTISTS] "
        f"{len(artists_docs)} fichier(s) chargé(s) | "
        f"{total} artiste(s) uniques",
        flush=True
    )

    for path, doc in artists_docs.items():

        print(
            f"[ARTISTS] "
            f"{path.name}: "
            f"{len(doc['artists'])} artistes",
            flush=True
        )

    return (
        artists_docs,
        artists_index
    )


def get_next_artist_file(
    artists_docs
):
    """
    Retourne le prochain fichier artistes_X.json.
    """

    max_number = 0

    for path in artists_docs:

        if path.name == "artistes.json":
            continue

        match = re.fullmatch(
            r"artistes_(\d+)\.json",
            path.name
        )

        if match:

            max_number = max(
                max_number,
                int(
                    match.group(1)
                )
            )

    if max_number == 0:
        return (
            DATA_DIR
            / "artistes_1.json"
        )

    return (
        DATA_DIR
        / f"artistes_{max_number + 1}.json"
    )


def get_artist_storage(
    artists_docs
):
    """
    Trouve le premier fichier ayant encore de la place.

    Maximum:
        45 000 artistes / fichier.

    Si tous les fichiers sont pleins,
    un nouveau fichier est créé.
    """

    files = get_artist_files()

    for path in files:

        doc = artists_docs.get(
            path
        )

        if doc is None:

            doc = {
                "artists": {}
            }

            artists_docs[path] = doc

        count = len(
            doc["artists"]
        )

        if count < ARTISTS_MAX_PER_FILE:

            return (
                path,
                doc
            )

    # Tous les fichiers existants sont pleins.

    new_path = get_next_artist_file(
        artists_docs
    )

    new_doc = {
        "artists": {}
    }

    artists_docs[new_path] = new_doc

    print(
        f"[ARTISTS] "
        f"Tous les fichiers sont pleins. "
        f"Création de {new_path.name}",
        flush=True
    )

    save_json(
        new_path,
        new_doc
    )

    return (
        new_path,
        new_doc
    )


def artist_exists(
    artist_id,
    artists_index,
    artists_docs
):
    """
    Vérifie qu'un artiste existe dans TOUS les fichiers.

    L'index permet une vérification rapide.

    Une vérification des documents est également effectuée
    comme sécurité supplémentaire.
    """

    if artist_id in artists_index:
        return True

    # Vérification complète des fichiers.
    for path, doc in artists_docs.items():

        if artist_id in doc.get(
            "artists",
            {}
        ):

            artists_index[
                artist_id
            ] = path

            return True

    return False


def add_artist(
    artist_id,
    profile,
    artists_docs,
    artists_index
):
    """
    Ajoute un artiste dans le fichier approprié.

    Aucun doublon n'est autorisé entre les fichiers.

    Retourne:
        True  = artiste ajouté
        False = artiste déjà présent
    """

    # --------------------------------------------------------
    # ANTI-DOUBLON
    # --------------------------------------------------------

    if artist_exists(
        artist_id,
        artists_index,
        artists_docs
    ):

        print(
            f"[ARTISTS] "
            f"[DUPLICATE] "
            f"{artist_id} déjà présent",
            flush=True
        )

        return False

    # --------------------------------------------------------
    # CHOIX DU FICHIER
    # --------------------------------------------------------

    path, doc = get_artist_storage(
        artists_docs
    )

    # --------------------------------------------------------
    # SÉCURITÉ
    # --------------------------------------------------------

    if len(
        doc["artists"]
    ) >= ARTISTS_MAX_PER_FILE:

        raise RuntimeError(
            f"Le fichier {path.name} "
            f"est déjà plein "
            f"({len(doc['artists'])} artistes)"
        )

    # --------------------------------------------------------
    # AJOUT
    # --------------------------------------------------------

    doc["artists"][
        artist_id
    ] = profile

    # Mise à jour immédiate de l'index.
    artists_index[
        artist_id
    ] = path

    # --------------------------------------------------------
    # SAUVEGARDE
    # --------------------------------------------------------

    save_json(
        path,
        doc
    )

    print(
        f"[ARTISTS] "
        f"[ADDED] "
        f"{artist_id} -> "
        f"{path.name} "
        f"({len(doc['artists'])}/"
        f"{ARTISTS_MAX_PER_FILE})",
        flush=True
    )

    return True


# ============================================================
# NORMALISATION ID
# ============================================================

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

    Le résultat est déterministe:
    le même artist_id sera toujours attribué
    au même worker tant que WORKER_COUNT
    reste identique.
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
    True si l'artiste appartient au worker actuel.
    """

    return (
        worker_for_artist(
            artist_id
        )
        == WORKER_ID
    )


# ============================================================
# EXTRACTION DU NOM
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
    Sources utilisées:

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
    Spotify change les textes des boutons
    de cookies selon la langue.
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
    Extrait les IDs artistes du DOM.

    Utilise également une extraction HTML
    de secours.
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
    # HTML FALLBACK
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

    return {
        "id": artist_id,

        "name": name,

        "url": url,

        "uri":
            f"spotify:artist:{artist_id}",

        "monthly_listeners":
            monthly,

        "genres": [],

        "external_urls": {
            "spotify": url
        },

        "first_seen": now_iso(),

        "last_seen": now_iso()
    }


# ============================================================
# PREPARATION STATE WORKER
# ============================================================

def prepare_worker_state(
    artists_index,
    state
):
    """
    Construit la queue du worker à partir de l'état global.

    Les artistes sont filtrés par worker.
    Les artistes déjà traités sont ignorés.
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

    for aid in artists_index:

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
    Sauvegarde l'état du worker actuel.
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
        f"[WORKER] "
        f"ARTISTS_MAX_PER_FILE="
        f"{ARTISTS_MAX_PER_FILE}",
        flush=True
    )

    print(
        "============================================================",
        flush=True
    )

    # --------------------------------------------------------
    # TOUS LES FICHIERS ARTISTES
    # --------------------------------------------------------

    (
        artists_docs,
        artists_index
    ) = load_all_artists()

    # --------------------------------------------------------
    # STATE
    # --------------------------------------------------------

    state = load_json(
        STATE_FILE,
        {}
    )

    queue, processed = prepare_worker_state(
        artists_index,
        state
    )

    # --------------------------------------------------------
    # STATS
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

    print(
        f"[WORKER {WORKER_ID}] "
        f"Total artists in database: "
        f"{len(artists_index)}",
        flush=True
    )

    # --------------------------------------------------------
    # DISTRIBUTION
    # --------------------------------------------------------

    assigned_count = 0

    for aid in artists_index:

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
            # SÉCURITÉ WORKER
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
            # DÉJÀ TRAITÉ
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
                    # NE PAS AJOUTER LA SOURCE
                    # ----------------------------------------

                    if related_id == aid:
                        continue

                    # ----------------------------------------
                    # DÉTERMINER LE WORKER
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
                    # ANTI-DOUBLON GLOBAL
                    # ----------------------------------------

                    already_exists = artist_exists(
                        related_id,
                        artists_index,
                        artists_docs
                    )

                    # ----------------------------------------
                    # NOUVEL ARTISTE
                    # ----------------------------------------

                    if not already_exists:

                        print(
                            f"[WORKER {WORKER_ID}] "
                            f"[NEW] "
                            f"{related_id} "
                            f"-> worker "
                            f"{target_worker}",
                            flush=True
                        )

                        # ------------------------------------
                        # SCRAPE DU PROFIL
                        # UNIQUEMENT SI CE WORKER
                        # EST RESPONSABLE
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

                            if add_artist(
                                related_id,
                                profile,
                                artists_docs,
                                artists_index
                            ):

                                stats[
                                    "added"
                                ] += 1

                    else:

                        print(
                            f"[WORKER {WORKER_ID}] "
                            f"[EXISTS] "
                            f"{related_id}",
                            flush=True
                        )

                    # ----------------------------------------
                    # QUEUE
                    # ----------------------------------------

                    if (
                        belongs_to_worker(
                            related_id
                        )
                        and related_id not in processed
                        and related_id not in queue
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
                # REMETTRE DANS LA QUEUE
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

        # Sauvegarde de sécurité de tous les fichiers.
        for path, doc in artists_docs.items():

            save_json(
                path,
                doc
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
        f"artists={len(artists_index)}",
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

    # --------------------------------------------------------
    # RÉSUMÉ DES FICHIERS
    # --------------------------------------------------------

    for path, doc in artists_docs.items():

        print(
            f"[WORKER {WORKER_ID}] "
            f"{path.name}="
            f"{len(doc['artists'])} artistes",
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
