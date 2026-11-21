"""Real Docker acceptance; refuses the working host and never provisions a VM."""

import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import struct
import subprocess
import sys
import threading


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("petar_operator", ROOT / "scripts/petar_operator.py")
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


def require(ok, reason):
    if not ok:
        raise ops.Refusal(reason)


def attest_vm():
    require(os.getuid() == os.getgid() == 1000, "run as non-root UID/GID 1000")
    marker = Path("/etc/petar-deploy-disposable")
    try:
        mode = marker.lstat()
    except OSError:
        raise ops.Refusal("no root-issued disposable VM attestation") from None
    require(stat.S_ISREG(mode.st_mode) and mode.st_uid == 0 and not mode.st_mode & 0o022
            and marker.read_text().strip() == Path("/etc/machine-id").read_text().strip(),
            "disposable VM attestation does not match this machine")
    host = Path("/proc/sys/kernel/osrelease").read_text().lower()
    virt = subprocess.run(["/usr/bin/systemd-detect-virt", "--vm"], capture_output=True)
    container = subprocess.run(["/usr/bin/systemd-detect-virt", "--container"], capture_output=True)
    require("microsoft" not in host and container.returncode == 1 and
            not any(p.exists() for p in (Path("/.dockerenv"), Path("/run/.containerenv"))) and
            virt.returncode == 0 and
            virt.stdout.strip() in (b"kvm", b"qemu", b"vmware", b"oracle", b"microsoft", b"xen", b"bhyve"),
            "requires a dedicated Linux VM, not a container or WSL")
    work_paths = (ops.DATA, ops.GO, ops.PLUGIN, ROOT)
    for path in work_paths:
        ops.trusted_dir(path)
    table = tuple(ops.mounts())
    local = {b"ext4", b"xfs"}
    for path in (*work_paths, marker, Path("/etc/machine-id")):
        ops.canonical(path)
        ops.no_mount_alias(path, table, local_filesystems=local)
    require(not (ops.GO / ".git").exists() and not any(ops.PLUGIN.iterdir()),
            "release acceptance requires no Go or PHP checkout")
    for profile in ops.PROFILES:
        state = ops.Operator(profile).state
        ops.trusted_dir(state)
        st = state.stat()
        require(st.st_uid == st.st_gid == 1000 and stat.S_IMODE(st.st_mode) == 0o700
                and not any(state.iterdir()) and all(not os.path.lexists(p) for p in ops.targets(profile)),
                "all operator directories and test targets must be fresh")
    op = ops.Operator("rehearsal")
    daemon_socket = Path(ops.SOCKET[len("unix://"):]).resolve()
    require(daemon_socket == Path("/run/docker.sock") and
            stat.S_ISSOCK(daemon_socket.stat().st_mode) and daemon_socket.stat().st_uid == 0,
            "expected the guest-local root-owned Docker socket")
    ops.no_mount_alias(daemon_socket, table, local_filesystems=local | {b"tmpfs"})
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(3)
        connection.connect(str(daemon_socket))
        pid, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    proc = Path(f"/proc/{pid}")
    require(pid > 1 and uid == 0 and (proc / "comm").read_bytes().strip() == b"dockerd",
            "Docker socket must be bound directly by the guest dockerd (not a proxy or socket activation)")
    info = op.check_context(details=True)
    require(info["KernelVersion"] == os.uname().release and info["Name"] == socket.gethostname(),
            "daemon kernel/hostname differ from the disposable VM")
    docker_root = Path(info["DockerRootDir"])
    ops.canonical(docker_root)
    daemon_table = tuple(ops.mounts(proc / "mountinfo"))
    for path in (*work_paths, docker_root):
        ops.no_mount_alias(path, table, local_filesystems=local)
        ops.no_mount_alias(path, daemon_table, local_filesystems=local)
        def provenance(records):
            return {(root, point, kind, dev) for root, point, kind, dev in records if point in path.parents}
        require(provenance(table) == provenance(daemon_table), "daemon does not see the same fixture filesystem")
    require(not op.d("ps", "-aq").stdout.strip(), "disposable Docker daemon must have no containers")


def setup():
    for profile in ops.PROFILES:
        state = ops.Operator(profile).state
        for name in ("control.lock", "tick.lock", "runner.log"):
            (state / name).touch(exist_ok=False)
        with (state / "secrets.env").open("x") as file:
            for key in ("POSTGRES_PASSWORD", "WIKI_CHECK_PASSWORD"):
                file.write(f"{key}={secrets.token_urlsafe(32)}\n")


def public(op, command, *args, log=False):
    call = [str(ROOT / f"scripts/{command}.sh"), *args]
    if log:
        with (op.state / "runner.log").open("ab") as file:
            result = subprocess.run(call, env={}, stdout=file, stderr=file, timeout=1200)
    else:
        result = subprocess.run(call, env={}, timeout=1200)
    require(result.returncode == 0, f"{command} exited {result.returncode}")


def projects(op):
    op.config()
    own = op.require_running(op.render(frozen=True))
    return {name: obj["Id"] for name, obj in own.items()}


class RehearsalMonitor:
    def __init__(self, op):
        self.op = op
        self.stop = threading.Event()
        self.error = None
        self.count = 0
        self.thread = threading.Thread(target=self.sample, daemon=True)

    def sample(self):
        while not self.stop.is_set():
            try:
                for port, path in ((82, "/doku.php?id=petar_check"), (8082, "/readyz")):
                    result = self.op.cmd(["/usr/bin/curl", "-qfsS", "--noproxy", "*", "--max-time", "2",
                                          f"http://127.0.0.1:{port}{path}"], timeout=3)
                    require(bool(result.stdout), "empty rehearsal HTTP response")
                self.count += 1
            except (ops.Refusal, OSError, subprocess.SubprocessError) as error:
                self.error = error
                return
            self.stop.wait(0.5)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, _exc, _trace):
        self.stop.set()
        self.thread.join()
        if exc_type is None:
            require(self.error is None and self.count > 1, "rehearsal HTTP sampling failed during dev lifecycle")


def git_sources(op):
    git = "/usr/bin/git"
    go = [git, "-C", ops.GO]
    old = os.umask(0o022)
    try:
        op.cmd([*go, "init", "-q"])
        op.cmd([*go, "sparse-checkout", "set", "--cone", "cmd", "internal", "postgres", "scripts"])
        op.cmd([*go, "fetch", "--depth=1", "https://github.com/tttest25/go-pet.git", "refs/tags/v0.1.0"], timeout=180)
        op.cmd([*go, "checkout", "--detach", "FETCH_HEAD"])
        op.cmd([git, "clone", "--depth=1", "--branch", "v0.1.1",
                "https://github.com/tttest25/php-wiki-plugin.git", ops.PLUGIN], timeout=180)
    finally:
        os.umask(old)


def main():
    os.umask(0o077)
    phase = "attestation"
    try:
        attest_vm()
        phase = "test fixtures"
        setup()
        dev, rehearsal, production = (ops.Operator(p) for p in ops.PROFILES)
        phase = "clean release on rehearsal"
        public(rehearsal, "preflight", "rehearsal", "deploy-001")
        public(rehearsal, "deploy", "rehearse", "deploy-001")
        rehearsal_ids = projects(rehearsal)
        config = rehearsal.wiki / "dokuwiki/conf/local.php"
        before = config.read_bytes()
        require(b"smoke pending" in (rehearsal.state / "ready").read_bytes(), "unexpected readiness mark")
        phase = "clean release on disposable production profile"
        public(production, "deploy", "init", "deploy-001")
        production_ids = projects(production)
        require(rehearsal_ids == projects(rehearsal), "rehearsal replaced by init")
        phase = "dev sources and isolated lifecycle"
        git_sources(rehearsal)
        with RehearsalMonitor(rehearsal):
            public(dev, "dev", "up")
            projects(dev)
            public(dev, "run-once", "dev", log=True)
            require(b"dev: tick completed" in (dev.state / "runner.log").read_bytes(), "missing cron-like tick")
            dev.config()
            dev_model = dev.render()
            require(dev_model["services"]["app"]["build"]["context"] == str(dev.dev_build)
                    and dev.runner_env["API_BASE_URL"] == "http://127.0.0.1:8081/api/v1",
                    "dev build or callback not isolated")
            sentinel = dev.wiki / "deploy06-dev-sentinel"
            sentinel.write_text("preserve\n")
            public(dev, "dev", "down")
            require(not dev.inventory(dev_model, allow_own=False, check_ports=True), "dev containers not removed")
            require(rehearsal_ids == projects(rehearsal), "dev down replaced rehearsal")
            public(dev, "dev", "up")
            require(sentinel.read_text() == "preserve\n", "dev data not preserved")
        require(rehearsal_ids == projects(rehearsal) and production_ids == projects(production)
                and before == config.read_bytes(), "release resources changed during dev lifecycle")
        phase = "stop only verified test projects"
        public(dev, "dev", "down")
        for op, expected in ((rehearsal, rehearsal_ids), (production, production_ids)):
            require(projects(op) == expected, "test project identity changed")
            with op.maintenance() as fds:
                op.compose("down", timeout=180, fds=fds, frozen=True)
        print("isolated: passed; no job submitted, test data left for disposal with VM")
        return 0
    except (ops.Refusal, OSError, ValueError, subprocess.SubprocessError, KeyError, IndexError) as error:
        detail = str(error) if isinstance(error, ops.Refusal) else "no raw diagnostics printed"
        print(f"isolated: {'BLOCKED' if phase == 'attestation' else 'FAILED'} ({phase}): {detail}", file=sys.stderr)
        return 77 if phase == "attestation" else 1


if __name__ == "__main__":
    sys.exit(main())
