#!/usr/bin/env bash
# Sets up a hosted copy from a release: downloads its database dump and images,
# loads them, and starts the wiki. Re-running it replaces the database and images
# with the release's. Needs Docker with compose, curl and sha256sum.
#
#   ./deploy/install.sh http://203.0.113.5:8090 [release tag]
#
# The first run writes .env (the address, generated passwords, the caps in
# compose.host.yml). Change the address later in .env, then: docker compose up -d
set -euo pipefail
cd "$(dirname "$0")/.."

server=${1:?usage: deploy/install.sh <public address, e.g. http://203.0.113.5:8090> [release tag]}
repo=RustCityRS/rs-wiki-2011
api=https://api.github.com/repos/$repo/releases
tag=${2:-$(curl -fsSL "$api/latest" | grep -o '"tag_name": *"[^"]*"' | cut -d'"' -f4)}

if [ ! -f .env ]; then
	(
		umask 077
		cat >.env <<EOF
COMPOSE_FILE=docker-compose.yml:compose.host.yml
WIKI_PORT=8090
MW_SERVER=$server
MW_DB_PASSWORD=$(openssl rand -hex 16)
MW_DB_ROOT_PASSWORD=$(openssl rand -hex 16)
MW_SECRET_KEY=$(openssl rand -hex 32)
MW_UPGRADE_KEY=$(openssl rand -hex 8)
EOF
	)
fi
set -a
. ./.env
set +a

echo "downloading release $tag"
mkdir -p dist
for url in $(curl -fsSL "$api/tags/$tag" | grep -o '"browser_download_url": *"[^"]*"' | cut -d'"' -f4); do
	file=dist/${url##*/}
	[ -f "$file" ] || { curl -fL --retry 3 -o "$file.part" "$url" && mv "$file.part" "$file"; }
done
(cd dist && sha256sum -c SHA256SUMS)

docker compose build mediawiki
docker compose up -d --wait db

echo "loading the database"
gunzip -c dist/rswiki.sql.gz | docker compose exec -T -e MYSQL_PWD="$MW_DB_PASSWORD" db mariadb -uwiki rswiki

echo "unpacking the images"
cat dist/images.tar.part-* | docker compose run --rm --no-deps -T --entrypoint sh mediawiki -c \
	'cd /var/www/html/images && rm -rf [0-9a-f] thumb cache && tar -xf - && chown -R www-data:www-data .'

docker compose up -d --wait
docker compose exec -T mediawiki php maintenance/run.php update --quick >/dev/null
rm -rf dist

curl -fsS "$MW_SERVER/api.php?action=query&meta=siteinfo&siprop=statistics&format=json"
echo
echo "up at $MW_SERVER"
