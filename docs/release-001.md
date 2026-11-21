# Подготовка `deploy-001`

Эти команды для **пустого репетиционного окружения**, не для production и не для обновления. Нужны Bash, Python 3.8+, curl, jq и Docker Compose v2 на `linux/amd64`. Команды выполнять из корня `petar-deploy`. Go, PHP checkout и сборка образов не нужны.

## Проверить артефакты

`releases/001.env` содержит только публичные закреплённые значения. Не выполнять его как shell-скрипт. Перед работой проверьте, что Docker context указывает на локальный демон, который видит хостовые пути. Команды ниже явно используют этот context и не наследуют переменные Petar/Compose.

```bash
set -euo pipefail
umask 077
manifest=releases/001.env
context=$(docker context show)
docker_config=${DOCKER_CONFIG:-$HOME/.docker}
d=(env -i PATH="$PATH" HOME="$HOME" docker --config "$docker_config" --context "$context")
python3 scripts/check-release.py "$manifest"
pin() { sed -n "s/^$1=//p" "$manifest"; }

download=$(mktemp -d)
bundle=$(mktemp -d /opt/go-pet/postgres/deploy-001.XXXXXXXX)
for key in GO_ARCHIVE_URL PLUGIN_ARCHIVE_URL; do
  url=$(pin "$key")
  curl -q --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    "$url" --output "$download/${url##*/}"
done
python3 scripts/check-release.py "$manifest" --downloads "$download" --extract-to "$bundle"

for key in APP_IMAGE POSTGRES_IMAGE DOKUWIKI_IMAGE; do
  image=$(pin "$key")
  "${d[@]}" pull --platform "$(pin RELEASE_PLATFORM)" "$image"
  test "$("${d[@]}" image inspect --format '{{.Os}}/{{.Architecture}}' "$image")" = "$(pin RELEASE_PLATFORM)"
done
test "$("${d[@]}" image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
  "$(pin APP_IMAGE)")" = "$(pin GO_GIT_SHA)"
```

Родитель `/opt/go-pet/postgres/` должен существовать и быть доступен на запись оператору. Скрипт ничего не скачивает и не запускает; перед извлечением он проверяет обе SHA-256, точный состав, тип и режим каждого файла. При ошибке целевой каталог не использовать. `go/postgres` содержит bootstrap, миграции и конфиг БД; `plugin/petar` — только файлы плагина. Архив runner — это часть того же Go-комплекта, но runner здесь не запускается.

## Подготовить пустую wiki

В `env/rehearsal.env.example` указаны отдельные rehearsal-пути. До запуска подготовьте
новые пустые каталоги `POSTGRES_DATA_DIR`, `WIKI_DATA_DIR`, `SPOOL_DIR`. Родитель,
монтируемый в `/var/lib/postgresql`, должен быть проходим для контейнерного
пользователя PostgreSQL. На проверенном образе с владельцем каталога со стороны
хоста подходит `0755` для этого родителя; вложенный каталог `18/docker` создаёт и
защищает сам PostgreSQL. Не ослабляйте права существующей БД.
Каталог spool должен быть доступен на запись UID `1000`, под которым работает
закреплённый Go API. Это не разрешение запускать runner.

В отдельном локальном env-файле с правами `0600` задайте только
`POSTGRES_PASSWORD` (случайный, URL-safe), `POSTGRES_FILES_DIR=$bundle/go/postgres`
и `PLUGIN_DIR=$bundle/plugin/petar`, **подставив абсолютные пути**, а не `$bundle`
буквально. Не помещайте секреты в `releases/001.env`. Пример использования:

```bash
local_env=/absolute/path/to/local-rehearsal.env
compose=("${d[@]}" compose --env-file "$manifest" --env-file env/rehearsal.env.example \
  --env-file "$local_env" -f compose.yaml)
wiki_dir=$(python3 scripts/check-release.py "$manifest" --check-rehearsal "$local_env" \
  --docker-context "$context" --docker-config "$docker_config")
```

Скрипт проверяет локальный env до передачи в Compose, затем сравнивает реальные
образы, проект, сеть, loopback-порты и bind mounts с контрактом репетиции.
Он выдаёт только проверенный путь wiki, без сырых диагностик и JSON с секретами.
При отказе не запускайте `up` и не копируйте файлы в wiki. Если меняли env или
Compose-файл, повторите проверку.

До первой загрузки wiki скопируйте штатные плагины из того же закреплённого
образа. Без этого вложенный mount `petar` может заставить LSIO пропустить
заселение каталога `lib/plugins`. Проверка пустоты `wiki_dir` выполняется перед
записью. Не применяйте к существующей wiki.

```bash
"${d[@]}" run --rm --network none --mount "type=bind,src=$wiki_dir,dst=/config" \
  --entrypoint /bin/sh "$(pin DOKUWIKI_IMAGE)" -ec '
    test -z "$(ls -A /config)"
    mkdir -p /config/dokuwiki/lib/plugins
    cp -a /app/www/public/lib/plugins/. /config/dokuwiki/lib/plugins/
  '
"${compose[@]}" up --detach --no-build --pull never --wait
```

Дождитесь ответа HTTP самой wiki: состояние контейнера в Compose не заменяет
проверку HTTP. Не открывайте необработанный `install.php` на публичном адресе.
В этой репетиции настройте `authplain` напрямую, не используя веб-установщик.
Откройте `bash` от root внутри **контейнера `dokuwiki` этого проекта** через
`"${compose[@]}" exec -u 0 dokuwiki bash` и выполните:

```bash
set -euo pipefail
cd /config/dokuwiki
test -d lib/plugins/authplain
for file in conf/local.php conf/users.auth.php conf/acl.auth.php data/pages/petar_check.txt; do
  test ! -e "$file" && test ! -L "$file" || exit 1
done
read -rsp 'Пароль отдельного пользователя petar_check: ' password
printf '\n'
test -n "$password"
hash=$(printf '%s' "$password" | php -r 'echo password_hash(stream_get_contents(STDIN), PASSWORD_BCRYPT);')
unset password
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
chown abc:abc conf/local.php conf/users.auth.php conf/acl.auth.php data/pages/petar_check.txt
```

В репетиции `http://127.0.0.1:82/doku.php?id=petar_check` не показывает форму
анонимному посетителю; после входа `petar_check` видит форму и журнал. Внутри сети
`http://app:8080/readyz` возвращает `ready`, а `/api/v1/actions` — четыре действия.
В логах PostgreSQL при первом старте — восемь `Applying /migrations/…`, последняя
`000008_retire_legacy_input.up.sql`. При рестарте той же репетиции сравните
контрольные суммы `conf/local.php`, `conf/users.auth.php`, `conf/acl.auth.php`: они
не должны измениться. PostgreSQL сообщает `Skipping initialization`; новых
строк `Applying` быть не должно.

Остановите только тестовый проект командой `"${compose[@]}" down`, без удаления
каталогов данных. Не включайте cron, не отправляйте задания и не переносите
репетиционного пользователя в production.

Регрессии манифеста, архивов и рендера проверяются командой
`python3 scripts/test-check-release.py`; она не запускает контейнеры.
