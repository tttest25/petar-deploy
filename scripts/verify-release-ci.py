"""Run release acceptance on an existing, attested VM; never relay raw logs."""

import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "releases/001.env"
SHA = r"[0-9a-f]{64}"
GIT_SHA = r"[0-9a-f]{40}"
ID = r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
IDS = rf"input_id=({ID}) process_id=({ID}) job_id="
ACTIONS = ("echo-test", "word-count")
SERVICES = {"app": "APP_IMAGE", "postgres": "POSTGRES_IMAGE", "dokuwiki": "DOKUWIKI_IMAGE"}
STAMP = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?Z"
REF = rf"[a-z0-9./_-]+@sha256:{SHA}"
LIMIT = 65536
CLEANUP = "isolated: verified no rehearsal containers or Compose network; fixture data preserved"
SUCCESS = "isolated: passed (real release, real Chromium, two submissions and post-restart reads)"
GUEST_STAGES = ("attestation", "test fixtures", "public preflight", "public installation",
                "public browser smoke", "release evidence", "cleanup")
SMOKE_STAGES = ("target", "managed runner", "API readiness", "API catalog", "browser prerequisite",
                "wiki login before submit", "wiki journal capacity", "wiki submit echo-test", "wiki submit word-count",
                "runner preparation journal", "runner pass", "before restart journal",
                "before restart publication echo-test", "before restart publication word-count",
                "rehearsal restart", "wiki readiness after restart", "wiki login after restart",
                "after restart journal", "after restart publication echo-test",
                "after restart publication word-count")


class Failed(Exception):
    def __init__(self, code=1):
        self.code = code if 0 < code < 256 else 1


class Report:
    def __init__(self, path):
        self.path = path
        self.stage = "revision"
        with self.path.open("x", encoding="ascii"):
            pass

    def write(self, line):
        print(line, flush=True)
        with self.path.open("a", encoding="ascii") as file:
            file.write(line + "\n")

    def begin(self, stage):
        self.stage = stage
        self.write("stage=" + stage)


def smoke_evidence(raw, report, manifest, code):
    """Full-line allowlist, not a redaction pass. Keep the actual smoke status."""
    findings = {}
    repeated = False

    def observe(key, value, line):
        nonlocal repeated
        repeated |= key in findings
        findings[key] = value
        report.write(line)

    if len(raw) <= LIMIT:
        for line in raw.decode("ascii", errors="replace").split("\n"):
            if match := re.fullmatch(rf"release evidence: (manifest|archive go|archive plugin) ({SHA})", line):
                observe(match[1], match[2], line)
            elif match := re.fullmatch(rf"release evidence: image (app|postgres|dokuwiki) ({REF}) sha256:{SHA}", line):
                observe(("image", match[1]), match[2], line)
            elif match := re.fullmatch(r"release evidence: size (app|postgres|dokuwiki) ([0-9]{1,18}) ([0-9]{1,18})", line):
                observe(("size", match[1]), match.groups()[1:], line)
            elif match := re.fullmatch(
                    rf"rehearsal: wiki submit (echo-test|word-count): \1: {IDS}"
                    r"not_received status=not_observed POST submit=201", line):
                observe(("submit", match[1]), match.groups()[1:], line)
            elif match := re.fullmatch(
                    rf"rehearsal: (before|after) restart publication (echo-test|word-count): \2: {IDS}"
                    rf"({ID}) status=completed GET job_result=200 type=\2 version=1 "
                    rf"data\.(echo=petar-smoke-{ID}|words=3) \(exact match\)", line):
                observe((match[1], match[2]), match.groups()[2:], line)
            elif match := re.fullmatch(
                    rf"rehearsal: rehearsal restart: (app|postgres|dokuwiki) StartedAt ({STAMP}) -> "
                    rf"({STAMP}) \(same container\)", line):
                observe(("restart", match[1]), match[2] != match[3], line)
            elif line in (CLEANUP, SUCCESS):
                observe(line, True, line)
            elif match := re.fullmatch(r"isolated: (?:FAILED|BLOCKED) \(([^)]+)\): .*", line):
                if match[1] in GUEST_STAGES:
                    observe(("failed", match[1]), False, "guest_failure_stage=" + match[1])
            elif match := re.fullmatch(r"rehearsal: ([^:]+): FAILED: .*", line):
                if match[1] in SMOKE_STAGES:
                    observe(("failed", match[1]), False, "browser_failure_stage=" + match[1])
    if code:
        raise Failed(code)
    pins = dict(line.split("=", 1) for line in manifest.decode("ascii").splitlines()
                if line and not line.startswith("#"))
    ok = (not repeated and len(findings) == 20
          and findings.get("manifest") == hashlib.sha256(manifest).hexdigest()
          and all(findings.get("archive " + component) == pins[component.upper() + "_ARCHIVE_SHA256"]
                  for component in ("go", "plugin"))
          and all(findings.get(("image", service)) == pins[key] and findings.get(("restart", service))
                  and findings.get(("size", service))
                  for service, key in SERVICES.items())
          and findings.get(CLEANUP) and findings.get(SUCCESS))
    ids = set()
    for action, result in zip(ACTIONS, ("echo=petar-smoke-", "words=3")):
        before = findings.get(("before", action))
        ok = (ok and before and before == findings.get(("after", action))
              and before[:2] == findings.get(("submit", action)) and before[3].startswith(result))
        if before:
            ids.update(before[:3])
    if not ok or len(ids) != 6:
        report.begin("guest-evidence")
        raise Failed()


def git(*args):
    env = {"GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1"}
    result = subprocess.run(["/usr/bin/git", "--no-optional-locks", "-c", "core.fsmonitor=false", *args],
                            cwd=ROOT, env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=30)
    if result.returncode:
        raise Failed()
    return result.stdout


def check_revision(head):
    if not re.fullmatch(GIT_SHA, head):
        raise Failed()
    if (Path(os.fsdecode(git("rev-parse", "--show-toplevel").removesuffix(b"\n"))) != ROOT
            or git("rev-parse", "HEAD") != head.encode("ascii") + b"\n"):
        raise Failed()
    # Reject index flags that can hide edits, as well as local/ignored files.
    if any(not entry.startswith(b"H ") for entry in git("ls-files", "-vz").split(b"\0") if entry):
        raise Failed()
    if git("status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching",
           "--ignore-submodules=none"):
        raise Failed()


def acceptance(report, manifest):
    report.begin("guest-smoke")
    with tempfile.TemporaryFile() as raw:
        # The harness bounds individual stages and performs its own cleanup.
        result = subprocess.run(["/bin/bash", "scripts/test-smoke.sh", "isolated"], cwd=ROOT, env={},
                                stdin=subprocess.DEVNULL, stdout=raw, stderr=subprocess.STDOUT)
        raw.seek(0)
        smoke_evidence(raw.read(LIMIT + 1), report, manifest, result.returncode)


def verify(args, report):
    report.begin("revision")
    check_revision(args.head)
    report.write("checkout_sha=" + args.head)
    manifest = git("show", f"{args.head}:{MANIFEST}")
    acceptance(report, manifest)
    report.begin("revision-after-smoke")
    check_revision(args.head)


def execute(args, report):
    try:
        verify(args, report)
    except Failed as err:
        code = err.code
    except subprocess.TimeoutExpired:
        code = 124
    except KeyboardInterrupt:
        code = 130
    except Exception:
        code = 1
    else:
        report.write("result=passed exit=0")
        return 0
    report.write(f"result=failed stage={report.stage} exit={code}")
    return code


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--head", required=True)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--resources-reviewed", action="store_true", required=True,
                        help="operator checked sizes and obtained required approvals")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        path = args.report.parent.resolve(strict=True) / args.report.name
        if path.is_relative_to(ROOT):
            parser.error("--report must be outside the checkout")
        report = Report(path)
    except (OSError, RuntimeError):
        parser.error("--report must be a new file in an existing writable directory")
    return execute(args, report)


if __name__ == "__main__":
    raise SystemExit(main())
