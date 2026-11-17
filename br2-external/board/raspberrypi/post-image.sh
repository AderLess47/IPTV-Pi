#!/bin/bash
#
# IPTV-Pi :: Buildroot post-image hook
#
# Assembles a ready-to-flash SD card image with an A/B rootfs layout:
#
#   partition 1 (FAT, bootable) : firmware + zImage + DTBs + config.txt/cmdline.txt
#   partition 2 (ext4)          : rootfs slot A (the image built by Buildroot)
#   partition 3 (ext4)          : rootfs slot B (empty until the first RAUC update)
#   partition 4 (ext4)          : data partition (LABEL=iptv-data), mounted at
#                                 /var/lib/iptv: playlists, state, logs, web-UI
#                                 updates and the Kodi profile survive a swap
#
# The FAT partition is staged in a clean directory first so it contains exactly
# what the VideoCore firmware expects at its root, with our own
# config.txt/cmdline.txt in place of Buildroot's defaults. cmdline.txt points at
# the PARTUUID of slot A and carries rauc.slot=A; /usr/bin/iptv-boot-slot
# rewrites both when RAUC switches slots.
#
# The same rootfs image is also packed into a signed RAUC bundle
# (binaries/iptvpi-<version>.raucb) for `rauc install`.
#
set -euo pipefail

BINARIES_DIR="${1:?usage: post-image.sh <binaries-dir>}"
BOARD_DIR="$(cd "$(dirname "$0")" && pwd)"

FIRMWARE_DIR="${BINARIES_DIR}/rpi-firmware"
BUILD_TMP="${BUILD_DIR:-${BINARIES_DIR}}"
STAGE_DIR="${BUILD_TMP}/iptvpi-boot"
GENIMAGE_TMP="${BUILD_TMP}/genimage.tmp"
# genimage copies --rootpath into <tmppath>/root and refuses to start when the
# tmppath it is given already holds anything, so the two must be separate
# directories and only the root one may be created by us.
GENIMAGE_ROOT="${BUILD_TMP}/genimage.root"
GENIMAGE_CFG="${STAGE_DIR}/genimage.cfg"

BOOT_PART_SIZE="${IPTVPI_BOOT_SIZE:-64M}"
# A slot must be larger than BR2_TARGET_ROOTFS_EXT2_SIZE (1024M in the
# defconfigs), otherwise the image does not fit and there is no room for
# resize2fs on a grown slot.
ROOTFS_SLOT_SIZE="${IPTVPI_ROOTFS_SLOT_SIZE:-1100M}"
DATA_SIZE="${IPTVPI_DATA_SIZE:-1024M}"
# Pinned MBR disk signature: PARTUUIDs become <signature>-NN, which is what
# /etc/iptv-slots.conf, /etc/rauc/system.conf and cmdline.txt refer to. Keep
# these in sync; post-build.sh warns when they do not match.
DISK_PARTUUID="${IPTVPI_DISK_PARTUUID:-49505456}"
DISK_SIGNATURE="0x${DISK_PARTUUID}"
VERSION="${IPTVPI_VERSION:-$(date -u +%Y.%m.%d-%H%M)}"

log() { printf '>>> IPTV-Pi: %s\n' "$*"; }
warn() { printf '>>> IPTV-Pi: WARNING: %s\n' "$*" >&2; }

# Work out which board is being built: the build script exports IPTVPI_BOARD,
# otherwise fall back to the configuration / the DTB that was built.
detect_board() {
    if [ -n "${IPTVPI_BOARD:-}" ]; then
        printf '%s\n' "${IPTVPI_BOARD}"
        return
    fi
    if [ -n "${BR2_CONFIG:-}" ] && [ -f "${BR2_CONFIG}" ] \
        && grep -qs '^BR2_cortex_a7=y' "${BR2_CONFIG}"; then
        printf 'pi2\n'
        return
    fi
    if [ -f "${BINARIES_DIR}/bcm2709-rpi-2-b.dtb" ]; then
        printf 'pi2\n'
        return
    fi
    printf 'pi1\n'
}

BOARD="$(detect_board)"
case "${BOARD}" in
    pi1|pi2) ;;
    *)
        echo "iptv-pi: unknown board '${BOARD}' (expected pi1 or pi2)" >&2
        exit 1
        ;;
esac

log "assembling the SD card image for ${BOARD}"

if [ ! -d "${FIRMWARE_DIR}" ]; then
    echo "iptv-pi: ${FIRMWARE_DIR} is missing (rpi-firmware not installed)" >&2
    exit 1
fi
if [ ! -f "${BINARIES_DIR}/rootfs.ext4" ]; then
    echo "iptv-pi: ${BINARIES_DIR}/rootfs.ext4 is missing" >&2
    exit 1
fi
# Buildroot leaves images/rootfs.ext4 as a symlink to rootfs.ext2, and both
# stat(1) and `ln -f` act on the link itself rather than on the image: resolve
# it once here so the size check and the staging step see the real file.
ROOTFS_IMG="$(readlink -f "${BINARIES_DIR}/rootfs.ext4")"

# The rootfs image has to fit into a slot with room to spare for its own
# growth, otherwise genimage fails with a much less helpful message.
ROOTFS_MB=$(( $(stat -c '%s' "${ROOTFS_IMG}") / 1048576 ))
SLOT_MB="${ROOTFS_SLOT_SIZE%[Mm]}"
if [ "${ROOTFS_MB}" -ge "${SLOT_MB}" ]; then
    echo "iptv-pi: rootfs.ext4 is ${ROOTFS_MB} MB but a slot is only ${ROOTFS_SLOT_SIZE}" >&2
    echo "iptv-pi: raise IPTVPI_ROOTFS_SLOT_SIZE or shrink BR2_TARGET_ROOTFS_EXT2_SIZE" >&2
    exit 1
fi

# --------------------------------------------------------------------------
# Stage the boot partition
# --------------------------------------------------------------------------
rm -rf "${STAGE_DIR}"
mkdir -p "${STAGE_DIR}"

for entry in "${FIRMWARE_DIR}"/*; do
    name="$(basename "${entry}")"
    case "${name}" in
        # Replaced by the board specific versions below / not needed at root.
        config.txt|cmdline.txt|*.dtb|overlays) continue ;;
    esac
    [ -f "${entry}" ] && cp -f "${entry}" "${STAGE_DIR}/${name}"
done

cp -f "${BINARIES_DIR}/zImage" "${STAGE_DIR}/"
for dtb in "${BINARIES_DIR}"/*.dtb; do
    [ -f "${dtb}" ] && cp -f "${dtb}" "${STAGE_DIR}/"
done

install -m 0644 "${BOARD_DIR}/config-${BOARD}.txt" "${STAGE_DIR}/config.txt"

# cmdline.txt: root lives on the PARTUUID of slot A, and rauc.slot tells RAUC
# which slot is running. iptv-boot-slot rewrites both on a slot switch.
sed -e "s#root=[^ ]*#root=PARTUUID=${DISK_PARTUUID}-02#" \
    -e 's/ *rauc\.slot=[^ ]*//g' "${BOARD_DIR}/cmdline.txt" \
    | tr -d '\n' > "${STAGE_DIR}/cmdline.txt"
# panic=10 turns a kernel panic into a reboot after ten seconds instead of a
# hung box on the TV wall. It comes from cmdline.txt; this keeps it there even
# when that file is edited by hand.
grep -q 'panic=' "${STAGE_DIR}/cmdline.txt" \
    || printf ' panic=10' >> "${STAGE_DIR}/cmdline.txt"
printf ' rauc.slot=A\n' >> "${STAGE_DIR}/cmdline.txt"

# The rootfs image is referenced by genimage through the input path; link
# instead of copying the file (ROOTFS_IMG is the resolved file, see above).
if ! ln -f "${ROOTFS_IMG}" "${STAGE_DIR}/rootfs.ext4" 2>/dev/null; then
    cp -fL "${ROOTFS_IMG}" "${STAGE_DIR}/rootfs.ext4"
fi

# --------------------------------------------------------------------------
# Data partition (empty ext4; iptv-data-init seeds it on first boot)
# --------------------------------------------------------------------------
log "creating the data filesystem (${DATA_SIZE})"
rm -f "${STAGE_DIR}/data.ext4"
truncate -s "${DATA_SIZE}" "${STAGE_DIR}/data.ext4"
if ! mke2fs -q -t ext4 -F -L iptv-data -m 0 "${STAGE_DIR}/data.ext4"; then
    echo "iptv-pi: mke2fs failed (is e2fsprogs available on the build host?)" >&2
    exit 1
fi

# --------------------------------------------------------------------------
# genimage description
# --------------------------------------------------------------------------
{
    printf 'image boot.vfat {\n'
    printf '\tvfat {\n'
    printf '\t\tfiles = {\n'
    # Everything staged in STAGE_DIR ends up at the root of the boot
    # partition, except the generated manifest and the partition images.
    for entry in "${STAGE_DIR}"/*; do
        name="$(basename "${entry}")"
        case "${name}" in
            genimage.cfg|rootfs.ext4|data.ext4) continue ;;
        esac
        printf '\t\t\t"%s",\n' "${name}"
    done
    printf '\t\t}\n'
    printf '\t}\n'
    printf '\tsize = %s\n' "${BOOT_PART_SIZE}"
    printf '}\n\n'
    printf 'image sdcard.img {\n'
    printf '\thdimage {\n'
    # genimage 13 (what Buildroot 2021.02 ships) has no partition-table-type
    # option: an MBR table is the default and all there is unless gpt = true.
    printf '\t\tdisk-signature = %s\n' "${DISK_SIGNATURE}"
    printf '\t}\n\n'
    printf '\tpartition boot {\n'
    printf '\t\tpartition-type = 0xC\n'
    printf '\t\tbootable = "true"\n'
    printf '\t\timage = "boot.vfat"\n'
    printf '\t}\n\n'
    printf '\tpartition rootfs_a {\n'
    printf '\t\tpartition-type = 0x83\n'
    printf '\t\timage = "rootfs.ext4"\n'
    printf '\t\tsize = %s\n' "${ROOTFS_SLOT_SIZE}"
    printf '\t}\n\n'
    printf '\tpartition rootfs_b {\n'
    printf '\t\tpartition-type = 0x83\n'
    printf '\t\tsize = %s\n' "${ROOTFS_SLOT_SIZE}"
    printf '\t}\n\n'
    printf '\tpartition data {\n'
    printf '\t\tpartition-type = 0x83\n'
    printf '\t\timage = "data.ext4"\n'
    printf '\t\tsize = %s\n' "${DATA_SIZE}"
    printf '\t}\n'
    printf '}\n'
} > "${GENIMAGE_CFG}"

rm -rf "${GENIMAGE_TMP}" "${GENIMAGE_ROOT}"
mkdir -p "${GENIMAGE_ROOT}"

log "running genimage"
genimage \
    --rootpath "${GENIMAGE_ROOT}" \
    --tmppath "${GENIMAGE_TMP}" \
    --inputpath "${STAGE_DIR}" \
    --outputpath "${BINARIES_DIR}" \
    --config "${GENIMAGE_CFG}"

log "SD card image ready: ${BINARIES_DIR}/sdcard.img"

# --------------------------------------------------------------------------
# RAUC bundle for A/B system updates
# --------------------------------------------------------------------------
# post-build.sh resolves the signing pair (IPTVPI_RAUC_CERT/IPTVPI_RAUC_KEY,
# else IPTVPI_RAUC_KEYDIR) and stages it in binaries/rauc, so every build - both
# boards and every release - signs with the same certificate.
RAUC_BIN="${HOST_DIR:-/usr}/bin/rauc"
RAUC_CERT="${BINARIES_DIR}/rauc/ca.cert.pem"
RAUC_KEY="${BINARIES_DIR}/rauc/ca.key.pem"

if [ ! -x "${RAUC_BIN}" ]; then
    warn "host rauc not found, skipping the update bundle"
elif [ ! -f "${RAUC_CERT}" ] || [ ! -f "${RAUC_KEY}" ]; then
    warn "no RAUC key material in ${BINARIES_DIR}/rauc, skipping the update bundle"
else
    BUNDLE_DIR="${BUILD_TMP}/iptvpi-bundle"
    rm -rf "${BUNDLE_DIR}"
    mkdir -p "${BUNDLE_DIR}"
    cp -f "${BINARIES_DIR}/rootfs.ext4" "${BUNDLE_DIR}/rootfs.ext4"
    cat > "${BUNDLE_DIR}/manifest.raucm" <<EOF
[update]
compatible=iptvpi
version=${VERSION}

[bundle]
# plain, not verity: no host-side veritysetup needed, the bundle is signed
format=plain

[image.rootfs]
filename=rootfs.ext4
EOF
    log "signing the RAUC bundle (version ${VERSION})"
    "${RAUC_BIN}" bundle --cert "${RAUC_CERT}" --key "${RAUC_KEY}" \
        "${BUNDLE_DIR}" "${BINARIES_DIR}/iptvpi-${VERSION}.raucb"
    log "update bundle ready: ${BINARIES_DIR}/iptvpi-${VERSION}.raucb"
    # The web UI can compare an upload against this number before installing.
    ( cd "${BINARIES_DIR}" && sha256sum "iptvpi-${VERSION}.raucb" \
        > "iptvpi-${VERSION}.raucb.sha256" )
    log "checksum ready: ${BINARIES_DIR}/iptvpi-${VERSION}.raucb.sha256"
fi
