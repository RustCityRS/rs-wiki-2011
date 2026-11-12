# RuneScape Wiki (2011)

A read-only copy of the RuneScape Wiki as it stood on **23 May 2011**: 18,590 articles and
28,340 images, each page at its last revision before that date and under the title it had then.
It is the reference for building a May 2011 (revision 647) server: what an item did, what a
quest said, where a monster stood, before later updates changed them.

MediaWiki 1.43 and MariaDB 10.11 in Docker.

## Credits and licence

The text and images are the [RuneScape Wiki](https://runescape.wiki)'s, under
[CC BY-NC-SA 3.0](https://creativecommons.org/licenses/by-nc-sa/3.0/), and the footer of every
page says so. RuneScape and its images belong to Jagex. This copy is non-commercial and
unofficial, and the live wiki is the place for anything current.

## Run it locally

```sh
docker compose up -d   # http://localhost:8090, API at /api.php
```

An empty wiki starts there. To fill it, either load a release (as the hosted copy does, below),
or rebuild it from the scrape (at the end).

## Host it

On a Linux server with Docker, from a clone of this repo:

```sh
./deploy/install.sh http://<address>:8090
```

It downloads the newest release's database and images, checks them against `SHA256SUMS`, loads
them, and starts the wiki. The first run writes `.env`: the public address, generated passwords,
and `compose.host.yml`'s caps (0.75 CPU and about 1.4 GB for both containers), which suit a small
server shared with another site. Running it again replaces the database and images with the
release's.

The hosted copy is meant for reading:

- Nobody can sign in or create an account (it serves plain HTTP). Any change is a maintenance
  script run on the server.
- Anonymous page views are served from saved HTML (`$wgUseFileCache`).
- `robots.txt` and `noindex` keep search engines out, so they send people to the live wiki.
- Older versions of files and deleted files aren't in the release: a file page's history lists
  them, but their links don't load.

Changes to this repo reach the server with:

```sh
git pull && docker compose up -d --build
```

## Make a new release

With the local wiki holding what you want to publish:

```sh
./deploy/export.sh
gh release create <tag> dist/* --repo RustCityRS/rs-wiki-2011 --title "<title>" --notes "<notes>"
```

`dist/` gets the database (`rswiki.sql.gz`, without the parser cache and job queue), the current
images with their thumbnails split into parts under GitHub's 2 GiB limit, and `SHA256SUMS`.

## Rebuild it from the scrape

`scrape_pages.py` and `scrape_images.py` pull the snapshot from runescape.wiki's API into `data/`
(`pages.xml`, `images/`). Set `WIKI_SCRAPER_CONTACT` to an email or URL first: the wiki asks API
clients to say who they are. `scrape_pages.py`'s docstring explains why it walks the move log:
without that, 2011 pages are filed under their 2026 titles and internal links break.

`restage_images.py` copies each image renamed since 2011 to its 2011 name, which is the name the
2011 pages link. `import.ps1` loads `data/` into an empty local wiki (and works around MediaWiki
1.43's missing search index on MariaDB). `reimport.ps1` re-imports a filled wiki after the titles
change, deleting the renamed pages first so none end up there twice.

## Query it from Claude

`mcp_server.py` is an MCP server over the wiki's API: search, a page's wikitext or HTML, a
category's members, and an item's or NPC's infobox. It uses
`http://localhost:8090/api.php`, or the address in `RS_WIKI_API`.

## Keep it up on Windows

`watchdog.ps1` brings the local wiki back when it stops answering. `register-watchdog.ps1` runs it
at logon and every 5 minutes, and `start-wiki.ps1` starts it and waits for it.
