"""DEPLOY-07 real acceptance on a fresh, attested Linux guest only."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys


spec = importlib.util.spec_from_file_location("deploy_isolated",
                                             Path(__file__).with_name("test-deploy-isolated.py"))
isolated = importlib.util.module_from_spec(spec)
spec.loader.exec_module(isolated)
ops = isolated.ops


def cleanup(op, expected):
    with op.lock("control.lock", ops.LOCK_WAIT) as control:
        isolated.require(control is not None, "control lock busy; no cleanup attempted")
        op.marker("maintenance", "isolated acceptance cleanup")
        with op.lock("tick.lock", ops.LOCK_WAIT) as tick:
            isolated.require(tick is not None, "tick lock busy; no cleanup attempted")
            op.config()
            op.check_context()
            op.check_paths(clean=False)
            if op.marked(op.runtime_file.name):
                ops.private_file(op.runtime_file)
                model = op.render(frozen=True)
                current = {name: obj["Id"] for name, obj in
                           op.inventory(model, allow_own=True, check_ports=False).items()}
                isolated.require(expected is None or current == expected, "test project identity changed")
                op.compose("down", timeout=180, fds=(control, tick), frozen=True)
            op.inventory({}, allow_own=False, check_ports=False)
            isolated.require(not op.d("ps", "-aq").stdout.strip()
                             and not op.d("volume", "ls", "-q").stdout.strip()
                             and sorted(op.d("network", "ls", "--format", "{{.Name}}").stdout.split()) ==
                             [b"bridge", b"host", b"none"], "disposable daemon not empty after cleanup")
    print("isolated: verified no rehearsal containers or Compose network; fixture data preserved", flush=True)


def release_evidence(op, expected):
    """Measure the bundle used by Compose and the images of the live containers."""
    op.verify_bundle()
    pins = op.pins
    own = op.require_running(op.render(frozen=True))
    isolated.require({name: obj["Id"] for name, obj in own.items()} == expected,
                     "service identity changed before collecting release evidence")
    print("release evidence: manifest", hashlib.sha256(
        (isolated.ROOT / "releases/001.env").read_bytes()).hexdigest(), flush=True)
    for component, _, data, _, _ in op.archives(op.bundle):
        print("release evidence: archive", component, hashlib.sha256(data).hexdigest(), flush=True)
    for service, key in (("app", "APP_IMAGE"), ("postgres", "POSTGRES_IMAGE"), ("dokuwiki", "DOKUWIKI_IMAGE")):
        ref = pins[key]
        image, = json.loads(op.d("image", "inspect", ref).stdout)
        isolated.require(ref in (image["RepoDigests"] or []) and image["Id"] == own[service]["Image"],
                         "running container image does not match the resolved pinned image")
        print("release evidence: image", service, ref, image["Id"], flush=True)
        container, = json.loads(op.d("container", "inspect", "--size", expected[service], timeout=60).stdout)
        sizes = (image["Size"], container["SizeRw"])
        isolated.require(container["Id"] == expected[service] and container["Image"] == image["Id"]
                         and all(type(size) is int and size >= 0 for size in sizes),
                         "container size evidence does not match the running service")
        print("release evidence: size", service, *sizes, flush=True)


def main():
    os.umask(0o077)
    try:
        isolated.attest_vm()
        op = ops.Operator("rehearsal")
        op.inventory({}, allow_own=False, check_ports=True)
        # A current test must never reuse any Docker volumes or custom networks.
        isolated.require(not op.d("volume", "ls", "-q").stdout.strip() and
                         sorted(op.d("network", "ls", "--format", "{{.Name}}").stdout.split()) ==
                         [b"bridge", b"host", b"none"], "disposable daemon has other networks or volumes")
    except Exception:
        print("isolated: BLOCKED (attestation): requires a clean attested VM and local daemon", file=sys.stderr)
        return 77
    print("isolated: fresh fixture paths, local dedicated Docker and rehearsal ports verified", flush=True)
    stage, failed, deploy_attempted, expected = "test fixtures", False, False, None
    try:
        isolated.setup()
        with (op.state / "smoke-managed").open("x") as flag:
            flag.write("dedicated rehearsal runner\n")
        stage = "public preflight"
        isolated.public(op, "preflight", "rehearsal", "deploy-001")
        stage = "public installation"
        deploy_attempted = True
        isolated.public(op, "deploy", "rehearse", "deploy-001")
        expected = isolated.projects(op)
        stage = "public browser smoke"
        isolated.public(op, "smoke", "rehearsal", "deploy-001")
        stage = "release evidence"
        release_evidence(op, expected)
        isolated.require((op.state / "ready").read_bytes() ==
                         b"deploy-001 prepared; end-to-end smoke pending\n", "prepared marker changed")
    except Exception as err:
        detail = str(err) if isinstance(err, ops.Refusal) else "no raw diagnostics printed"
        print(f"isolated: FAILED ({stage}): {detail}", file=sys.stderr)
        failed = True
    finally:
        if deploy_attempted:
            try:
                cleanup(op, expected)
            except Exception:
                print("isolated: FAILED (cleanup): inspect the verified guest; no raw diagnostics printed",
                      file=sys.stderr)
                failed = True
    if not failed:
        print("isolated: passed (real release, real Chromium, two submissions and post-restart reads)")
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
