# Runs only in a networkless container with a verified empty /config.
set -eu
test -z "$(ls -A /config)"
# Lock the installer out before the production port can ever be published.
sample=/defaults/nginx/site-confs/default.conf.sample
test "$(grep -c '^server {$' "$sample")" -eq 1
mkdir -p /config/nginx/site-confs
sed '/^server {$/a\    location ~* ^/install\\.php(?:/|$) { return 404; }' \
    "$sample" > /config/nginx/site-confs/default.conf
grep -Fq 'location ~* ^/install\.php(?:/|$) { return 404; }' \
    /config/nginx/site-confs/default.conf
mkdir -p /config/dokuwiki/lib/plugins /config/dokuwiki/conf /config/dokuwiki/data/pages
cd /config/dokuwiki
cp -a /app/www/public/lib/plugins/. lib/plugins/
cp -a /app/www/public/conf/. conf/
cp -a /app/www/public/data/pages/. data/pages/
test -d lib/plugins/authplain
hash=$(php -r 'echo password_hash(stream_get_contents(STDIN), PASSWORD_BCRYPT);')
umask 077
cat > conf/local.php <<'PHP'
<?php if (!defined('DOKU_INC')) die();
$conf['lang'] = 'en';
$conf['useacl'] = 1;
$conf['authtype'] = 'authplain';
$conf['disableactions'] = 'register';
$conf['plugin']['petar']['petar_url'] = 'http://app:8080';
$conf['savedir'] = '/app/www/public/data';
PHP
printf 'petar_check:%s:Petar Check:petar@example.invalid:ai\n' "$hash" > conf/users.auth.php
printf '*\t@ALL\t0\n*\t@ai\t1\n' > conf/acl.auth.php
printf '====== Deploy check ======\n\n{{petar}}\n\n{{petar>jobs}}\n' > data/pages/petar_check.txt
chown 1000:1000 conf/local.php conf/users.auth.php conf/acl.auth.php data/pages/petar_check.txt
