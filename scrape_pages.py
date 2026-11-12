"""Scrape RuneScape wiki page content as of a specific date from runescape.wiki API.

Uses SQLite for resume capability. Outputs a MediaWiki XML import file.

Strategy (works around MediaWiki API limitation where rvstart/rvdir/rvlimit
only work on single-page queries):

  Phase 1: Scan `list=allrevisions` with arvstart=CUTOFF arvdir=older to find
           all revisions before the cutoff. Record the newest revid per page.
  Phase 2: Fetch content in batches using `revids=id1|id2|...` (supports batching).
  Phase 3: Scan the move log forward from the cutoff and work out what each
           page was CALLED at the cutoff (see below).
  Phase 4: Export to MediaWiki XML import format, under those titles.

Why phase 3 exists
------------------
The API reports every revision under the page's title *today*, so a snapshot
built without it has 2011 content filed under 2026 names. That is not a cosmetic
problem: it silently breaks the snapshot's internal links.

`Death Plateau` is the worked example. In 2011 that was the quest article. In
January 2012 it was moved to `Death Plateau (historical)` and a new page was
written at the old name. So the scrape stored the 2011 quest under
`Death Plateau (historical)`, and `Death plateau` -- a redirect page we also
captured, whose 2011 text is `#REDIRECT [[Death Plateau]]` -- now points at a
title the snapshot does not contain. The same had happened to `Digsite` (moved
to `Varrock Dig Site` in 2019), `Dungeons` (`List of Dungeons`, 2013), and the
task-set pages behind every achievement diary.

This was originally read as pages being dropped by the scan. They were not: the
scan captured all of them, under names nobody would look for. Phase 3 walks the
move log from the cutoff to now and unwinds each chain, so a page is exported
under the name it actually had in 2011.
"""

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

WIKI_API = "https://runescape.wiki/api.php"
CUTOFF_DATE = "2011-05-23T00:00:00Z"
DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "staging.db"
XML_PATH = DATA_DIR / "pages.xml"

# runescape.wiki asks API clients to name a contact: set WIKI_SCRAPER_CONTACT
# (an email or a URL) before scraping.
USER_AGENT = (
    "rs647-wiki-scraper/1.0 "
    f"(private-server-reference; contact: {os.environ.get('WIKI_SCRAPER_CONTACT', 'unset')}) "
    "python-httpx"
)

# 0 main, 6 File, 10 Template, 14 Category, 100 Update.
#
# Namespace 100 holds the dated update pages -- one per release, which is the
# backlog for working forward from 289 to 647 in the order Jagex shipped things.
# The snapshot was taken without it, so the local wiki can answer "what are a
# hatchet's stats" but not "what shipped in November 2005".
TARGET_NAMESPACES = [0, 6, 10, 14, 100]
MAX_CONCURRENT = 20
BATCH_SIZE = 50
REQUEST_DELAY = 0.05


def init_db(db: sqlite3.Connection):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS target_revisions (
            title TEXT NOT NULL,
            ns INTEGER NOT NULL,
            revid INTEGER PRIMARY KEY,
            timestamp TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS revisions (
            title TEXT PRIMARY KEY,
            ns INTEGER NOT NULL,
            revid INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            content TEXT NOT NULL,
            contributor TEXT
        );
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        -- Page moves made AFTER the cutoff. Each one is a step between the name
        -- a page has now and the name it had in 2011; see phase 3.
        CREATE TABLE IF NOT EXISTS moves (
            logid INTEGER PRIMARY KEY,
            timestamp TEXT NOT NULL,
            from_title TEXT NOT NULL,
            to_title TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_target_title ON target_revisions(title);
        CREATE INDEX IF NOT EXISTS idx_moves_to ON moves(to_title);
    """)
    # `title_at_cutoff` is added rather than baked into the CREATE so an
    # existing staging.db can be repaired in place -- the content is already
    # correct in one, only the names are wrong.
    for table in ("revisions", "target_revisions"):
        columns = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        if "title_at_cutoff" not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN title_at_cutoff TEXT")
    db.commit()


def get_meta(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(db: sqlite3.Connection, key: str, value: str):
    db.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
    )
    db.commit()


async def api_query(client: httpx.AsyncClient, semaphore: asyncio.Semaphore, **params):
    params.setdefault("format", "json")
    params.setdefault("maxlag", "5")

    retries = 0
    max_retries = 5
    while True:
        async with semaphore:
            await asyncio.sleep(REQUEST_DELAY)
            try:
                resp = await client.get(WIKI_API, params=params, timeout=30)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as e:
                retries += 1
                if retries > max_retries:
                    raise
                wait = min(5 * (2 ** retries), 120)
                print(f"  Connection error ({e}), retrying in {wait}s...", flush=True)
                await asyncio.sleep(wait)
                continue

        if resp.status_code == 429 or "maxlag" in resp.text[:200].lower():
            retry_after = int(resp.headers.get("Retry-After", "30"))
            print(f"  Rate limited, waiting {retry_after}s...", flush=True)
            await asyncio.sleep(retry_after)
            continue

        if resp.status_code != 200:
            retries += 1
            if retries > max_retries:
                resp.raise_for_status()
            wait = min(5 * (2 ** retries), 120)
            print(f"  HTTP {resp.status_code}, retrying in {wait}s...", flush=True)
            await asyncio.sleep(wait)
            continue

        return resp.json()


async def scan_allrevisions(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    db: sqlite3.Connection,
    ns: int,
):
    """Use list=allrevisions to find the newest revision per page before cutoff."""
    meta_key = f"scan_done_{ns}"
    if get_meta(db, meta_key):
        count = db.execute(
            "SELECT COUNT(DISTINCT title) FROM target_revisions WHERE ns = ?", (ns,)
        ).fetchone()[0]
        print(f"  NS {ns}: already scanned ({count} unique pages)", flush=True)
        return

    resume_key = f"scan_continue_{ns}"
    seen_titles: set[str] = set(
        r[0] for r in db.execute(
            "SELECT DISTINCT title FROM target_revisions WHERE ns = ?", (ns,)
        ).fetchall()
    )

    ns_name = {0: "Main", 6: "File", 10: "Template", 14: "Category"}.get(ns, f"NS{ns}")
    print(f"  Scanning NS {ns} ({ns_name})...", flush=True)

    params = {
        "action": "query",
        "list": "allrevisions",
        "arvprop": "ids|timestamp",
        "arvnamespace": str(ns),
        "arvstart": CUTOFF_DATE,
        "arvdir": "older",
        "arvlimit": "500",
    }

    saved_continue = get_meta(db, resume_key)
    if saved_continue:
        cont_data = json.loads(saved_continue)
        params.update(cont_data)
        print(f"    Resuming from saved continue token ({len(seen_titles)} pages so far)", flush=True)

    request_count = 0
    new_pages = 0
    start_time = time.time()

    while True:
        data = await api_query(client, semaphore, **params)

        if "error" in data:
            # Stop, but do NOT fall through to the `scan_done` marker below: a
            # half-scanned namespace that calls itself finished can never be
            # resumed, because the next run skips it on that marker and the
            # saved continue token is never read again.
            print(f"  API error: {data['error']}", flush=True)
            print(f"  NS {ns}: scan INCOMPLETE, not marking done; "
                  f"re-run to resume from the saved token", flush=True)
            db.commit()
            return

        entries = data.get("query", {}).get("allrevisions", [])
        batch_new = 0
        for entry in entries:
            title = entry["title"]
            if title not in seen_titles:
                seen_titles.add(title)
                rev = entry["revisions"][0]
                db.execute(
                    "INSERT OR IGNORE INTO target_revisions (title, ns, revid, timestamp) "
                    "VALUES (?, ?, ?, ?)",
                    (title, ns, rev["revid"], rev["timestamp"]),
                )
                batch_new += 1
                new_pages += 1

        request_count += 1

        cont = data.get("continue")
        if cont:
            set_meta(db, resume_key, json.dumps(cont))
            params.update(cont)
        db.commit()

        if request_count % 50 == 0:
            elapsed = time.time() - start_time
            print(
                f"    {request_count} requests, {len(seen_titles)} unique pages, "
                f"+{new_pages} new ({elapsed:.0f}s)",
                flush=True,
            )

        if not cont:
            break

    db.commit()
    set_meta(db, meta_key, "1")
    elapsed = time.time() - start_time
    print(
        f"  NS {ns}: {len(seen_titles)} unique pages found in {request_count} requests ({elapsed:.0f}s)",
        flush=True,
    )


async def scan_moves(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    db: sqlite3.Connection,
):
    """Record every page move made since the cutoff.

    One pass forward through `list=logevents&letype=move`. Roughly 500 moves per
    request and about three weeks of wiki time per 500, so fifteen years is a
    few hundred requests -- cheap next to the revision scan, and it only ever
    has to catch up from where it left off.
    """
    if get_meta(db, "moves_done"):
        n = db.execute("SELECT COUNT(*) FROM moves").fetchone()[0]
        print(f"  Move log: already scanned ({n} moves)", flush=True)
        return

    params = {
        "action": "query",
        "list": "logevents",
        "letype": "move",
        "lestart": CUTOFF_DATE,
        "ledir": "newer",
        "lelimit": "500",
        "leprop": "ids|title|timestamp|details",
    }
    saved_continue = get_meta(db, "moves_continue")
    if saved_continue:
        params.update(json.loads(saved_continue))
        print("    Resuming move scan from saved token", flush=True)

    total, requests = db.execute("SELECT COUNT(*) FROM moves").fetchone()[0], 0
    start = time.time()
    while True:
        data = await api_query(client, semaphore, **params)
        if "error" in data:
            print(f"  API error: {data['error']}", flush=True)
            print("  Move log: scan INCOMPLETE, not marking done", flush=True)
            db.commit()
            return

        for ev in data.get("query", {}).get("logevents", []):
            target = (ev.get("params") or {}).get("target_title")
            if not target:
                continue                     # suppressed or malformed entry
            db.execute(
                "INSERT OR IGNORE INTO moves (logid, timestamp, from_title, to_title) "
                "VALUES (?, ?, ?, ?)",
                (ev["logid"], ev["timestamp"], ev["title"], target),
            )
            total += 1

        requests += 1
        cont = data.get("continue")
        if cont:
            set_meta(db, "moves_continue", json.dumps(cont))
            params.update(cont)
        db.commit()

        if requests % 50 == 0:
            print(f"    {requests} requests, {total} moves ({time.time() - start:.0f}s)",
                  flush=True)
        if not cont:
            break

    set_meta(db, "moves_done", "1")
    db.commit()
    print(f"  Move log: {total} moves in {requests} requests "
          f"({time.time() - start:.0f}s)", flush=True)


#: A page renamed more than this many times since 2011 is a loop, not a chain.
MAX_MOVE_HOPS = 25


async def load_namespace_names(client, semaphore, db):
    """The wiki's namespace prefixes, so `Beta:Mining` can be told from a title
    that merely contains a colon (`Fairy Tale II: Cure a Queen` is one page in
    the main namespace, not a page in a `Fairy Tale II` namespace)."""
    cached = get_meta(db, "namespace_names")
    if cached:
        return json.loads(cached)
    data = await api_query(client, semaphore, action="query", meta="siteinfo",
                           siprop="namespaces|namespacealiases")
    # The API's legacy JSON puts the display name in "*"; formatversion=2 calls
    # it "name". Accept both rather than depending on which one this call got.
    names = {}
    for entry in (data.get("query", {}).get("namespaces") or {}).values():
        for key in ("name", "canonical", "*"):
            if entry.get(key):
                names[entry[key]] = entry["id"]
    for alias in data.get("query", {}).get("namespacealiases") or []:
        name = alias.get("alias") or alias.get("*")
        if name:
            names[name] = alias["id"]
    set_meta(db, "namespace_names", json.dumps(names))
    db.commit()
    return names


def namespace_of(title: str, names: dict) -> int:
    prefix, sep, _ = title.partition(":")
    return names.get(prefix, 0) if sep else 0


def resolve_cutoff_titles(db: sqlite3.Connection, captured_at: str | None = None,
                          ns_names: dict | None = None):
    """Work out what each captured page was called at the cutoff.

    Unwinds the move chain backwards. A move recorded as `A -> B` means the page
    now at `B` was called `A` before that timestamp, so resolving starts from
    the stored title and repeatedly asks "what was the most recent move INTO
    this name, earlier than the last one I applied?".

    The timestamp bound is what makes it a chain rather than a guess: a name can
    be reused, and without it a page could be walked back into a move that
    happened after the one already applied.

    `captured_at` is the second bound and matters just as much. The titles in
    `revisions` are the ones the page had when the *content* was fetched, not
    today's, so a move made after that fetch describes a rename this snapshot
    never saw. Applying those produced 117 collisions on the first run -- the
    wiki renamed `Throwing knife` to `Throwing knives` in July 2026, months
    after this snapshot was taken, and unwinding it walked the wrong page onto a
    name another page was still holding.
    """
    if captured_at is None:
        captured_at = get_meta(db, "captured_at")
    if not captured_at:
        captured_at = "9999"
        print("  NOTE: no captured_at recorded; assuming the stored titles are "
              "current. Pass --captured-at for an older snapshot.", flush=True)

    ns_names = ns_names if ns_names is not None else json.loads(
        get_meta(db, "namespace_names") or "{}")

    into: dict[str, list[tuple[str, str]]] = {}
    skipped = cross_ns = 0
    for ts, src, dst in db.execute("SELECT timestamp, from_title, to_title FROM moves"):
        if ts >= captured_at:
            skipped += 1
            continue
        # A move that crosses namespaces is a different page arriving, not this
        # one being renamed. `Beta:Mining` was moved onto `Mining` in 2019, and
        # following that walked the real Mining article -- which has revisions
        # from 2011 and was never in the beta namespace -- back to a title that
        # did not exist until the EoC beta a year after this snapshot.
        if ns_names and namespace_of(src, ns_names) != namespace_of(dst, ns_names):
            cross_ns += 1
            continue
        into.setdefault(dst, []).append((ts, src))
    if skipped:
        print(f"  Ignoring {skipped} moves made after the snapshot was captured "
              f"({captured_at})", flush=True)
    if cross_ns:
        print(f"  Ignoring {cross_ns} moves that cross namespaces", flush=True)
    for entries in into.values():
        entries.sort(reverse=True)

    rows = db.execute("SELECT title FROM revisions").fetchall()
    renamed, updates = 0, []
    for (title,) in rows:
        current, limit, hops = title, "9999", 0
        while hops < MAX_MOVE_HOPS:
            candidates = [(ts, src) for ts, src in into.get(current, []) if ts < limit]
            if not candidates:
                break
            limit, current = max(candidates)
            hops += 1
        updates.append((current, title))
        if current != title:
            renamed += 1

    db.executemany("UPDATE revisions SET title_at_cutoff = ? WHERE title = ?", updates)
    db.commit()

    # Two pages cannot share a name in the export: MediaWiki would import them
    # as one page and the snapshot would quietly lose the loser. Rather than
    # warn and ship it, the page with no positive evidence gives the name back.
    #
    # "Positive evidence" is having actually been moved: the log says that page
    # held the name at the cutoff. A page that never moved and merely sits on
    # the contested name is the weaker claim, and it keeps its own title, which
    # costs nothing -- it is where a reader would look for it anyway.
    # A page still sitting on the contested name keeps it -- it is the only one
    # of the two that has nowhere else to go, and it is where a reader would
    # look for it anyway. The page that moved away gives the name up and stays
    # under its current title.
    #
    # Reverting can itself collide with a third page, so this repeats until it
    # settles rather than assuming one pass is enough.
    reverted = 0
    for _ in range(5):
        dups = [n for (n,) in db.execute(
            "SELECT title_at_cutoff FROM revisions GROUP BY title_at_cutoff "
            "HAVING COUNT(*) > 1").fetchall()]
        if not dups:
            break
        for name in dups:
            holders = [t for (t,) in db.execute(
                "SELECT title FROM revisions WHERE title_at_cutoff = ?", (name,))]
            sitting = [t for t in holders if t == name]
            keeper = sitting[0] if sitting else sorted(holders)[0]
            for loser in holders:
                if loser == keeper:
                    continue
                db.execute("UPDATE revisions SET title_at_cutoff = title WHERE title = ?",
                           (loser,))
                reverted += 1
    db.commit()
    if reverted:
        print(f"  {reverted} pages kept their current title to avoid a collision "
              f"with a page still holding that name", flush=True)

    still = db.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM revisions GROUP BY title_at_cutoff "
        "HAVING COUNT(*) > 1)").fetchone()[0]
    if still:
        print(f"  WARNING: {still} duplicate titles remain after resolution", flush=True)

    # `target_revisions` carries the same titles and is what anything reading
    # the snapshot's index joins on, so it gets the resolved names too. Copied
    # from `revisions` rather than recomputed: one mapping, one source.
    db.execute("""
        UPDATE target_revisions SET title_at_cutoff = (
            SELECT r.title_at_cutoff FROM revisions r WHERE r.title = target_revisions.title
        ) WHERE EXISTS (
            SELECT 1 FROM revisions r WHERE r.title = target_revisions.title
        )""")
    db.commit()

    renamed = db.execute(
        "SELECT COUNT(*) FROM revisions WHERE title_at_cutoff <> title").fetchone()[0]
    print(f"  Restored 2011 titles for {renamed} of {len(rows)} pages", flush=True)
    return renamed


async def fetch_content_batch(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    db: sqlite3.Connection,
    revids: list[tuple[str, int, int]],
):
    """Fetch revision content by revision IDs (supports batching)."""
    id_str = "|".join(str(rid) for _, _, rid in revids)
    data = await api_query(
        client,
        semaphore,
        action="query",
        prop="revisions",
        rvprop="content|timestamp|user|ids",
        revids=id_str,
    )

    if "error" in data:
        print(f"  API error: {data['error']}", flush=True)
        return 0

    pages = data.get("query", {}).get("pages", {})
    saved = 0
    for _pid, page_data in pages.items():
        title = page_data.get("title", "")
        ns = page_data.get("ns", 0)
        revs = page_data.get("revisions")
        if revs and len(revs) > 0:
            rev = revs[0]
            content = rev.get("*", rev.get("content", ""))
            if content:
                db.execute(
                    "INSERT OR REPLACE INTO revisions (title, ns, revid, timestamp, content, contributor) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (title, ns, rev.get("revid", 0), rev.get("timestamp", ""), content, rev.get("user", "")),
                )
                saved += 1

    db.commit()
    return saved


def export_xml(db: sqlite3.Connection):
    print("\nExporting to MediaWiki XML...", flush=True)

    ns_names = {0: "", 6: "File", 10: "Template", 14: "Category"}

    root = ET.Element("mediawiki")
    root.set("xmlns", "http://www.mediawiki.org/xml/export-0.11/")
    root.set("xml:lang", "en")

    siteinfo = ET.SubElement(root, "siteinfo")
    ET.SubElement(siteinfo, "sitename").text = "RuneScape Wiki (2011 snapshot)"
    ET.SubElement(siteinfo, "dbname").text = "rswiki"
    ET.SubElement(siteinfo, "base").text = "http://localhost:8090/wiki/Main_Page"
    ET.SubElement(siteinfo, "generator").text = "scrape_pages.py"
    namespaces_el = ET.SubElement(siteinfo, "namespaces")
    for ns_id, ns_name in ns_names.items():
        ns_el = ET.SubElement(namespaces_el, "namespace")
        ns_el.set("key", str(ns_id))
        ns_el.set("case", "first-letter")
        if ns_name:
            ns_el.text = ns_name

    # The 2011 name where phase 3 worked one out, today's otherwise. Exporting
    # today's names is what left the snapshot's own redirects pointing at pages
    # it does not contain.
    cursor = db.execute(
        "SELECT COALESCE(title_at_cutoff, title), ns, timestamp, content, contributor "
        "FROM revisions ORDER BY ns, 1"
    )

    count = 0
    for title, ns, timestamp, content, contributor in cursor:
        page_el = ET.SubElement(root, "page")
        ET.SubElement(page_el, "title").text = title
        ET.SubElement(page_el, "ns").text = str(ns)

        rev_el = ET.SubElement(page_el, "revision")
        ET.SubElement(rev_el, "timestamp").text = timestamp
        if contributor:
            contrib_el = ET.SubElement(rev_el, "contributor")
            ET.SubElement(contrib_el, "username").text = contributor
        ET.SubElement(rev_el, "model").text = "wikitext"
        ET.SubElement(rev_el, "format").text = "text/x-wiki"
        text_el = ET.SubElement(rev_el, "text")
        text_el.set("xml:space", "preserve")
        text_el.text = content
        count += 1

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(str(XML_PATH), encoding="utf-8", xml_declaration=True)
    print(f"Exported {count} pages to {XML_PATH}", flush=True)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--namespaces",
        type=lambda s: [int(n) for n in s.split(",")],
        default=TARGET_NAMESPACES,
        help="Comma-separated namespace ids to scan. Defaults to all of them. "
             "Scanning is the expensive half -- it walks every revision before "
             "the cutoff -- and the fetch phase already skips pages it holds, "
             "so adding one namespace to an existing snapshot is worth scoping "
             "rather than re-walking half a million revisions of someone "
             "else's server. The export always writes the whole database.",
    )
    parser.add_argument(
        "--captured-at",
        metavar="ISO8601",
        help="When the stored page titles were current, e.g. 2026-05-16. Moves "
             "made after it are ignored, because the snapshot never saw them. "
             "Recorded automatically on a fresh scrape; needed by hand only for "
             "a snapshot taken before that was written down.",
    )
    parser.add_argument(
        "--retitle-only",
        action="store_true",
        help="Skip the revision scan and the content fetch; only scan the move "
             "log, restore 2011 titles and re-export. This is the repair path "
             "for a snapshot captured before phase 3 existed: its content is "
             "already right and only the page names are wrong, so there is no "
             "reason to re-fetch 80,000 pages to fix them.",
    )
    args = parser.parse_args()
    namespaces = args.namespaces

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_PATH))
    init_db(db)

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    # Each namespace scan needs its own DB connection (sqlite + threads)
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        limits=httpx.Limits(max_connections=MAX_CONCURRENT + 5, max_keepalive_connections=MAX_CONCURRENT),
    ) as client:
        # Phase 1: Scan allrevisions — all namespaces in parallel
        if not args.retitle_only:
            print(f"=== Phase 1: Scan revisions before {CUTOFF_DATE} (parallel) ===", flush=True)
            scan_tasks = [
                scan_allrevisions(client, semaphore, db, ns)
                for ns in namespaces
            ]
            await asyncio.gather(*scan_tasks)

            total_targets = db.execute("SELECT COUNT(*) FROM target_revisions").fetchone()[0]
            print(f"\nTotal pages with pre-cutoff revisions: {total_targets}", flush=True)

        # Phase 2: Fetch content — fire batches concurrently
        already_fetched = db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
        fetched_titles = set(
            r[0] for r in db.execute("SELECT title FROM revisions").fetchall()
        )

        targets = db.execute(
            "SELECT title, ns, revid FROM target_revisions ORDER BY ns, title"
        ).fetchall()
        to_fetch = [(t, ns, rid) for t, ns, rid in targets if t not in fetched_titles]

        if args.retitle_only:
            to_fetch = []
            print("\n=== Phase 2: Fetch content — skipped (--retitle-only) ===", flush=True)
        else:
            print(f"\n=== Phase 2: Fetch content ({len(to_fetch)} pages, {already_fetched} already done) ===", flush=True)

        if to_fetch:
            batches = [to_fetch[i : i + BATCH_SIZE] for i in range(0, len(to_fetch), BATCH_SIZE)]
            total_saved = already_fetched
            start_time = time.time()
            completed = 0

            # Fire batches in parallel waves
            WAVE_SIZE = MAX_CONCURRENT
            for wave_start in range(0, len(batches), WAVE_SIZE):
                wave = batches[wave_start : wave_start + WAVE_SIZE]
                results = await asyncio.gather(*[
                    fetch_content_batch(client, semaphore, db, batch)
                    for batch in wave
                ])
                for saved in results:
                    total_saved += saved
                    completed += 1

                elapsed = time.time() - start_time
                rate = completed / elapsed if elapsed > 0 else 0
                eta = (len(batches) - completed) / rate if rate > 0 else 0
                print(
                    f"  Batch {completed}/{len(batches)} - "
                    f"{total_saved} pages fetched "
                    f"(ETA: {eta:.0f}s)",
                    flush=True,
                )

            rev_count = db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
            elapsed = time.time() - start_time
            print(f"\nFetch complete: {rev_count} pages with content", flush=True)
            print(f"Time: {elapsed:.0f}s", flush=True)
        else:
            print("All content already fetched!", flush=True)

        # Phase 3: what was each page CALLED at the cutoff?
        print("\n=== Phase 3: Restore 2011 page titles ===", flush=True)
        if args.captured_at:
            set_meta(db, "captured_at", args.captured_at)
        elif not get_meta(db, "captured_at") and not args.retitle_only:
            # A fresh fetch means the titles are current as of now, and saying
            # so is what lets a later re-run tell its own moves from the wiki's.
            set_meta(db, "captured_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        await scan_moves(client, semaphore, db)
        ns_names = await load_namespace_names(client, semaphore, db)
        resolve_cutoff_titles(db, ns_names=ns_names)

    # Phase 4: Export XML
    export_xml(db)
    db.close()


if __name__ == "__main__":
    asyncio.run(main())
