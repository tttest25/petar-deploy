"""DEPLOY-06 operator commands and fixed host contracts."""

from contextlib import contextmanager
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import zlib


ROOT = Path(__file__).resolve().parents[1]
DATA = Path("/opt/petar-data")
GO = Path("/opt/go-pet")
PLUGIN = Path("/opt/php-wiki-plugin")
TOOLS = Path("/usr/bin")
MOUNTINFO = Path("/proc/self/mountinfo")
SOCKET = "unix:///var/run/docker.sock"
SYS_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
PROFILES = ("dev", "rehearsal", "production")
PORTS = dict(zip(PROFILES, ((81, 8081, 5433), (82, 8082, 5434), (80, 8080, 5432))))
MIN_BYTES = 2 * 1024**3
LOCK_WAIT = 120
UID = 1000

spec = importlib.util.spec_from_file_location("release_checker", ROOT / "scripts/check-release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class Refusal(Exception):
    pass


def require(value, message):
    if not value:
        raise Refusal(message)


def say(message):
    print(message, flush=True)


def canonical(path):
    require(path.is_absolute() and path.resolve() == path, "path isolation: symlink or noncanonical path")


def trusted_dir(path, create_target=False):
    """Check even missing targets without writes. Never change existing modes."""
    canonical(path)
    check = path.parent if create_target else path
    for part in (Path("/"), *reversed(check.parents), check):
        st = part.stat()
        require(stat.S_ISDIR(st.st_mode) and st.st_uid in (0, UID) and
                (not st.st_mode & 0o022 or
                 (st.st_uid == 0 and st.st_mode & stat.S_ISVTX)),
                "path permissions: unsafe directory or ancestor")
    if create_target:
        require(os.access(check, os.W_OK | os.X_OK), "path permissions: target parent is not writable")


def private_file(path):
    st = path.lstat()
    require(stat.S_ISREG(st.st_mode) and st.st_uid == UID and st.st_nlink == 1
            and stat.S_IMODE(st.st_mode) == 0o600, "operator files must be owned regular files with mode 0600")


def overlap(a, b):
    return a == b or a in b.parents or b in a.parents


def aliases(path):
    """Track directory inodes as well as names (including bind aliases)."""
    return {(s.st_dev, s.st_ino): p for p in (*path.parents, path)
            if (s := p.stat() if p.exists() else None) is not None}


def intersects(a, b):
    if overlap(a, b):
        return True
    aa, bb = aliases(a), aliases(b)
    for key in aa.keys() & bb.keys():
        tail_a, tail_b = a.relative_to(aa[key]), b.relative_to(bb[key])
        if overlap(tail_a, tail_b):
            return True
    return False


def mounts(source=None):
    def unescape(field):
        return Path(os.fsdecode(re.sub(rb"\\([0-7]{3})", lambda m: bytes([int(m[1], 8)]), field)))

    for line in (source or MOUNTINFO).read_bytes().splitlines():
        pre, post = line.split(b" - ", 1)
        fields = pre.split()
        yield unescape(fields[3]), unescape(fields[4]), post.split()[0], fields[2]


def no_mount_alias(path, table, *, local_filesystems=None):
    # A mount at/below a selected tree can hide a foreign descendant.
    for root, point, filesystem, device in table:
        require(not (point == path or path in point.parents or
                     (point in path.parents and point != Path("/") and
                      (root != Path("/") or any(p != point and d == device and r == root
                                                for r, p, _, d in table)))),
                "mount isolation: filesystem mount or bind alias in selected path")
        if local_filesystems is not None and point in path.parents:
            require(filesystem in local_filesystems and root == Path("/"),
                    "disposable VM requires guest-local fixture filesystems")


def backing_paths(path, table):
    """Find the filesystem-relative paths visible at a mount and nested mounts."""
    ancestors = [(root, point, device) for root, point, _, device in table
                 if point == path or point in path.parents]
    require(bool(ancestors), "mount isolation: no backing filesystem for selected path")
    depth = max(len(point.parts) for _, point, _ in ancestors)
    visible = [(device, root / path.relative_to(point)) for root, point, device in ancestors
               if len(point.parts) == depth]
    return visible + [(device, root) for root, point, _, device in table if path in point.parents]


def backing_overlap(source, target, table):
    return any(src_dev == dst_dev and overlap(src_path, dst_path)
               for src_dev, src_path in backing_paths(source, table)
               for dst_dev, dst_path in backing_paths(target, table))


def layout(profile):
    wiki, api, pg = PORTS[profile]
    folder = DATA / profile
    return {
        "COMPOSE_PROJECT_NAME": "petar-" + profile,
        "WIKI_BIND_IP": "0.0.0.0" if profile == "production" else "127.0.0.1",
        "WIKI_PORT": str(wiki), "API_PORT": str(api), "POSTGRES_PORT": str(pg),
        "POSTGRES_DATA_DIR": str(GO / "postgres/deploy" / profile / "data"),
        "WIKI_DATA_DIR": str(folder / "wiki"),
        "SPOOL_DIR": str(GO / "spool" if profile == "production" else folder / "spool"),
        "API_BASE_URL": f"http://127.0.0.1:{api}/api/v1",
    }


def targets(profile):
    return tuple(Path(layout(profile)[key]) for key in ("POSTGRES_DATA_DIR", "WIKI_DATA_DIR", "SPOOL_DIR"))


class Operator:
    def __init__(self, profile):
        require(os.getuid() == os.geteuid() == UID and os.getgid() == os.getegid() == UID,
                "operator must be non-root UID/GID 1000")
        self.profile = profile
        self.dev = profile == "dev"
        self.settings = layout(profile)
        self.project = self.settings["COMPOSE_PROJECT_NAME"]
        self.pgdata, self.wiki, self.spool = targets(profile)
        self.state = DATA / profile / "operator"
        self.bundle = self.state / "deploy-001"
        self.dev_build = self.state / "dev-build"
        self.runtime_file = self.state / "runtime.compose.json"
        self.home = Path(pwd.getpwuid(UID).pw_dir)
        self.env = {"HOME": str(self.home), "PATH": SYS_PATH, "LC_ALL": "C"}
        self.runner_env = dict(self.env, PATH=f"{SYS_PATH}:{self.home}/.local/bin:{self.home}/go/bin",
                               SPOOL_DIR=str(self.spool), API_BASE_URL=self.settings["API_BASE_URL"])
        self.docker = [str(TOOLS / "docker"), "--config", str(self.home / ".docker"), "--context", "default"]
        self.files = [str(ROOT / "compose.yaml")]
        if self.dev:
            self.files.append(str(ROOT / "compose.dev.yaml"))
        self.stage = "operator configuration"

    def config(self):
        trusted_dir(self.state)
        no_mount_alias(self.state, tuple(mounts()))
        st = self.state.stat()
        require(st.st_uid == UID and stat.S_IMODE(st.st_mode) == 0o700,
                "operator directory must be owned by the operator with mode 0700")
        for name in ("control.lock", "tick.lock", "runner.log"):
            private_file(self.state / name)
        for name in ("maintenance", "ready", "pause", "inflight"):
            try:
                private_file(self.state / name)
            except FileNotFoundError:
                pass
        secrets = self.state / "secrets.env"
        private_file(secrets)
        try:
            self.local = release.read_fields(secrets, {
                "POSTGRES_PASSWORD": r"[A-Za-z0-9._~-]{24,72}",
                "WIKI_CHECK_PASSWORD": r"[A-Za-z0-9._~-]{24,72}",
            })
        except (ValueError, UnicodeError):
            raise Refusal("invalid local secrets file (expected exactly two URL-safe passwords)") from None
        require(self.local["POSTGRES_PASSWORD"] != self.local["WIKI_CHECK_PASSWORD"],
                "use different database and wiki passwords")
        self.pins = release.read_manifest(ROOT / "releases/001.env")
        if self.dev:
            self.pins["APP_IMAGE"] = "petar-app:dev"
        self.local.update(
            POSTGRES_FILES_DIR=str(GO / "postgres" if self.dev else self.bundle / "go/postgres"),
            PLUGIN_DIR=str(PLUGIN if self.dev else self.bundle / "plugin/petar"),
        )
        self.compose_env = dict(self.env, **self.settings, **{
            k: self.pins[k] for k in ("APP_IMAGE", "POSTGRES_IMAGE", "DOKUWIKI_IMAGE")
        }, **{k: self.local[k] for k in ("POSTGRES_PASSWORD", "POSTGRES_FILES_DIR", "PLUGIN_DIR")})
        if self.dev:
            self.compose_env["DEV_BUILD_CONTEXT"] = str(self.dev_build)

    def cmd(self, args, *, data=None, timeout=30, env=None, allow_fail=False, fds=()):
        result = subprocess.run(
            list(map(str, args)), cwd=ROOT, env=self.env if env is None else env,
            input=data, stdin=subprocess.DEVNULL if data is None else None,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout, pass_fds=fds,
        )
        require(allow_fail or result.returncode == 0, "command failed; no raw diagnostics printed")
        return result

    def d(self, *args, **opts):
        return self.cmd([*self.docker, *args], **opts)

    def compose(self, *args, frozen=False, **opts):
        paths = [str(self.runtime_file)] if frozen else self.files
        files = [item for path in paths for item in ("-f", path)]
        return self.d("compose", "--env-file", "/dev/null", "--project-directory", ROOT,
                      "-p", self.project, *files, *args, env=self.compose_env, **opts)

    def check_context(self, *, details=False):
        context = json.loads(self.d("context", "inspect", "default").stdout)
        require(len(context) == 1 and context[0]["Endpoints"]["docker"]["Host"] == SOCKET,
                "Docker context must be the fixed local Unix socket")
        info = json.loads(self.d("info", "--format", "{{json .}}").stdout)
        require(info["OSType"] == "linux" and info["Architecture"] == "x86_64"
                and info.get("OperatingSystem") != "Docker Desktop"
                and info.get("KernelVersion") == platform.release() and info.get("Name") == platform.node()
                and not any("rootless" in opt or "userns" in opt for opt in info["SecurityOptions"] or []),
                "Docker must be local linux/amd64 without UID remapping")
        return info if details else Path(info["DockerRootDir"])

    def check_paths(self, clean):
        mine = (*targets(self.profile), self.state)
        table = tuple(mounts())
        for path in mine:
            canonical(path)
            no_mount_alias(path, table)
            for other in PROFILES:
                if other != self.profile:
                    for sibling in (*targets(other), DATA / other / "operator"):
                        require(not intersects(path, sibling.resolve()),
                                "paths overlap another environment")
        for i, path in enumerate(mine):
            require(not any(intersects(path, other) for other in mine[:i]), "local targets overlap")
        for path in targets(self.profile):
            trusted_dir(path, create_target=not path.exists())
            if path.exists():
                st = path.stat()
                require(st.st_uid == UID and st.st_gid == UID and stat.S_IMODE(st.st_mode) == 0o755,
                        "existing target must be owned by UID/GID 1000 and have mode 0755")
                if clean:
                    require(not any(path.iterdir()), "clean install requires empty PGDATA, wiki and spool")
            for parent in path.parents:
                require(parent.stat().st_mode & 0o001,
                        "non-traversable bind parent")
            require(shutil.disk_usage(path if path.exists() else path.parent).free >= MIN_BYTES,
                    "less than 2 GiB free on a target filesystem")
        if self.dev:
            require(os.access(TOOLS / "git", os.X_OK), "dev requires Git")
            migration_dir = GO / "postgres/migrations"
            sources = (GO, PLUGIN, GO / "postgres", migration_dir, GO / "cmd", GO / "internal")
            for source in sources:
                trusted_dir(source)
            for source in (PLUGIN, migration_dir, GO / "cmd", GO / "internal", self.dev_build):
                no_mount_alias(source, table)
            canonical(self.dev_build)
            require((GO / ".git").is_dir(), "dev Go source must be a checkout")
            require(migration_dir.stat().st_mode & 0o005 == 0o005,
                    "PostgreSQL cannot traverse/read dev migrations")
            migrations = sorted(migration_dir.glob("*.up.sql"))
            require(bool(migrations), "missing dev PostgreSQL migrations")
            for source in (GO / "Dockerfile", GO / "go.mod", GO / "go.sum", GO / "postgres/bootstrap.sh",
                           GO / "postgres/postgresql.conf", GO / "scripts/spool-runner.sh",
                           PLUGIN / "plugin.info.txt", *migrations):
                canonical(source)
                no_mount_alias(source, table)
                require(source.is_file() and os.access(source, os.R_OK), "missing dev source")
                if source.parent in (GO / "postgres", migration_dir):
                    mode = 0o005 if source.name == "bootstrap.sh" else 0o004
                    require(source.stat().st_mode & mode == mode, "PostgreSQL cannot read/execute dev source")
        pg = Path(self.local["POSTGRES_FILES_DIR"])
        for path in (pg / "bootstrap.sh", pg / "migrations", pg / "postgresql.conf",
                     Path(self.local["PLUGIN_DIR"])):
            canonical(path)
            require(not any(intersects(path, p) for p in targets(self.profile)), "code and data overlap")
        require(shutil.disk_usage(self.state).free >= MIN_BYTES, "less than 2 GiB free on operator filesystem")

    def check_tools(self, report):
        for cmd in ("bash", "jq", "python3", "curl", "timeout", "flock", "mkdir",
                    "mktemp", "mv", "rm", "sleep"):
            require(shutil.which(cmd, path=self.runner_env["PATH"]), f"runner requires {cmd}")
        require(self.cmd(["timeout", "--version"], env=self.runner_env).stdout.startswith(b"timeout (GNU coreutils)"),
                "runner requires GNU timeout")
        if report:
            for cmd in ("fabric", "yt-dlp"):
                say(f"{self.profile}: {cmd}: " + ("available" if shutil.which(cmd, path=self.runner_env["PATH"])
                                                  else "missing (required only by matching jobs)"))

    def render(self, *, frozen=False):
        model = json.loads(self.compose("config", "--format", "json", frozen=frozen).stdout)
        release.check_runtime(self.pins, self.local, model, self.profile, self.settings, str(self.dev_build))
        return model

    def freeze(self, model):
        # Serialize only the validated model, preserving the non-default bind
        # behavior suppressed as an empty object by Compose's JSON renderer.
        model.pop("x-existing-path", None)
        for service in model["services"].values():
            for volume in service["volumes"]:
                volume["bind"] = {"create_host_path": False}
        if self.marked(self.runtime_file.name):
            private_file(self.runtime_file)
        fd, name = tempfile.mkstemp(prefix=".compose-", dir=self.state)
        with os.fdopen(fd, "w") as file:
            json.dump(model, file, sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
        Path(name).replace(self.runtime_file)
        self.sync_state()
        self.check_frozen(model)

    def check_frozen(self, model):
        private_file(self.runtime_file)
        actual = self.render(frozen=True)
        for item in (model, actual):
            for service in item["services"].values():
                for volume in service["volumes"]:
                    volume["bind"] = {"create_host_path": False}
        require(model == actual, "frozen Compose configuration differs from the verified model")

    def inventory(self, model, allow_own, check_ports):
        ids = self.d("ps", "-aq", "--no-trunc").stdout.decode("ascii").split()
        containers = json.loads(self.d("inspect", "--type", "container", *ids).stdout) if ids else []
        own = {}
        table = tuple(mounts())
        volumes = {}
        for container in containers:
            labels = container["Config"]["Labels"] or {}
            name = container["Name"].lstrip("/")
            service = labels.get("com.docker.compose.service")
            if labels.get("com.docker.compose.project") == self.project:
                require(allow_own and service in model["services"] and service not in own
                        and name == self.project + "-" + service + "-1"
                        and labels.get("com.docker.compose.project.working_dir") == str(ROOT)
                        and labels.get("com.docker.compose.project.config_files") == str(self.runtime_file),
                        "unexpected existing Compose project or container")
                expected = model["services"][service]
                bindings = {(k, b["HostIp"], b["HostPort"]) for k, values in
                            (container["HostConfig"]["PortBindings"] or {}).items() for b in values}
                bind_specs = {(m["Source"], m["Destination"], not m["RW"]) for m in container["Mounts"]}
                require(all(m["Type"] == "bind" for m in container["Mounts"])
                        and container["Config"]["Image"] == expected["image"]
                        and bind_specs == {(v["source"], v["target"], v.get("read_only", False))
                                   for v in expected["volumes"]}
                         and bindings == {(f"{p['target']}/tcp", p["host_ip"], str(p["published"]))
                                          for p in expected["ports"]}
                         and set(container["NetworkSettings"]["Networks"] or {}) ==
                         {self.project + "_default"},
                         "existing project has different mounts, ports or network attachments")
                own[service] = container
            else:
                require(not name.startswith(self.project + "-"), "foreign container name conflicts")
                for m in container["Mounts"]:
                    for source in self.foreign_sources(m, volumes):
                        require(not any(overlap(source, p) or backing_overlap(source, p, table)
                                        for p in (*targets(self.profile), self.state)),
                                "foreign container shares a selected target or its backing mount")
                if check_ports and container["State"]["Running"]:
                    for values in (container["HostConfig"]["PortBindings"] or {}).values():
                        require(not any(int(b["HostPort"] or 0) in PORTS[self.profile] for b in values),
                                "port already published by a foreign container")
        network = self.project + "_default"
        ids = self.d("network", "ls", "--no-trunc", "--format", "{{json .}}").stdout.splitlines()
        matches = [n for line in ids if (n := json.loads(line))["Name"] == network]
        require(len(matches) <= 1 and (allow_own or not matches), "existing Compose network conflicts")
        if matches:
            net, = json.loads(self.d("network", "inspect", matches[0]["ID"]).stdout)
            labels = net["Labels"] or {}
            require(labels.get("com.docker.compose.project") == self.project and
                    labels.get("com.docker.compose.network") == "default" and
                    net.get("Driver") == "bridge" and net.get("Scope") == "local" and
                    {c["Id"] for c in own.values() if c["State"]["Running"]}
                    <= set(net["Containers"] or {}) <= {c["Id"] for c in own.values()}
                    and all(c["NetworkSettings"]["Networks"][network]["NetworkID"] == net["Id"]
                            for c in own.values()),
                    "foreign or shared Compose network")
        else:
            require(not own, "existing project network is missing")
        if check_ports:
            listeners = self.cmd([TOOLS / "ss", "-H", "-ltn"]).stdout.decode("ascii").splitlines()
            known = {(p["host_ip"], str(p["published"])) for service, c in own.items() if c["State"]["Running"]
                     for p in model["services"][service]["ports"]}
            for line in listeners:
                address, port = line.split()[3].rsplit(":", 1)
                require(port not in self.settings_ports or (address, port) in known, "host TCP port already listening")
        return own

    def foreign_sources(self, mount, cache):
        def resolved_source(path):
            require(path.is_absolute(), "cannot identify foreign mount source")
            try:
                # Docker can traverse directories the operator cannot. A
                # non-strict resolve can silently keep an unresolved alias.
                return path.resolve(strict=True)
            except (OSError, RuntimeError):
                raise Refusal("cannot establish provenance of foreign mount source") from None

        kind = mount["Type"]
        if kind == "tmpfs":
            return ()
        if kind == "bind":
            return (resolved_source(Path(mount["Source"])),)
        require(kind == "volume", "unsupported foreign Docker mount type")
        name = mount["Name"]
        if name not in cache:
            volumes = json.loads(self.d("volume", "inspect", name).stdout)
            require(len(volumes) == 1 and volumes[0]["Name"] == name and volumes[0]["Driver"] == "local",
                    "cannot establish backing path for foreign Docker volume")
            volume = volumes[0]
            options = volume["Options"] or {}
            point = Path(volume["Mountpoint"])
            if options:
                device = Path(options.get("device") or "")
                flags = set((options.get("o") or "").split(","))
                require(device.is_absolute() and flags & {"bind", "rbind"},
                        "cannot establish backing path for foreign local volume")
                sources = (resolved_source(point), resolved_source(device))
            else:
                sources = (resolved_source(point),)
            cache[name] = (point, sources)
        point, sources = cache[name]
        require(Path(mount["Source"]) == point, "foreign volume mountpoint disagrees with Docker")
        return sources

    @property
    def settings_ports(self):
        return {str(p) for p in PORTS[self.profile]}

    def require_running(self, model):
        own = self.inventory(model, allow_own=True, check_ports=True)
        require(set(own) == set(model["services"]) and all(c["State"]["Running"] for c in own.values()),
                "the verified Compose project is not running")
        return own

    def inspect_image(self, key, required):
        image = self.pins[key]
        result = self.d("image", "inspect", image, allow_fail=True)
        if result.returncode:
            require(not required, "pinned image missing after pull")
            raw = self.d("manifest", "inspect", "--verbose", image, timeout=60).stdout
            metadata = json.loads(raw)
            entries = metadata if isinstance(metadata, list) else [metadata]
            require(any(e.get("Descriptor", {}).get("platform", {}).get("os") == "linux"
                        and e["Descriptor"]["platform"].get("architecture") == "amd64" for e in entries),
                    "pinned registry manifest lacks linux/amd64")
            return
        obj, = json.loads(result.stdout)
        require(obj["Os"] == "linux" and obj["Architecture"] == "amd64"
                and image in (obj["RepoDigests"] or []), "pinned image digest/platform mismatch")
        if key == "APP_IMAGE":
            require((obj["Config"]["Labels"] or {}).get("org.opencontainers.image.revision") ==
                    self.pins["GO_GIT_SHA"], "API OCI revision mismatch")

    def archives(self, cache=None):
        verified = []
        for key, expected in (("GO", release.GO_FILES), ("PLUGIN", release.PLUGIN_FILES)):
            url = self.pins[key + "_ARCHIVE_URL"]
            name = url.rsplit("/", 1)[1]
            digest = self.pins[key + "_ARCHIVE_SHA256"]
            try:
                if cache is None:
                    data = self.cmd([TOOLS / "curl", "-qfsSL", "--proto", "=https", "--proto-redir",
                                     "=https", "--max-filesize", release.MAX_BUNDLE, url], timeout=60).stdout
                    entries = release.read_archive_bytes(name, data, digest, expected)
                else:
                    path = cache / name
                    private_file(path)
                    with path.open("rb") as source:
                        data = source.read(release.MAX_BUNDLE + 1)
                    entries = release.read_archive_bytes(name, data, digest, expected)
            except (ValueError, tarfile.TarError, zlib.error, EOFError):
                raise Refusal(f"{key} archive failed SHA-256 or archive format verification") from None
            verified.append((key.lower(), name, data, entries, expected))
        return verified

    def verify_bundle(self):
        trusted_dir(self.bundle)
        for component, _, _, entries, modes in self.archives(self.bundle):
            parents = set()
            for rel, content in entries.items():
                path = self.bundle / component / rel
                canonical(path)
                if path.parent not in parents:
                    trusted_dir(path.parent)
                    parents.add(path.parent)
                st = path.lstat()
                require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_uid == UID and
                        stat.S_IMODE(st.st_mode) == modes[rel] and st.st_size == len(content)
                        and path.read_bytes() == content, "installed bundle differs from verified archive")
            actual = {str(p.relative_to(self.bundle / component))
                      for p in (self.bundle / component).rglob("*") if p.is_symlink() or not p.is_dir()}
            require(actual == entries.keys(), "unexpected files in installed bundle")

    def preflight(self, *, clean=False, report=False, artifacts=True, ports=True, own=None):
        self.stage = "paths and permissions"
        self.check_paths(clean)
        self.check_tools(report)
        self.stage = "local Docker context"
        require(platform.system() == "Linux" and platform.machine() == "x86_64", "host must be linux/amd64")
        root = self.check_context()
        require(root.is_absolute(), "unexpected Docker root")
        # DockerRootDir can be private to root. df on its nearest visible
        # ancestor is not proof of free space, so require a visible filesystem.
        self.stage = "Docker disk space"
        require(shutil.disk_usage(root).free >= MIN_BYTES, "less than 2 GiB free in Docker root")
        self.stage = "Compose configuration"
        model = self.render()
        self.stage = "Docker isolation and ports"
        self.inventory(model, allow_own=self.dev if own is None else own, check_ports=ports)
        if artifacts:
            self.stage = "release artifacts"
            if not self.dev:
                if self.bundle.exists():
                    self.verify_bundle()
                else:
                    self.archives()
            for key in self.image_keys():
                self.inspect_image(key, required=False)
        return model

    def image_keys(self):
        return ("POSTGRES_IMAGE", "DOKUWIKI_IMAGE") if self.dev else (
            "APP_IMAGE", "POSTGRES_IMAGE", "DOKUWIKI_IMAGE")

    def marker(self, name, value):
        path = self.state / name
        if not os.path.lexists(path):
            with path.open("x") as file:
                file.write(value + "\n")
                file.flush()
                os.fsync(file.fileno())
            self.sync_state()

    def sync_state(self):
        fd = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def clear(self, name):
        (self.state / name).unlink()
        self.sync_state()

    def marked(self, name):
        return os.path.lexists(self.state / name)

    @contextmanager
    def lock(self, name, wait):
        path = self.state / name
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        yield None
                        return
                    time.sleep(0.1)
            yield fd
        finally:
            # No explicit LOCK_UN: a surviving child may retain this fd.
            os.close(fd)

    @contextmanager
    def maintenance(self, resume=False):
        self.stage = "maintenance lock"
        with self.lock("control.lock", LOCK_WAIT) as control:
            require(control is not None, "another control operation is active")
            with self.maintenance_under_control(control, resume=resume) as fds:
                yield fds

    @contextmanager
    def maintenance_under_control(self, control, resume=False):
        """Enter maintenance with an already-held control lock."""
        if self.marked("maintenance"):
            require(resume and (self.state / "maintenance").read_bytes() == b"dev-stopped\n",
                    "maintenance is paused; inspect the previous operation first")
        else:
            self.marker("maintenance", "operation-in-progress")
        # Never leave a resumable marker while running a new operation.
        if resume and (self.state / "maintenance").read_bytes() == b"dev-stopped\n":
            self.replace_maintenance("operation-in-progress")
        with self.lock("tick.lock", LOCK_WAIT) as tick:
            require(tick is not None, "runner is still active; maintenance remains paused")
            yield (control, tick)

    def replace_maintenance(self, value):
        fd, name = tempfile.mkstemp(prefix=".maintenance-", dir=self.state)
        with os.fdopen(fd, "w") as file:
            file.write(value + "\n")
            file.flush()
            os.fsync(file.fileno())
        Path(name).replace(self.state / "maintenance")
        self.sync_state()

    def pull(self, fds):
        self.stage = "pull and verify pinned images"
        for key in self.image_keys():
            self.d("pull", "--platform", "linux/amd64", self.pins[key], timeout=600, fds=fds)
            self.inspect_image(key, required=True)

    def publish_bundle(self, fds):
        self.stage = "release staging"
        require(not os.path.lexists(self.bundle), "release bundle already exists; no overwrite")
        verified = self.archives()
        with tempfile.TemporaryDirectory(prefix=".staging-", dir=self.state) as staging:
            base = Path(staging) / "bundle"
            base.mkdir(mode=0o700)
            for _, name, data, _, _ in verified:
                with (base / name).open("xb") as archive:
                    archive.write(data)
            for component, _, _, entries, modes in verified:
                for name, content in entries.items():
                    path = base / component / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    for p in path.parents:
                        if p == base:
                            break
                        p.chmod(0o755)
                    with path.open("xb") as file:
                        file.write(content)
                    path.chmod(modes[name])
            self.pull(fds)
            base.rename(self.bundle)
        self.verify_bundle()

    def prepare(self, fds, *, fresh):
        self.stage = "initialize empty targets"
        self.check_paths(clean=fresh)
        for path in targets(self.profile):
            if not path.exists():
                path.mkdir(mode=0o700)
                path.chmod(0o755)
        if fresh:
            require(not any(self.wiki.iterdir()), "wiki is not empty")
            self.d("run", "--rm", "-i", "--network", "none", "--mount",
                   f"type=bind,src={self.wiki},dst=/config", "--entrypoint", "/bin/sh",
                   self.pins["DOKUWIKI_IMAGE"], "-ec",
                   (ROOT / "scripts/seed-wiki.sh").read_text(),
                   data=self.local["WIKI_CHECK_PASSWORD"].encode("ascii"), timeout=90, fds=fds)

    def snapshot_dev_build(self):
        # The checkout also contains the old spool and PG data: never send the
        # checkout root (or .git, postgres, scripts) to the Docker builder.
        self.stage = "snapshot dev Go sources"
        table = tuple(mounts())
        for path in (GO / "cmd", GO / "internal", self.dev_build):
            no_mount_alias(path, table)
        names = self.cmd([TOOLS / "git", "-C", GO, "-c", "core.fsmonitor=false", "ls-files",
                          "-z", "--cached", "--others", "--exclude-standard", "--", "cmd", "internal"]).stdout
        sources = [Path(os.fsdecode(name)) for name in names.split(b"\0") if name]
        require(bool(sources), "no dev Go sources")
        with tempfile.TemporaryDirectory(prefix=".dev-build-", dir=self.state) as temp:
            snapshot = Path(temp) / "new"
            snapshot.mkdir()
            for rel in (*sources, Path("Dockerfile"), Path("go.mod"), Path("go.sum")):
                require(not rel.is_absolute() and ".." not in rel.parts
                        and rel.parts[0] in ("cmd", "internal", "Dockerfile", "go.mod", "go.sum"),
                        "unexpected dev build source")
                source = GO / rel
                canonical(source)
                no_mount_alias(source, table)
                if not source.exists() and rel.parts[0] in ("cmd", "internal"):
                    continue  # local deletion of a tracked source
                st = source.stat()
                require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1, "dev build source must be a regular file")
                dest = snapshot / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, dest)
            if self.dev_build.exists():
                self.dev_build.rename(Path(temp) / "previous")
            snapshot.rename(self.dev_build)

    def http_args(self, path, wiki):
        port = self.settings["WIKI_PORT" if wiki else "API_PORT"]
        return [TOOLS / "curl", "-qfsS", "--noproxy", "*", "--max-time", "5",
                "--max-filesize", "1048576", f"http://127.0.0.1:{port}{path}"]

    def wait_http(self, path, *, wiki=False, cookies=None, accept=lambda body: True):
        args = self.http_args(path, wiki)
        if cookies is not None:
            args.extend(["--cookie", "-"])
        for _ in range(45):
            result = self.cmd(args, data=cookies, timeout=8, allow_fail=True)
            if not result.returncode and accept(result.stdout):
                return result.stdout
            time.sleep(1)
        raise Refusal("HTTP verification timed out")

    def verify_installer_closed(self):
        url = "http://127.0.0.1:" + self.settings["WIKI_PORT"] + "/install.php"
        status = self.cmd([TOOLS / "curl", "-qsS", "--noproxy", "*", "--max-time", "5",
                           "--output", "/dev/null", "--write-out", "%{http_code}", url]).stdout
        require(status in (b"403", b"404"), "wiki installer is not blocked")

    def assess_boot(self, fresh):
        self.stage = "API readiness and actions"
        self.wait_http("/readyz", accept=lambda b: json.loads(b).get("status") == "ready"
                       if b.lstrip().startswith(b"{") else b.strip() == b"ready")
        actions = json.loads(self.wait_http("/api/v1/actions"))
        require(len(actions["actions"] if isinstance(actions, dict) else actions) == 4,
                "expected four API actions")
        self.stage = "PostgreSQL bootstrap"
        if fresh:
            logs = self.compose("logs", "--no-color", "postgres", frozen=True).stdout
            seen = re.findall(rb"Applying /migrations/([0-9]{6})_[a-z_]+\.up\.sql", logs)
            require(seen == [f"{i:06}".encode() for i in range(1, 9)], "expected migrations 000001–000008")
        current = self.compose(
            "exec", "-T", "postgres", "psql", "-XqAt", "-v", "ON_ERROR_STOP=1", "-U", "petar_user",
            "-d", "petar_db", "-c", "BEGIN READ ONLY; "
            "SELECT to_regprocedure('public.sm_create_input(text,jsonb)') IS NULL AND "
            "to_regprocedure('public.sm_create_action(text,integer,jsonb)') IS NOT NULL; COMMIT;",
            frozen=True,
        ).stdout
        require(current.strip() == b"t", "schema 000008 effect not confirmed (legacy input still present)")
        self.stage = "wiki HTTP and restricted Petar page"
        page = "/doku.php?id=petar_check"
        form = rb'<form[^>]+id="[^"]+-form"'
        anon = self.wait_http(page, wiki=True)
        self.verify_installer_closed()
        require(not re.search(form, anon) and b"data-petar-jobs" not in anon, "Petar page exposed without login")
        self.stage = "wiki authenticated form and journal"
        data = ("do=login&id=petar_check&u=petar_check&p=" + self.local["WIKI_CHECK_PASSWORD"]).encode()
        # DokuWiki authenticates on POST, then redirects. Never follow a
        # redirect with credentials: keep the cookie jar in memory and GET
        # the fixed loopback page explicitly.
        cookies = self.cmd([*self.http_args(page, True), "--output", "/dev/null",
                            "--cookie-jar", "-", "--data-binary", "@-"], data=data, timeout=8).stdout
        require(cookies.startswith(b"# Netscape HTTP Cookie File"), "wiki did not return a cookie jar")
        self.wait_http(page, wiki=True, cookies=cookies,
                       accept=lambda b: re.search(form, b) and b"data-petar-jobs" in b)

    def install(self):
        self.preflight(clean=True)
        with self.maintenance() as fds:
            self.preflight(clean=True, artifacts=False)
            require(not self.marked("ready"), "release has an existing prepared marker")
            self.publish_bundle(fds)
            model = self.preflight(clean=True, artifacts=False)
            self.stage = "freeze verified Compose configuration"
            self.freeze(model)
            self.prepare(fds, fresh=True)
            self.stage = "verify configuration before start"
            self.check_paths(clean=False)
            require(not any(self.pgdata.iterdir()) and not any(self.spool.iterdir()),
                    "PGDATA or spool became nonempty during preparation")
            self.check_frozen(model)
            self.inventory(model, allow_own=False, check_ports=True)
            self.stage = "initialize release spool directories"
            # The spool bind hides the image's directories. The Go writer
            # publishes into in/ immediately, before the host runner ever ticks.
            for name in ("in", "processing", "out"):
                (self.spool / name).mkdir(mode=0o700)
            self.stage = "start pinned release"
            self.compose("up", "--detach", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "180",
                         timeout=240, fds=fds, frozen=True)
            self.require_running(model)
            self.assess_boot(fresh=True)
            self.marker("ready", "deploy-001 prepared; end-to-end smoke pending")
            self.clear("maintenance")
        say(f"{self.profile}: prepared, awaiting smoke")

    def dev_control(self, up):
        if not up:
            return self.stop_dev()
        self.preflight()
        with self.maintenance(resume=up) as fds:
            fresh = not self.marked("ready")
            self.preflight(clean=fresh, artifacts=False)
            self.snapshot_dev_build()
            self.pull(fds)
            model = self.preflight(clean=fresh, artifacts=False)
            self.stage = "freeze verified dev configuration"
            self.freeze(model)
            self.prepare(fds, fresh=fresh)
            self.stage = "start dev only"
            self.check_frozen(model)
            self.inventory(model, allow_own=True, check_ports=True)
            self.compose("up", "--detach", "--build", "--pull", "never", "--wait", "--wait-timeout", "180",
                         timeout=900, fds=fds, frozen=True)
            self.require_running(model)
            self.assess_boot(fresh=fresh)
            self.marker("ready", "dev prepared")
            self.clear("maintenance")
        say("dev: up")

    def stop_dev(self):
        # Do not require source YAML or build inputs: a broken checkout is
        # precisely when the operator may need to stop the running project.
        with self.maintenance() as fds:
            self.stage = "verify frozen dev project for stop"
            self.check_context()
            table = tuple(mounts())
            for path in (*targets(self.profile), self.state):
                canonical(path)
                no_mount_alias(path, table)
            if self.marked(self.runtime_file.name):
                private_file(self.runtime_file)
                model = self.render(frozen=True)
                self.inventory(model, allow_own=True, check_ports=False)
                self.stage = "stop dev only"
                self.compose("down", timeout=180, fds=fds, frozen=True)
            else:
                self.inventory({}, allow_own=False, check_ports=False)
            self.replace_maintenance("dev-stopped")
        say("dev: down (data preserved)")

    def check_queue(self):
        """The pinned runner follows input/result paths. Check metadata, not payloads."""
        no_mount_alias(self.spool, tuple(mounts()))
        trusted_dir(self.spool)
        pending = False

        def children(path, files_only):
            with os.scandir(path) as entries:
                for entry in entries:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        # The API publishes ordinary input files by rename.
                        if path == self.spool / "in":
                            continue
                        raise
                    safe_type = (stat.S_ISREG(st.st_mode) and st.st_nlink == 1
                                 if files_only else stat.S_ISDIR(st.st_mode))
                    require(safe_type and st.st_uid == st.st_gid == UID and not st.st_mode & 0o022,
                            "unsafe queue entry type, link, ownership or permissions")
                    yield Path(entry.path)

        for name in ("in", "processing", "out"):
            path = self.spool / name
            if not os.path.lexists(path):
                continue
            trusted_dir(path)
            st = path.stat()
            require(st.st_uid == st.st_gid == UID and os.access(path, os.W_OK | os.X_OK),
                    "spool directory must be writable by the selected operator")
            for entry in children(path, files_only=name != "out"):
                if name == "out":
                    for _ in children(entry, files_only=True):
                        pass
                elif name == "processing":
                    pending = True
        return pending

    def tick(self):
        self.stage = "runner lock"
        with self.lock("tick.lock", 0) as tick:
            if tick is None:
                say(f"{self.profile}: tick skipped (busy)")
                return
            if self.marked("maintenance"):
                say(f"{self.profile}: tick skipped (maintenance)")
                return
            self.stage = "runner pause"
            require(not self.marked("pause") and not self.marked("inflight"),
                    "runner paused; reconcile processing and database before resuming")
            self.stage = "runner prerequisites"
            require(self.marked("ready"), "environment is not prepared")
            processing = self.spool / "processing"
            runner = (GO if self.dev else self.bundle / "go") / "scripts/spool-runner.sh"
            if not self.dev:
                self.verify_bundle()
            canonical(runner)
            no_mount_alias(runner, tuple(mounts()))
            require(runner.is_file() and os.access(runner, os.X_OK), "runner is missing or not executable")
            self.check_tools(False)
            canonical(self.spool)
            no_mount_alias(self.spool, tuple(mounts()))
            runner_lock = self.spool / ".runner.lock"
            if os.path.lexists(runner_lock):
                private_file(runner_lock)
            self.stage = "runner Docker isolation"
            self.check_context()
            private_file(self.runtime_file)
            self.require_running(self.render(frozen=True))
            ready = self.cmd([TOOLS / "curl", "-qfsS", "--noproxy", "*", "--max-time", "5",
                              f"http://127.0.0.1:{self.settings['API_PORT']}/readyz"], timeout=8)
            require(json.loads(ready.stdout).get("status") == "ready", "API is not ready")
            self.stage = "runner queue metadata"
            try:
                if self.check_queue():
                    raise Refusal("processing is not empty; reconcile with the database")
            except (Refusal, OSError):
                self.marker("pause", "unsafe or pending queue state")
                raise
            self.stage = "one runner pass"
            self.marker("inflight", "runner outcome not yet confirmed")
            # Discard raw tool output: the pinned runner can print job identifiers.
            # The cron log records wrapper outcomes, never payloads or credentials.
            result = subprocess.run([str(runner)], cwd=self.home, env=self.runner_env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, pass_fds=(tick,))
            if result.returncode or (processing.exists() and any(processing.iterdir())):
                self.marker("pause", "ambiguous runner outcome")
                raise Refusal("runner outcome unconfirmed; reconcile processing and database")
            self.clear("inflight")
        say(f"{self.profile}: tick completed")


def main(args):
    op = None
    try:
        require(args, "expected an operator command")
        command, *rest = args
        if command == "deploy":
            require(len(rest) == 2 and rest[0] in ("rehearse", "init", "upgrade") and rest[1] == "deploy-001",
                    "usage: deploy.sh rehearse|init|upgrade deploy-001")
            require(rest[0] != "upgrade", "upgrade is unsupported; no changes made")
            profile = "rehearsal" if rest[0] == "rehearse" else "production"
        elif command == "dev":
            require(rest in (["up"], ["down"]), "usage: dev.sh up|down")
            profile = "dev"
        elif command == "preflight":
            require(len(rest) == 2 and rest[0] in PROFILES and rest[1] == "deploy-001",
                    "usage: preflight.sh dev|rehearsal|production deploy-001")
            profile = rest[0]
        else:
            require(command == "run-once" and len(rest) == 1 and rest[0] in PROFILES,
                    "usage: run-once.sh dev|rehearsal|production")
            profile = rest[0]
        op = Operator(profile)
        op.config()
        if command == "preflight":
            op.preflight(clean=not op.dev, report=True)
            say(f"{profile}: read-only preflight passed")
        elif command == "deploy":
            op.install()
        elif command == "dev":
            op.dev_control(rest[0] == "up")
        else:
            op.tick()
        return 0
    except Refusal as error:
        print(f"DEPLOY-06: {op.stage if op else 'arguments'}: {error}", file=sys.stderr)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, IndexError,
            subprocess.SubprocessError, tarfile.TarError, zlib.error, EOFError):
        print(f"DEPLOY-06: {op.stage if op else 'startup'}: failed; no raw diagnostics printed",
              file=sys.stderr)
    return 1


if __name__ == "__main__":
    # A killed controller leaves the durable gate; subprocesses carry its locks.
    os.umask(0o077)
    signal.signal(signal.SIGTERM, lambda _sig, _frame: sys.exit(143))
    sys.exit(main(sys.argv[1:]))
