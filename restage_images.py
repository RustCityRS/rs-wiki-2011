"""Stage image files under the names they had at the 2011 cutoff.

Why this is needed
------------------
`scrape_pages.py` phase 3 restores each page's 2011 title, and most of what the
wiki has renamed since 2011 is images -- 16,689 of 19,520 retitled pages are in
the File namespace. But `scrape_images.py` downloaded the binaries under the
names the wiki uses *today*, so after retitling the description page sits at
`File:Magebane.png` while the file on disk is `Clean magebane.png`.

That mismatch is not cosmetic. The 2011 article text links `[[File:Magebane.png]]`
-- nine pages do, against one that uses the modern name -- so an image imported
under its modern name renders nowhere in this snapshot.

This copies each renamed file to its 2011 name in a staging directory, which
`importImages` can then register. The originals are left alone: the download
cache stays keyed the way `scrape_images.py` expects to find things.

Run:  py tools/wiki/restage_images.py
Then: docker compose exec -T mediawiki php maintenance/run.php importImages \
          --overwrite /import/images-2011
"""

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"


def disk_name(title: str) -> str:
    """`File:Clean magebane.png` -> `Clean magebane.png`."""
    return title.split(":", 1)[1] if ":" in title else title


def find_source(images: Path, name: str):
    """The downloaded file for a wiki filename.

    MediaWiki treats spaces and underscores in a filename as the same character,
    and the scrape saved both spellings for most files, so try both rather than
    reporting a miss for a file that is sitting right there under the other one.
    """
    for candidate in (name, name.replace(" ", "_"), name.replace("_", " ")):
        path = images / candidate
        if path.is_file():
            return path
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DATA / "staging.db")
    ap.add_argument("--images", type=Path, default=DATA / "images")
    ap.add_argument("--out", type=Path, default=DATA / "images-2011")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.db.is_file():
        sys.exit(f"missing {args.db}")
    if not args.images.is_dir():
        sys.exit(f"missing {args.images}")

    con = sqlite3.connect(args.db)
    rows = con.execute(
        "SELECT title, title_at_cutoff FROM revisions "
        "WHERE ns = 6 AND title_at_cutoff IS NOT NULL AND title_at_cutoff <> title"
    ).fetchall()
    if not rows:
        sys.exit("no renamed File pages -- has scrape_pages.py phase 3 run?")

    args.out.mkdir(parents=True, exist_ok=True)
    staged = missing = skipped = 0
    missing_examples = []

    for modern, cutoff in rows:
        source = find_source(args.images, disk_name(modern))
        if source is None:
            missing += 1
            if len(missing_examples) < 8:
                missing_examples.append(disk_name(modern))
            continue
        target = args.out / disk_name(cutoff)
        # A name can only be staged once; `resolve_cutoff_titles` guarantees the
        # 2011 titles are unique, so a clash here means something upstream
        # changed and is worth knowing about rather than silently overwriting.
        if target.exists():
            skipped += 1
            continue
        if not args.dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        staged += 1

    print(f"renamed File pages: {len(rows)}")
    print(f"  staged   : {staged} -> {args.out}")
    print(f"  no source: {missing}"
          + (f"  e.g. {', '.join(missing_examples)}" if missing_examples else ""))
    print(f"  already staged: {skipped}")
    if args.dry_run:
        print("(dry run -- nothing copied)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
