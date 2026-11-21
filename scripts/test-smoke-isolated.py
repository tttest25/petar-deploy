"""DEPLOY-07 real acceptance on a fresh, attested Linux guest only."""

import importlib.util
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
    print("isolated: verified no rehearsal containers or Compose network; fixture data preserved", flush=True)


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
