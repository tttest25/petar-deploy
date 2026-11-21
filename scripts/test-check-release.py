"""Regression checks for the deploy-001 manifest and rehearsal render."""

from copy import deepcopy
import gzip
import hashlib
import io
import os
from pathlib import Path
import runpy
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "releases/001.env"
CHECK = runpy.run_path(str(ROOT / "scripts/check-release.py"))


class ReleaseChecks(unittest.TestCase):
    def test_manifest_requires_literal_lf_and_known_fields(self):
        original = MANIFEST.read_bytes()
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "manifest.env"
            for data in (
                original.replace(b"\nAPP_IMAGE=", b"\vAPP_IMAGE="),
                original + b"POSTGRES_PASSWORD=fixture\n",
                original + b"GO_VERSION=v0.1.0\n",
                original + b"# arbitrary extra comment\n",
            ):
                with self.subTest(data=data[-40:]):
                    path.write_bytes(data)
                    with self.assertRaises(ValueError):
                        CHECK["read_manifest"](path)

    def test_archive_rejects_suffix_even_with_matching_sha256(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "fixture.tar.gz"
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                item = tarfile.TarInfo("petar/example.php")
                item.mode, item.size = 0o644, 1
                archive.addfile(item, io.BytesIO(b"x"))
            expected = {"petar/example.php": 0o644}
            raw = buffer.getvalue()
            valid = gzip.compress(raw)
            path.write_bytes(valid)
            self.assertEqual(CHECK["read_archive"](path, hashlib.sha256(valid).hexdigest(), expected),
                             {"petar/example.php": b"x"})
            for index, data in enumerate(
                (valid + b"\0", valid + gzip.compress(b""), gzip.compress(raw + b"\0"))
            ):
                with self.subTest(case=index):
                    path.write_bytes(data)
                    with self.assertRaises(ValueError):
                        CHECK["read_archive"](path, hashlib.sha256(data).hexdigest(), expected)

    def test_archive_oversize_inflation_is_a_controlled_refusal(self):
        data = gzip.compress(b"\0" * (CHECK["MAX_BUNDLE"] + 1))
        with self.assertRaisesRegex(ValueError, "fixture: uncompressed archive too large"):
            CHECK["read_archive_bytes"]("fixture", data, hashlib.sha256(data).hexdigest(), {})

    def test_rehearsal_checks_effective_model(self):
        pins = CHECK["read_manifest"](MANIFEST)
        with tempfile.TemporaryDirectory() as scratch:
            local = Path(scratch) / "local.env"
            valid = (
                "POSTGRES_PASSWORD=fixture-only\n"
                "POSTGRES_FILES_DIR=/opt/go-pet/postgres/deploy-001.Test/go/postgres\n"
                "PLUGIN_DIR=/opt/go-pet/postgres/deploy-001.Test/plugin/petar\n"
            )
            local.write_text(valid, encoding="ascii")
            local.chmod(0o600)
            local_values, model = CHECK["render_rehearsal"](
                MANIFEST, local, "default", Path.home() / ".docker",
            )
            self.assertEqual(CHECK["check_rehearsal"](pins, local_values, model),
                             "/opt/petar-data/rehearsal/wiki")
            for path, value in (
                (("name",), "petar-production"),
                (("services", "app", "image"), "petar-app:dev"),
                (("services", "dokuwiki", "ports", 0, "host_ip"), "0.0.0.0"),
                (("services", "dokuwiki", "volumes", 0, "source"), "/opt/petar-data/production/wiki"),
                (("services", "app", "environment", "DB_DSN"), "unexpected"),
                (("services", "app", "ipc"), "host"),
                (("services", "app", "logging"), {"driver": "json-file", "options": {"env": "DB_DSN"}}),
                (("services", "postgres", "healthcheck", "test"), ["CMD", "pg_isready"]),
            ):
                with self.subTest(path=path):
                    changed = deepcopy(model)
                    target = changed
                    for item in path[:-1]:
                        target = target[item]
                    target[path[-1]] = value
                    with self.assertRaises(ValueError):
                        CHECK["check_rehearsal"](pins, local_values, changed)
            local.write_text(valid + "WIKI_DATA_DIR=/opt/petar-data/production/wiki\n", encoding="ascii")
            with self.assertRaises(ValueError):
                CHECK["read_local"](local)

    def test_rehearsal_never_leaks_password_diagnostics(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            local = root / "local.env"
            invoked = root / "docker-invoked"
            secret = "test-only-sensitive-value"
            stub = root / "docker"
            stub.write_text(
                f"#!/bin/sh\n: > '{invoked}'\nprintf '%s\\n' '{secret}'\n"
                f"printf '%s\\n' '{secret}' >&2\nexit 1\n", encoding="ascii",
            )
            stub.chmod(0o700)
            command = [
                "python3", "scripts/check-release.py", str(MANIFEST),
                "--check-rehearsal", str(local), "--docker-context", "default",
                "--docker-config", str(root),
            ]
            for assignment, diagnostic, calls_docker in (
                (f"POSTGRES_PASSWORD='{secret}", "invalid, unknown or duplicate field", False),
                (f"POSTGRES_PASSWORD={secret}", "cannot render rehearsal configuration", True),
            ):
                local.write_text(
                    f"{assignment}\n"
                    "POSTGRES_FILES_DIR=/opt/go-pet/postgres/deploy-001.Test/go/postgres\n"
                    "PLUGIN_DIR=/opt/go-pet/postgres/deploy-001.Test/plugin/petar\n",
                    encoding="ascii",
                )
                local.chmod(0o600)
                result = subprocess.run(
                    command, cwd=ROOT, env={**os.environ, "PATH": f"{root}:{os.environ['PATH']}"},
                    capture_output=True, text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(diagnostic, result.stderr)
                self.assertNotIn(secret, result.stdout)
                self.assertNotIn(secret, result.stderr)
                self.assertEqual(invoked.exists(), calls_docker)


if __name__ == "__main__":
    unittest.main()
