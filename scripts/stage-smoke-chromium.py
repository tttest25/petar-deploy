"""Stage a size- and hash-pinned Chromium archive before VM provisioning."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys


PINS = Path(__file__).with_name("smoke-chromium.json")


def fetch(version, size, target):
    url = (f"https://storage.googleapis.com/chrome-for-testing-public/{version}/"
           "linux64/chrome-headless-shell-linux64.zip")
    subprocess.run(["/usr/bin/curl", "-q", "-fsS", "--proto", "=https", "--connect-timeout", "10",
                    "--max-time", "300", "--max-filesize", str(size), "--", url],
                   stdin=subprocess.DEVNULL, stdout=target, stderr=subprocess.DEVNULL,
                   check=True, timeout=310)


def stage(pins, output, archive=None):
    version, digest, size = (pins[k] for k in ("chromium_version", "chromium_sha256", "chromium_bytes"))
    if (not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", version) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or type(size) is not int or size <= 0):
        raise ValueError("invalid Chromium pins")
    with output.open("x+b") as target:
        try:
            if archive is None:
                fetch(version, size, target)
            else:
                if not archive.is_file():
                    raise ValueError("archive must be a regular file")
                with archive.open("rb") as source:
                    if source.seek(0, 2) != size:
                        raise ValueError("archive size mismatch")
                    source.seek(0)
                    shutil.copyfileobj(source, target)
            if target.seek(0, 2) != size:
                raise ValueError("archive size mismatch")
            target.seek(0)
            if hashlib.file_digest(target, "sha256").hexdigest() != digest:
                raise ValueError("archive SHA-256 mismatch")
        except BaseException:
            output.unlink()
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    try:
        stage(json.loads(PINS.read_text()), args.output, args.archive)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        print("Chromium staging failed (download, file, size or SHA-256); no packages installed.",
              file=sys.stderr)
        return 1
    print("Pinned Chromium archive verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
