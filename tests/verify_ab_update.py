#!/usr/bin/env python3
"""IPTV-Pi: A/B updates, data partition and RAUC integration.

    python3 tests/verify_ab_update.py

Three kinds of checks:
  1. behavioural - iptv-boot-slot and iptv-data-init run in temp dirs with
     their IPTV_* overrides, exactly as systemd/RAUC call them;
  2. end-to-end  - post-image.sh runs against a fake binaries dir with stubbed
     genimage/mke2fs/rauc, and its generated files are inspected;
  3. static      - unit files, fstab, system.conf, defconfigs, README and the
     overlay layout (no stale single-partition assumptions left).

Runs on Python 3 and on Python 2.7 (the image itself boots Kodi 18, which
brings 2.7), so both are checked by CI.
"""

from __future__ import print_function

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOARD = os.path.join(ROOT, "br2-external/board/raspberrypi")
OVERLAY = os.path.join(BOARD, "rootfs-overlay")
UNITS = os.path.join(OVERLAY, "etc/systemd/system")

passed = 0
failures = []


def check(cond, msg):
    global passed
    if cond:
        passed += 1
    else:
        failures.append(msg)
        print("FAIL: " + msg)


def read(path):
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8", "replace")


def which(name):
    """shutil.which() equivalent: it only exists from Python 3.3 on."""
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        path = os.path.join(directory, name)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def run(cmd, env=None, cwd=None):
    proc = subprocess.Popen(cmd, env=env, cwd=cwd,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = proc.communicate()
    return (proc.returncode,
            out.decode("utf-8", "replace"),
            err.decode("utf-8", "replace"))


def script(path):
    return read(path).replace("\r\n", "\n")


# --------------------------------------------------------------------------
# 1. iptv-boot-slot (RAUC custom bootloader backend)
# --------------------------------------------------------------------------
def test_boot_slot():
    boot_slot = os.path.join(OVERLAY, "usr/bin/iptv-boot-slot")
    tmp = tempfile.mkdtemp(prefix="iptv-bootslot-")
    boot = os.path.join(tmp, "boot")
    os.makedirs(boot)
    slots = os.path.join(tmp, "iptv-slots.conf")
    with open(slots, "w") as fh:
        fh.write('ROOTFS_A="49505456-02"\nROOTFS_B="49505456-03"\n')
    cmdline = os.path.join(boot, "cmdline.txt")
    with open(cmdline, "w") as fh:
        fh.write("root=/dev/mmcblk0p2 rootwait rootfstype=ext4 "
                 "console=ttyAMA0,115200 quiet\n")
    proc_cmdline = os.path.join(tmp, "proc-cmdline")
    with open(proc_cmdline, "w") as fh:
        fh.write("console=tty1 root=PARTUUID=49505456-02 rootwait rauc.slot=A\n")

    env = dict(os.environ)
    env["IPTV_BOOT_DIR"] = boot
    env["IPTV_SLOTS_CONF"] = slots
    env["IPTV_PROC_CMDLINE"] = proc_cmdline

    rc, out, err = run(["/bin/sh", boot_slot, "get-primary"], env=env)
    check(rc == 0, "get-primary exits 0 on a fresh boot (%s)" % err.strip())
    check(out.strip() == "A", "get-primary reports A on a fresh boot, got %r" % out)

    rc, out, err = run(["/bin/sh", boot_slot, "get-current"], env=env)
    check(rc == 0 and out.strip() == "A", "get-current reads rauc.slot=A")
    check(err.strip() == "", "get-current writes nothing to stderr")

    rc, out, _ = run(["/bin/sh", boot_slot, "get-state", "B"], env=env)
    check(rc == 0 and out.strip() == "good", "get-state defaults to good")
    rc, out, _ = run(["/bin/sh", boot_slot, "get-state", "A"], env=env)
    check(rc == 0 and out.strip() == "good", "get-state A defaults to good")

    rc, out, err = run(["/bin/sh", boot_slot, "set-primary", "B"], env=env)
    check(rc == 0, "set-primary B exits 0 (%s)" % err.strip())
    new = read(cmdline)
    check("root=PARTUUID=49505456-03" in new, "set-primary B rewrites root= to slot B")
    check("rauc.slot=B" in new, "set-primary B writes rauc.slot=B")
    check("/dev/mmcblk0p2" not in new, "set-primary B removes the old root=")
    check(new.count("root=") == 1, "cmdline has exactly one root= after the switch")
    check(new.count("rauc.slot=") == 1, "cmdline has one rauc.slot= after the switch")
    check(new.endswith("\n"), "cmdline still ends with a newline")
    check(len(new.splitlines()) == 1, "cmdline stays a single line")
    check(new.startswith("root=PARTUUID=49505456-03 "), "root= stays the first token")
    for token in ("rootwait", "rootfstype=ext4", "console=ttyAMA0,115200", "quiet"):
        check(token in new.split(), "set-primary keeps the %r token" % token)
    # A panic has to end in a reboot, not in a hung box on the wall.
    check("panic=10" in new.split(),
          "set-primary adds panic=10 when the cmdline lacks it, got %r" % new)
    state = os.path.join(boot, "rauc", "primary")
    check(os.path.isfile(state), "/boot/rauc/primary is written")
    check(read(state).strip() == "B", "/boot/rauc/primary records B")

    rc, out, _ = run(["/bin/sh", boot_slot, "get-primary"], env=env)
    check(rc == 0 and out.strip() == "B", "get-primary follows the recorded slot")

    run(["/bin/sh", boot_slot, "set-primary", "A"], env=env)
    run(["/bin/sh", boot_slot, "set-primary", "B"], env=env)
    again = read(cmdline)
    check(again.count("rauc.slot=") == 1, "repeated switches keep one rauc.slot=")
    check(again.count("root=PARTUUID=49505456-03") == 1, "repeated switches keep one root=")
    check(again.count("panic=10") == 1, "repeated switches keep exactly one panic=10")

    rc, _, _ = run(["/bin/sh", boot_slot, "set-state", "A", "bad"], env=env)
    check(rc == 0, "set-state A bad exits 0")
    check(read(os.path.join(boot, "rauc", "state-A")).strip() == "bad", "state-A is bad")
    rc, out, _ = run(["/bin/sh", boot_slot, "get-state", "A"], env=env)
    check(out.strip() == "bad", "get-state reports bad for A")
    rc, out, _ = run(["/bin/sh", boot_slot, "get-state", "B"], env=env)
    check(out.strip() == "good", "the state of B is unaffected")

    rc, out, err = run(["/bin/sh", boot_slot], env=env)
    check(rc != 0, "no argument is an error")
    check("unknown command" in err, "no argument explains itself on stderr")
    rc, _, err = run(["/bin/sh", boot_slot, "frobnicate"], env=env)
    check(rc != 0 and "unknown command" in err, "an unknown command fails")
    rc, _, err = run(["/bin/sh", boot_slot, "get-state", "C"], env=env)
    check(rc != 0 and "unknown slot" in err, "get-state rejects an unknown slot")
    rc, _, err = run(["/bin/sh", boot_slot, "set-primary", "C"], env=env)
    check(rc != 0 and "unknown slot" in err, "set-primary rejects an unknown slot")
    rc, _, err = run(["/bin/sh", boot_slot, "set-state", "A", "maybe"], env=env)
    check(rc != 0 and "unknown state" in err, "set-state rejects an unknown state")
    rc, _, _ = run(["/bin/sh", boot_slot, "set-state", "A"], env=env)
    check(rc != 0, "set-state without a state fails")
    rc, _, _ = run(["/bin/sh", boot_slot, "set-primary"], env=env)
    check(rc != 0, "set-primary without a slot fails")

    env_bad = dict(env)
    env_bad["IPTV_SLOTS_CONF"] = os.path.join(tmp, "nope.conf")
    rc, _, err = run(["/bin/sh", boot_slot, "get-primary"], env=env_bad)
    check(rc != 0 and "cannot read" in err, "a missing slots.conf is a hard failure")

    os.rename(cmdline, cmdline + ".away")
    rc, _, err = run(["/bin/sh", boot_slot, "set-primary", "A"], env=env)
    check(rc != 0 and "does not exist" in err, "set-primary fails without cmdline.txt")
    os.rename(cmdline + ".away", cmdline)

    with open(proc_cmdline, "w") as fh:
        fh.write("console=tty1 root=PARTUUID=49505456-03 rootwait\n")
    rc, out, _ = run(["/bin/sh", boot_slot, "get-current"], env=env)
    check(out.strip() == "B", "get-current falls back to the root= PARTUUID")
    with open(proc_cmdline, "w") as fh:
        fh.write("console=tty1 root=/dev/mmcblk0p2\n")
    rc, _, err = run(["/bin/sh", boot_slot, "get-current"], env=env)
    check(rc != 0, "get-current fails when it cannot identify the slot")

    shutil.rmtree(tmp)


# --------------------------------------------------------------------------
# 2. iptv-data-init (seeding of the data partition)
# --------------------------------------------------------------------------
def test_data_init():
    data_init = os.path.join(OVERLAY, "usr/bin/iptv-data-init")
    tmp = tempfile.mkdtemp(prefix="iptv-data-init-")
    data = os.path.join(tmp, "var-lib-iptv")
    kodi = os.path.join(data, "kodi")
    defaults = os.path.join(tmp, "defaults")
    os.makedirs(kodi)
    os.makedirs(os.path.join(defaults, "userdata/addon_data/pvr.iptvsimple"))
    for name in ("advancedsettings.xml", "guisettings.xml", "autoexec.py"):
        with open(os.path.join(defaults, "userdata", name), "w") as fh:
            fh.write("<%s/>\n" % name)

    env = dict(os.environ)
    env.update(IPTV_DIR=data, IPTV_KODI_DIR=kodi, IPTV_KODI_DEFAULTS=defaults,
               IPTV_DATA_INIT_ASSUME_MOUNTED="1")

    rc, out, err = run(["/bin/sh", data_init], env=env)
    check(rc == 0, "iptv-data-init succeeds on a fresh data partition (%s)" % err.strip())
    for name in ("advancedsettings.xml", "guisettings.xml", "autoexec.py"):
        check(os.path.isfile(os.path.join(kodi, "userdata", name)),
              "%s is seeded" % name)
    check(os.path.isdir(os.path.join(kodi, "userdata/addon_data/pvr.iptvsimple")),
          "the pvr.iptvsimple addon dir exists")
    check(os.path.isfile(os.path.join(kodi, ".seeded")), "the .seeded marker is written")
    playlist = os.path.join(data, "playlist.m3u")
    check(os.path.isfile(playlist), "a placeholder playlist is created")
    check(read(playlist).startswith("#EXTM3U"), "the placeholder playlist is a valid M3U")
    check(os.path.isdir(os.path.join(data, "log")), "the log directory exists")

    with open(playlist, "w") as fh:
        fh.write("#EXTM3U\n#EXTINF:-1,Das Erste\nhttp://example/1\n")
    with open(os.path.join(kodi, "userdata/guisettings.xml"), "w") as fh:
        fh.write("<settings>user</settings>\n")
    rc, out, err = run(["/bin/sh", data_init], env=env)
    check(rc == 0, "iptv-data-init is idempotent")
    check("Das Erste" in read(playlist), "an existing playlist is never overwritten")
    check("user" in read(os.path.join(kodi, "userdata/guisettings.xml")),
          "an existing Kodi profile is never overwritten")
    check(read(os.path.join(kodi, ".seeded")).strip().startswith("seeded at"),
          "the marker keeps its original timestamp")

    with open(os.path.join(defaults, "userdata/newdefault.xml"), "w") as fh:
        fh.write("<new/>\n")
    run(["/bin/sh", data_init], env=env)
    check(not os.path.exists(os.path.join(kodi, "userdata/newdefault.xml")),
          "new defaults do not leak into an existing profile")

    env_nomount = dict(env)
    env_nomount.pop("IPTV_DATA_INIT_ASSUME_MOUNTED")
    rc, out, err = run(["/bin/sh", data_init], env=env_nomount)
    if which("mountpoint"):
        check(rc != 0 and "not a mountpoint" in err,
              "refuses to seed when the data partition is not mounted")
    else:
        check(True, "mountpoint is unavailable, the guard cannot be exercised")

    env_missing = dict(env)
    env_missing["IPTV_DIR"] = os.path.join(tmp, "gone")
    rc, _, err = run(["/bin/sh", data_init], env=env_missing)
    check(rc != 0 and "missing" in err, "a missing data directory is a hard failure")

    shutil.rmtree(tmp)


# --------------------------------------------------------------------------
# 3. post-image.sh end to end with stubbed genimage/mke2fs/rauc
# --------------------------------------------------------------------------
def test_post_image():
    tmp = tempfile.mkdtemp(prefix="iptv-postimage-")
    binaries = os.path.join(tmp, "binaries")
    firmware = os.path.join(binaries, "rpi-firmware")
    stubbin = os.path.join(tmp, "bin")
    os.makedirs(firmware)
    os.makedirs(stubbin)
    os.makedirs(os.path.join(binaries, "rauc"))
    os.makedirs(os.path.join(tmp, "host/bin"))

    for name in ("bootcode.bin", "start.elf", "fixup.dat", "config.txt", "cmdline.txt"):
        with open(os.path.join(firmware, name), "wb") as fh:
            fh.write(b"firmware:" + name.encode())
    with open(os.path.join(firmware, "bcm2708-rpi-b.dtb"), "wb") as fh:
        fh.write(b"dtb-firmware")
    with open(os.path.join(binaries, "bcm2708-rpi-b.dtb"), "wb") as fh:
        fh.write(b"dtb-built")
    with open(os.path.join(binaries, "zImage"), "wb") as fh:
        fh.write(b"kernel")
    # Buildroot leaves images/rootfs.ext4 as a symlink to rootfs.ext2
    # (BR2_TARGET_ROOTFS_EXT2_4); staging has to follow that link.
    with open(os.path.join(binaries, "rootfs.ext2"), "wb") as fh:
        fh.truncate(4 * 1024 * 1024)
    os.symlink("rootfs.ext2", os.path.join(binaries, "rootfs.ext4"))
    with open(os.path.join(binaries, "rauc/ca.cert.pem"), "w") as fh:
        fh.write("cert\n")
    with open(os.path.join(binaries, "rauc/ca.key.pem"), "w") as fh:
        fh.write("key\n")

    log = os.path.join(tmp, "calls.log")
    # The genimage stub rejects what the real genimage 13 rejects: a --tmppath
    # that already holds files, a tmppath that doubles as the rootpath, and an
    # image named in the config that is not readable below the input path.
    genimage_stub = (
        'tmppath=""\nrootpath=""\ninputpath=""\nprev=""\n'
        'for a in "$@"; do\n'
        '  case "$prev" in\n'
        '    --tmppath) tmppath="$a" ;;\n'
        '    --rootpath) rootpath="$a" ;;\n'
        '    --inputpath) inputpath="$a" ;;\n'
        '  esac\n'
        '  prev="$a"\n'
        'done\n'
        'if [ -n "$tmppath" ] && [ "$tmppath" = "$rootpath" ]; then\n'
        '  echo "genimage stub: tmppath and rootpath are the same directory" >&2\n'
        '  exit 1\n'
        'fi\n'
        'if [ -n "$tmppath" ] && [ -d "$tmppath" ] \\\n'
        '   && [ -n "$(ls -A "$tmppath")" ]; then\n'
        '  echo "genimage stub: tmppath $tmppath exists and is not empty" >&2\n'
        '  exit 1\n'
        'fi\n'
        'for img in rootfs.ext4 data.ext4; do\n'
        '  [ -e "$inputpath/$img" ] && continue\n'
        '  echo "genimage stub: file($img): stat($inputpath/$img) failed: " \\\n'
        '       "No such file or directory" >&2\n'
        '  exit 1\n'
        'done\n'
        'printf "genimage %s\\n" "$*" >> "' + log + '"\n'
    )
    stubs = {
        "genimage": genimage_stub,
        "mke2fs": 'printf "mke2fs %s\\n" "$*" >> "' + log + '"\n',
        "rauc": ('printf "rauc %s\\n" "$*" >> "' + log + '"\n'
                 'out="${!#}"\n: > "$out"\n'),
    }
    for name, body in stubs.items():
        path = os.path.join(stubbin, name)
        with open(path, "w") as fh:
            fh.write("#!/bin/bash\n" + body)
        os.chmod(path, 0o755)
    os.symlink(os.path.join(stubbin, "rauc"), os.path.join(tmp, "host/bin/rauc"))

    env = dict(os.environ)
    env["PATH"] = stubbin + ":" + env["PATH"]
    env["BUILD_DIR"] = os.path.join(tmp, "build")
    env["IPTVPI_BOARD"] = "pi1"
    env["IPTVPI_DATA_SIZE"] = "8M"
    env["IPTVPI_VERSION"] = "1.2.3"
    env["HOST_DIR"] = os.path.join(tmp, "host")

    rc, out, err = run(["bash", os.path.join(BOARD, "post-image.sh"), binaries], env=env)
    check(rc == 0, "post-image.sh completes with stubs (%s)" % err.strip()[-500:])
    check("SD card image ready" in out, "post-image.sh announces the image")
    check("update bundle ready" in out, "post-image.sh announces the bundle")

    stage = os.path.join(tmp, "build/iptvpi-boot")
    genimage_cfg = os.path.join(stage, "genimage.cfg")
    check(os.path.isfile(genimage_cfg), "a genimage config is generated")
    cfg = read(genimage_cfg)
    check("partition-table-type" not in cfg,
          "the config leaves out options genimage 13 does not know")
    check("disk-signature = 0x49505456" in cfg, "the disk signature is pinned")
    for part in ("boot", "rootfs_a", "rootfs_b", "data"):
        check("partition %s {" % part in cfg, "genimage has the %s partition" % part)
    order = [cfg.index("partition %s {" % p) for p in ("boot", "rootfs_a", "rootfs_b", "data")]
    check(order == sorted(order), "partitions are ordered boot/rootfs_a/rootfs_b/data")
    check('image = "rootfs.ext4"' in cfg and 'image = "data.ext4"' in cfg,
          "both filesystem images are referenced")
    check(cfg.count("size = 1100M") == 2, "both rootfs slots are 1100M")
    check("size = 8M" in cfg, "the data partition honours IPTVPI_DATA_SIZE")
    check("partition-type = 0xC" in cfg, "the boot partition keeps the FAT type byte")
    check('bootable = "true"' in cfg, "the boot partition is bootable")

    vfat = cfg.split("image sdcard.img", 1)[0]
    check('"bootcode.bin"' in vfat and '"start.elf"' in vfat, "firmware files are staged")
    check('"zImage"' in vfat, "the kernel is staged")
    check('"bcm2708-rpi-b.dtb"' in vfat, "the DTB is staged")
    check('"rootfs.ext4"' not in vfat, "rootfs.ext4 is not copied into boot.vfat")
    check('"data.ext4"' not in vfat, "data.ext4 is not copied into boot.vfat")
    check('"genimage.cfg"' not in vfat, "genimage.cfg is not copied into boot.vfat")
    check('"config.txt"' in vfat, "a config.txt is staged")

    check(read(os.path.join(stage, "config.txt")) == read(os.path.join(BOARD, "config-pi1.txt")),
          "the board config.txt replaces the firmware one")

    cmdline = read(os.path.join(stage, "cmdline.txt"))
    check(cmdline.startswith("root=PARTUUID=49505456-02 "), "cmdline boots slot A by PARTUUID")
    check(cmdline.count("rauc.slot=") == 1 and "rauc.slot=A" in cmdline,
          "cmdline carries exactly one rauc.slot=A")
    check(cmdline.count("root=") == 1, "cmdline has exactly one root=")
    check(cmdline.endswith("\n"), "cmdline ends with a newline")
    check("rootwait" in cmdline.split() and "quiet" in cmdline.split(),
          "the original cmdline options are preserved")
    check("panic=10" in cmdline.split(),
          "the image cmdline reboots ten seconds after a kernel panic, got %r" % cmdline)

    check(os.path.isfile(os.path.join(stage, "data.ext4")),
          "the data filesystem image is created")
    calls = read(log)
    check("mke2fs" in calls and "-L iptv-data" in calls,
          "the data image gets the label iptv-data")
    check("genimage" in calls, "genimage is run")
    check("bundle" in calls, "a RAUC bundle is signed")
    check("--cert" in calls and "--key" in calls, "the bundle is signed with cert and key")

    bundle = os.path.join(tmp, "build/iptvpi-bundle")
    manifest = read(os.path.join(bundle, "manifest.raucm"))
    check("compatible=iptvpi-pi1" in manifest,
          "the bundle manifest names the board in its compatible string")
    check("version=1.2.3" in manifest, "the bundle carries the version")
    check("format=plain" in manifest, "the bundle uses the plain format")
    check("[image.rootfs]" in manifest and "filename=rootfs.ext4" in manifest,
          "the bundle ships rootfs.ext4 as the rootfs image")
    check(os.path.isfile(os.path.join(bundle, "rootfs.ext4")),
          "the bundle contains the rootfs image")
    check("iptvpi-1.2.3.raucb" in calls, "the bundle is named after the version")

    # The web UI compares an upload against this file, so it has to be right.
    bundle = os.path.join(binaries, "iptvpi-1.2.3.raucb")
    digest_file = bundle + ".sha256"
    check(os.path.isfile(digest_file), "a checksum file is written next to the bundle")
    check("iptvpi-1.2.3.raucb" in read(digest_file),
          "the checksum file names the bundle")
    _rc, expected, _err = run(["sha256sum", bundle])
    check(read(digest_file).split()[0] == expected.split()[0],
          "the checksum file matches the bundle, got %r" % read(digest_file))

    # The real rootfs image is exactly BR2_TARGET_ROOTFS_EXT2_SIZE (1024M): the
    # default slot must still fit it, or every real build would be refused.
    with open(os.path.join(binaries, "rootfs.ext4"), "wb") as fh:
        fh.truncate(1024 * 1024 * 1024)
    rc, out, err = run(["bash", os.path.join(BOARD, "post-image.sh"), binaries], env=env)
    check(rc == 0, "a 1024M rootfs fits the default slot (%s)" % err.strip()[-300:])
    check("size = 1100M" in read(genimage_cfg), "the default slot size is 1100M")

    with open(os.path.join(binaries, "rootfs.ext4"), "wb") as fh:
        fh.truncate(1100 * 1024 * 1024)
    rc, out, err = run(["bash", os.path.join(BOARD, "post-image.sh"), binaries], env=env)
    check(rc != 0, "an oversized rootfs fails the build")
    check("slot" in err and "MB" in err, "the size error names the cause")

    os.remove(os.path.join(binaries, "rauc/ca.key.pem"))
    with open(os.path.join(binaries, "rootfs.ext4"), "wb") as fh:
        fh.truncate(4 * 1024 * 1024)
    rc, out, err = run(["bash", os.path.join(BOARD, "post-image.sh"), binaries], env=env)
    check(rc == 0, "a missing key does not break the image")
    check("skipping the update bundle" in err, "the missing key is reported")

    shutil.rmtree(tmp)


# --------------------------------------------------------------------------
# 4. post-build.sh keeps one signing key across builds
# --------------------------------------------------------------------------
def test_shared_rauc_key():
    """An image trusts the certificate it was built with, so the signing pair
    has to survive a build instead of being generated per board and per run."""
    if which("openssl") is None:
        check(True, "openssl is not installed, the shared key test is skipped")
        return

    tmp = tempfile.mkdtemp(prefix="iptv-rauckey-")
    keydir = os.path.join(tmp, "keys")
    env = dict(os.environ)
    env["IPTVPI_RAUC_KEYDIR"] = keydir
    for name in ("IPTVPI_RAUC_CERT", "IPTVPI_RAUC_KEY", "IPTVPI_ROOT_PASSWORD"):
        env.pop(name, None)

    def post_build(tag, extra_env=None):
        binaries = os.path.join(tmp, tag, "binaries")
        target = os.path.join(tmp, tag, "target")
        os.makedirs(binaries)
        os.makedirs(target)
        run_env = dict(env)
        run_env["BINARIES_DIR"] = binaries
        run_env.update(extra_env or {})
        rc, out, err = run(["bash", os.path.join(BOARD, "post-build.sh"), target],
                           env=run_env)
        return rc, out, err, binaries, target

    rc, out, err, binaries1, target1 = post_build("first")
    check(rc == 0, "post-build.sh runs against an empty tree (%s)" % err.strip()[-300:])
    key1 = os.path.join(keydir, "ca.key.pem")
    cert1 = os.path.join(keydir, "ca.cert.pem")
    check(os.path.isfile(key1), "a signing key is created in the shared directory")
    check(os.path.isfile(cert1), "the certificate is created next to it")
    mode = os.stat(key1).st_mode & 0o777
    check(mode == 0o600, "the shared private key is mode 0600, got %s" % oct(mode))
    shared = read(cert1)
    check(read(os.path.join(binaries1, "rauc/ca.cert.pem")) == shared,
          "the certificate is staged for post-image.sh")
    check(read(os.path.join(target1, "etc/rauc/ca.cert.pem")) == shared,
          "the image carries the shared certificate")
    check("RAUC signing certificate (SHA-256)" in out,
          "the certificate fingerprint is logged")

    rc, out, err, binaries2, _target2 = post_build("second")
    check(rc == 0, "a second build succeeds (%s)" % err.strip()[-300:])
    check("reusing the RAUC signing key" in out,
          "the second build reports that the key was reused")
    check(read(cert1) == shared, "the shared certificate is not regenerated")
    check(read(os.path.join(binaries2, "rauc/ca.cert.pem")) == shared,
          "the second build signs with the same certificate")

    # An explicit pair (a production key) still wins over the shared directory.
    explicit_cert = os.path.join(tmp, "explicit", "c.pem")
    explicit_key = os.path.join(tmp, "explicit", "k.pem")
    os.makedirs(os.path.dirname(explicit_cert))
    rc, _out, err = run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                         "-days", "1", "-subj", "/CN=IPTV-Pi production test",
                         "-keyout", explicit_key, "-out", explicit_cert], env=env)
    check(rc == 0, "a test key pair can be generated (%s)" % err.strip()[-200:])
    rc, _out, err, _binaries3, target3 = post_build(
        "third", {"IPTVPI_RAUC_CERT": explicit_cert, "IPTVPI_RAUC_KEY": explicit_key})
    check(rc == 0, "post-build accepts an explicit key pair (%s)" % err.strip()[-300:])
    check(read(os.path.join(target3, "etc/rauc/ca.cert.pem")) == read(explicit_cert),
          "IPTVPI_RAUC_CERT overrides the shared development key")
    check(read(cert1) == shared,
          "an explicit pair does not overwrite the shared key")

    shutil.rmtree(tmp)


# --------------------------------------------------------------------------
# 4. board specific compatible string (a Pi 2 bundle must not fit a Pi 1)
# --------------------------------------------------------------------------
def test_board_compatible():
    """RAUC compares only the compatible string, so it has to name the board:
    an ARMv7 rootfs on the ARMv6 Pi 1 boots into nothing."""
    if which("openssl") is None:
        check(True, "openssl is not installed, the compatible test is skipped")
        return

    tmp = tempfile.mkdtemp(prefix="iptv-compatible-")
    env = dict(os.environ)
    env["IPTVPI_RAUC_KEYDIR"] = os.path.join(tmp, "keys")
    for name in ("IPTVPI_RAUC_CERT", "IPTVPI_RAUC_KEY", "IPTVPI_ROOT_PASSWORD"):
        env.pop(name, None)
    template = read(os.path.join(OVERLAY, "etc/rauc/system.conf"))

    def build(board):
        binaries = os.path.join(tmp, board, "binaries")
        target = os.path.join(tmp, board, "target")
        os.makedirs(os.path.join(target, "etc/rauc"))
        os.makedirs(binaries)
        with open(os.path.join(target, "etc/rauc/system.conf"), "w") as fh:
            fh.write(template)
        run_env = dict(env)
        run_env["BINARIES_DIR"] = binaries
        run_env["IPTVPI_BOARD"] = board
        rc, out, err = run(["bash", os.path.join(BOARD, "post-build.sh"), target],
                           env=run_env)
        return rc, out, err, read(os.path.join(target, "etc/rauc/system.conf"))

    for board in ("pi1", "pi2"):
        rc, out, err, conf = build(board)
        check(rc == 0, "post-build runs for %s (%s)" % (board, err.strip()[-300:]))
        check("compatible=iptvpi-%s\n" % board in conf,
              "the %s image declares compatible=iptvpi-%s" % (board, board))
        check("compatible=iptvpi\n" not in conf,
              "the board neutral compatible is gone for %s" % board)
        check("RAUC compatible string: iptvpi-%s" % board in out,
              "the build logs the compatible it wrote for %s" % board)
        check("[slot.rootfs.0]" in conf and "bootname=A" in conf,
              "the slot configuration survives the rewrite for %s" % board)

    shutil.rmtree(tmp)


# --------------------------------------------------------------------------
# 4b. the CI build refuses to sign without the shared key
#
def workflow_step_script(workflow, step_name):
    """Return the `run:` block of one workflow step (no YAML parser needed)."""
    lines = workflow.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == "- name: " + step_name:
            start = index
            break
    if start is None:
        return None
    body = []
    inside = False
    for line in lines[start + 1:]:
        if not inside:
            if line.strip() == "run: |":
                inside = True
            continue
        if line.strip() and not line.startswith("          "):
            break
        body.append(line[10:].rstrip())
    return "\n".join(body) if inside else None


def test_ci_key_guard():
    """The private key comes from the repository secrets, so a build has to
    notice when they are empty or do not belong together: signing with a key of
    its own would produce a bundle that every already flashed box rejects, and
    only a box would show that."""
    workflow = read(os.path.join(ROOT, ".github/workflows/build-iptvpi.yml"))
    guard = workflow_step_script(workflow, "Materialise the shared signing key")
    check(guard is not None, "the workflow materialises the shared RAUC key")
    check("download-artifact" not in workflow,
          "the private key is not handed around as a workflow artifact")
    check(workflow.count("upload-artifact") == 1,
          "only the image upload uses upload-artifact")
    check("secrets.IPTVPI_RAUC_CERT_PEM" in workflow
          and "secrets.IPTVPI_RAUC_KEY_PEM" in workflow,
          "the pair is read from the repository secrets")
    # Caches are scoped per ref, so a tag build cannot fetch the key that way.
    check("actions/cache" not in workflow.split("Cache the Buildroot downloads")[0],
          "the signing key does not travel through the Actions cache")
    if guard is None or which("bash") is None or which("openssl") is None:
        check(True, "bash/openssl missing, the key guard test is skipped")
        return

    tmp = tempfile.mkdtemp(prefix="iptv-keyguard-")
    keys = os.path.join(tmp, "rauc-keys")
    script_path = os.path.join(tmp, "guard.sh")
    with open(script_path, "w") as fh:
        fh.write(guard + "\n")

    def guard_env(cert_pem=None, key_pem=None):
        env = dict(os.environ)
        env.pop("RAUC_CERT_PEM", None)
        env.pop("RAUC_KEY_PEM", None)
        if cert_pem is not None:
            env["RAUC_CERT_PEM"] = cert_pem
        if key_pem is not None:
            env["RAUC_KEY_PEM"] = key_pem
        return env

    rc, out, err = run(["bash", script_path], env=guard_env(), cwd=tmp)
    check(rc != 0, "empty secrets fail the build, got %r" % rc)
    check("::error::" in out, "the failure is reported as an annotation")
    check("IPTVPI_RAUC_CERT_PEM" in out and "IPTVPI_RAUC_KEY_PEM" in out,
          "the message names both secrets")
    check("Secrets and variables" in out, "the message says where to set them")

    os.makedirs(keys)
    cert = os.path.join(keys, "ca.cert.pem")
    key = os.path.join(keys, "ca.key.pem")
    rc, _, err = run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                      "-days", "1", "-subj", "/CN=guard test",
                      "-keyout", key, "-out", cert], cwd=tmp)
    check(rc == 0, "a test pair can be created (%s)" % err.strip()[-300:])

    rc, out, err = run(["bash", script_path],
                       env=guard_env(read(cert), read(key)), cwd=tmp)
    check(rc == 0, "a matching pair passes the guard (%s)" % err.strip()[-300:])
    check("Fingerprint" in out, "the guard prints the certificate fingerprint")
    materialised = os.path.join(keys, "ca.key.pem")
    check(read(materialised) == read(key), "the keys are written where post-build.sh reads them")
    mode = os.stat(materialised).st_mode & 0o777
    check(mode == 0o600, "the materialised private key is mode 0600, got %s" % oct(mode))

    other = os.path.join(tmp, "other.key")
    run(["openssl", "genrsa", "-out", other, "2048"], cwd=tmp)
    rc, out, _ = run(["bash", script_path],
                     env=guard_env(read(cert), read(other)), cwd=tmp)
    check(rc != 0, "a key that does not belong to the certificate is refused")
    check("do not belong together" in out, "the mismatch is named")

    shutil.rmtree(tmp)


# --------------------------------------------------------------------------
# 5. iptv-rauc-mark-good (the read-write window around rauc)
# --------------------------------------------------------------------------
def test_mark_good():
    helper = os.path.join(OVERLAY, "usr/bin/iptv-rauc-mark-good")
    tmp = tempfile.mkdtemp(prefix="iptv-markgood-")
    boot = os.path.join(tmp, "boot")
    os.makedirs(boot)
    log = os.path.join(tmp, "calls.log")
    rauc = os.path.join(tmp, "rauc")
    body = ('#!/bin/sh\nprintf "%s\\n" "$*" >> "' + log + '"\nexit ${STUB_RC:-0}\n')
    with open(rauc, "w") as fh:
        fh.write(body)
    os.chmod(rauc, 0o755)

    env = dict(os.environ)
    env["IPTV_BOOT_DIR"] = boot
    env["IPTV_RAUC"] = rauc

    rc, _, err = run(["/bin/sh", helper], env=env)
    check(rc == 0, "mark-good succeeds with a working rauc (%s)" % err.strip())
    check("status mark-good" in read(log), "the helper asks rauc to mark the slot good")

    env["STUB_RC"] = "7"
    rc, _, _ = run(["/bin/sh", helper], env=env)
    check(rc == 7, "a rauc failure is passed through, got %r" % rc)
    env.pop("STUB_RC", None)

    env["IPTV_RAUC"] = os.path.join(tmp, "missing-rauc")
    rc, _, err = run(["/bin/sh", helper], env=env)
    check(rc != 0 and "missing" in err, "a missing rauc is reported")

    shutil.rmtree(tmp)


# --------------------------------------------------------------------------
# 5. static checks
# --------------------------------------------------------------------------
def test_static():
    for shell, path in (("sh", os.path.join(OVERLAY, "usr/bin/iptv-boot-slot")),
                        ("sh", os.path.join(OVERLAY, "usr/bin/iptv-data-init")),
                        ("sh", os.path.join(OVERLAY, "usr/bin/iptv-rauc-mark-good")),
                        ("sh", os.path.join(OVERLAY, "usr/bin/iptv-wifi-import")),
                        ("bash", os.path.join(BOARD, "post-image.sh")),
                        ("bash", os.path.join(BOARD, "post-build.sh"))):
        rc, _, err = run([shell, "-n", path])
        check(rc == 0, "%s is valid %s (%s)" % (os.path.basename(path), shell, err.strip()))

    boot_slot = script(os.path.join(OVERLAY, "usr/bin/iptv-boot-slot"))
    check(boot_slot.startswith("#!/bin/sh"), "iptv-boot-slot is a /bin/sh script")
    check("set -eu" in boot_slot, "iptv-boot-slot is strict")
    check('IPTV_SLOTS_CONF:-/etc/iptv-slots.conf' in boot_slot,
          "iptv-boot-slot reads /etc/iptv-slots.conf by default")
    for cmd in ("get-primary", "set-primary", "get-state", "set-state", "get-current"):
        check(cmd in boot_slot, "iptv-boot-slot implements %s" % cmd)
    check("mount -o remount,rw" in boot_slot,
          "iptv-boot-slot opens /boot for its writes")
    check("remount,ro" in boot_slot, "iptv-boot-slot closes /boot again")
    check("trap" in boot_slot and "sync" in boot_slot,
          "the read-write window is synced in an EXIT trap")
    check("panic=10" in boot_slot, "iptv-boot-slot keeps panic=10 in cmdline.txt")

    slots = read(os.path.join(OVERLAY, "etc/iptv-slots.conf"))
    check('ROOTFS_A="49505456-02"' in slots, "slots.conf names slot A")
    check('ROOTFS_B="49505456-03"' in slots, "slots.conf names slot B")
    check('DATA_PARTUUID="49505456-04"' in slots, "slots.conf documents the data partition")

    system_conf = read(os.path.join(OVERLAY, "etc/rauc/system.conf"))
    check("[system]" in system_conf and "compatible=iptvpi" in system_conf,
          "system.conf declares the iptvpi compatible")
    check("rewritten per board by post-build.sh" in system_conf,
          "system.conf documents that the compatible is board specific")
    check("bootloader=custom" in system_conf, "system.conf uses the custom bootloader")
    check("bootloader-custom-backend=iptv-boot-slot" in system_conf,
          "system.conf points at iptv-boot-slot")
    check("device=/dev/disk/by-partuuid/49505456-02" in system_conf,
          "slot 0 is addressed by PARTUUID")
    check("device=/dev/disk/by-partuuid/49505456-03" in system_conf,
          "slot 1 is addressed by PARTUUID")
    check("bootname=A" in system_conf and "bootname=B" in system_conf,
          "system.conf gives both slots a bootname")
    check("type=ext4" in system_conf, "both slots are ext4")
    check("[slot.rootfs.0]" in system_conf and "[slot.rootfs.1]" in system_conf,
          "system.conf defines two rootfs slots")
    system_conf_effective = "\n".join(
        ln for ln in system_conf.splitlines() if not ln.strip().startswith("#"))
    check("/dev/mmcblk0p" not in system_conf_effective,
          "system.conf does not depend on mmcblk names")

    fstab = read(os.path.join(OVERLAY, "etc/fstab"))
    lines = [ln for ln in fstab.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    data_lines = [ln for ln in lines if "/var/lib/iptv" in ln]
    check(len(data_lines) == 1, "fstab mounts /var/lib/iptv exactly once")
    check("LABEL=iptv-data" in data_lines[0], "the data partition is mounted by label")
    check("ext4" in data_lines[0], "the data partition is ext4")
    check("noatime" in data_lines[0], "the data partition is mounted noatime")
    check("errors=remount-ro" in data_lines[0], "the data partition remounts read-only on errors")
    check(any("/boot" in ln for ln in lines), "fstab still mounts /boot")
    check(any("tmpfs" in ln and "/tmp" in ln for ln in lines), "fstab keeps /tmp in RAM")
    check(any("tmpfs" in ln and "/var/log" in ln for ln in lines),
          "fstab keeps /var/log in RAM")

    boot_line = [ln for ln in lines if "/boot" in ln][0]
    check(boot_line.split()[3].split(",")[0] == "ro",
          "/boot is mounted read-only, got %r" % boot_line)
    check("nofail" in boot_line, "/boot does not block the boot when it is missing")

    fragment = read(os.path.join(BOARD, "kernel-iptv.fragment"))
    check("CONFIG_WATCHDOG=y" in fragment, "watchdog support is built in")
    check("CONFIG_BCM2835_WDT=y" in fragment, "the BCM2835 watchdog is built in")
    manager_conf = read(os.path.join(OVERLAY, "etc/systemd/system.conf"))
    check("RuntimeWatchdogSec=15" in manager_conf,
          "systemd pets the watchdog every 15 s")

    kodi_link = os.path.join(OVERLAY, "var/kodi")
    check(os.path.islink(kodi_link), "/var/kodi is a symlink")
    if os.path.islink(kodi_link):
        check(os.readlink(kodi_link) == "/var/lib/iptv/kodi",
              "/var/kodi points into the data partition")
    check(os.path.isdir(os.path.join(OVERLAY, "var/lib/iptv")),
          "the /var/lib/iptv mountpoint exists in the rootfs")
    defaults = os.path.join(OVERLAY, "usr/share/iptv-kodi-defaults")
    for name in ("userdata/advancedsettings.xml", "userdata/guisettings.xml",
                 "userdata/autoexec.py"):
        check(os.path.isfile(os.path.join(defaults, name)),
              "the Kodi defaults ship %s" % name)

    data_unit = read(os.path.join(UNITS, "iptv-data-init.service"))
    check("ExecStart=/usr/bin/iptv-data-init" in data_unit, "the data unit runs iptv-data-init")
    check("RequiresMountsFor=/var/lib/iptv" in data_unit, "the data unit waits for the mount")
    check("Before=iptv-webui.service" in data_unit and "kodi.service" in data_unit,
          "the data unit runs before Kodi and the web UI")
    check("ConditionPathExists=/var/lib/iptv" in data_unit,
          "the data unit only runs when the mountpoint exists")
    check("Type=oneshot" in data_unit, "the data unit is oneshot")
    check("WantedBy=multi-user.target" in data_unit, "the data unit is installable")

    mark = read(os.path.join(UNITS, "iptv-rauc-mark-good.service"))
    check("ExecStart=-/usr/bin/iptv-rauc-mark-good" in mark,
          "the mark-good unit runs the /boot-aware helper")
    mark_good = read(os.path.join(OVERLAY, "usr/bin/iptv-rauc-mark-good"))
    check("status mark-good" in mark_good, "the helper asks rauc to mark the slot good")
    check("mount -o remount,rw" in mark_good, "the helper opens /boot read-write")
    check("remount ro" in mark_good, "the helper closes /boot again")
    check("trap" in mark_good and "sync" in mark_good,
          "the helper syncs inside an EXIT trap")

    wifi = read(os.path.join(OVERLAY, "usr/bin/iptv-wifi-import"))
    check("mount -o remount,rw" in wifi and "trap" in wifi,
          "the wifi import remounts /boot for the rename")

    webui_unit = read(os.path.join(UNITS, "iptv-webui.service"))
    check("TMPDIR=/var/lib/iptv/updates/tmp" in webui_unit,
          "the web UI spools uploads on the data partition")
    check("MemoryMax=" in webui_unit, "the web UI keeps its memory cap")
    launch = read(os.path.join(ROOT, "webui/launch.sh"))
    check('mkdir -p "$TMP_DIR"' in launch, "launch.sh creates the spool directory")
    check('find "$TMP_DIR"' in launch and "-mmin" in launch,
          "launch.sh cleans stale spool files")
    app_src = read(os.path.join(ROOT, "webui/app.py"))
    check("def webui_temp_dir()" in app_src,
          "app.py chooses the spool directory itself")
    check("tempfile.tempdir = webui_temp_dir()" in app_src,
          "werkzeug is pointed at the spool directory")
    check("size=65536" in app_src, "uploads and downloads are streamed in 64 KB chunks")
    check("def sha256_file" in app_src, "the checksum is streamed, not buffered")

    # The Python 2.7 job must be reproducible on a laptop, not only in CI.
    runner = os.path.join(ROOT, "tests/run-py27.sh")
    check(os.path.isfile(runner), "the Python 2.7 runner script exists")
    check(os.access(runner, os.X_OK), "the Python 2.7 runner is executable")
    runner_src = read(runner)
    check("python:2.7-slim" in runner_src, "the runner defaults to the CI image")
    check('"flask==1.1.4"' in runner_src, "the runner installs the pinned Flask")
    for suite in ("tests/verify_ab_update.py", "tests/verify_rauc_ui.py"):
        check(suite in runner_src, "the runner executes %s" % suite)
    workflow = read(os.path.join(ROOT, ".github/workflows/build-iptvpi.yml"))
    check("tests/run-py27.sh" in workflow,
          "CI and the laptop run the same Python 2.7 script")

    checks = os.path.join(ROOT, "tests/run-checks.sh")
    check(os.path.isfile(checks), "the bundled check script exists")
    check(os.access(checks, os.X_OK), "the bundled check script is executable")
    checks_src = read(checks)
    for needle in ("py_compile", "node --check", "tests/run-py27.sh", "--no-py27",
                   "tests/verify_ab_update.py", "tests/verify_rauc_ui.py"):
        check(needle in checks_src, "run-checks.sh covers %r" % needle)
    for shell_script in ("webui/launch.sh", "post-image.sh", "post-build.sh",
                         "build_os.sh"):
        check(shell_script in checks_src,
              "run-checks.sh syntax-checks %s" % shell_script)
    check("After=iptv-webui.service" in mark, "marking good requires a running UI")
    check("ConditionPathExists=/usr/bin/rauc" in mark, "the unit needs rauc installed")

    for name in ("iptv-webui.service", "kodi.service",
                 "iptv-playlist-refresh.service", "iptv-health-watch.service",
                 "iptv-unit-alert@.service", "iptv-hardware-sample.service"):
        content = read(os.path.join(UNITS, name))
        check("RequiresMountsFor=/var/lib/iptv" in content,
              "%s waits for the data partition" % name)

    post_build = script(os.path.join(BOARD, "post-build.sh"))
    check('KODI_CFG_DIR="${TARGET_DIR}/usr/share/iptv-kodi-defaults"' in post_build,
          "post-build seeds the defaults, not the live profile")
    check("${TARGET_DIR}/var/kodi" not in post_build,
          "post-build no longer writes the old profile path")
    check('> "${IPTV_DIR}/playlist.m3u"' not in post_build,
          "post-build leaves the playlist to iptv-data-init")
    check('chmod 0755 "${TARGET_DIR}/usr/bin/iptv-boot-slot"' in post_build,
          "post-build makes iptv-boot-slot executable")
    check('chmod 0755 "${TARGET_DIR}/usr/bin/iptv-data-init"' in post_build,
          "post-build makes iptv-data-init executable")
    check('chmod 0755 "${TARGET_DIR}/usr/bin/iptv-rauc-mark-good"' in post_build,
          "post-build makes the mark-good helper executable")
    check('iptv-data-init.service"' in post_build, "post-build enables the data unit")
    check('iptv-rauc-mark-good.service"' in post_build, "post-build enables the mark-good unit")
    check("openssl req -x509" in post_build, "post-build can generate a signing key")
    check("IPTVPI_RAUC_KEYDIR" in post_build,
          "post-build signs with a key from a shared directory")
    check("/.cache/iptvpi-rauc" in post_build,
          "the shared key directory has a documented default")
    check("reusing the RAUC signing key" in post_build,
          "an existing key is reused instead of being regenerated")
    check("adopting the RAUC signing key already in" in post_build,
          "a pair in the build tree is adopted instead of replaced")
    check("-fingerprint -sha256" in post_build,
          "the certificate fingerprint is logged so a rotation is visible")
    check("${TARGET_DIR}/etc/rauc/ca.cert.pem" in post_build, "the certificate reaches the image")
    check('install -m 0600' in post_build or 'chmod 0600' in post_build,
          "the private key is kept private")
    check("IPTVPI_RAUC_CERT" in post_build and "IPTVPI_RAUC_KEY" in post_build,
          "a real key can replace the development one")
    check("IPTVPI_DISK_PARTUUID" in post_build, "post-build knows the disk signature")
    check(post_build.count('log "post-build finished"') == 1,
          "post-build reports completion exactly once")

    post_image = script(os.path.join(BOARD, "post-image.sh"))
    check("set -euo pipefail" in post_image, "post-image.sh is strict")
    check('DISK_PARTUUID="${IPTVPI_DISK_PARTUUID:-49505456}"' in post_image,
          "the disk signature has a documented default")
    check('ROOTFS_SLOT_SIZE="${IPTVPI_ROOTFS_SLOT_SIZE:-1100M}"' in post_image,
          "the rootfs slot size is overridable")
    check("BR2_TARGET_ROOTFS_EXT2_SIZE" in post_image,
          "post-image.sh documents the rootfs size it has to fit")
    check('DATA_SIZE="${IPTVPI_DATA_SIZE:-1024M}"' in post_image,
          "the data size is overridable")
    check("iptvpi-${VERSION}.raucb" in post_image, "the bundle name follows the version")
    check("compatible=iptvpi-${BOARD}" in post_image,
          "the bundle manifest names the board it was built for")

    for board in ("iptvpi1", "iptvpi2"):
        cfg = read(os.path.join(ROOT, "br2-external/configs/%s_defconfig" % board))
        check("BR2_PACKAGE_RAUC=y" in cfg, "%s enables RAUC" % board)
        check("BR2_PACKAGE_RAUC_NETWORK=y" in cfg, "%s enables network installs" % board)
        check('BR2_TARGET_ROOTFS_EXT2_SIZE="1024M"' in cfg,
              "%s keeps a 1024M rootfs that fits a slot" % board)

    board_cmdline = read(os.path.join(BOARD, "cmdline.txt")).strip()
    check("root=" in board_cmdline, "the cmdline template contains root=")
    check("panic=10" in board_cmdline.split(),
          "the cmdline template asks the kernel to reboot after a panic")
    check("rauc.slot=" not in board_cmdline,
          "the cmdline template leaves rauc.slot to post-image.sh")

    readme = read(os.path.join(ROOT, "README.md"))
    for needle in ("RAUC", "iptv-data", "A/B", "49505456", "iptv-boot-slot"):
        check(needle in readme, "README mentions %s" % needle)


def main():
    test_boot_slot()
    test_data_init()
    test_shared_rauc_key()
    test_board_compatible()
    test_ci_key_guard()
    test_post_image()
    test_mark_good()
    test_static()
    print("")
    print("A/B update + data partition: %d assertions, %d failures" % (passed, len(failures)))
    for msg in failures:
        print("  - " + msg)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
