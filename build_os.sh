#!/usr/bin/env bash
#
# IPTV-Pi :: build automation
#
# Fetches Buildroot, applies one of the IPTV-Pi defconfigs and produces a
# ready-to-flash sdcard.img for a Raspberry Pi 1 or Raspberry Pi 2.
#
#   ./build_os.sh                 # build both boards
#   ./build_os.sh --board pi2     # build only the Pi 2 image
#   ./build_os.sh --board pi1 --clean
#   ./build_os.sh --board pi2 --menuconfig
#
# Host requirements (Ubuntu/Debian):
#   sudo apt-get install build-essential git bc rsync cpio unzip file \
#        bzip2 wget patch perl python3 libncurses-dev
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXTERNAL_DIR="${PROJECT_DIR}/br2-external"
BUILDROOT_DIR="${PROJECT_DIR}/buildroot"
DL_DIR="${PROJECT_DIR}/dl"
OUTPUT_DIR="${PROJECT_DIR}/output"

BUILDROOT_BRANCH="${BUILDROOT_BRANCH:-2021.02.x}"
BUILDROOT_REPO="${BUILDROOT_REPO:-https://github.com/buildroot/buildroot.git}"

BOARDS=()
JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"
DO_CLEAN=0
DO_MENUCONFIG=0
DO_HOST_TOOLS_ONLY=0

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m==>\033[0m warning: %s\n' "$*" >&2; }
die()  { printf '\033[1;31m==>\033[0m error: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
IPTV-Pi build script - builds a minimal Kodi IPTV image with Buildroot.

Usage: ./build_os.sh [options]

Options:
  -b, --board <pi1|pi2>   Board to build (repeatable, default: both)
  -j, --jobs <n>          Parallel make jobs (default: number of CPUs)
      --clean             Remove the board's output directory first
      --menuconfig        Open menuconfig and save the result back to the
                          defconfig instead of building
      --host-tools-only   Build Buildroot's host python and stop; a minutes
                          long smoke test before the hours long image build
  -h, --help              Show this help

Environment:
  BUILDROOT_BRANCH        Buildroot branch to fetch (default: 2021.02.x)
  BUILDROOT_REPO          Buildroot git repository to clone
  IPTVPI_ROOT_PASSWORD    If set, the image gets this root password (hashed
                          at build time; nothing is stored in the repo)
  BR2_DL_DIR              Download cache (default: ./dl)

Output:
  output/<board>/images/sdcard.img    ready to flash with dd
  output/<board>/images/iptvpi-<version>.raucb
  output/<board>/images/iptvpi-<version>.raucb.sha256
                                      signed A/B update bundle (`rauc install`)
  output/<board>/images/rauc/         signing key pair (keep the .key.pem safe)

Notes:
  IPTV-Pi needs Buildroot 2021.02.x: that is the last series whose Kodi
  (18.9 "Leia") still ships the native Raspberry Pi platform that decodes
  H.264 on the VideoCore IV without X11 or Wayland.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        -b|--board)
            [ $# -ge 2 ] || die "--board needs an argument (pi1 or pi2)"
            BOARDS+=("$2")
            shift 2
            ;;
        pi1|pi2)
            BOARDS+=("$1")
            shift
            ;;
        -j|--jobs)
            [ $# -ge 2 ] || die "--jobs needs an argument"
            JOBS="$2"
            shift 2
            ;;
        --clean)
            DO_CLEAN=1
            shift
            ;;
        --menuconfig)
            DO_MENUCONFIG=1
            shift
            ;;
        --host-tools-only)
            DO_HOST_TOOLS_ONLY=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            warn "unknown argument: $1"
            usage
            exit 1
            ;;
    esac
done

[ ${#BOARDS[@]} -gt 0 ] || BOARDS=(pi1 pi2)
for board in "${BOARDS[@]}"; do
    case "${board}" in
        pi1|pi2) ;;
        *) die "unknown board '${board}' (expected pi1 or pi2)" ;;
    esac
done

# --------------------------------------------------------------------------
# Host tool check
# --------------------------------------------------------------------------
REQUIRED_TOOLS=(make gcc g++ git tar wget cpio unzip rsync bc file patch perl sed python3)
missing=()
for tool in "${REQUIRED_TOOLS[@]}"; do
    command -v "${tool}" >/dev/null 2>&1 || missing+=("${tool}")
done
if [ ${#missing[@]} -gt 0 ]; then
    die "missing host tools: ${missing[*]}
Install them first, for example:
  sudo apt-get install build-essential git bc rsync cpio unzip file bzip2 wget patch perl python3 libncurses-dev"
fi

# --------------------------------------------------------------------------
# Buildroot checkout
# --------------------------------------------------------------------------
fetch_buildroot() {
    if [ -d "${BUILDROOT_DIR}/.git" ]; then
        local current
        current="$(git -C "${BUILDROOT_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
        log "reusing Buildroot checkout (${current}) in ${BUILDROOT_DIR}"
        return
    fi
    [ -e "${BUILDROOT_DIR}" ] && die "${BUILDROOT_DIR} exists but is not a git checkout"
    log "cloning Buildroot ${BUILDROOT_BRANCH} (shallow)"
    git clone --depth 1 --branch "${BUILDROOT_BRANCH}" \
        "${BUILDROOT_REPO}" "${BUILDROOT_DIR}"
}

fetch_buildroot

log "Buildroot version: $(git -C "${BUILDROOT_DIR}" log -1 --format='%h %ad' --date=short 2>/dev/null || echo unknown)"

# --------------------------------------------------------------------------
# Configuration sanity check
#
# Kconfig silently drops symbols whose dependencies are unmet, which turns a
# defconfig typo into a Kodi-less image. Catch that here instead.
# --------------------------------------------------------------------------
verify_config() {
    local config="$1"
    local missing=0 symbol

    local required_y=(
        BR2_INIT_SYSTEMD
        BR2_PACKAGE_RPI_FIRMWARE
        BR2_PACKAGE_RPI_USERLAND
        BR2_PACKAGE_ALSA_UTILS
        BR2_PACKAGE_WPA_SUPPLICANT
        BR2_PACKAGE_PYTHON
        BR2_PACKAGE_PYTHON_FLASK
        BR2_PACKAGE_KODI
        BR2_PACKAGE_KODI_PLATFORM_RBPI
        BR2_PACKAGE_KODI_ALSA_LIB
        BR2_PACKAGE_KODI_PVR_IPTVSIMPLE
        BR2_PACKAGE_SYSTEMD_NETWORKD
        BR2_PACKAGE_SYSTEMD_RESOLVED
        BR2_TARGET_ROOTFS_EXT2
    )

    for symbol in "${required_y[@]}"; do
        if ! grep -q "^${symbol}=y$" "${config}"; then
            warn "config option ${symbol} is not enabled"
            missing=1
        fi
    done

    for symbol in BR2_ROOTFS_OVERLAY BR2_ROOTFS_POST_BUILD_SCRIPT BR2_ROOTFS_POST_IMAGE_SCRIPT; do
        if ! grep -qE "^${symbol}=\".+\"" "${config}"; then
            warn "config option ${symbol} is empty"
            missing=1
        fi
    done

    if [ "${missing}" -ne 0 ]; then
        if ! grep -q '^BR2_PACKAGE_KODI_PLATFORM_RBPI=y' "${config}"; then
            die "Kodi's Raspberry Pi platform is not available in this Buildroot.
IPTV-Pi requires the 2021.02.x series (Kodi 18.9 'Leia'). Newer Buildroot
releases ship Kodi 19/20, which dropped the native Pi platform - there is no
X11/Wayland here, and the Pi 1 (ARMv6, no NEON) cannot use the GBM backend.
Remove ${BUILDROOT_DIR} and re-run with BUILDROOT_BRANCH=2021.02.x."
        fi
        die "the configuration is incomplete (see the warnings above)"
    fi
}

# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------
build_board() {
    local board="$1"
    local defconfig="iptvpi${board:2}_defconfig"
    local out="${OUTPUT_DIR}/${board}"
    local config="${out}/.config"

    if [ "${DO_CLEAN}" -eq 1 ]; then
        log "removing ${out}"
        rm -rf "${out}"
    fi

    log "configuring ${board}: ${defconfig}"
    export IPTVPI_BOARD="${board}"
    make -C "${BUILDROOT_DIR}" \
        O="${out}" \
        BR2_EXTERNAL="${EXTERNAL_DIR}" \
        BR2_DL_DIR="${DL_DIR}" \
        "${defconfig}"

    verify_config "${config}"

    if [ "${DO_MENUCONFIG}" -eq 1 ]; then
        make -C "${BUILDROOT_DIR}" O="${out}" BR2_EXTERNAL="${EXTERNAL_DIR}" menuconfig
        log "saving the modified configuration back to br2-external/configs/${defconfig}"
        make -C "${BUILDROOT_DIR}" O="${out}" BR2_EXTERNAL="${EXTERNAL_DIR}" \
            BR2_DEFCONFIG="${EXTERNAL_DIR}/configs/${defconfig}" savedefconfig
        return 0
    fi

    if [ "${DO_HOST_TOOLS_ONLY}" -eq 1 ]; then
        # The image build runs Buildroot's own host python first. Building just
        # that pair turns a broken host toolchain into a minutes long failure
        # instead of one that shows up hours into the Kodi build.
        log "building Buildroot's host python for ${board} (smoke test)"
        make -C "${BUILDROOT_DIR}" \
            O="${out}" \
            BR2_EXTERNAL="${EXTERNAL_DIR}" \
            BR2_DL_DIR="${DL_DIR}" \
            host-python host-python-setuptools \
            -j"${JOBS}"
        local hostpy="${out}/host/bin/python2"
        if ! "${hostpy}" -V; then
            warn "${hostpy} does not start; collecting details"
            ldd "${hostpy}" || true
            PYTHONVERBOSE=1 "${hostpy}" -V || true
            die "Buildroot's host python does not run on this host"
        fi
        "${hostpy}" -c 'import setuptools, sys; print("setuptools %s on Python %s" % (setuptools.__version__, sys.version.split()[0]))'
        log "host python smoke test passed: ${hostpy}"
        return 0
    fi

    log "building ${board} with ${JOBS} jobs (this takes a while: Kodi is big)"
    local started
    started="$(date +%s)"
    make -C "${BUILDROOT_DIR}" \
        O="${out}" \
        BR2_EXTERNAL="${EXTERNAL_DIR}" \
        BR2_DL_DIR="${DL_DIR}" \
        -j"${JOBS}"
    local elapsed=$(( $(date +%s) - started ))

    local image="${out}/images/sdcard.img"
    [ -f "${image}" ] || die "build finished but ${image} does not exist"

    log "${board} image built in $(( elapsed / 60 )) min: ${image}"
    log "flash it with:"
    printf '    sudo dd if=%s of=/dev/sdX bs=4M conv=fsync status=progress\n' "${image}"

    local bundle
    bundle="$(ls -1 "${out}"/images/*.raucb 2>/dev/null | head -1 || true)"
    if [ -n "${bundle}" ]; then
        log "A/B update bundle: ${bundle}"
        log "install it on a running box with: rauc install <bundle>"
    else
        log "no update bundle was created (host rauc or signing key missing)"
    fi
}

if [ "${DO_MENUCONFIG}" -eq 1 ]; then
    [ ${#BOARDS[@]} -eq 1 ] || die "--menuconfig works on one board at a time, use --board"
fi

for board in "${BOARDS[@]}"; do
    build_board "${board}"
done

if [ "${DO_MENUCONFIG}" -eq 0 ]; then
    log "done"
    cat <<EOF

Next steps
  1. Flash the image:   sudo dd if=${OUTPUT_DIR}/<board>/images/sdcard.img of=/dev/sdX bs=4M conv=fsync status=progress
  2. Insert the card, connect HDMI + network, power on.
  3. Open the playlist manager from any browser on the same network:
         http://iptvpi.local/     (or http://<board-ip>/)
     Paste an M3U playlist, upload a file or point it at a playlist URL.
  4. Kodi restarts and shows the channels from the playlist.
  5. System updates are A/B: copy iptvpi-<version>.raucb to the box and run
         rauc install /var/lib/iptv/iptvpi-<version>.raucb && reboot
     The inactive slot is written; playlists, logs and the Kodi profile stay.

Serial console (115200 8N1 on GPIO 14/15) stays available for debugging.
EOF
fi
