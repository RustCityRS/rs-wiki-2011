"""Download RuneScape wiki images as of a specific date from runescape.wiki API.

Enumerates all images, filters by date, downloads archive versions.
Resumable via progress JSON file.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx

WIKI_API = "https://runescape.wiki/api.php"
CUTOFF_DATE = "2011-05-23T00:00:00Z"
DATA_DIR = Path(__file__).parent / "data"
IMAGES_DIR = DATA_DIR / "images"
PROGRESS_PATH = DATA_DIR / "progress_images.json"

# runescape.wiki asks API clients to name a contact: set WIKI_SCRAPER_CONTACT
# (an email or a URL) before scraping.
USER_AGENT = (
    "rs647-wiki-scraper/1.0 "
    f"(private-server-reference; contact: {os.environ.get('WIKI_SCRAPER_CONTACT', 'unset')}) "
    "python-httpx"
)

MAX_API_CONCURRENT = 20
MAX_DOWNLOAD_CONCURRENT = 10
BATCH_SIZE = 50
REQUEST_DELAY = 0.05


def load_progress() -> dict:
    if PROGRESS_PATH.exists():
        return json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    return {
        "enumerated": False,
        "all_images": [],
        "image_info": {},
        "checked": [],
        "downloaded": [],
    }


def save_progress(progress: dict):
    PROGRESS_PATH.write_text(
        json.dumps(progress, ensure_ascii=False), encoding="utf-8"
    )


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
                print(f"  Connection error ({e}), retrying in {wait}s...")
                await asyncio.sleep(wait)
                continue

        if resp.status_code == 429 or "maxlag" in resp.text[:200].lower():
            retry_after = int(resp.headers.get("Retry-After", "30"))
            print(f"  Rate limited, waiting {retry_after}s...")
            await asyncio.sleep(retry_after)
            continue

        if resp.status_code != 200:
            retries += 1
            if retries > max_retries:
                resp.raise_for_status()
            wait = min(5 * (2 ** retries), 120)
            print(f"  HTTP {resp.status_code}, retrying in {wait}s...")
            await asyncio.sleep(wait)
            continue

        return resp.json()


async def enumerate_images(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    progress: dict,
):
    if progress["enumerated"]:
        print(f"  Already enumerated: {len(progress['all_images'])} images")
        return

    print("  Enumerating all images...")
    all_images = []
    params = {"action": "query", "list": "allimages", "ailimit": "500", "aisort": "name"}

    while True:
        data = await api_query(client, semaphore, **params)
        images = data.get("query", {}).get("allimages", [])
        for img in images:
            all_images.append(img["name"])

        cont = data.get("continue")
        if not cont:
            break
        params["aicontinue"] = cont["aicontinue"]

        if len(all_images) % 5000 < 500:
            print(f"    ...{len(all_images)} images enumerated")

    progress["all_images"] = all_images
    progress["enumerated"] = True
    save_progress(progress)
    print(f"  Enumerated {len(all_images)} total images")


async def fetch_image_info_batch(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    progress: dict,
    names: list[str],
):
    titles = "|".join(f"File:{name}" for name in names)
    data = await api_query(
        client,
        semaphore,
        action="query",
        prop="imageinfo",
        iiprop="url|timestamp|size|archivename",
        iistart=CUTOFF_DATE,
        iilimit="1",
        titles=titles,
    )

    pages = data.get("query", {}).get("pages", {})
    found = 0
    for _pid, page_data in pages.items():
        title = page_data.get("title", "")
        name = title.removeprefix("File:")
        imageinfo = page_data.get("imageinfo", [])
        if imageinfo:
            info = imageinfo[0]
            url = info.get("url")
            if url:
                progress["image_info"][name] = {
                    "url": url,
                    "timestamp": info.get("timestamp", ""),
                    "size": info.get("size", 0),
                }
                found += 1

    # Handle normalized titles
    for n in data.get("query", {}).get("normalized", []):
        from_name = n["from"].removeprefix("File:")
        to_name = n["to"].removeprefix("File:")
        if to_name in progress["image_info"] and from_name not in progress["image_info"]:
            progress["image_info"][from_name] = progress["image_info"][to_name]

    return found


def sanitize_filename(name: str) -> str:
    """Make a filename safe for Windows."""
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    if len(name) > 200:
        name = name[:200]
    return name


async def download_image(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    name: str,
    url: str,
    progress: dict,
):
    safe_name = sanitize_filename(name)
    dest = IMAGES_DIR / safe_name

    retries = 0
    max_retries = 3
    while True:
        async with semaphore:
            try:
                resp = await client.get(url, timeout=60, follow_redirects=True)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as e:
                retries += 1
                if retries > max_retries:
                    print(f"  FAILED: {name} ({e})")
                    return False
                await asyncio.sleep(5 * retries)
                continue

        if resp.status_code == 200:
            dest.write_bytes(resp.content)
            return True

        retries += 1
        if retries > max_retries:
            print(f"  FAILED: {name} (HTTP {resp.status_code})")
            return False
        await asyncio.sleep(5 * retries)

    return False


async def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    progress = load_progress()
    api_semaphore = asyncio.Semaphore(MAX_API_CONCURRENT)
    dl_semaphore = asyncio.Semaphore(MAX_DOWNLOAD_CONCURRENT)

    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:
        # Phase 1: Enumerate all images
        print(f"=== Phase 1: Enumerate images ===")
        await enumerate_images(client, api_semaphore, progress)

        # Phase 2: Get image info (date-filtered)
        all_images = progress["all_images"]
        checked_set = set(progress.get("checked", []))
        downloaded_set = set(progress["downloaded"])

        unchecked = [n for n in all_images if n not in checked_set]
        print(f"\n=== Phase 2: Fetch image info (cutoff: {CUTOFF_DATE}) ===", flush=True)
        print(f"  Total: {len(all_images)}, already checked: {len(checked_set)}, remaining: {len(unchecked)}", flush=True)

        if unchecked:
            batches = [unchecked[i : i + BATCH_SIZE] for i in range(0, len(unchecked), BATCH_SIZE)]
            start_time = time.time()
            completed = 0

            WAVE_SIZE = MAX_API_CONCURRENT
            for wave_start in range(0, len(batches), WAVE_SIZE):
                wave = batches[wave_start : wave_start + WAVE_SIZE]
                results = await asyncio.gather(*[
                    fetch_image_info_batch(client, api_semaphore, progress, batch)
                    for batch in wave
                ])
                for batch, _found in zip(wave, results):
                    checked_set.update(batch)
                    completed += 1

                progress["checked"] = list(checked_set)

                elapsed = time.time() - start_time
                rate = completed / elapsed if elapsed > 0 else 0
                eta = (len(batches) - completed) / rate if rate > 0 else 0
                print(
                    f"  Batch {completed}/{len(batches)} - "
                    f"{len(progress['image_info'])} pre-2011, {len(checked_set)} checked "
                    f"(ETA: {eta:.0f}s)",
                    flush=True,
                )
                save_progress(progress)

            save_progress(progress)

        total_with_info = len(progress["image_info"])
        print(f"\n{total_with_info} images existed before {CUTOFF_DATE}")

        # Phase 3: Download images
        to_download = [
            (name, info["url"])
            for name, info in progress["image_info"].items()
            if name not in downloaded_set
        ]

        print(f"\n=== Phase 3: Download images ===")
        print(f"  To download: {len(to_download)}, already downloaded: {len(downloaded_set)}")

        if to_download:
            start_time = time.time()
            success = 0
            failed = 0

            # Process in chunks for progress reporting
            chunk_size = 100
            for chunk_start in range(0, len(to_download), chunk_size):
                chunk = to_download[chunk_start : chunk_start + chunk_size]
                tasks = [
                    download_image(client, dl_semaphore, name, url, progress)
                    for name, url in chunk
                ]
                results = await asyncio.gather(*tasks)

                for (name, _url), ok in zip(chunk, results):
                    if ok:
                        progress["downloaded"].append(name)
                        success += 1
                    else:
                        failed += 1

                save_progress(progress)
                done_total = chunk_start + len(chunk)
                elapsed = time.time() - start_time
                rate = done_total / elapsed if elapsed > 0 else 0
                eta = (len(to_download) - done_total) / rate if rate > 0 else 0
                print(
                    f"  {done_total}/{len(to_download)} - "
                    f"{success} ok, {failed} failed "
                    f"(ETA: {eta:.0f}s)"
                )

            elapsed = time.time() - start_time
            print(f"\nDownload complete: {success} ok, {failed} failed in {elapsed:.0f}s")
        else:
            print("  All images already downloaded!")

    total_downloaded = len(progress["downloaded"])
    total_size = sum(
        f.stat().st_size for f in IMAGES_DIR.iterdir() if f.is_file()
    )
    print(f"\nDone! {total_downloaded} images, {total_size / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    asyncio.run(main())
