#!/bin/bash -p
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash prepare-smoke-vm.sh check
  bash prepare-smoke-vm.sh prepare --confirmed-disposable --resources-reviewed [--chromium-archive FILE]

Run on an existing disposable Ubuntu 26.04 amd64 KVM guest as UID/GID 1000:1000.
check does not update APT or install anything.
prepare requires a reviewed size estimate and any required approvals.
Without --chromium-archive, prepare requires curl to download Chromium before installing anything.
EOF
}

refuse() {
  printf 'VM preparation refused: %s\n' "$*" >&2
  exit 1
}

archive=()
case "$#:${1-}:${2-}:${3-}:${4-}" in
  1:check:::) mode=check ;;
  3:prepare:--confirmed-disposable:--resources-reviewed:) mode=prepare ;;
  5:prepare:--confirmed-disposable:--resources-reviewed:--chromium-archive)
    mode=prepare
    [[ -n "$5" ]] || refuse 'expected an archive path'
    archive=(--archive="$5")
    ;;
  1:--help:::|1:-h:::) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
for file in provision-smoke-vm.yml smoke-requirements.txt smoke-chromium.json stage-smoke-chromium.py; do
  [[ -f "$script_dir/$file" && -r "$script_dir/$file" ]] ||
    refuse "missing $file next to this script"
done

[[ "$(id -u):$(id -g)" == 1000:1000 ]] || refuse 'expected non-root UID/GID 1000:1000'
[[ "$(uname -m)" == x86_64 ]] || refuse 'expected x86_64'
case "$(systemd-detect-virt --vm || true)" in
  kvm|qemu) ;;
  *) refuse 'expected an existing KVM/QEMU guest' ;;
esac
[[ "$(systemd-detect-virt --container || true)" == none ]] || refuse 'not allowed inside a container'
. /etc/os-release
[[ "${ID-}:${VERSION_ID-}" == ubuntu:26.04 ]] || refuse 'expected Ubuntu 26.04'
[[ "$(findmnt -T /opt -n -o FSTYPE)" == ext4 ]] || refuse 'expected /opt on ext4'
mount_targets=$(findmnt -rn -o TARGET)
if grep -Eq '^/opt(/|$)' <<<"$mount_targets"; then
  refuse 'unexpected mount in or at /opt'
fi
opt_children=$(ls -A -- /opt)
[[ -z "$opt_children" ]] || refuse '/opt must be empty on a fresh VM'
[[ "$(systemctl is-active cron || true)" == inactive ]] || refuse 'cron must be inactive'
listeners=$(ss -H -ltn '( sport = :82 or sport = :8082 or sport = :5434 )')
[[ -z "$listeners" ]] || refuse 'test ports 82, 8082 and 5434 must be free'
daemon_processes=$(ps -eo comm)
if grep -Eq '^(dockerd|containerd)$' <<<"$daemon_processes"; then
  refuse 'found a running Docker or containerd process'
fi
sudo -n /bin/sh -ec '
  for path in /run/docker.sock /run/containerd/containerd.sock \
    /etc/petar-deploy-disposable /etc/docker /etc/containerd \
    /etc/petar-acceptance-docker.json /etc/systemd/system/petar-acceptance-docker.service \
    /var/lib/docker /var/lib/containerd /var/lib/petar-acceptance-docker; do
    if test -e "$path" || test -L "$path"; then
      printf "Pre-existing resource: %s\n" "$path" >&2
      exit 1
    fi
  done
' || refuse 'requires noninteractive sudo and no existing Docker state or attestation'

printf 'Fresh VM checks passed. Bootstrap tools:\n'
for tool in /usr/bin/git /usr/bin/ansible-playbook; do
  if [[ -x "$tool" ]]; then
    printf '  present %s\n' "$tool"
  else
    printf '  missing %s\n' "$tool"
  fi
done
if [[ "$mode" == check ]]; then
  printf 'Check only: no packages installed and no services started.\n'
  exit 0
fi

if ((${#archive[@]} == 0)); then
  [[ -x /usr/bin/curl ]] || refuse 'curl is absent; supply a pinned archive with --chromium-archive'
fi
work=$(mktemp -d)
trap 'rm -f -- "$work/chromium.zip"; rmdir -- "$work"' EXIT
work_abs=$(cd -- "$work" && pwd -P)
work=$work_abs
/usr/bin/python3 -IB "$script_dir/stage-smoke-chromium.py" \
  --output "$work/chromium.zip" "${archive[@]}"

packages=()
[[ -x /usr/bin/git ]] || packages+=(git)
[[ -x /usr/bin/ansible-playbook ]] || packages+=(ansible-core)
if ((${#packages[@]})); then
  sudo -n /usr/bin/apt-get update
  sudo -n /usr/bin/env DEBIAN_FRONTEND=noninteractive \
    /usr/bin/apt-get install -y --no-install-recommends "${packages[@]}"
fi

cd -- "$script_dir"
PETAR_CHROMIUM_ARCHIVE="$work/chromium.zip" \
  /usr/bin/ansible-playbook -i localhost, provision-smoke-vm.yml -e confirmed_disposable=yes
printf 'VM prepared. Reconnect over SSH to pick up the Docker group before running DEPLOY-08.\n'
