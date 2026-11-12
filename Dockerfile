# The wiki's web image: mediawiki with this folder's LocalSettings.php and logo
# baked in. They used to be bind mounts from the Windows disk, which Docker
# Desktop serves through its file share; PHP reads LocalSettings.php on every
# request, and when that share stalled Apache hung with it (2026-11-04).
# After editing LocalSettings.php or the logo: docker compose up -d --build
FROM mediawiki:1.43
COPY LocalSettings.php /var/www/html/LocalSettings.php
COPY wiki-logo.png /var/www/html/resources/assets/wiki-logo.png
COPY robots.txt /var/www/html/robots.txt
# At most 8 Apache workers, each a PHP process: the hosted copy has well under a gigabyte.
COPY mpm_prefork.conf /etc/apache2/mods-available/mpm_prefork.conf
