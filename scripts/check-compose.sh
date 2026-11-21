#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

usage() {
  echo "Usage: $0 [--render dev|rehearsal|production]" >&2
  exit 2
}

if (($#)); then
  [[ $# == 2 && $1 == --render ]] || usage
  case "$2" in dev|rehearsal|production) ;; *) usage ;; esac
  environments=("$2")
else
  environments=(dev rehearsal production)
fi

export LC_ALL=C
umask 077
scratch=$(mktemp -d)
trap 'rm -rf -- "$scratch"' EXIT
fixture_password=render-only-DEPLOY02
digest=sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
fixture_app_image="ghcr.io/example/petar-app@$digest"
fixture_pg_image="pgvector/pgvector@$digest"
fixture_wiki_image="lscr.io/linuxserver/dokuwiki@$digest"
fixture_pg_dir=/opt/go-pet/postgres/releases/render-only
fixture_plugin_dir=/opt/petar-releases/render-only/plugin
projects='[]'

for env_name in "${environments[@]}"; do
  env_file="env/$env_name.env.example"
  case "$env_name" in
    dev)
      wiki=81 api=8081 pg=5433 wiki_ip=127.0.0.1
      spool=/opt/petar-data/dev/spool
      app_image=petar-app:dev
      pg_dir=/opt/go-pet/postgres
      plugin_dir=/opt/php-wiki-plugin
      ;;
    rehearsal)
      wiki=82 api=8082 pg=5434 wiki_ip=127.0.0.1
      spool=/opt/petar-data/rehearsal/spool
      ;;
    production)
      wiki=80 api=8080 pg=5432 wiki_ip=0.0.0.0
      spool=/opt/go-pet/spool
      ;;
  esac

  compose=(docker compose --env-file "$env_file" -f compose.yaml)
  fixture_vars=(POSTGRES_IMAGE="$fixture_pg_image" DOKUWIKI_IMAGE="$fixture_wiki_image")
  if [[ $env_name == dev ]]; then
    compose+=(-f compose.dev.yaml)
  else
    app_image=$fixture_app_image pg_dir=$fixture_pg_dir plugin_dir=$fixture_plugin_dir
    fixture_vars+=(APP_IMAGE="$app_image" POSTGRES_FILES_DIR="$pg_dir" PLUGIN_DIR="$plugin_dir")
  fi

  # An explicit env file with a clean process environment: no inherited Compose
  # overrides, credentials or accidental fallback to the project's .env.
  clean_env=(env -i PATH="$PATH" HOME="$scratch" LC_ALL=C)
  output="$scratch/$env_name.json"
  "${clean_env[@]}" "${fixture_vars[@]}" POSTGRES_PASSWORD="$fixture_password" \
    "${compose[@]}" config --format json >"$output"
  interpolation=$("${clean_env[@]}" "${fixture_vars[@]}" POSTGRES_PASSWORD="$fixture_password" \
    "${compose[@]}" config --environment)
  runner_spool=$(sed -n 's/^SPOOL_DIR=//p' <<<"$interpolation")
  runner_api=$(sed -n 's/^API_BASE_URL=//p' <<<"$interpolation")
  unset interpolation
  [[ $runner_spool == "$spool" && $runner_api == "http://127.0.0.1:$api/api/v1" ]] || {
    echo "$env_name: wrong runner spool or callback" >&2
    exit 1
  }

  if ! jq -e --arg e "$env_name" --arg wiki_ip "$wiki_ip" \
    --arg wiki "$wiki" --arg api "$api" --arg pg "$pg" --arg spool "$spool" \
    --arg app_image "$app_image" --arg pg_image "$fixture_pg_image" \
    --arg wiki_image "$fixture_wiki_image" --arg pg_dir "$pg_dir" \
    --arg plugin "$plugin_dir" --arg password "$fixture_password" '
    def port($ip; $published; $target):
      (.ports | length) == 1 and
      (.ports[0] | .host_ip == $ip and (.published | tostring) == $published and
        .target == $target and .protocol == "tcp");
    def bind($source; $target; $ro):
      {type: "bind", source: $source, target: $target, read_only: $ro};
    def mounts($expected):
      [.volumes[] | {type, source, target, read_only: (.read_only // false)}] ==
        $expected and all(.volumes[];
          (.bind | type) == "object" and (.bind.create_host_path // false) == false);
    .name == ("petar-" + $e) and
    (.networks | keys) == ["default"] and
    .networks.default.name == ("petar-" + $e + "_default") and
    (.networks.default.external // false) == false and
    (.services | keys) == ["app", "dokuwiki", "postgres"] and
    all(.services[]; (has("container_name") or has("network_mode") or has("extends")) | not) and
    all(.services[]; (.networks | keys) == ["default"]) and
    (.services.app |
      port("127.0.0.1"; $api; 8080) and .image == $app_image and
      (if $e == "dev" then .build.context == "/opt/go-pet" and .build.network == "host"
       else has("build") | not end) and
      .networks.default.aliases == ["petar_app"] and
      .depends_on.postgres.condition == "service_healthy" and
      .environment.SPOOL_DIR == "/app/spool" and .environment.HTTP_ADDR == "0.0.0.0:8080" and
      .environment.DB_DSN == ("postgres://petar_user:" + $password + "@postgres:5432/petar_db?sslmode=disable") and
      mounts([bind($spool; "/app/spool"; false)])) and
    (.services.postgres |
      port("127.0.0.1"; $pg; 5432) and .image == $pg_image and (has("build") | not) and
      .environment.POSTGRES_PASSWORD == $password and
      (.healthcheck.disable // false) == false and
      .healthcheck.test == ["CMD", "pg_isready", "-h", "127.0.0.1", "-U", "petar_user", "-d", "petar_db"] and
      .healthcheck.start_period == "1m30s" and
      mounts([
        bind("/opt/go-pet/postgres/deploy/" + $e + "/data"; "/var/lib/postgresql"; false),
        bind($pg_dir + "/bootstrap.sh"; "/docker-entrypoint-initdb.d/10-petar.sh"; true),
        bind($pg_dir + "/migrations"; "/migrations"; true),
        bind($pg_dir + "/postgresql.conf"; "/etc/postgresql/postgresql.conf"; true),
        bind($spool; "/spool"; false)
      ])) and
    (.services.dokuwiki |
      port($wiki_ip; $wiki; 80) and .image == $wiki_image and (has("build") | not) and
      ((.extra_hosts // {}) | length) == 0 and
      mounts([
        bind("/opt/petar-data/" + $e + "/wiki"; "/config"; false),
        bind($plugin; "/config/dokuwiki/lib/plugins/petar"; true)
      ]))
  ' "$output" >/dev/null; then
    echo "$env_name: Compose model does not match the isolation contract" >&2
    exit 1
  fi

  if [[ $env_name != dev ]]; then
    release_vars=("${fixture_vars[@]}" POSTGRES_PASSWORD="$fixture_password")
    for required in "${release_vars[@]}"; do
      remaining=()
      for setting in "${release_vars[@]}"; do
        if [[ $setting != "$required" ]]; then
          remaining+=("$setting")
        fi
      done
      name=${required%%=*}
      if "${clean_env[@]}" "${remaining[@]}" "${compose[@]}" config --quiet >"$scratch/error" 2>&1 ||
         ! grep -Fq "required variable $name is missing" "$scratch/error"; then
        echo "$env_name: expected config to fail because $name is missing" >&2
        exit 1
      fi
    done
  fi

  projects=$(jq -c --argjson state "$projects" '
    $state + [{
      name, network: .networks.default.name,
      ports: [.services[].ports[].published],
      paths: [.services[].volumes[] | select(.read_only != true) | .source] | unique
    }] ' "$output")
  if (($#)); then
    cat -- "$output"
  else
    echo "$env_name: Compose isolation checks passed"
  fi
done

jq -e '
  def overlaps($a; $b):
    $a == $b or ($a | startswith($b + "/")) or ($b | startswith($a + "/"));
  . as $projects |
  (map(.name) | unique | length) == length and
  (map(.network) | unique | length) == length and
  ([.[].ports[]] | (unique | length) == length) and
  all($projects[] as $a | $projects[] as $b |
    select($a.name != $b.name) | $a.paths[] as $x | $b.paths[] as $y |
    overlaps($x; $y); not)
' <<<"$projects" >/dev/null || {
  echo 'Compose projects share a network, published port or writable path' >&2
  exit 1
}
