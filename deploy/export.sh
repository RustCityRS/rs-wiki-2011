#!/usr/bin/env bash
# Packs the running local wiki into dist/ for a GitHub release: the database and
# the current images with their thumbnails. Old file versions and deleted files
# stay behind, and so do the parser cache and the job queue.
#
# Run from anywhere with the wiki up, then publish (a new tag per snapshot):
#   gh release create <tag> dist/* --repo RustCityRS/rs-wiki-2011 --title <title> --notes <notes>
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1 # Git Bash: pass the container paths below through untouched

pw=${MW_DB_PASSWORD:-wiki_pw}
rm -rf dist
mkdir dist

echo "dumping the database"
docker compose exec -T -e MYSQL_PWD="$pw" db mariadb-dump -uwiki --single-transaction --quick --hex-blob \
	--ignore-table-data=rswiki.objectcache --ignore-table-data=rswiki.job rswiki | gzip -6 >dist/rswiki.sql.gz

echo "packing the images"
docker compose exec -T mediawiki tar -C /var/www/html/images -cf - 0 1 2 3 4 5 6 7 8 9 a b c d e f thumb |
	split -b 1900M -d -a 2 - dist/images.tar.part- # a release asset holds at most 2 GiB

(cd dist && sha256sum -- * >SHA256SUMS)
ls -lh dist
