#!/usr/bin/env bash
set -Eeuo pipefail

repository=https://github.com/AshCooperUK/SDR-Scanner.git
service=local-sdr-scanner.service
decoder_dir="$HOME/.local/share/sdr-scanner"
dmr_version=2.12.1
welle_commit=512558d1f8ac4c524d3c63e97510ea36c1bd7a70
desktop=
stage="checking the host"

usage() {
    printf '%s\n' 'Usage: bash install.sh [--headless | --desktop]' \
        'Install SDr Scanner, DAB and DMR on Ubuntu or Debian (AMD64 or ARM64).' \
        'LAN access and boot startup are enabled. The desktop launcher is detected automatically.'
}

fail() {
    printf 'SDr Scanner: %s\n' "$*" >&2
    exit 1
}

while (( $# )); do
    case "$1" in
        --headless|--desktop)
            [[ -z $desktop || $desktop == "$1" ]] || fail 'Choose either --headless or --desktop.'
            desktop=$1
            ;;
        -h|--help) usage; exit ;;
        *) usage >&2; fail "Unknown option $1." ;;
    esac
    shift
done

trap 'printf "SDr Scanner: installation failed while %s. Fix the error above, then run the installer again.\n" "$stage" >&2' ERR

[[ $EUID != 0 ]] || fail 'Run the installer as your normal user. It will use sudo for system setup.'
[[ -r /etc/os-release ]] || fail 'Use an Ubuntu or Debian host.'
. /etc/os-release
case "${ID:-}" in
    ubuntu) minimum_version=22.04 ;;
    debian) minimum_version=12 ;;
    *) fail 'Use Ubuntu 22.04+, Debian 12+, or a separate supported Linux host for the SDR.' ;;
esac
dpkg --compare-versions "${VERSION_ID:-0}" ge "$minimum_version" || fail "Use $ID $minimum_version or newer."
[[ -d /run/systemd/system ]] || fail 'Start this on the Linux host with systemd running.'

case "$(dpkg --print-architecture)" in
    amd64)
        dmr_arch=x86_64
        dmr_sha=3110c5bcf8572789d57dd96ef09c351a61e2d1b1a4cf156ac542b0b3e73053a6
        ;;
    arm64)
        dmr_arch=aarch64
        dmr_sha=f416c04a3692fd8de23af4df4103d3f4e4bb65a896d5f0b185fb886c91c82a66
        ;;
    *) fail 'Install a 64-bit AMD64 or ARM64 edition of Ubuntu or Debian for the bundled DMR decoder.' ;;
esac

if [[ -z $desktop ]]; then
    desktop=--headless
    if [[ -z ${SSH_CONNECTION:-}${SSH_CLIENT:-}${SSH_TTY:-} && ${XDG_SESSION_TYPE:-} != tty &&
          -n ${DISPLAY:-}${WAYLAND_DISPLAY:-} ]]; then
        desktop=--desktop
    fi
fi

work_dir=$(mktemp -d)
decoder_staging=
trap 'rm -rf -- "$work_dir"; if [[ -n $decoder_staging ]]; then rm -rf -- "$decoder_staging"; fi' EXIT

welle="$decoder_dir/bin/welle-cli"
dmr_bundle="$decoder_dir/dsd-neo-$dmr_version"
welle_ready() {
    local help
    [[ -x $1 ]] || return 1
    # welle-cli prints its usage with exit status 1.
    help=$(timeout 10 "$1" -h 2>&1 || :)
    [[ $help == *'Usage: welle-cli'* ]]
}

dmr_ready() {
    local help
    [[ -x $1 ]] || return 1
    help=$(timeout 10 "$1" -h 2>&1 || :)
    [[ $help == *'Usage: dsd-neo'* && $help == *'--api-token-file'* ]]
}

stage="installing Linux packages"
printf '%s\n' 'Checking scanner dependencies…'
packages=(git curl ca-certificates python3 python3-numpy python3-scipy rtl-sdr
    libmp3lame0 libpam-systemd dbus-user-session iproute2
    gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good)
if ! welle_ready "$welle"; then
    packages+=(build-essential cmake xxd libfaad-dev libmpg123-dev libmp3lame-dev)
fi
if [[ $desktop == --desktop ]]; then
    packages+=(xdg-utils)
fi
missing=()
for package in "${packages[@]}"; do
    if ! dpkg-query -W -f='${db:Status-Status}\n' "$package" 2>/dev/null | grep -x installed >/dev/null; then
        missing+=("$package")
    fi
done
if (( ${#missing[@]} )); then
    sudo apt-get update
    if [[ $ID == ubuntu ]] && ! apt-cache show rtl-sdr >/dev/null 2>&1; then
        sudo apt-get install -y --no-install-recommends software-properties-common
        sudo add-apt-repository --yes --no-update universe
        sudo apt-get update
    fi
    sudo apt-get install -y --no-install-recommends "${missing[@]}"
fi

stage="locating the scanner source"
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
if [[ ! -f $source_dir/launcher.py || ! -f $source_dir/app.py || ! -f $source_dir/dmr_decoder.py ]]; then
    source_dir="$HOME/SDR-Scanner"
    if [[ ! -e $source_dir ]]; then
        git clone --depth 1 --branch main "$repository" "$source_dir"
    fi
    [[ -f $source_dir/install.sh && -f $source_dir/launcher.py && -f $source_dir/app.py && -f $source_dir/dmr_decoder.py ]] ||
        fail "The directory $source_dir already exists. Update your SDr Scanner checkout and run its install.sh."
fi

stage="setting up the DAB decoder"
if ! welle_ready "$welle"; then
    printf '%s\n' 'Building the headless DAB decoder. This can take a few minutes…'
    git clone --depth 1 --branch v2.7 https://github.com/AlbrechtL/welle.io.git "$work_dir/welle"
    [[ $(git -C "$work_dir/welle" rev-parse HEAD) == "$welle_commit" ]] || fail 'The welle.io source revision did not match.'
    dab_socket="$work_dir/welle/src/various/Socket.cpp"
    grep -Fq 'htons(INADDR_ANY)' "$dab_socket" || fail 'Could not locate the DAB HTTP listener.'
    sed -i 's/htons(INADDR_ANY)/htonl(INADDR_LOOPBACK)/g' "$dab_socket"
    cmake -S "$work_dir/welle" -B "$work_dir/welle-build" \
        -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_WELLE_IO=OFF -DBUILD_WELLE_CLI=ON -DKISS_FFT=ON \
        -DRTLSDR=OFF -DSOAPYSDR=OFF
    cmake --build "$work_dir/welle-build" --parallel 2
    welle_ready "$work_dir/welle-build/welle-cli" || fail 'The packaged DAB decoder could not start.'
    mkdir -p "$decoder_dir/bin"
    decoder_staging=$(mktemp -d "$decoder_dir/.install.XXXXXXXX")
    install -m 0755 "$work_dir/welle-build/welle-cli" "$decoder_staging/welle-cli"
    mv -fT -- "$decoder_staging/welle-cli" "$welle"
fi
for plugin in souphttpsrc mpegaudioparse mpg123audiodec audioconvert audioresample fdsink; do
    gst-inspect-1.0 "$plugin" >/dev/null
done

stage="setting up the DMR decoder"
if ! dmr_ready "$dmr_bundle/AppRun"; then
    printf '%s\n' 'Downloading and checking the DMR decoder…'
    dmr_asset="dsd-neo-linux-$dmr_arch-portable-v$dmr_version.AppImage"
    curl -fL --retry 3 --output "$work_dir/dsd-neo.AppImage" \
        "https://github.com/arancormonk/dsd-neo/releases/download/v$dmr_version/$dmr_asset"
    printf '%s  %s\n' "$dmr_sha" "$work_dir/dsd-neo.AppImage" | sha256sum -c -
    chmod +x "$work_dir/dsd-neo.AppImage"
    mkdir -p "$decoder_dir"
    if [[ -z $decoder_staging ]]; then
        decoder_staging=$(mktemp -d "$decoder_dir/.install.XXXXXXXX")
    fi
    (cd "$decoder_staging" && "$work_dir/dsd-neo.AppImage" --appimage-extract > /dev/null)
    dmr_ready "$decoder_staging/squashfs-root/AppRun" || fail 'The packaged DMR decoder could not start.'
    if [[ -e $dmr_bundle || -L $dmr_bundle ]]; then
        backup_dir=$(mktemp -d "$decoder_dir/.decoder-backup.XXXXXXXX")
        mv -T -- "$dmr_bundle" "$backup_dir/dsd-neo-$dmr_version"
        printf 'The previous decoder bundle is saved in %s\n' "$backup_dir"
    fi
    mv -T -- "$decoder_staging/squashfs-root" "$dmr_bundle"
fi
mkdir -p "$decoder_dir/bin"
ln -sfnT -- "$dmr_bundle/AppRun" "$decoder_dir/bin/dsd-neo"
(
    cd "$source_dir"
    SDR_SCANNER_DMR_DECODER="$dmr_bundle/AppRun" /usr/bin/python3 - <<'PY'
from dmr_decoder import DmrDecoder

decoder = DmrDecoder()
decoder.close()
PY
)

stage="configuring USB access and startup"
account=$(id -un)
if grep -sq 'GROUP="plugdev"' /usr/lib/udev/rules.d/*rtl*sdr*.rules /etc/udev/rules.d/*rtl*sdr*.rules &&
    getent group plugdev >/dev/null && [[ " $(id -nG "$account") " != *" plugdev "* ]]; then
    sudo usermod -aG plugdev "$account"
    printf '%s\n' 'Added USB permissions. Reboot once after setup to apply the new group to the service.'
fi
if [[ $(loginctl show-user "$account" --property=Linger --value 2>/dev/null || :) != yes ]]; then
    sudo loginctl enable-linger "$account"
fi
if ! systemctl --user show-environment >/dev/null 2>&1; then
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
    for (( second=0; second<10; second++ )); do
        systemctl --user show-environment >/dev/null 2>&1 && break
        sleep 1
    done
    systemctl --user show-environment >/dev/null 2>&1 ||
        fail 'The user service manager is unavailable. Sign in again or reconnect over SSH, then rerun this installer.'
fi

stage="starting the scanner"
printf '%s\n' 'Starting the scanner service…'
was_active=false
if systemctl --user is-active --quiet "$service"; then
    was_active=true
fi
/usr/bin/python3 "$source_dir/launcher.py" install "$desktop"
if $was_active; then
    systemctl --user restart "$service"
fi
(
    cd "$source_dir"
    /usr/bin/python3 - <<'PY'
from time import monotonic, sleep
from launcher import panel_ready

deadline = monotonic() + 15
while not panel_ready():
    if monotonic() >= deadline:
        raise SystemExit("Scanner startup did not respond. Run journalctl --user -u local-sdr-scanner.service -n 30.")
    sleep(0.25)
PY
)

printf '\nSDr Scanner is ready with DAB and DMR.\n'
printf 'On this computer: http://127.0.0.1:8766/\n'
while read -r address; do
    printf 'On your network: http://%s:8766/\n' "$address"
done < <(ip -o -4 addr show up scope global |
    awk '$2 !~ /^(docker[0-9]*|br-[[:xdigit:]]+)$/ {split($4, address, "/"); print address[1]}')
printf '%s\n' 'Plug in or reconnect the SDR, open the panel and click VOLUME to power it on.' \
    'LAN connections use the local IP. Remote connections require the PIN set on the radio or in CHIRP.'
