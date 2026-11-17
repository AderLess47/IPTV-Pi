#!/bin/bash
#
# IPTV-Pi :: Buildroot post-build hook
#
# Called by Buildroot after every package is installed and before the root
# filesystem image is packed. It finishes the appliance configuration:
#
#   * installs the playlist web interface,
#   * makes sure the playlist / Kodi profile directories exist,
#   * checks that Kodi and the IPTV Simple Client really made it into the
#     image (a wrong defconfig otherwise produces a very quiet black screen),
#   * enables the systemd units used by the appliance,
#   * frees tty1 for Kodi and wires up DNS via systemd-resolved.
#
set -euo pipefail

TARGET_DIR="${1:?usage: post-build.sh <target-dir>}"
BOARD_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "${BOARD_DIR}/../../.." && pwd)"
WEBUI_SRC="${PROJECT_DIR}/webui"

KODI_CFG_DIR="${TARGET_DIR}/usr/share/iptv-kodi-defaults"
IPTV_DIR="${TARGET_DIR}/var/lib/iptv"
WEBUI_DIR="${TARGET_DIR}/usr/share/iptv-webui"
SYSTEMD_ETC="${TARGET_DIR}/etc/systemd/system"
SYSTEMD_VENDOR="${TARGET_DIR}/usr/lib/systemd/system"

log() { printf '>>> IPTV-Pi: %s\n' "$*"; }
warn() { printf '>>> IPTV-Pi: WARNING: %s\n' "$*" >&2; }

# enable_unit <unit file inside the image> <wants directory>
enable_unit() {
    local unit="$1" wants="$2"
    local name
    name="$(basename "${unit}")"
    install -d -m 0755 "${SYSTEMD_ETC}/${wants}.wants"
    ln -sf "${unit}" "${SYSTEMD_ETC}/${wants}.wants/${name}"
}

# --------------------------------------------------------------------------
# 1. Playlist web interface
# --------------------------------------------------------------------------
log "installing the playlist web interface"
if [ ! -f "${WEBUI_SRC}/app.py" ]; then
    echo "iptv-pi: ${WEBUI_SRC}/app.py not found" >&2
    exit 1
fi

install -d -m 0755 "${WEBUI_DIR}/templates" "${WEBUI_DIR}/static"
install -m 0644 "${WEBUI_SRC}/app.py" "${WEBUI_DIR}/app.py"
install -m 0755 "${WEBUI_SRC}/launch.sh" "${WEBUI_DIR}/launch.sh"
install -m 0644 "${WEBUI_SRC}/templates/index.html" "${WEBUI_DIR}/templates/index.html"
install -m 0644 "${WEBUI_SRC}/static/style.css" "${WEBUI_DIR}/static/style.css"

# --------------------------------------------------------------------------
# 2. Runtime directories and the playlist Kodi reads
# --------------------------------------------------------------------------
log "preparing the data mountpoint and the Kodi defaults"
# /var/lib/iptv is its own partition (LABEL=iptv-data); the rootfs only holds
# the empty mountpoint. Kodi's profile ships as defaults and is copied there on
# first boot by iptv-data-init, so a rootfs swap keeps the real profile.
install -d -m 0755 "${IPTV_DIR}"
install -d -m 0755 "${KODI_CFG_DIR}" \
    "${KODI_CFG_DIR}/userdata" \
    "${KODI_CFG_DIR}/userdata/addon_data/pvr.iptvsimple"

# --------------------------------------------------------------------------
# 3. Sanity checks (fail loudly instead of shipping a black screen)
# --------------------------------------------------------------------------
if [ -x "${TARGET_DIR}/usr/lib/kodi/kodi.bin" ]; then
    log "Kodi binary found: /usr/lib/kodi/kodi.bin"
else
    warn "/usr/lib/kodi/kodi.bin is missing - check BR2_PACKAGE_KODI"
fi

if [ -d "${TARGET_DIR}/usr/share/kodi/addons/pvr.iptvsimple" ]; then
    log "IPTV Simple Client add-on bundled in the image"
else
    warn "pvr.iptvsimple add-on is missing - check BR2_PACKAGE_KODI_PVR_IPTVSIMPLE"
fi

if [ ! -f "${KODI_CFG_DIR}/userdata/autoexec.py" ]; then
    warn "Kodi autoexec.py is missing from the overlay"
fi

# --------------------------------------------------------------------------
# 4. systemd units
# --------------------------------------------------------------------------
log "enabling systemd units"
enable_unit "/etc/systemd/system/kodi.service" "multi-user.target"
enable_unit "/etc/systemd/system/iptv-webui.service" "multi-user.target"
# Seeds the data partition before Kodi or the web UI read anything from it.
enable_unit "/etc/systemd/system/iptv-data-init.service" "multi-user.target"
# Marks the running A/B slot good once the UI is up; see iptv-rauc-mark-good.
enable_unit "/etc/systemd/system/iptv-rauc-mark-good.service" "multi-user.target"
# Scheduled playlist refresh (a no-op until a URL has been saved in the UI).
enable_unit "/etc/systemd/system/iptv-playlist-refresh.timer" "timers.target"
# Hardware log sample every 30 minutes (writes one line to hardware.log).
enable_unit "/etc/systemd/system/iptv-hardware-sample.timer" "timers.target"
# System health watchdog (failed units, journal errors, throttling, OOM, disk).
enable_unit "/etc/systemd/system/iptv-health-watch.timer" "timers.target"
enable_unit "/etc/systemd/system/wpa_supplicant-wlan0.service" "multi-user.target"
enable_unit "/etc/systemd/system/iptv-zram.service" "sysinit.target"
enable_unit "/etc/systemd/system/iptv-cpufreq.service" "multi-user.target"
enable_unit "/etc/systemd/system/iptv-wifi-import.service" "multi-user.target"
enable_unit "${SYSTEMD_VENDOR}/systemd-networkd.service" "multi-user.target"
enable_unit "${SYSTEMD_VENDOR}/systemd-networkd.socket" "sockets.target"
enable_unit "${SYSTEMD_VENDOR}/systemd-resolved.service" "multi-user.target"
enable_unit "${SYSTEMD_VENDOR}/systemd-timesyncd.service" "multi-user.target"
# Applies /etc/sysctl.d/60-iptv-vm.conf (VM tuning) at every boot.
enable_unit "${SYSTEMD_VENDOR}/systemd-sysctl.service" "sysinit.target"

# tty1 belongs to Kodi: no login shell may grab it.
rm -f "${SYSTEMD_ETC}/getty.target.wants/getty@tty1.service"

# --------------------------------------------------------------------------
# 5. DNS through systemd-resolved
# --------------------------------------------------------------------------
log "pointing /etc/resolv.conf at systemd-resolved"
ln -sf /run/systemd/resolve/stub-resolv.conf "${TARGET_DIR}/etc/resolv.conf"

# The FAT boot partition is mounted here by /etc/fstab (needed by the
# headless /boot/wifi.txt import).
install -d -m 0755 "${TARGET_DIR}/boot"

# The overlay cannot carry the executable bit reliably through every
# checkout, so make sure the helpers are executable.
chmod 0755 "${TARGET_DIR}/usr/bin/iptv-zram-setup" 2>/dev/null || true
chmod 0755 "${TARGET_DIR}/usr/bin/iptv-cpufreq-setup" 2>/dev/null || true
chmod 0755 "${TARGET_DIR}/usr/bin/iptv-wifi-import" 2>/dev/null || true
chmod 0755 "${TARGET_DIR}/usr/bin/iptv-boot-slot" 2>/dev/null || true
chmod 0755 "${TARGET_DIR}/usr/bin/iptv-data-init" 2>/dev/null || true
chmod 0755 "${TARGET_DIR}/usr/bin/iptv-rauc-mark-good" 2>/dev/null || true

# --------------------------------------------------------------------------
# 6. RAUC signing material (A/B system updates)
# --------------------------------------------------------------------------
# The private key never enters the image: it stays outside the target tree and
# is used by post-image.sh to sign the bundle. Only the certificate becomes the
# target keyring (/etc/rauc/ca.cert.pem), so a bundle signed with anything else
# is rejected by the box.
#
# The pair has to be the same for every build. An image trusts exactly the
# certificate it was built with, so a key generated per build (per board, per
# run) would make a bundle from a later release impossible to install on an
# already flashed box. It therefore lives outside output/ and is reused:
#
#   1. IPTVPI_RAUC_CERT + IPTVPI_RAUC_KEY   explicit files, e.g. a production key
#   2. IPTVPI_RAUC_KEYDIR                   default ${HOME}/.cache/iptvpi-rauc
#   3. a pair already in binaries/rauc       adopted into the key directory
#
# Only when none of the three exists is a new development key generated.
RAUC_DIR="${BINARIES_DIR}/rauc"
RAUC_CERT="${RAUC_DIR}/ca.cert.pem"
RAUC_KEY="${RAUC_DIR}/ca.key.pem"
RAUC_KEYDIR="${IPTVPI_RAUC_KEYDIR:-${HOME:-/root}/.cache/iptvpi-rauc}"

install -d -m 0755 "${RAUC_DIR}" "${TARGET_DIR}/etc/rauc"

if ! command -v openssl >/dev/null 2>&1; then
    warn "openssl is not available on the build host, skipping RAUC key setup"
elif [ -n "${IPTVPI_RAUC_CERT:-}" ] && [ -n "${IPTVPI_RAUC_KEY:-}" ]; then
    log "using the RAUC key material from IPTVPI_RAUC_CERT/IPTVPI_RAUC_KEY"
    install -m 0644 "${IPTVPI_RAUC_CERT}" "${RAUC_CERT}"
    install -m 0600 "${IPTVPI_RAUC_KEY}" "${RAUC_KEY}"
else
    if [ -n "${IPTVPI_RAUC_CERT:-}" ] || [ -n "${IPTVPI_RAUC_KEY:-}" ]; then
        warn "IPTVPI_RAUC_CERT and IPTVPI_RAUC_KEY must be set together, using ${RAUC_KEYDIR}"
    fi
    install -d -m 0700 "${RAUC_KEYDIR}"
    if [ -f "${RAUC_KEYDIR}/ca.cert.pem" ] && [ -f "${RAUC_KEYDIR}/ca.key.pem" ]; then
        log "reusing the RAUC signing key from ${RAUC_KEYDIR}"
    elif [ -f "${RAUC_CERT}" ] && [ -f "${RAUC_KEY}" ]; then
        log "adopting the RAUC signing key already in ${RAUC_DIR}"
        install -m 0644 "${RAUC_CERT}" "${RAUC_KEYDIR}/ca.cert.pem"
        install -m 0600 "${RAUC_KEY}" "${RAUC_KEYDIR}/ca.key.pem"
    else
        log "generating a RAUC development signing key in ${RAUC_KEYDIR}"
        log "every build reuses it; pass IPTVPI_RAUC_CERT/IPTVPI_RAUC_KEY for a production key"
        openssl req -x509 -newkey rsa:4096 -nodes -days 3650 \
            -subj "/CN=IPTV-Pi development signing key" \
            -keyout "${RAUC_KEYDIR}/ca.key.pem" \
            -out "${RAUC_KEYDIR}/ca.cert.pem" >/dev/null 2>&1
        chmod 0600 "${RAUC_KEYDIR}/ca.key.pem"
    fi
    install -m 0644 "${RAUC_KEYDIR}/ca.cert.pem" "${RAUC_CERT}"
    install -m 0600 "${RAUC_KEYDIR}/ca.key.pem" "${RAUC_KEY}"
fi

if [ -f "${RAUC_CERT}" ]; then
    install -m 0644 "${RAUC_CERT}" "${TARGET_DIR}/etc/rauc/ca.cert.pem"
    # A key that changes between builds silently breaks updates for every box
    # that is already flashed, so its fingerprint always shows up in the log.
    cert_fp="$(openssl x509 -in "${RAUC_CERT}" -noout -fingerprint -sha256 2>/dev/null | sed 's/^.*=//' || true)"
    if [ -n "${cert_fp}" ]; then
        log "RAUC signing certificate (SHA-256): ${cert_fp}"
    fi
fi

# The slot UUIDs used by iptv-boot-slot and RAUC's system.conf come from a
# pinned disk signature in post-image.sh. Warn early when they disagree.
DISK_PARTUUID="${IPTVPI_DISK_PARTUUID:-49505456}"
SLOTS_CONF="${TARGET_DIR}/etc/iptv-slots.conf"
if [ -f "${SLOTS_CONF}" ]; then
    configured="$(sed -n 's/^ROOTFS_A="\(.*\)"$/\1/p' "${SLOTS_CONF}" | head -1)"
    if [ -n "${configured}" ] && [ "${configured}" != "${DISK_PARTUUID}-02" ]; then
        warn "etc/iptv-slots.conf says ${configured} but IPTVPI_DISK_PARTUUID is ${DISK_PARTUUID}; the boot script and the partition table would disagree"
    fi
else
    warn "etc/iptv-slots.conf is missing from the overlay"
fi

# --------------------------------------------------------------------------
# 7. Optional: set a root password from the environment
# --------------------------------------------------------------------------
# build_os.sh exports IPTVPI_ROOT_PASSWORD if the user asked for one, so that
# no default password ever ends up in this repository.
if [ -n "${IPTVPI_ROOT_PASSWORD:-}" ]; then
    log "setting the root password from IPTVPI_ROOT_PASSWORD"
    if ! command -v openssl >/dev/null 2>&1; then
        warn "openssl is not available on the build host, cannot hash the password"
    else
    hash="$(openssl passwd -6 "${IPTVPI_ROOT_PASSWORD}")"
    if [ -f "${TARGET_DIR}/etc/shadow" ]; then
        # Passwords are hashed; keep the account unlocked.
        sed -i "s|^root:[^:]*:|root:${hash}:|" "${TARGET_DIR}/etc/shadow"
        if grep -q '^root::' "${TARGET_DIR}/etc/shadow"; then
            warn "could not update /etc/shadow"
        fi
    else
        warn "/etc/shadow not found, skipping password setup"
    fi
    fi
fi

log "post-build finished"
