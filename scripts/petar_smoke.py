"""Submit and verify two jobs through the release DokuWiki UI."""

from contextlib import contextmanager
from dataclasses import dataclass
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import subprocess
import sys
import time
from uuid import uuid4


def sibling(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ops = sibling("petar_operator")
BROWSER = Path("/opt/petar-smoke/chrome-headless-shell-linux64/chrome-headless-shell")
CHROMIUM = "148.0.7778.96"
PLAYWRIGHT = "1.60.0"
ACTIONS = ("echo-test", "fabric", "word-count", "yt-dlp")
STATUSES = {
    "Pending": "pending", "Dispatched": "dispatched", "In progress": "in_progress",
    "Completed": "completed", "Failed": "failed",
}
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")


class Failure(Exception):
    """Only raise with fixed diagnostic text or validated public metadata."""


def check(ok, reason):
    if not ok:
        raise Failure(reason)


def valid_id(value):
    return isinstance(value, str) and UUID.fullmatch(value) is not None


@dataclass
class Job:
    action: str
    field: str
    value: object
    input_id: str = ""
    process_id: str = ""
    job_id: str = ""
    status: str = "not_observed"

    def evidence(self):
        return (f"{self.action}: input_id={self.input_id or 'not_received'} "
                f"process_id={self.process_id or 'not_received'} "
                f"job_id={self.job_id or 'not_received'} status={self.status}")


class Smoke:
    def __init__(self, op, mode, browser, *, wait=None, confirm_restart=False):
        self.op, self.mode, self.browser = op, mode, browser
        self.confirm_restart = confirm_restart
        self.wait = wait if wait is not None else (40 if mode == "managed" else 150)
        self.stage = "target"
        self.jobs = (Job("echo-test", "echo", "petar-smoke-" + str(uuid4())), Job("word-count", "words", 3))

    def note(self, message):
        ops.say(f"{self.op.profile}: {self.stage}: {message}")

    def target(self):
        op = self.op
        op.check_context()
        op.check_paths(clean=False)
        op.verify_bundle()
        ops.private_file(op.state / "ready")
        check((op.state / "ready").read_bytes() == b"deploy-001 prepared; end-to-end smoke pending\n",
              "expected prepared deploy-001")
        ops.private_file(op.runtime_file)
        model = op.render(frozen=True)
        return op.require_running(model)

    def active(self):
        check(not any(self.op.marked(name) for name in ("maintenance", "pause")),
              "maintenance or runner pause is active")

    def queued(self, jobs=()):
        op = self.op
        check(not op.marked("inflight") and not op.check_queue(), "runner has unresolved work")
        inbox = op.spool / "in"
        names = {entry.name for entry in inbox.iterdir()} if inbox.exists() else set()
        expected = {job.job_id for job in jobs}
        check(names <= {job + suffix for job in expected for suffix in (".json", ".tmp")},
              "managed rehearsal queue contains unrelated work")
        return names == {job + ".json" for job in expected}

    def require_idle(self):
        with self.op.lock("tick.lock", 0) as tick:
            check(tick is not None and self.queued(), "managed rehearsal runner or queue is busy")
            result = self.op.compose(
                "exec", "-T", "postgres", "psql", "-XqAt", "-v", "ON_ERROR_STOP=1",
                "-U", "petar_user", "-d", "petar_db", "-c", "BEGIN READ ONLY; "
                "SELECT NOT EXISTS (SELECT 1 FROM public.sm_jobs "
                "WHERE status IN ('pending', 'dispatched', 'in_progress')); COMMIT;",
                frozen=True, timeout=15, allow_fail=True,
            )
            check(not result.returncode and result.stdout.strip() == b"t",
                  "database queue has unfinished work or could not be checked")

    def api(self, *, boot=False):
        op = self.op
        self.stage = "API readiness"
        if boot:
            deadline = time.monotonic() + 90
        while True:
            response = op.cmd(op.http_args("/readyz", False), timeout=8, allow_fail=True)
            if not response.returncode and json.loads(response.stdout).get("status") == "ready":
                break
            check(boot and time.monotonic() < deadline, "API is not ready")
            time.sleep(1)
        self.stage = "API catalog"
        response = op.cmd(op.http_args("/api/v1/actions", False), timeout=8, allow_fail=True)
        check(not response.returncode, "actions endpoint unavailable")
        rows = json.loads(response.stdout)
        check(isinstance(rows, list) and len(rows) == len(ACTIONS)
              and all(type(row["version"]) is int and row["version"] == 1 for row in rows)
              and sorted(row["action"] for row in rows) == list(ACTIONS), "unexpected action catalog")
        self.note("ready; echo-test, fabric, word-count, yt-dlp at version 1")

    def observe(self, session, *, phase, queue=False):
        deadline = time.monotonic() + self.wait
        self.stage = f"{phase} journal"
        while True:
            self.active()
            rows = session.journal(deadline, self.jobs)
            for job in self.jobs:
                own = [row for row in rows if row[1] == job.process_id]
                check(len(own) <= 1, "multiple rows for one smoke process")
                if not own:
                    continue
                job_id, _, action, label = own[0]
                check(valid_id(job_id) and (not job.job_id or job_id == job.job_id)
                      and all(job_id not in (j.input_id, j.process_id) for j in self.jobs)
                      and all(j is job or j.job_id != job_id for j in self.jobs),
                      "invalid or changed journal job ID")
                job.job_id = job_id
                job.status = STATUSES.get(label, "unknown")
                check(action == job.action and job.status != "unknown", "journal row type or status mismatch")
                check(job.status != "failed", "job failed")
            if queue:
                if all(j.job_id for j in self.jobs):
                    with self.op.lock("tick.lock", 0) as tick:
                        check(tick is not None, "another runner is active on managed rehearsal")
                        if self.queued(self.jobs):
                            return
            elif all(j.status == "completed" for j in self.jobs):
                # Do not treat a previously completed status as a current row.
                if all(any(r[1] == j.process_id for r in rows) for j in self.jobs):
                    return
            check(time.monotonic() < deadline, "timed out waiting for the smoke jobs")
            time.sleep(min(1, max(0, deadline - time.monotonic())))

    def publish(self, session, phase):
        self.observe(session, phase=phase)
        for job in self.jobs:
            self.stage = f"{phase} publication {job.action}"
            found = session.publication(job)
            check(isinstance(found, dict) and found.get("type") == job.action
                  and type(found.get("version")) is int and found["version"] == 1
                  and isinstance(found.get("data"), dict)
                  and type(found["data"].get(job.field)) is type(job.value)
                  and found["data"][job.field] == job.value, "incorrect wiki publication")
            self.note(f"{job.evidence()} GET job_result=200 type={job.action} version=1 "
                      f"data.{job.field}={job.value} (exact match)")

    def wait_wiki(self):
        self.stage = "wiki readiness after restart"
        args = self.op.http_args("/doku.php?id=petar_check&do=login", True)
        deadline = time.monotonic() + 90
        while True:
            response = self.op.cmd(args, timeout=8, allow_fail=True)
            if not response.returncode and re.search(rb'<form\b[^>]*\bid="dw__login"', response.stdout):
                self.note("restricted DokuWiki login page available")
                return
            check(time.monotonic() < deadline, "wiki login page is not ready")
            time.sleep(1)

    def restart(self, control, started):
        self.stage = "rehearsal restart"
        op = self.op
        self.active()
        with op.maintenance_under_control(control) as fds:
            check(not any(op.marked(name) for name in ("pause", "inflight")),
                  "runner outcome needs reconciliation")
            before = self.target()
            check(identity(before) == started, "verified services changed during smoke")
            op.compose("restart", "--timeout", "30", "postgres", "app", "dokuwiki",
                       frozen=True, fds=fds, timeout=150)
            after = self.target()
            check(after.keys() == before.keys(), "service set changed during restart")
            for name, obj in sorted(after.items()):
                old = before[name]["State"]["StartedAt"]
                new = obj["State"]["StartedAt"]
                check(obj["Id"] == before[name]["Id"] and valid_timestamp(old)
                      and valid_timestamp(new) and old != new, "service did not demonstrably restart")
                self.note(f"{name} StartedAt {old} -> {new} (same container)")
            self.api(boot=True)
            self.wait_wiki()
            op.clear("maintenance")

    def run(self):
        op = self.op
        op.config()
        with op.lock("control.lock", ops.LOCK_WAIT) as control:
            check(control is not None, "another control operation is active")
            self.active()
            started = identity(self.target())
            if self.mode == "managed":
                self.stage = "managed runner"
                if not self.confirm_restart:
                    marker = op.state / "smoke-managed"
                    ops.private_file(marker)
                    check(marker.read_bytes() == b"dedicated rehearsal runner\n",
                          "expected explicit dedicated rehearsal runner")
                op.check_tools(False)
                self.require_idle()
            self.api()
            self.stage = "browser prerequisite"
            with self.browser(op) as browser:
                self.stage = "wiki login before submit"
                with browser.session() as session:
                    if self.mode == "managed":
                        self.stage = "managed runner"
                        self.require_idle()
                    for job in self.jobs:
                        self.stage = f"wiki submit {job.action}"
                        self.active()
                        session.submit(job)
                        ids = [i for j in self.jobs for i in (j.input_id, j.process_id) if i]
                        check(valid_id(job.input_id) and valid_id(job.process_id)
                              and len(set(ids)) == len(ids), "invalid or repeated submission ID")
                        self.note(f"{job.evidence()} POST submit=201")
                    if self.mode == "managed":
                        self.observe(session, phase="runner preparation", queue=True)
                        self.stage = "runner pass"
                        self.active()
                        tick = op.cmd(["timeout", "--kill-after=5s", "60s", ops.ROOT / "scripts/run-once.sh",
                                       "rehearsal"], env=op.runner_env, timeout=70, allow_fail=True)
                        check(not tick.returncode and tick.stdout.strip() == b"rehearsal: tick completed",
                              "managed runner did not confirm a completed pass")
                    self.publish(session, "before restart" if op.profile == "rehearsal" else "production")
                if op.profile == "rehearsal":
                    self.restart(control, started)
                    self.stage = "wiki login after restart"
                    with browser.session() as session:
                        self.publish(session, "after restart")
        self.stage = "complete"
        self.note("smoke passed; exactly two submissions; installation marker unchanged")


def identity(containers):
    return {name: (obj["Id"], obj["State"]["StartedAt"]) for name, obj in containers.items()}


def valid_timestamp(text):
    return isinstance(text, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?Z", text)


def main(args, operator=ops.Operator, browser=None, *, wait=None):
    smoke = None
    try:
        modes = {
            ("rehearsal", "deploy-001"): "managed",
            ("rehearsal", "deploy-001", "--confirm-restart"): "managed",
            ("rehearsal", "deploy-001", "--scheduled"): "scheduled",
            ("production", "deploy-001"): "scheduled",
        }
        mode = modes.get(tuple(args))
        check(mode is not None,
              "usage: smoke.sh rehearsal deploy-001 [--scheduled|--confirm-restart] | production deploy-001")
        smoke = Smoke(operator(args[0]), mode,
                      browser or (lambda op: sibling("petar_smoke_browser").chromium(op)), wait=wait,
                      confirm_restart=tuple(args) == ("rehearsal", "deploy-001", "--confirm-restart"))
        smoke.run()
        return 0
    except Failure as err:
        detail = str(err)
    except KeyboardInterrupt:
        detail = "interrupted"
    except Exception:
        detail = "verification failed; no raw diagnostics printed"
    prefix = f"{args[0]}: {smoke.stage}" if smoke else "DEPLOY-07: arguments/startup"
    print(f"{prefix}: FAILED: {detail}", file=sys.stderr)
    if smoke:
        for job in smoke.jobs:
            print(job.evidence(), file=sys.stderr)
        if smoke.stage.startswith("wiki submit"):
            print("Submit was not retried; creation may be unknown. Reconcile in the wiki before re-running.",
                  file=sys.stderr)
    else:
        print("No submission ID received.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.modules["petar_smoke"] = sys.modules[__name__]
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    sys.exit(main(sys.argv[1:]))
