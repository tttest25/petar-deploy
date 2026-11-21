"""Public DEPLOY-06 entry points in a private copy; no Docker daemon is used."""

import gzip
import hashlib
import importlib.util
import io
import json
import os
import grp
from pathlib import Path
import pwd
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import time
import unittest


REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_checker", REPO / "scripts/check-release.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)
SECRET = "secret-sentinel-local-db-2026"
FAKE = r'''#!/usr/bin/python3 -IB
import json, os, subprocess, sys, time
from pathlib import Path
fixture = Path(__file__).resolve().parents[1]
root = fixture / "observations"
tool = Path(sys.argv[0]).name
args = sys.argv[1:]
def log(value):
    with (root / "calls").open("a") as f:
        print(value, file=f)
def flag(name):
    return (root / name).exists()
def stop():
    print("secret-sentinel-local-db-2026", file=sys.stderr)
    print("secret-sentinel-local-db-2026")
    sys.exit(1)
def output(value):
    print(json.dumps(value))
if tool == "runner":
    log("runner " + os.environ["SPOOL_DIR"] + " " + os.environ["API_BASE_URL"])
    assert "EVIL" not in os.environ and len(args) == 0
    if flag("claim-before-crash"):
        spool = Path(os.environ["SPOOL_DIR"])
        (spool / "processing").mkdir(exist_ok=True)
        (spool / "in" / "claimed.json").replace(spool / "processing" / "claimed.json")
    if flag("hold") and os.environ["API_BASE_URL"].endswith(":8081/api/v1"):
        while flag("hold"):
            time.sleep(.02)
    if flag("unconfirmed"):
        processing = Path(os.environ["SPOOL_DIR"]) / "processing"
        processing.mkdir(exist_ok=True)
        (processing / "pending.json").write_text("synthetic")
        stop()
elif tool == "ss":
    if flag("port") or (flag("late-port") and "docker pull\n" in (root/"calls").read_text()):
        print("LISTEN 0 128 127.0.0.1:82 0.0.0.0:*")
elif tool == "curl":
    url = next(a for a in args if a.startswith(("https://", "http://")))
    if url.startswith("https:"):
        name = url.rsplit("/", 1)[1]
        log("archive " + name)
        data = (root / name).read_bytes()
        if flag("bad-" + name):
            data += b"corrupt"
        sys.stdout.buffer.write(data)
    elif url.endswith("/readyz"):
        output({"status":"ready"})
    elif url.endswith("/api/v1/actions"):
        output([1, 2, 3, 4])
    elif url.endswith("/install.php"):
        print("404", end="")
    elif "--data-binary" in args:
        assert sys.stdin.buffer.read().startswith(b"do=login&id=petar_check&")
        assert args[args.index("--cookie-jar")+1] == "-"
        assert args[args.index("--output")+1] == "/dev/null"
        assert "--location" not in args
        print("# Netscape HTTP Cookie File\n# fixture session")
    elif "--cookie" in args:
        assert args[args.index("--cookie")+1] == "-"
        assert sys.stdin.buffer.read() == b"# Netscape HTTP Cookie File\n# fixture session\n"
        print('<form id="petar-x-form"><section data-petar-jobs="">')
    else:
        print("<html>restricted wiki page</html>")
elif tool == "docker":
    assert args[:1] == ["--config"] and args[2:4] == ["--context", "default"]
    args = args[4:]
    log("docker " + args[0])
    if args[0] == "context":
        output([{"Endpoints":{"docker":{"Host":"tcp://example.invalid:2375" if flag("remote")
                                       else "unix:///var/run/docker.sock"}}}])
    elif args[0] == "info":
        output({"OSType":"linux","Architecture":"x86_64","SecurityOptions":[],
                "KernelVersion":os.uname().release, "Name":os.uname().nodename,
                "DockerRootDir": "/" if not flag("bad-root") else "relative"})
    elif args[0] in ("pull", "image"):
        if args[0] == "image":
            pins = dict(line.split("=",1) for line in
                        (fixture/"repo/releases/001.env").read_text().splitlines() if "=" in line)
            output([{"Os":"linux","Architecture":"amd64","RepoDigests":[args[-1]],
                     "Config":{"Labels":{"org.opencontainers.image.revision":
                     "wrong" if flag("wrong-revision") and "docker pull\n" in (root/"calls").read_text()
                     else pins["GO_GIT_SHA"]}}}])
    elif args[0] == "compose":
        i = args.index("-p")
        project = args[i+1]
        end = i+2
        files = []
        while args[end] == "-f":
            files.append(args[end+1])
            end += 2
        verb = args[end]
        log(verb + " " + project)
        def model():
            result = subprocess.run(["/usr/bin/docker", *args[:end], "config", "--format", "json"],
                                    capture_output=True, check=True)
            return json.loads(result.stdout)
        ready = root / project
        if verb == "config":
            if flag("bad-render"):
                stop()
            output(model())
        elif verb == "up":
            assert len(files) == 1 and files[0].endswith("/operator/runtime.compose.json")
            assert Path(files[0]).stat().st_mode & 0o777 == 0o600
            m = model()
            build = m["services"]["app"].get("build")
            if project == "petar-dev":
                assert build["network"] == "host"
                contents = {str(p.relative_to(build["context"])) for p in Path(build["context"]).rglob("*")}
                assert {"cmd/petar/main.go", "Dockerfile"} <= contents
                assert not any(p.startswith(("postgres", "spool", ".git")) for p in contents)
            else:
                assert build is None
            assert "--pull" in args and args[args.index("--pull")+1] == "never"
            containers = []
            for service, s in m["services"].items():
                ports = {f"{p['target']}/tcp":[{"HostIp":p["host_ip"],"HostPort":str(p["published"])}]
                         for p in s["ports"]}
                mounts = [{"Type":"bind","RW":not v.get("read_only",False),"Source":v["source"],
                           "Destination":v["target"]} for v in s["volumes"]]
                name = f"{project}-{service}-1"
                containers.append({"Id":name,"Name":"/"+name,"Mounts":mounts,"State":{"Running":True},
                    "NetworkSettings":{"Networks":{project+"_default":{"NetworkID":project}}},
                    "Config":{"Image":s["image"],"Labels":{"com.docker.compose.service":service,
                        "com.docker.compose.project":project,"com.docker.compose.project.working_dir":os.getcwd(),
                        "com.docker.compose.project.config_files":",".join(files)}},
                    "HostConfig":{"PortBindings":ports}})
            ready.write_text(json.dumps(containers))
        elif verb == "down":
            assert project == "petar-dev" and "--volumes" not in args and "-v" not in args
            assert len(files) == 1 and files[0].endswith("/operator/runtime.compose.json")
            ready.unlink(missing_ok=True)
        elif verb == "logs":
            for i in range(1, 9):
                print(f"Applying /migrations/{i:06}_migration.up.sql")
        elif verb == "exec":
            assert "psql" in args and "BEGIN READ ONLY;" in args[-1] and "sm_create_input" in args[-1]
            print("f" if flag("legacy-schema") else "t")
        else:
            stop()
    elif args[0] in ("inspect", "ps"):
        known = [c for p in root.iterdir() if p.name in ("petar-dev", "petar-rehearsal", "petar-production")
                 for c in json.loads(p.read_text())]
        if flag("foreign"):
            known.append({"Id":"foreign","Name":"/petar-rehearsal-other",
                          "Config":{"Labels":{}},"Mounts":[],"State":{"Running":True}})
        if flag("foreign-mounts.json"):
            known.append({"Id":"foreign-mounts","Name":"/unrelated",
                          "Config":{"Labels":{}}, "HostConfig":{"PortBindings":{}}, "State":{"Running":True},
                          "Mounts":json.loads((root/"foreign-mounts.json").read_text())})
        if args[0] == "ps":
            print("\n".join(c["Id"] for c in known))
        else:
            output(known)
    elif args[:2] == ["network", "ls"]:
        for p in root.iterdir():
            if p.name in ("petar-dev", "petar-rehearsal", "petar-production"):
                output({"Name": p.name+"_default", "ID":p.name})
    elif args[:2] == ["network", "inspect"]:
        project = args[-1]
        containers = json.loads((root/project).read_text())
        output([{"Labels":{"com.docker.compose.project":project, "com.docker.compose.network":"default"},
                 "Driver":"bridge", "Scope":"local", "Id":project,
                 "Containers":{c["Id"]:{} for c in containers}}])
    elif args[:2] == ["volume", "inspect"]:
        output([json.loads((root/"volume.json").read_text())])
    elif args[0] == "run":
        assert args[args.index("--network")+1] == "none"
        assert "install" in args[-1] and "password_hash" in args[-1]
        assert sys.stdin.buffer.read() == b"another-wiki-test-password-2026"
        if flag("edit-source-during-seed"):
            source = fixture / "repo/compose.yaml"
            source.write_text(source.read_text().replace("  app:\n", "  app:\n    ipc: host\n"))
        wiki = Path(args[args.index("--mount")+1].split(",")[1][4:])
        assert not any(wiki.iterdir())
        (wiki / "preseeded").write_text("synthetic")
    else:
        stop()
'''


def archive(files, runner):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, mode in files.items():
            data = runner if name == "scripts/spool-runner.sh" else b"test archive content\n"
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(out.getvalue())


class Contracts(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="petar-deploy-contracts-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.base.chmod(0o755)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        for name in ("scripts", "releases", "env", "templates"):
            shutil.copytree(REPO / name, self.repo / name, ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("compose.yaml", "compose.dev.yaml"):
            shutil.copy(REPO / name, self.repo / name)
        self.obs = self.base / "observations"
        self.obs.mkdir()
        (self.obs / "calls").touch()
        self.mountinfo = self.obs / "mountinfo"
        self.mountinfo.write_bytes(Path("/proc/self/mountinfo").read_bytes())
        self.tools = self.base / "tools"
        self.tools.mkdir()
        for name in ("docker", "ss", "curl", "runner"):
            self.executable(self.tools / name, FAKE)
        (self.tools / "git").symlink_to("/usr/bin/git")
        self.data, self.go, self.plugin = (self.base / p for p in ("data", "go", "plugin"))
        self.go.mkdir()
        self.plugin.mkdir()
        self.executable(self.go / "scripts/spool-runner.sh", f"#!/bin/bash\nexec {self.tools/'runner'}\n")
        git_env = {"HOME": str(self.base), "PATH": "/usr/bin:/bin"}
        subprocess.run(["git", "init", "-q", str(self.go)], check=True, env=git_env)
        for f in ("Dockerfile", "go.mod", "go.sum", "cmd/petar/main.go", "internal/pkg/main.go",
                  "postgres/bootstrap.sh", "postgres/postgresql.conf", "postgres/migrations/000001_fake.up.sql"):
            path = self.go / f
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        (self.go / "postgres/bootstrap.sh").chmod(0o755)
        (self.plugin / "plugin.info.txt").touch()
        pinfile = self.repo / "releases/001.env"
        pins = check.read_manifest(pinfile)
        text = pinfile.read_text()
        self.archive_names = {}
        for key, expected in (("GO", check.GO_FILES), ("PLUGIN", check.PLUGIN_FILES)):
            data = archive(expected, (self.go / "scripts/spool-runner.sh").read_bytes())
            name = self.archive_names[key] = pins[key + "_ARCHIVE_URL"].rsplit("/", 1)[1]
            (self.obs / name).write_bytes(data)
            text = text.replace(pins[key + "_ARCHIVE_SHA256"], hashlib.sha256(data).hexdigest())
        pinfile.write_text(text)
        for profile in ("dev", "production", "rehearsal"):
            op = self.state(profile)
            op.mkdir(parents=True, mode=0o700)
            (self.go / "postgres/deploy" / profile).mkdir(parents=True)
            for name in ("control.lock", "tick.lock", "runner.log"):
                self.private(op / name)
            self.private(op / "secrets.env",
                         f"POSTGRES_PASSWORD={SECRET}\nWIKI_CHECK_PASSWORD=another-wiki-test-password-2026\n")
        module = self.repo / "scripts/petar_operator.py"
        module.write_text(module.read_text().replace('Path("/opt/petar-data")', f"Path({str(self.data)!r})")
                          .replace('Path("/opt/go-pet")', f"Path({str(self.go)!r})")
                          .replace('Path("/opt/php-wiki-plugin")', f"Path({str(self.plugin)!r})")
                          .replace('Path("/usr/bin")', f"Path({str(self.tools)!r})")
                          .replace('Path("/proc/self/mountinfo")', f"Path({str(self.mountinfo)!r})")
                          .replace("LOCK_WAIT = 120", "LOCK_WAIT = 5"))

    @staticmethod
    def executable(path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        path.chmod(0o755)

    @staticmethod
    def private(path, body=""):
        path.write_text(body)
        path.chmod(0o600)

    def state(self, profile):
        return self.data / profile / "operator"

    def set_flag(self, name):
        (self.obs / name).touch()

    def add_mount(self, source, path):
        with self.mountinfo.open("a") as file:
            file.write(f"80000 10 8:1 {source} {path} rw - ext4 /dev/test rw\n")

    def start(self, command, *args, own_group=False):
        env = {"COMPOSE_PROJECT_NAME": "evil", "DOCKER_HOST": "tcp://evil.example", "SPOOL_DIR": "/wrong",
               "API_BASE_URL": "http://wrong", "HOME": "/wrong", "PATH": "/wrong",
               "POSTGRES_PASSWORD": "evil", "EVIL": "yes", "BASH_ENV": "/wrong"}
        proc = subprocess.Popen([self.repo / "scripts" / (command + ".sh"), *args],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=own_group)
        self.addCleanup(self.cleanup_process, proc)
        return proc

    @staticmethod
    def cleanup_process(proc):
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=10)

    def done(self, proc, expected=0, message=None):
        out, err = (v.decode() for v in proc.communicate(timeout=30))
        data = out + err
        self.assertNotIn(SECRET, data)
        self.assertEqual(proc.returncode, expected, data)
        if message:
            self.assertIn(message, data)
        return data

    def call(self, command, *args, expected=0, message=None):
        return self.done(self.start(command, *args), expected, message)

    def calls(self):
        return (self.obs / "calls").read_text().splitlines()

    def no_mutation(self):
        self.assertFalse({line.split()[0] for line in self.calls()} & {"up", "down"})
        self.assertFalse({"docker pull", "docker run"} & set(self.calls()))

    def snapshot(self):
        files = {}
        for root in (self.data, self.go):
            for p in root.rglob("*"):
                s = p.lstat()
                files[p] = (s.st_mode, s.st_uid, s.st_gid, s.st_mtime_ns,
                            p.read_bytes() if stat.S_ISREG(s.st_mode) else None)
        return files

    def until(self, condition):
        limit = time.monotonic() + 10
        while not condition():
            self.assertLess(time.monotonic(), limit, self.calls())
            time.sleep(.025)

    def test_readonly_preflight_and_validation(self):
        original = self.snapshot()
        self.call("preflight", "rehearsal", "deploy-001", message="read-only preflight passed")
        self.assertEqual(original, self.snapshot())
        for flag, reason in (("port", "already listening"), ("foreign", "foreign container name"),
                             ("remote", "fixed local Unix socket"), ("bad-render", "Compose configuration"),
                             ("bad-root", "unexpected Docker root")):
            with self.subTest(flag=flag):
                self.set_flag(flag)
                self.call("preflight", "rehearsal", "deploy-001", expected=1, message=reason)
                (self.obs / flag).unlink()
                self.assertEqual(original, self.snapshot())
        self.no_mutation()

    def test_path_permissions_links_space_and_tool(self):
        parent = self.data / "rehearsal"
        (parent / "wiki").symlink_to(self.go / "spool")
        self.call("preflight", "rehearsal", "deploy-001", expected=1, message="symlink")
        (parent / "wiki").unlink()
        parent.chmod(0o777)
        self.call("preflight", "rehearsal", "deploy-001", expected=1, message="unsafe directory")
        parent.chmod(0o755)
        module = self.repo / "scripts/petar_operator.py"
        original = module.read_text()
        module.write_text(original.replace("MIN_BYTES = 2 * 1024**3", "MIN_BYTES = 2**80"))
        self.call("preflight", "rehearsal", "deploy-001", expected=1, message="less than 2 GiB free")
        module.write_text(original.replace('for cmd in ("bash", "jq",', 'for cmd in ("missing-test-tool", "jq",'))
        self.call("preflight", "rehearsal", "deploy-001", expected=1, message="requires missing-test-tool")
        self.no_mutation()

    def test_upgrade_bad_id_empty_checks(self):
        start = self.snapshot()
        self.call("deploy", "upgrade", "deploy-001", expected=1, message="unsupported")
        self.call("deploy", "init", "deploy-000", expected=1, message="usage:")
        self.assertEqual(self.calls(), [])
        self.assertEqual(start, self.snapshot())
        for path in (self.go / "postgres/deploy/production/data", self.data / "production/wiki", self.go / "spool"):
            path.mkdir()
            token = path / ".sentinel"
            self.private(token, "dont touch")
            before = self.snapshot()
            self.call("deploy", "init", "deploy-001", expected=1, message="requires empty")
            self.assertEqual(before, self.snapshot())
            token.unlink()
            path.rmdir()
        self.assertEqual(self.calls(), [])

    def test_archive_mismatch_and_revision(self):
        for key, name in self.archive_names.items():
            self.set_flag("bad-" + name)
            before = self.snapshot()
            self.call("deploy", "rehearse", "deploy-001", expected=1, message=key + " archive failed SHA-256")
            self.assertEqual(before, self.snapshot())
            (self.obs / ("bad-" + name)).unlink()
        self.no_mutation()
        self.set_flag("wrong-revision")
        self.call("deploy", "rehearse", "deploy-001", expected=1, message="OCI revision mismatch")
        self.assertNotIn("docker run", self.calls())
        self.assertTrue((self.state("rehearsal") / "maintenance").exists())
        self.assertFalse((self.state("rehearsal") / "ready").exists())

    def test_subtree_mounts_refused_before_preparation_and_runner(self):
        self.call("dev", "up")
        original = self.mountinfo.read_bytes()
        self.add_mount("/other/spool/in", self.data / "dev/spool/in")
        self.call("run-once", "dev", expected=1, message="mount isolation")
        self.assertFalse(any(s.startswith("runner ") for s in self.calls()))
        self.mountinfo.write_bytes(original)
        self.add_mount("/other/data", self.data / "rehearsal/wiki/sub")
        self.call("deploy", "rehearse", "deploy-001", expected=1, message="mount isolation")
        self.assertNotIn("up petar-rehearsal", self.calls())
        self.mountinfo.write_bytes(original)
        self.add_mount("/other/data", self.go / "cmd/petar")
        self.call("preflight", "dev", "deploy-001", expected=1, message="mount isolation")

    def test_foreign_bind_alias_and_bind_backed_named_volume_are_refused(self):
        self.mountinfo.write_text("10 1 8:1 / / rw - ext4 /dev/test rw\n")
        aliases = self.base / "foreign-parent"
        (aliases / "alias").mkdir(parents=True)
        self.add_mount(self.data / "rehearsal/spool/in", aliases / "alias")
        fixture = self.obs / "foreign-mounts.json"
        for source in (aliases, aliases / "alias"):
            fixture.write_text(json.dumps([{"Type": "bind", "Source": str(source)}]))
            self.call("preflight", "rehearsal", "deploy-001", expected=1, message="shares a selected target")
        volume_point = self.base / "daemon/volumes/named/_data"
        volume_point.mkdir(parents=True)
        fixture.write_text(json.dumps([{"Type": "volume", "Name": "named", "Source": str(volume_point)}]))
        volume = {"Name": "named", "Driver": "local", "Mountpoint": str(volume_point),
                  "Options": {"device": str(aliases / "alias"), "type": "none", "o": "bind"}}
        (self.obs / "volume.json").write_text(json.dumps(volume))
        self.call("deploy", "rehearse", "deploy-001", expected=1, message="shares a selected target")
        self.assertIn("docker volume", self.calls())
        volume["Options"] = None
        (self.obs / "volume.json").write_text(json.dumps(volume))
        self.call("preflight", "rehearsal", "deploy-001", message="read-only preflight passed")
        self.no_mutation()

    def test_refuses_foreign_bind_whose_source_cannot_be_resolved(self):
        hidden = self.base / "inaccessible"
        hidden.mkdir()
        wiki = self.data / "rehearsal/wiki"
        wiki.mkdir()
        alias = hidden / "alias"
        alias.symlink_to(wiki)
        fixture = self.obs / "foreign-mounts.json"
        fixture.write_text(json.dumps([{"Type": "bind", "Source": str(alias)}]))
        hidden.chmod(0)
        self.addCleanup(hidden.chmod, 0o700)
        self.call("deploy", "rehearse", "deploy-001", expected=1, message="cannot establish provenance")
        self.assertEqual(list(wiki.iterdir()), [])
        self.assertFalse((self.state("rehearsal") / "maintenance").exists())
        self.no_mutation()

    def test_runner_refuses_queue_links_and_nonregular_entries_without_reading(self):
        self.call("dev", "up")
        spool = self.data / "dev/spool"
        for name in ("in", "out"):
            (spool / name).mkdir()
        job = spool / "out/11111111-1111-1111-1111-111111111111"
        job.mkdir()
        foreign = self.base / "not-a-spool-file"
        self.private(foreign, "leave untouched")
        for path, create in (
            (spool / "in/unsafe.json", lambda p: p.symlink_to(foreign)),
            (spool / "in/unsafe.json", lambda p: os.mkfifo(p, 0o600)),
            (spool / "in/unsafe.json", lambda p: os.link(foreign, p)),
            (job / "result.json", lambda p: p.symlink_to(foreign)),
            (job / "stderr.log", lambda p: os.link(foreign, p)),
            (spool / "out/aliased-dir", lambda p: p.symlink_to(self.base)),
        ):
            with self.subTest(path=path):
                create(path)
                self.call("run-once", "dev", expected=1, message="unsafe queue entry")
                self.assertFalse(any(line.startswith("runner ") for line in self.calls()))
                self.assertEqual(foreign.read_text(), "leave untouched")
                self.assertTrue(os.path.lexists(path))
                path.unlink()
                (self.state("dev") / "pause").unlink()
        self.private(job / "result.json", "regular output")
        self.call("run-once", "dev", message="tick completed")

    def test_dev_stop_does_not_reparse_yaml_or_require_live_build_sources(self):
        self.call("dev", "up")
        (self.go / "go.sum").unlink()
        (self.repo / "compose.dev.yaml").write_text("[ broken YAML\n")
        self.call("dev", "down", message="data preserved")
        self.assertIn("down petar-dev", self.calls())
        self.assertFalse((self.obs / "petar-dev").exists())

    def test_reject_unexpected_runtime_network_and_missing_dev_sources(self):
        gomod = self.go / "go.mod"
        gomod.unlink()
        self.call("preflight", "dev", "deploy-001", expected=1, message="missing dev source")
        gomod.touch()
        migrations = self.go / "postgres/migrations"
        migrations.chmod(0o700)
        self.call("preflight", "dev", "deploy-001", expected=1, message="PostgreSQL cannot traverse")
        migrations.chmod(0o755)
        self.call("dev", "up")
        fixture = self.obs / "petar-dev"
        current = json.loads(fixture.read_text())
        next(c for c in current if c["Name"] == "/petar-dev-app-1")["NetworkSettings"]["Networks"]["foreign"] = {}
        fixture.write_text(json.dumps(current))
        self.call("run-once", "dev", expected=1, message="network attachments")
        self.assertFalse(any(s.startswith("runner ") for s in self.calls()))

    def test_failed_last_migration_never_marks_ready(self):
        self.set_flag("legacy-schema")
        self.call("deploy", "rehearse", "deploy-001", expected=1, message="schema 000008 effect not confirmed")
        self.assertIn("logs petar-rehearsal", self.calls())
        self.assertIn("exec petar-rehearsal", self.calls())
        self.assertFalse((self.state("rehearsal") / "ready").exists())
        self.call("run-once", "rehearsal", message="skipped (maintenance)")

    def test_conflict_after_pull_stops_before_target_preseed(self):
        self.set_flag("late-port")
        self.call("deploy", "rehearse", "deploy-001", expected=1, message="already listening")
        self.assertIn("docker pull", self.calls())
        self.assertNotIn("docker run", self.calls())
        self.assertNotIn("up petar-rehearsal", self.calls())
        self.assertFalse((self.data / "rehearsal/wiki").exists())
        self.assertFalse((self.go / "postgres/deploy/rehearsal/data").exists())

    def test_isolated_guard_rejects_container_and_imported_mount_before_setup(self):
        module = self.repo / "scripts/test-deploy-isolated.py"
        marker, machine, kernel = (self.obs / p for p in ("vm-marker", "machine-id", "kernel"))
        for path, value in ((marker, "fake-unique-id\n"), (machine, "fake-unique-id\n"), (kernel, "linux\n")):
            path.write_text(value)
        detection = self.tools / "systemd-detect-virt"
        module.write_text(module.read_text().replace('Path("/etc/petar-deploy-disposable")', f"Path({str(marker)!r})")
                          .replace('Path("/etc/machine-id")', f"Path({str(machine)!r})")
                          .replace('Path("/proc/sys/kernel/osrelease")', f"Path({str(kernel)!r})")
                          .replace('Path("/.dockerenv")', f"Path({str(self.obs / 'dockerenv')!r})")
                          .replace('Path("/run/.containerenv")', f"Path({str(self.obs / 'containerenv')!r})")
                          .replace('"/usr/bin/systemd-detect-virt"', repr(str(detection)))
                          .replace("mode.st_uid == 0", "mode.st_uid == os.getuid()"))
        self.mountinfo.write_text("10 1 8:1 / / rw - ext4 /dev/test rw\n")
        original = self.snapshot()
        self.executable(detection, '#!/bin/sh\n[ "$1" != --vm ] || { echo kvm; exit 0; }\necho docker\n')
        self.call("test-deploy", "isolated", expected=77, message="not a container or WSL")
        self.assertEqual(original, self.snapshot())
        self.executable(detection, '#!/bin/sh\n[ "$1" != --vm ] || { echo kvm; exit 0; }\nexit 1\n')
        self.add_mount("/foreign/subtree", self.go / "spool")
        self.call("test-deploy", "isolated", expected=77, message="mount isolation")
        self.assertEqual(original, self.snapshot())
        self.assertEqual(self.calls(), [])

    def test_up_uses_verified_snapshot_if_source_changes_during_seed(self):
        self.set_flag("edit-source-during-seed")
        self.call("deploy", "rehearse", "deploy-001", message="prepared, awaiting smoke")
        self.assertIn("ipc: host", (self.repo / "compose.yaml").read_text())
        frozen = self.state("rehearsal") / "runtime.compose.json"
        self.assertNotIn("ipc", json.loads(frozen.read_text())["services"]["app"])
        self.assertEqual(stat.S_IMODE(frozen.stat().st_mode), 0o600)

    def test_release_dev_and_isolation(self):
        self.call("deploy", "rehearse", "deploy-001", message="prepared, awaiting smoke")
        self.call("deploy", "init", "deploy-001", message="production: prepared, awaiting smoke")
        self.call("run-once", "rehearsal", message="tick completed")
        bundle = self.state("rehearsal") / "deploy-001"
        (bundle / "go/scripts/spool-runner.sh").write_text("changed")
        self.call("run-once", "rehearsal", expected=1, message="differs from verified archive")
        self.call("dev", "up")
        protected = (self.obs / "petar-rehearsal").read_bytes()
        sentinel = self.data / "dev/wiki/local"
        sentinel.write_text("preserve")
        self.call("run-once", "dev", message="tick completed")
        self.call("dev", "down")
        self.call("run-once", "dev", message="maintenance")
        self.call("dev", "up")
        self.assertEqual(sentinel.read_text(), "preserve")
        self.assertEqual(protected, (self.obs / "petar-rehearsal").read_bytes())
        self.assertIn(f"runner {self.data}/dev/spool http://127.0.0.1:8081/api/v1", self.calls())

    def test_serialization_and_durable_pause(self):
        self.call("dev", "up")
        self.call("deploy", "rehearse", "deploy-001")
        self.set_flag("hold")
        first = self.start("run-once", "dev")
        self.until(lambda: any(s.startswith("runner ") for s in self.calls()))
        self.call("run-once", "dev", message="skipped (busy)")
        down = self.start("dev", "down")
        self.until(lambda: (self.state("dev") / "maintenance").exists())
        second_down = self.start("dev", "down")
        self.call("run-once", "dev", message="skipped (busy)")
        self.assertNotIn("down petar-dev", self.calls())
        self.call("run-once", "rehearsal", message="tick completed")
        (self.obs / "hold").unlink()
        self.done(first)
        self.done(down)
        self.done(second_down, 1, "maintenance is paused")
        self.assertEqual(self.calls().count("down petar-dev"), 1)
        self.call("dev", "up")
        self.set_flag("unconfirmed")
        self.call("run-once", "dev", expected=1, message="outcome unconfirmed")
        self.assertTrue((self.state("dev") / "pause").exists())
        pending = self.data / "dev/spool/processing/pending.json"
        self.call("run-once", "dev", expected=1, message="runner paused")
        self.assertEqual(pending.read_text(), "synthetic")
        (self.state("dev") / "pause").unlink()
        (self.state("dev") / "inflight").unlink()
        self.call("run-once", "dev", expected=1, message="processing is not empty")
        self.assertEqual(pending.read_text(), "synthetic")

    def test_pause_survives_release_bundle_replacement(self):
        self.call("deploy", "rehearse", "deploy-001")
        self.set_flag("unconfirmed")
        self.call("run-once", "rehearsal", expected=1, message="outcome unconfirmed")
        previous = self.state("rehearsal") / "deploy-001"
        archive = previous.rename(self.base / "previous-release")
        shutil.copytree(archive, previous)
        (previous / "replacement-fixture").touch()
        self.assertTrue((self.state("rehearsal") / "pause").exists())
        self.call("run-once", "rehearsal", expected=1, message="runner paused")
        self.assertEqual(len([s for s in self.calls() if s.startswith("runner ")]), 1)

    def test_sigkill_does_not_let_runner_or_maintenance_overtake(self):
        self.call("dev", "up")
        self.set_flag("hold")
        first = self.start("run-once", "dev")
        self.until(lambda: any(s.startswith("runner ") for s in self.calls()))
        first.kill()
        first.wait(timeout=5)
        self.call("run-once", "dev", message="skipped (busy)")
        self.assertTrue((self.state("dev") / "inflight").exists())
        (self.obs / "hold").unlink()
        self.until(lambda: "runner paused" in self.probe_tick())
        self.assertEqual(len([s for s in self.calls() if s.startswith("runner ")]), 1)

    def test_sigkill_after_claim_preserves_processing_and_stops_next_tick(self):
        self.call("dev", "up")
        self.set_flag("hold")
        self.set_flag("claim-before-crash")
        spool = self.data / "dev/spool"
        (spool / "in").mkdir()
        queued = spool / "in/claimed.json"
        pending = spool / "processing/claimed.json"
        self.private(queued, "synthetic claimed job")
        first = self.start("run-once", "dev", own_group=True)
        try:
            self.until(pending.exists)
        finally:
            try:
                os.killpg(first.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.done(first, -signal.SIGKILL)
        self.assertFalse(queued.exists())
        self.assertEqual(pending.read_text(), "synthetic claimed job")
        self.call("run-once", "dev", expected=1, message="runner paused")
        # Even without the in-flight marker, the claim independently blocks a tick.
        (self.state("dev") / "inflight").unlink()
        self.call("run-once", "dev", expected=1, message="processing is not empty")
        self.assertTrue((self.state("dev") / "pause").exists())
        self.assertEqual(pending.read_text(), "synthetic claimed job")
        self.assertEqual(len([s for s in self.calls() if s.startswith("runner ")]), 1)

    def probe_tick(self):
        proc = self.start("run-once", "dev")
        _, err = proc.communicate(timeout=10)
        return err.decode()

    def test_cron_templates(self):
        cron = (REPO / "templates/petar-runner.cron").read_text()
        logrotate = (REPO / "templates/petar-runner.logrotate").read_text()
        self.assertIn("* * * * * eam /opt/petar-deploy/scripts/run-once.sh production", cron)
        self.assertIn("create 0600 eam eam", logrotate)
        self.assertIn("/opt/petar-data/production/operator/runner.log", cron)
        self.assertIn("/opt/petar-data/production/operator/runner.log", logrotate)
        log = self.state("production") / "runner.log"
        log.write_text("safe wrapper outcome\n")
        user = pwd.getpwuid(os.getuid()).pw_name
        group = grp.getgrgid(os.getgid()).gr_name
        config = self.base / "test.logrotate"
        config.write_text(logrotate.replace("/opt/petar-data/production/operator/runner.log", str(log))
                          .replace("eam eam", f"{user} {group}"))
        subprocess.run(["/usr/sbin/logrotate", "--force", "--state", str(self.base / "logrotate.state"), config],
                       check=True, capture_output=True)
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.assertEqual(log.with_suffix(".log.1").read_text(), "safe wrapper outcome\n")


if __name__ == "__main__":
    if os.getuid() != 1000 or os.getgid() != 1000:
        raise SystemExit("Run contracts as non-root UID/GID 1000")
    unittest.main(verbosity=2)
