"""DEPLOY-07 control-flow contracts; no Docker, network or browser installation."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import importlib.util
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID


spec = importlib.util.spec_from_file_location("petar_smoke", Path(__file__).with_name("petar_smoke.py"))
smoke = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = smoke
spec.loader.exec_module(smoke)

PASSWORD, COOKIE, TOKEN = "fixture-password-987", "fixture-cookie-543", "fixture-csrf-210"
PRIVATE = " ".join((PASSWORD, COOKIE, TOKEN))


def uid(n):
    return str(UUID(int=n, version=4))


class Fixture:
    def __init__(self, root, defect=""):
        self.root, self.defect = root, defect
        self.events = []
        self.submits = 0
        self.sessions = 0
        self.starts = 1
        self.locks = set()
        self.state = root / "operator"
        self.state.mkdir()
        self.spool = root / "spool"
        (self.spool / "in").mkdir(parents=True)
        (self.state / "smoke-managed").write_text("dedicated rehearsal runner\n")
        (self.state / "ready").write_text("deploy-001 prepared; end-to-end smoke pending\n")
        self.runtime_file = self.state / "runtime.compose.json"
        self.runtime_file.touch()
        self.runner_env = {}
        self.local = {"WIKI_CHECK_PASSWORD": PASSWORD}
        self.db_checks = 0
        if defect in ("pause", "maintenance"):
            (self.state / defect).touch()
        if defect == "queue":
            (self.spool / "in" / f"{uid(300)}.json").touch()
        if defect == "no marker":
            (self.state / "smoke-managed").unlink()

    def operator(self, profile):
        self.profile = profile
        self.events.append(("operator", profile))
        return self

    def config(self):
        pass

    @contextmanager
    def lock(self, name, _wait):
        assert name not in self.locks
        if name == "tick.lock" and self.defect == "runner locked":
            yield None
            return
        self.locks.add(name)
        self.events.append(("lock", name))
        try:
            yield 9
        finally:
            self.locks.remove(name)

    def check_context(self):
        self.events.append(("context",))

    def check_paths(self, *, clean):
        assert not clean

    def verify_bundle(self):
        self.events.append(("bundle",))

    def render(self, *, frozen):
        assert frozen
        return "frozen"

    def require_running(self, model):
        assert model == "frozen"
        if self.defect == "target":
            raise smoke.ops.Refusal(PRIVATE)
        self.events.append(("verify", self.starts))
        return {name: {"Id": name, "State": {"StartedAt": f"2026-11-22T00:00:0{self.starts}Z"}}
                for name in ("postgres", "app", "dokuwiki")}

    def marked(self, name):
        return (self.state / name).exists()

    def clear(self, name):
        assert self.locks == {"control.lock", "tick.lock"}
        (self.state / name).unlink()

    def check_tools(self, _report):
        self.events.append(("tools",))

    def check_queue(self):
        assert "tick.lock" in self.locks
        return False

    def http_args(self, path, wiki):
        assert wiki == path.startswith("/doku.php")
        return [path]

    def cmd(self, args, **_opts):
        if args == ["/readyz"]:
            return SimpleNamespace(returncode=0, stdout=b'{"status":"ready"}')
        if args == ["/doku.php?id=petar_check&do=login"]:
            assert self.locks == {"control.lock", "tick.lock"}
            self.events.append(("wiki ready",))
            return SimpleNamespace(returncode=0, stdout=b'<form id="dw__login"></form>')
        if args == ["/api/v1/actions"]:
            rows = [{"action": a, "version": 1} for a in smoke.ACTIONS]
            if self.defect == "wrong catalog":
                rows[0]["action"] = "other"
            if self.defect == "duplicate catalog":
                rows[0] = rows[1]
            if self.defect == "missing catalog":
                return SimpleNamespace(returncode=22, stdout=PRIVATE.encode())
            return SimpleNamespace(returncode=0, stdout=json.dumps(rows).encode())
        assert self.profile == "rehearsal" and args[-2:] == [smoke.ops.ROOT / "scripts/run-once.sh", "rehearsal"]
        assert self.locks == {"control.lock"}
        assert sorted(p.name for p in (self.spool / "in").iterdir()) == [uid(n) + ".json" for n in (3, 6)]
        self.events.append(("runner",))
        return SimpleNamespace(returncode=0, stdout=b"rehearsal: tick completed\n" if self.defect != "busy tick"
                               else b"rehearsal: tick skipped (busy)\n")

    @contextmanager
    def maintenance_under_control(self, control):
        assert control == 9 and self.locks == {"control.lock"}
        self.events.append(("maintenance",))
        (self.state / "maintenance").touch(exist_ok=False)
        with self.lock("tick.lock", 0) as tick:
            yield (control, tick)

    def compose(self, *args, **options):
        assert self.profile == "rehearsal" and options["frozen"]
        assert self.locks == {"control.lock", "tick.lock"}
        if args[:4] == ("exec", "-T", "postgres", "psql"):
            assert "BEGIN READ ONLY" in args[-1] and "public.sm_jobs" in args[-1]
            assert all(status in args[-1] for status in ("pending", "dispatched", "in_progress"))
            self.db_checks += 1
            self.events.append(("db queue",))
            busy = self.defect == "database queue" or self.defect == "late database queue" and self.db_checks == 2
            return SimpleNamespace(returncode=int(self.defect == "database error"),
                                   stdout=b"f\n" if busy else b"t\n")
        assert options["fds"] == (9, 9)
        assert args == ("restart", "--timeout", "30", "postgres", "app", "dokuwiki")
        self.events.append(("restart",))
        if self.defect != "no restart":
            self.starts += 1

    @contextmanager
    def browser(self, op):
        assert op is self
        self.events.append(("browser",))
        if self.defect == "no browser":
            raise RuntimeError(PRIVATE)
        yield self

    @contextmanager
    def session(self):
        self.sessions += 1
        self.events.append(("new session", self.sessions))
        try:
            yield self
        finally:
            self.events.append(("close session", self.sessions))

    def submit(self, job):
        assert self.sessions == 1 and self.locks == {"control.lock"}
        self.submits += 1
        self.events.append(("submit", job.action))
        if self.defect == "submit timeout" and self.submits == 2:
            raise TimeoutError(PRIVATE)
        job.input_id = uid(self.submits * 3 - 2)
        job.process_id = uid(self.submits * 3 - 1)

    def journal(self, _deadline, jobs):
        assert self.locks == {"control.lock"}
        self.events.append(("journal", self.sessions))
        for i, _ in enumerate(jobs, 1):
            (self.spool / "in" / f"{uid(i * 3)}.json").touch()
        if self.defect == "foreign queue":
            (self.spool / "in" / f"{uid(300)}.json").touch()
        status = {"runner stopped": "Pending", "job failed": "Failed"}.get(self.defect, "Completed")
        stale = [uid(111), uid(112), "echo-test", "Completed"]
        rows = [stale] + [[uid(i * 3), j.process_id, j.action, status] for i, j in enumerate(jobs, 1)]
        if self.defect == "duplicate journal":
            rows.append(rows[1])
        if self.defect == "changed ID" and self.sessions == 2:
            rows[1][0] = uid(222)
        return rows

    def publication(self, job):
        assert self.locks == {"control.lock"}
        self.events.append(("result", self.sessions, job.job_id))
        if self.defect == "publication unavailable":
            raise smoke.Failure("wiki job_result HTTP 410")
        if self.defect == "raw exception":
            raise ValueError(PRIVATE)
        value = "old-smoke-marker" if self.defect == "stale publication" else job.value
        return {"type": job.action, "version": True if self.defect == "wrong version" else 1,
                "data": {job.field: value}}


class Contracts(unittest.TestCase):
    def run_smoke(self, args, defect=""):
        with tempfile.TemporaryDirectory(prefix="petar-contract-") as tmp, \
                patch.object(smoke.ops, "private_file", lambda p: p.stat()):
            fixture = Fixture(Path(tmp), defect)
            out, err = StringIO(), StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = smoke.main(args, fixture.operator, fixture.browser, wait=0)
            # This transcript is the only data that can be copied into an acceptance report.
            transcript = out.getvalue() + err.getvalue()
            for secret in (*PRIVATE.split(), uid(111), uid(112), uid(300), "old-smoke-marker"):
                self.assertNotIn(secret, transcript)
            self.assertEqual("smoke passed" in transcript, code == 0)
            return code, transcript, fixture.events

    def failure(self, defect, stage, *, args=("production", "deploy-001"), submits=2):
        code, transcript, events = self.run_smoke(args, defect)
        self.assertNotEqual(code, 0)
        self.assertIn(f"{stage}: FAILED", transcript)
        self.assertEqual(sum(event[0] == "submit" for event in events), submits)
        self.assertNotIn(("restart",), events)
        return transcript, events

    def test_invalid_public_commands(self):
        for args in (["dev", "deploy-001"], ["production", "wrong"],
                     ["production", "deploy-001", "--scheduled"], ["rehearsal", "deploy-001", "--force"], []):
            with self.subTest(args=args):
                self.assertEqual(self.run_smoke(args)[2], [])
                public = subprocess.run(["/bin/bash", str(smoke.ops.ROOT / "scripts/smoke.sh"), *args],
                                        capture_output=True, timeout=5)
                self.assertNotEqual(public.returncode, 0)
                self.assertIn(b"No submission ID received", public.stderr)

    def test_retest_wrapper_requires_restart_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            retest = root / "retest-rehearsal.sh"
            retest.write_bytes((smoke.ops.ROOT / "scripts" / retest.name).read_bytes())
            (root / "smoke.sh").write_text("printf '%s\\n' \"$@\"\n")
            call = ["/bin/bash", str(retest)]
            for args in ([], ["--scheduled"], ["--confirm-restart", "production"]):
                refused = subprocess.run(call + args, capture_output=True, timeout=5)
                self.assertEqual(refused.returncode, 2)
                self.assertEqual(refused.stdout, b"")
            confirmed = subprocess.run(call + ["--confirm-restart"], capture_output=True, timeout=5)
            self.assertEqual(confirmed.returncode, 0)
            self.assertEqual(confirmed.stdout, b"rehearsal\ndeploy-001\n--confirm-restart\n")

    def test_confirmed_retest_without_marker_uses_same_jobs_after_restart(self):
        args = ("rehearsal", "deploy-001", "--confirm-restart")
        code, transcript, events = self.run_smoke(args, "no marker")
        self.assertEqual(code, 0, transcript)
        self.assertEqual(events.count(("db queue",)), 2)
        self.assertEqual(events.count(("runner",)), 1)
        self.assertEqual(events.count(("restart",)), 1)
        self.assertEqual([e for e in events if e[0] == "result"],
                         [("result", session, uid(job)) for session in (1, 2) for job in (3, 6)])
        self.failure("no marker", "managed runner", args=args[:-1], submits=0)

    def test_retest_refuses_work_before_any_submission(self):
        args = ("rehearsal", "deploy-001", "--confirm-restart")
        for defect in ("queue", "runner locked", "database queue", "late database queue", "database error"):
            with self.subTest(defect=defect):
                self.failure(defect, "managed runner", args=args, submits=0)
        for defect in ("maintenance", "pause"):
            with self.subTest(defect=defect):
                self.failure(defect, "target", args=args, submits=0)

    def test_production_success_never_runs_tick_or_restart(self):
        code, transcript, events = self.run_smoke(("production", "deploy-001"))
        self.assertEqual(code, 0, transcript)
        self.assertEqual([e for e in events if e[0] in ("runner", "restart", "maintenance")], [])
        self.assertEqual([e for e in events if e[0] == "result"], [("result", 1, uid(3)), ("result", 1, uid(6))])

    def test_rehearsal_managed_and_scheduled(self):
        for flag in ((), ("--scheduled",)):
            with self.subTest(flag=flag):
                code, transcript, events = self.run_smoke(("rehearsal", "deploy-001", *flag))
                self.assertEqual(code, 0, transcript)
                self.assertEqual(events.count(("runner",)), int(not flag))
                self.assertEqual(events.count(("restart",)), 1)
                self.assertLess(events.index(("close session", 1)), events.index(("restart",)))
                self.assertLess(events.index(("restart",)), events.index(("new session", 2)))
                self.assertLess(events.index(("wiki ready",)), events.index(("new session", 2)))
                self.assertEqual([e for e in events if e[0] == "result"],
                                 [("result", n, uid(j)) for n in (1, 2) for j in (3, 6)])
                self.assertEqual(sum(e[0] == "submit" for e in events), 2)

    def test_catalog_unavailable_or_wrong(self):
        for defect in ("missing catalog", "wrong catalog", "duplicate catalog"):
            with self.subTest(defect=defect):
                transcript, events = self.failure(defect, "API catalog", submits=0)
                self.assertIn("input_id=not_received", transcript)
                self.assertNotIn(("browser",), events)

    def test_target_pause_maintenance_browser_before_submit(self):
        for defect, stage in (("target", "target"), ("pause", "target"),
                              ("maintenance", "target"), ("no browser", "browser prerequisite")):
            with self.subTest(defect=defect):
                self.failure(defect, stage, submits=0)

    def test_stopped_runner_is_bounded_and_retains_own_ids(self):
        transcript, events = self.failure("runner stopped", "production journal")
        self.assertIn(f"input_id={uid(1)} process_id={uid(2)} job_id={uid(3)} status=pending", transcript)
        self.assertIn("timed out", transcript)
        self.assertNotIn(("runner",), events)

    def test_failed_job_is_immediate(self):
        transcript, _ = self.failure("job failed", "production journal")
        self.assertIn("status=failed", transcript)
        self.assertIn("job failed", transcript)
        self.assertNotIn("timed out", transcript)

    def test_missing_publication_and_stale_foreign_rows(self):
        for defect in ("publication unavailable", "stale publication", "wrong version", "raw exception"):
            with self.subTest(defect=defect):
                transcript, events = self.failure(defect, "production publication echo-test")
                self.assertIn(f"job_id={uid(3)} status=completed", transcript)
                self.assertIn(("result", 1, uid(3)), events)

    def test_duplicate_row_for_same_process_is_rejected(self):
        transcript, _ = self.failure("duplicate journal", "production journal")
        self.assertIn("multiple rows", transcript)

    def test_submit_not_retried_even_when_outcome_unknown(self):
        transcript, _ = self.failure("submit timeout", "wiki submit word-count")
        self.assertIn(f"input_id={uid(1)} process_id={uid(2)}", transcript)
        self.assertIn("creation may be unknown", transcript)

    def test_managed_runner_refuses_foreign_queue_or_unconfirmed_tick(self):
        for defect, stage, count in (("queue", "managed runner", 0),
                                     ("foreign queue", "runner preparation journal", 2),
                                     ("busy tick", "runner pass", 2)):
            with self.subTest(defect=defect):
                _, events = self.failure(defect, stage, args=("rehearsal", "deploy-001"), submits=count)
                self.assertEqual(events.count(("runner",)), int(defect == "busy tick"))

    def test_rehearsal_requires_a_restart_and_same_job_ids(self):
        for defect, stage in (("no restart", "rehearsal restart"), ("changed ID", "after restart journal")):
            with self.subTest(defect=defect):
                code, transcript, events = self.run_smoke(("rehearsal", "deploy-001", "--scheduled"), defect)
                self.assertNotEqual(code, 0)
                self.assertIn(f"{stage}: FAILED", transcript)
                self.assertEqual(sum(e[0] == "submit" for e in events), 2)
                self.assertEqual(events.count(("restart",)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
