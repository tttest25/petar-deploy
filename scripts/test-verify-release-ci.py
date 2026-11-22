"""Offline contracts for the manual release verifier."""

from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("verify_release_ci",
                                             Path(__file__).with_name("verify-release-ci.py"))
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)
MANIFEST = (ci.ROOT / ci.MANIFEST).read_bytes()
PINS = dict(line.split("=", 1) for line in MANIFEST.decode().splitlines()
            if line and not line.startswith("#"))
MARKER = "synthetic-password-never-publish"


def uuid(number):
    return f"00000000-0000-4000-a000-{number:012x}"


def transcript():
    lines = []
    for action, n in zip(ci.ACTIONS, (1, 4)):
        lines.append(f"rehearsal: wiki submit {action}: {action}: input_id={uuid(n)} "
                     f"process_id={uuid(n + 1)} job_id=not_received status=not_observed POST submit=201")
    for phase in ("before", "after"):
        if phase == "after":
            for service in ci.SERVICES:
                lines.append(f"rehearsal: rehearsal restart: {service} StartedAt "
                             "2026-01-01T00:00:00Z -> 2026-01-01T00:00:01Z (same container)")
        for action, n, result in zip(ci.ACTIONS, (1, 4), ("echo=petar-smoke-" + uuid(7), "words=3")):
            lines.append(f"rehearsal: {phase} restart publication {action}: {action}: input_id={uuid(n)} "
                         f"process_id={uuid(n + 1)} job_id={uuid(n + 2)} status=completed "
                         f"GET job_result=200 type={action} version=1 data.{result} (exact match)")
    lines.append("release evidence: manifest " + hashlib.sha256(MANIFEST).hexdigest())
    for key in ("go", "plugin"):
        lines.append(f"release evidence: archive {key} {PINS[key.upper() + '_ARCHIVE_SHA256']}")
    for service, key in ci.SERVICES.items():
        lines.append(f"release evidence: image {service} {PINS[key]} sha256:{'a' * 64}")
        lines.append(f"release evidence: size {service} 123456 789")
    return "\n".join([*lines, ci.CLEANUP, ci.SUCCESS]) + "\n"


class DiagnosticTest(unittest.TestCase):
    def run_guest(self, body, status):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "report"
            console = io.StringIO()
            (root / "transcript.txt").write_text(body)
            (root / "scripts").mkdir()
            (root / "scripts/test-smoke.sh").write_text(
                'test "$#:$1" = "1:isolated" || exit 99\n'
                f"printf '%s\\n' '{MARKER}'\n"
                f"/bin/cat transcript.txt >&2\nexit {status}\n")

            def verify(_args, report):
                ci.acceptance(report, MANIFEST)

            with (patch.object(ci, "ROOT", root), patch.object(ci, "verify", verify),
                  redirect_stdout(console)):
                actual = ci.execute(None, ci.Report(path))
            log = path.read_text()
            self.assertEqual(log, console.getvalue())
            self.assertNotIn(MARKER, log)
            return actual, log

    def test_failure_preserves_status_and_stage_despite_cleanup(self):
        failure = f"isolated: FAILED (public browser smoke): {MARKER}"
        browser = f"rehearsal: runner pass: FAILED: {MARKER}"
        code, log = self.run_guest(f"{MARKER}\n{failure}\n{browser}\n{ci.CLEANUP}", 23)
        self.assertEqual(code, 23)
        self.assertIn("guest_failure_stage=public browser smoke", log)
        self.assertIn("browser_failure_stage=runner pass", log)
        self.assertIn("exit=23", log)

    def test_attestation_refusal_is_not_skip(self):
        code, log = self.run_guest(f"isolated: BLOCKED (attestation): {MARKER}", 77)
        self.assertEqual(code, 77)
        self.assertIn("guest_failure_stage=attestation", log)

    def test_journal_capacity_failure_is_reported_without_details(self):
        code, log = self.run_guest(f"rehearsal: wiki journal capacity: FAILED: {MARKER}", 1)
        self.assertEqual(code, 1)
        self.assertIn("browser_failure_stage=wiki journal capacity", log)

    def test_cleanup_failure_never_passes(self):
        failure = f"isolated: FAILED (cleanup): {MARKER}"
        body = transcript().replace(ci.CLEANUP, failure).replace(ci.SUCCESS + "\n", "")
        code, log = self.run_guest(body, 1)
        self.assertEqual(code, 1)
        self.assertIn("guest_failure_stage=cleanup", log)
        self.assertNotIn("result=passed", log)

    def test_success_requires_complete_correlated_evidence(self):
        good = transcript()
        self.assertEqual(self.run_guest(good + MARKER, 0)[0], 0)
        for wrong in (good.replace(ci.CLEANUP, ""),
                      good.replace("release evidence: size app 123456 789\n", ""),
                      good.replace("data.words=3", "data.words=4", 1),
                      good.replace(PINS["GO_ARCHIVE_SHA256"], "a" * 64),
                      good.replace(PINS["APP_IMAGE"], PINS["POSTGRES_IMAGE"]),
                      good.replace(f"job_id={uuid(3)}", f"job_id={uuid(9)}", 1),
                      good.replace("00:00:01Z", "00:00:00Z"),
                      good + good.splitlines()[0],
                      good + "x" * ci.LIMIT):
            with self.subTest(length=len(wrong)):
                code, log = self.run_guest(wrong, 0)
                self.assertEqual(code, 1)
                self.assertIn("stage=guest-evidence exit=1", log)

    def test_report_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report"
            path.write_text(MARKER)
            with self.assertRaises(FileExistsError):
                ci.Report(path)
            self.assertEqual(path.read_text(), MARKER)


class RevisionTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "checkout"
        self.root.mkdir()
        self.report = self.root.parent / "report"
        change_root = patch.object(ci, "ROOT", self.root)
        change_root.start()
        self.addCleanup(change_root.stop)
        self.git("init", "-q")
        (self.root / "releases").mkdir()
        (self.root / ci.MANIFEST).write_bytes(MANIFEST)
        (self.root / ".gitignore").write_text("/local.env\n")
        self.git("add", ".")
        self.git("-c", "user.name=Contract Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "fixture")
        self.head = self.git("rev-parse", "HEAD").decode().strip()

    def git(self, *args):
        return subprocess.run(["/usr/bin/git", *args], cwd=self.root, env={"HOME": str(self.root.parent)},
                              capture_output=True, check=True).stdout

    def run_verify(self, head, *, side_effect=None):
        with (patch.object(ci, "acceptance", side_effect=side_effect) as smoke,
              redirect_stdout(io.StringIO())):
            result = ci.execute(SimpleNamespace(head=head), ci.Report(self.report))
        return result, self.report.read_text(), smoke

    def assert_refusal(self, head):
        result, log, smoke = self.run_verify(head)
        self.assertEqual(result, 1)
        self.assertIn("result=failed stage=revision exit=1", log)
        smoke.assert_not_called()

    def test_clean_matching_head_uses_committed_manifest(self):
        result, log, smoke = self.run_verify(self.head)
        self.assertEqual(result, 0)
        self.assertIn("checkout_sha=" + self.head, log)
        self.assertIn("result=passed exit=0", log)
        smoke.assert_called_once()
        self.assertEqual(smoke.call_args.args[1], MANIFEST)

    def test_wrong_head_is_rejected(self):
        self.assert_refusal("0" * 40)

    def test_tracked_change_is_rejected(self):
        (self.root / ci.MANIFEST).write_bytes(b"changed\n")
        self.assert_refusal(self.head)

    def test_ignored_file_is_rejected(self):
        (self.root / "local.env").touch()
        self.assert_refusal(self.head)

    def test_untracked_file_is_rejected(self):
        (self.root / "untracked").touch()
        self.assert_refusal(self.head)

    def test_hidden_index_entry_is_rejected(self):
        self.git("update-index", "--assume-unchanged", ci.MANIFEST)
        self.assert_refusal(self.head)

    def test_change_during_smoke_is_rejected(self):
        def change_tree(*_):
            (self.root / "changed-by-smoke").touch()

        result, log, smoke = self.run_verify(self.head, side_effect=change_tree)
        self.assertEqual(result, 1)
        self.assertIn("result=failed stage=revision-after-smoke exit=1", log)
        smoke.assert_called_once()


if __name__ == "__main__":
    unittest.main()
