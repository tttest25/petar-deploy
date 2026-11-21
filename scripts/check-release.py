#!/usr/bin/env python3
"""Validate deploy-001 pins and, optionally, the two downloaded archives."""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import zlib


SHA = r"[0-9a-f]{64}"
VERSION = r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
PATTERNS = {
    "RELEASE_ID": "deploy-001",
    "RELEASE_PLATFORM": "linux/amd64",
    "GO_VERSION": VERSION,
    "GO_GIT_SHA": r"[0-9a-f]{40}",
    "APP_IMAGE": r"ghcr\.io/tttest25/go-pet@sha256:" + SHA,
    "GO_ARCHIVE_URL": r"https://github\.com/tttest25/go-pet/releases/download/" + VERSION + r"/go-pet-" + VERSION + r"\.tar\.gz",
    "GO_ARCHIVE_SHA256": SHA,
    "PLUGIN_VERSION": VERSION,
    "PLUGIN_GIT_SHA": r"[0-9a-f]{40}",
    "PLUGIN_ARCHIVE_URL": r"https://github\.com/tttest25/php-wiki-plugin/releases/download/" + VERSION + r"/petar-" + VERSION + r"\.tar\.gz",
    "PLUGIN_ARCHIVE_SHA256": SHA,
    "POSTGRES_IMAGE": r"pgvector/pgvector@sha256:" + SHA,
    "DOKUWIKI_IMAGE": r"lscr\.io/linuxserver/dokuwiki@sha256:" + SHA,
    "POSTGRES_SCHEMA_VERSION": "000008",
}
MIGRATIONS = (
    "init_extensions", "create_state_machine", "db_state_functions",
    "validate_tool_payload", "action_catalog", "universal_json_input",
    "word_count", "retire_legacy_input",
)
GO_FILES = {
    "postgres/bootstrap.sh": 0o755,
    "postgres/postgresql.conf": 0o644,
    "scripts/spool-runner.sh": 0o755,
    **{
        f"postgres/migrations/{number:06d}_{name}.{direction}.sql": 0o644
        for number, name in enumerate(MIGRATIONS, 1)
        for direction in ("up", "down")
    },
}
PLUGIN_FILES = {
    "petar/" + name: 0o644
    for name in (
        "PetarClient.php", "action.php", "conf/default.php", "conf/metadata.php",
        "lang/en/lang.php", "lang/en/settings.php", "lang/ru/lang.php",
        "lang/ru/settings.php", "plugin.info.txt", "script.js", "style.css", "syntax.php",
    )
}
MAX_BUNDLE = 4 * 1024 * 1024
MANIFEST_HEADER = "# Public release pins only. Host paths and secrets belong in separate files."


def read_fields(path, patterns, header=None):
    pins = {}
    for no, line in enumerate(path.read_bytes().decode("ascii").split("\n"), 1):
        if not line or (no == 1 and line == header):
            continue
        key, _, value = line.partition("=")
        if key not in patterns or key in pins or not re.fullmatch(patterns[key], value):
            raise ValueError(f"invalid, unknown or duplicate field on line {no}")
        pins[key] = value
    if pins.keys() != patterns.keys():
        raise ValueError("missing fields: " + ", ".join(sorted(patterns.keys() - pins.keys())))
    return pins


def read_manifest(path):
    pins = read_fields(path, PATTERNS, MANIFEST_HEADER)
    for component, filename in (("GO", "go-pet"), ("PLUGIN", "petar")):
        version = pins[component + "_VERSION"]
        if not pins[component + "_ARCHIVE_URL"].endswith(f"/{version}/{filename}-{version}.tar.gz"):
            raise ValueError(f"{component} version and archive URL disagree")
    return pins


def read_archive(path, expected_digest, expected):
    # Read and hash the same bytes that are passed to the parser.
    with path.open("rb") as file:
        compressed = file.read(MAX_BUNDLE + 1)
    return read_archive_bytes(path.name, compressed, expected_digest, expected)


def read_archive_bytes(name, compressed, expected_digest, expected):
    if len(compressed) > MAX_BUNDLE or hashlib.sha256(compressed).hexdigest() != expected_digest:
        raise ValueError(f"{name}: SHA-256 mismatch or archive too large")
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    plain = decoder.decompress(compressed, MAX_BUNDLE + 1)
    if len(plain) > MAX_BUNDLE:
        raise ValueError(f"{name}: uncompressed archive too large")
    if not decoder.eof or decoder.unused_data:
        raise ValueError(f"{name}: expected one complete gzip member")
    contents = {}
    next_header = 0
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:") as archive:
        for member in archive:
            if (
                member.name not in expected
                or member.name in contents
                or member.type != tarfile.REGTYPE
                or member.mode != expected[member.name]
                or member.pax_headers
                or member.sparse is not None
                or member.offset != next_header
                or member.offset_data != next_header + tarfile.BLOCKSIZE
            ):
                raise ValueError(f"{name}: unexpected archive member or mode")
            contents[member.name] = archive.extractfile(member).read()
            next_header = member.offset_data + member.size
            next_header += -next_header % tarfile.BLOCKSIZE
    tail = plain[next_header:]
    if len(tail) < 2 * tarfile.BLOCKSIZE or len(tail) % tarfile.BLOCKSIZE or any(tail):
        raise ValueError(f"{name}: unexpected data after archive files")
    if contents.keys() != expected.keys():
        raise ValueError(f"{name}: archive files missing")
    return contents


def check_downloads(pins, download, destination):
    bundles = {}
    for component, expected in (("GO", GO_FILES), ("PLUGIN", PLUGIN_FILES)):
        name = pins[component + "_ARCHIVE_URL"].rsplit("/", 1)[1]
        bundle = read_archive(download / name, pins[component + "_ARCHIVE_SHA256"], expected)
        bundles[component] = (bundle, expected)
        print(f"{name}: SHA-256 and {len(expected)} files verified")
    if destination is not None:
        if (
            not destination.is_dir()
            or destination.is_symlink()
            or destination.stat().st_mode & 0o077
            or any(destination.iterdir())
        ):
            raise ValueError("output must be an existing, empty, private directory")
        for component, (bundle, expected) in bundles.items():
            for name, data in bundle.items():
                path = destination / component.lower() / name
                path.parent.mkdir(parents=True, exist_ok=True)
                for parent in path.relative_to(destination).parents:
                    if parent != Path("."):
                        (destination / parent).chmod(0o755)
                with path.open("xb") as file:
                    file.write(data)
                path.chmod(expected[name])


def read_local(local_path):
    if local_path.stat().st_mode & 0o077:
        raise ValueError("local env file must not be accessible to group or others")
    bundle = r"/opt/go-pet/postgres/deploy-001\.[A-Za-z0-9]+"
    local = read_fields(local_path, {
        "POSTGRES_PASSWORD": r"[A-Za-z0-9._~-]+",
        "POSTGRES_FILES_DIR": bundle + r"/go/postgres",
        "PLUGIN_DIR": bundle + r"/plugin/petar",
    })
    if local["POSTGRES_FILES_DIR"].rsplit("/", 2)[0] != local["PLUGIN_DIR"].rsplit("/", 2)[0]:
        raise ValueError("PostgreSQL files and plugin must come from the same extracted bundle")
    return local


def check_rehearsal(pins, local, model):
    return check_runtime(pins, local, model, "rehearsal", {
        "WIKI_DATA_DIR": "/opt/petar-data/rehearsal/wiki",
        "SPOOL_DIR": "/opt/petar-data/rehearsal/spool",
        "POSTGRES_DATA_DIR": "/opt/go-pet/postgres/deploy/rehearsal/data",
        "POSTGRES_PORT": "5434", "API_PORT": "8082", "WIKI_PORT": "82",
        "WIKI_BIND_IP": "127.0.0.1",
    })


def check_runtime(pins, local, model, profile, layout, dev_context="/opt/go-pet"):
    pg_files, plugin = local["POSTGRES_FILES_DIR"], local["PLUGIN_DIR"]
    wiki, spool, pgdata = (layout[key] for key in ("WIKI_DATA_DIR", "SPOOL_DIR", "POSTGRES_DATA_DIR"))
    project = "petar-" + profile
    expected = {
        "postgres": ("POSTGRES_IMAGE", layout["POSTGRES_PORT"], 5432, [
            (pgdata, "/var/lib/postgresql", False),
            (pg_files + "/bootstrap.sh", "/docker-entrypoint-initdb.d/10-petar.sh", True),
            (pg_files + "/migrations", "/migrations", True),
            (pg_files + "/postgresql.conf", "/etc/postgresql/postgresql.conf", True),
            (spool, "/spool", False),
        ]),
        "app": ("APP_IMAGE", layout["API_PORT"], 8080, [(spool, "/app/spool", False)]),
        "dokuwiki": ("DOKUWIKI_IMAGE", layout["WIKI_PORT"], 80, [
            (wiki, "/config", False),
            (plugin, "/config/dokuwiki/lib/plugins/petar", True),
        ]),
    }
    services, networks = model["services"], model["networks"]
    allowed = {"image", "restart", "environment", "ports", "volumes",
               "entrypoint", "command", "networks"}
    additional = {
        "postgres": {"shm_size", "mem_limit", "healthcheck"},
        "app": {"depends_on"} | ({"build", "pull_policy"} if profile == "dev" else set()),
        "dokuwiki": set(),
    }
    if (
        model["name"] != project
        or model.keys() - {"name", "services", "networks", "x-existing-path"}
        or services.keys() != expected.keys()
        or networks != {"default": {"name": project + "_default", "ipam": {}}}
    ):
        raise ValueError("unexpected project, service set or network")
    for name, (key, published, target, mounts) in expected.items():
        service = services[name]
        ports = service.get("ports", [])
        volumes = service.get("volumes", [])
        actual = [(v["source"], v["target"], v.get("read_only", False)) for v in volumes]
        build = profile == "dev" and name == "app"
        if (
            service["image"] != pins[key]
            or service.keys() - allowed - additional[name]
            or service.get("restart") != "unless-stopped"
            or (build and (service.get("build") != {
                "context": dev_context, "dockerfile": "Dockerfile", "network": "host",
            } or service.get("pull_policy") != "build"))
            or (not build and "build" in service)
            or service.get("entrypoint") is not None
            or service.get("command") != (["postgres", "-c", "config_file=/etc/postgresql/postgresql.conf"]
                                          if name == "postgres" else None)
            or service["networks"] != {"default": {"aliases": ["petar_app"]} if name == "app" else None}
            or len(ports) != 1
            or ports[0].get("host_ip") != (layout["WIKI_BIND_IP"] if name == "dokuwiki" else "127.0.0.1")
            or str(ports[0].get("published")) != str(published)
            or ports[0].get("target") != target
            or ports[0].get("protocol") != "tcp"
            or ports[0].get("mode") != "ingress"
            or ports[0].keys() - {"mode", "host_ip", "target", "published", "protocol"}
            or sorted(actual) != sorted(mounts)
            or any(v.keys() - {"type", "source", "target", "read_only", "bind"}
                   or v.get("type") != "bind" or not isinstance(v.get("bind"), dict)
                   or v["bind"] not in ({}, {"create_host_path": False})
                   for v in volumes)
        ):
            raise ValueError(f"unexpected configuration for {name}")
    password = local["POSTGRES_PASSWORD"]
    if (
        services["postgres"]["environment"] != {
            "POSTGRES_PASSWORD": password, "POSTGRES_USER": "petar_user", "POSTGRES_DB": "petar_db",
        }
        or services["app"]["environment"] != {
            "DB_DSN": f"postgres://petar_user:{password}@postgres:5432/petar_db?sslmode=disable",
            "SPOOL_DIR": "/app/spool", "HTTP_ADDR": "0.0.0.0:8080",
        }
        or services["dokuwiki"]["environment"] != {"PUID": "1000", "PGID": "1000"}
        or services["app"].get("depends_on") != {
            "postgres": {"condition": "service_healthy", "required": True},
        }
        or services["postgres"].get("healthcheck") != {
            "test": ["CMD", "pg_isready", "-h", "127.0.0.1", "-U", "petar_user", "-d", "petar_db"],
            "interval": "5s", "timeout": "5s", "start_period": "1m30s", "retries": 5,
        }
        or services["postgres"].get("mem_limit") != "471859200"
        or services["postgres"].get("shm_size") != "134217728"
    ):
        raise ValueError("unexpected credentials or database target")
    return next(v["source"] for v in services["dokuwiki"]["volumes"] if v["target"] == "/config")


def render_rehearsal(manifest, local_path, context, docker_config):
    # A Compose env parse error may include the offending value, including
    # a password. Validate before launching it and never forward diagnostics.
    local = read_local(local_path)
    env = {key: os.environ[key] for key in ("HOME", "PATH") if key in os.environ}
    result = subprocess.run(
        ["docker", "--config", str(docker_config), "--context", context,
         "compose", "--env-file", str(manifest), "--env-file", "env/rehearsal.env.example",
         "--env-file", str(local_path), "-f", "compose.yaml", "config", "--format", "json"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
    )
    if result.returncode:
        raise ValueError("cannot render rehearsal configuration; check the Docker context and Compose files")
    try:
        model = json.loads(result.stdout)
    except ValueError:
        raise ValueError("invalid Compose render") from None
    return local, model


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--downloads", type=Path)
    parser.add_argument("--extract-to", type=Path)
    parser.add_argument("--check-rehearsal", type=Path, metavar="LOCAL_ENV",
                        help="validate local env, render rehearsal Compose and print the wiki bind source")
    parser.add_argument("--docker-context")
    parser.add_argument("--docker-config", type=Path)
    args = parser.parse_args()
    if args.extract_to and not args.downloads:
        parser.error("--extract-to requires --downloads")
    try:
        pins = read_manifest(args.manifest)
        if args.check_rehearsal:
            if args.downloads or not (args.docker_context and args.docker_config):
                parser.error("--check-rehearsal requires --docker-context and --docker-config, not --downloads")
            local, model = render_rehearsal(
                args.manifest, args.check_rehearsal, args.docker_context, args.docker_config,
            )
            print(check_rehearsal(pins, local, model))
            return
        if args.downloads:
            check_downloads(pins, args.downloads, args.extract_to)
    except (OSError, ValueError, EOFError, KeyError, TypeError, tarfile.TarError, zlib.error) as error:
        sys.exit(f"Release check failed: {error}")
    print(f"{pins['RELEASE_ID']}: manifest checks passed")


if __name__ == "__main__":
    main()
