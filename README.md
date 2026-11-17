# IPTV-Pi

A custom, ultra-minimal IPTV operating system for the **Raspberry Pi 1** and
**Raspberry Pi 2**, built with [Buildroot](https://buildroot.org/).

The image boots straight into **Kodi** in full-screen kiosk mode, plays IPTV
from an M3U playlist, and exposes a tiny web page on port 80 where anyone on
the local network can replace that playlist and restart Kodi.

There is no desktop: no X11, no Wayland, no browser, no package manager, no
login prompt on the TV. Just the kernel, systemd, ALSA, the VideoCore
firmware, Kodi with hardware H.264 decoding, and a Flask playlist manager.

What is finished and verified, and what is still open, is tracked in
[STATUS.md](STATUS.md) (features, system, kernel, checklist).

---

## 1. What is in the image

| Component | What it does |
|---|---|
| Linux 5.10 (Raspberry Pi Foundation fork) | BCM2835/BCM2836 support, `bcmrpi` (Pi 1) or `bcm2709` (Pi 2) defconfig |
| VideoCore firmware (`start.elf`) | Hardware H.264 decoding, dispmanx display, HDMI audio |
| systemd | Init, networking (`systemd-networkd`), DNS (`systemd-resolved`), NTP |
| ALSA | `snd_bcm2835` for the analog jack and HDMI audio, `alsa-utils` for debugging |
| Kodi 18.9 "Leia" | The only user interface, started by `kodi.service` on tty1 |
| `pvr.iptvsimple` 3.9.8 | IPTV client, pre-configured to read `/var/lib/iptv/playlist.m3u` |
| `libcec` | HDMI-CEC: the TV remote drives Kodi, no second remote needed |
| `inputstream.adaptive` + OpenSSL + CA certificates | HLS (`.m3u8`) streams and HTTPS playlists/EPG without SSL errors |
| Flask web UI | Upload / paste / fetch an M3U playlist, filter channel groups, set the EPG URL, show live temperatures and clocks, reload Kodi |
| System updates from the page | Installs a signed `.raucb` from an upload or a LAN URL, compares an optional SHA-256 (typed in, or fetched from `<bundle>.raucb.sha256` next to the URL), shows download and install progress, can reboot on its own, verifies the slot on the next boot and keeps a history of the last five updates with version, origin and checksum |
| Scheduled refresh | systemd timer re-fetches the playlist from its URL every 12 h, keeps the group filter and reloads Kodi (a no-op until a URL has been saved) |
| zram swap | Compressed RAM swap so 256 MB boards survive Kodi |
| Dynamic clocking | `ondemand` governor (600 ↔ 900 MHz, `up_threshold=75`), firmware DVFS for the H.264 block (250 ↔ 400 MHz), 80 MHz SD host clock |
| Service priorities | Kodi at `Nice=-10` with `OOMScoreAdjust=-500`; the web UI at `Nice=19`, idle I/O class, capped at 96 MB |
| Kernel VM tuning | `sysctl.d`: swap only into zram, small dirty-page window, 4 MB reserved pool |
| Power-loss safety | `/boot` mounted `ro` with a short read-write window for the three writers, `panic=10` in the command line, hardware watchdog (`bcm2835_wdt`, `RuntimeWatchdogSec=15`) |

Deliberately **not** included: X11, Wayland, Python 3 `pip`, a package
manager, Samba, Bluetooth, PulseAudio, a browser, a desktop, an OSD console.

### Hardware

* Raspberry Pi 1 (A/A+/B/B+, 256 or 512 MB) — ARMv6, builds `output/pi1`
* Raspberry Pi 2 (model B, 1 GB) — ARMv7, builds `output/pi2`

An SD card of 2 GB or more is needed (the image is about 1.1 GB), HDMI
display, Ethernet cable (or a supported USB Wi-Fi dongle).

> **Memory reality check.** Kodi needs roughly 180–250 MB of RAM. On a 256 MB
> Pi 1 this only works because H.264 decoding happens in the GPU and because
> `iptv-zram.service` adds compressed swap. A 512 MB Pi 1 or the 1 GB Pi 2 is
> the comfortable target; expect the Pi 1 user interface to feel slow.

---

## 2. Why Buildroot 2021.02 and Kodi 18.9

This is a deliberate choice, not an oversight:

* Kodi's **native Raspberry Pi platform** (`BR2_PACKAGE_KODI_PLATFORM_RBPI`)
  renders through dispmanx and decodes H.264 through MMAL/OpenMAX on the
  VideoCore IV. That is exactly the "no desktop, hardware decoded" design this
  project needs, and it works on ARMv6.
* Kodi **19 removed that platform**. From Kodi 19/20 on, the only desktop-free
  backend is GBM/KMS, which needs Mesa's `vc4` driver.
* Mesa's `vc4` gallium driver **requires NEON** in Buildroot
  (`BR2_PACKAGE_MESA3D_GALLIUM_DRIVER_VC4` depends on
  `BR2_ARM_CPU_HAS_NEON`), so it cannot be built for the ARMv6 Pi 1 at all.
* Buildroot 2021.02.x is the last LTS series that ships Kodi 18.9 with the Pi
  platform and a matching `pvr.iptvsimple` 3.9.8.

`build_os.sh` therefore pins `2021.02.x`, and refuses to continue if
`BR2_PACKAGE_KODI_PLATFORM_RBPI` is not present in the resulting `.config`.

If you only care about the Pi 2 and want a newer Kodi, you would need to port
this project to a GBM/Mesa stack (`dtoverlay=vc4-kms-v3d`), which is a
different design with a much larger memory footprint.

---

## 3. Repository layout

```
.
├── .github/workflows/build-iptvpi.yml  # CI: verify on 3.x + 2.7, image builds per board
├── build_os.sh                     # fetch Buildroot, configure, build, report
├── README.md
├── STATUS.md                       # interim status: features, system, kernel, open checklist
├── br2-external/                   # Buildroot "external tree" (BR2_EXTERNAL)
│   ├── external.desc               # name: IPTVPI -> $(BR2_EXTERNAL_IPTVPI_PATH)
│   ├── external.mk
│   ├── Config.in
│   ├── configs/
│   │   ├── iptvpi1_defconfig       # Raspberry Pi 1 image
│   │   └── iptvpi2_defconfig       # Raspberry Pi 2 image
│   └── board/raspberrypi/
│       ├── config-pi1.txt          # boot config.txt (DVFS, h264 clock, SD overclock)
│       ├── config-pi2.txt
│       ├── cmdline.txt             # kernel command line
│       ├── kernel-iptv.fragment    # cpufreq/ondemand + thermal kernel symbols
│       ├── post-build.sh           # rootfs finalisation hook
│       ├── post-image.sh           # sdcard.img (boot + rootfs A/B + data) and the RAUC bundle
│       └── rootfs-overlay/
│           ├── etc/systemd/system/kodi.service
│           ├── etc/systemd/system/iptv-webui.service
│           ├── etc/systemd/system/iptv-zram.service
│           ├── etc/systemd/system/wpa_supplicant-wlan0.service
│           ├── etc/systemd/network/10-eth0.network, 20-wlan0.network
│           ├── etc/wpa_supplicant/wpa_supplicant-wlan0.conf
│           ├── etc/modules-load.d/iptv-audio.conf
│           ├── etc/udev/rules.d/99-vchiq.rules
│           ├── etc/systemd/system/iptv-cpufreq.service
│           ├── etc/systemd/system/iptv-wifi-import.service
│           ├── etc/systemd/system/iptv-playlist-refresh.service
│           ├── etc/systemd/system/iptv-playlist-refresh.timer
│           ├── etc/systemd/system/iptv-hardware-sample.service
│           ├── etc/systemd/system/iptv-hardware-sample.timer
│           ├── etc/systemd/system/iptv-health-watch.service
│           ├── etc/systemd/system/iptv-health-watch.timer
│           ├── etc/systemd/system/iptv-unit-alert@.service
│           ├── etc/fstab                       # noatime root + data + tmpfs /tmp, /var/log
│           ├── etc/iptv-slots.conf             # PARTUUIDs of rootfs slot A and B
│           ├── etc/rauc/system.conf            # compatible=iptvpi-pi1/-pi2, two slots
│           ├── etc/systemd/system/iptv-data-init.service
│           ├── etc/systemd/system/iptv-rauc-mark-good.service
│           ├── usr/bin/iptv-zram-setup
│           ├── usr/bin/iptv-cpufreq-setup      # selects the ondemand governor
│           ├── usr/bin/iptv-wifi-import        # reads /boot/wifi.txt
│           ├── usr/bin/iptv-boot-slot          # RAUC bootloader backend (cmdline.txt)
│           ├── usr/bin/iptv-data-init          # first-boot seeding of the data partition
│           ├── var/lib/iptv/                   # empty mountpoint, filled by the data partition
│           ├── var/kodi -> /var/lib/iptv/kodi  # symlink: the profile lives on the data partition
│           └── usr/share/iptv-kodi-defaults/   # profile shipped in the rootfs
│               ├── userdata/autoexec.py
│               ├── userdata/advancedsettings.xml  # splash, log level, stream cache
│               ├── userdata/guisettings.xml       # localhost JSON-RPC, no thumbnails
│               └── userdata/addon_data/pvr.iptvsimple/settings.xml
├── webui/                          # source of the port-80 playlist manager
│   ├── app.py                      # Flask backend (Python 2.7 and 3 compatible)
│   ├── launch.sh                   # picks the interpreter that has Flask
│   ├── templates/index.html
│   └── static/style.css
├── .githooks/pre-commit           # runs the local checks before every commit
└── tests/
    ├── run-checks.sh               # every local check in one run (3.x, JS, shell, 2.7)
    ├── run-py27.sh                 # the Python 2.7 job, runnable on a laptop
    ├── setup-py27.sh               # builds that 2.7 once, outside /tmp
    ├── verify_ab_update.py         # A/B update, data partition, RAUC checks
    └── verify_rauc_ui.py           # the system update card in the web UI
```

`webui/` is copied into the image by `post-build.sh`, so it is edited in one
place only.

---

## 4. Building

### Host requirements

Linux x86_64 build host with:

```sh
sudo apt-get install build-essential git bc rsync cpio unzip file \
     bzip2 wget patch perl python3 libncurses-dev openssl
```

Allow **about 20 GB of disk** and **several hours** on a 4-core machine:
Buildroot cross-compiles a full glibc toolchain, the Pi kernel, ffmpeg and
Kodi. 4 GB of RAM is comfortable (2 GB works with `-j2`).

### Build

```sh
./build_os.sh                    # both boards
./build_os.sh --board pi2        # only the Pi 2 image
./build_os.sh --board pi1 --clean
./build_os.sh --board pi2 -j 2   # limit parallelism on a small host
```

What the script does:

1. checks the host tools;
2. shallow-clones Buildroot `2021.02.x` into `./buildroot/` (reused on later
   runs);
3. runs `make BR2_EXTERNAL=… O=output/<board> iptvpi<n>_defconfig`;
4. **verifies** the resulting `.config` (Kodi, the Pi platform, the IPTV
   add-on, Flask, systemd-networkd, the overlay and post-build hooks). Kconfig
   silently drops symbols whose dependencies are unmet, so this step turns a
   silent misconfiguration into a clear error instead of a black screen;
5. builds the image;
6. prints the path and the `dd` command.

Downloads are cached in `./dl`, so a second board only rebuilds what differs.

Artifacts:

```
output/pi1/images/sdcard.img     # bootable SD card image
output/pi2/images/sdcard.img
output/<board>/images/rootfs.ext4
output/<board>/build/            # per-package build trees
```

### Manual build (no script)

```sh
git clone --depth 1 -b 2021.02.x https://github.com/buildroot/buildroot.git
make -C buildroot O=$PWD/output/pi2 BR2_EXTERNAL=$PWD/br2-external iptvpi2_defconfig
make -C buildroot O=$PWD/output/pi2 BR2_EXTERNAL=$PWD/br2-external -j$(nproc)
```

`--menuconfig` in `build_os.sh` opens `menuconfig` and writes the result back
to the defconfig in `br2-external/configs/` with `savedefconfig`.

### Optional root password

```sh
IPTVPI_ROOT_PASSWORD='something-strong' ./build_os.sh --board pi2
```

The password is hashed into `/etc/shadow` at build time by `post-build.sh`;
nothing secret is stored in this repository. Without it, root has an empty
password and SSH is disabled (see §8).

---

## 5. Flash and first boot

```sh
sudo dd if=output/pi2/images/sdcard.img of=/dev/sdX bs=4M conv=fsync status=progress
sync
```

(`/dev/sdX` is the whole card, not a partition. On macOS use `dd` against
`/dev/rdiskN`.)

Then:

1. Insert the card, connect HDMI and Ethernet, power up.
2. First boot takes 20–60 seconds (filesystem, network, Kodi start).
3. Kodi appears full-screen. With the placeholder playlist you will see an
   empty channel list — that is expected until you load a playlist.
4. From any browser on the same network: **http://iptvpi.local/** (mDNS) or
   **http://<board-ip>/**.

The board's IP address can be read from the TV (Kodi → System → System info →
Network) or from the serial console (`getty` on the GPIO UART at 115200 8N1).

### What ends up on the card

The image has four partitions instead of the usual two, so a system update can
be installed next to the running system:

| # | Size | Filesystem | Purpose |
|---|---|---|---|
| p1 | 64 MB | FAT | firmware, `zImage`, DTBs, `config.txt`, **`cmdline.txt`** |
| p2 | 1100 MB | ext4 | **rootfs slot A** — the rootfs this build produced |
| p3 | 1100 MB | ext4 | **rootfs slot B** — empty until the first update |
| p4 | 1024 MB | ext4, `LABEL=iptv-data` | playlists, state, logs, web-UI updates, Kodi profile |

The MBR disk signature is pinned to `0x49505456`, so the PARTUUIDs never move
between builds: `49505456-01` (boot), `-02` (slot A), `-03` (slot B), `-04`
(data). `cmdline.txt` and `/etc/rauc/system.conf` address the slots by those
UUIDs, never by `/dev/mmcblk0pN`.

The **data partition** is why a rootfs swap costs you nothing: the playlist,
`state.json`, `/var/lib/iptv/log`, the web-UI update in `/var/lib/iptv/app`
and the Kodi profile (`/var/kodi` is a symlink to `/var/lib/iptv/kodi`) all
live there. `iptv-data-init` copies the profile defaults from
`/usr/share/iptv-kodi-defaults` exactly once (marker: `/var/kodi/.seeded`) and
never overwrites anything a user created.

Each slot is slightly larger than the 1024 MB rootfs image
(`BR2_TARGET_ROOTFS_EXT2_SIZE`), so a slot can be grown with `resize2fs` later
on. The image is therefore ~3.2 GiB: use a card of **4 GB or more**. The data
partition keeps its size; grow it on the box with `resize2fs` after enlarging
p4 if you want the extra space on a bigger card. Sizes and the signature are
build-time variables: `IPTVPI_BOOT_SIZE`, `IPTVPI_ROOTFS_SLOT_SIZE`,
`IPTVPI_DATA_SIZE`, `IPTVPI_DISK_PARTUUID`. If you change the last one, update
`etc/iptv-slots.conf` and `etc/rauc/system.conf` to match — `post-build.sh`
warns when they disagree.

### A/B system updates with RAUC

Why not SWUpdate: RAUC 1.5.1 is in Buildroot 2021.02 together with its host
tool, so the bundle is produced by the same build that produces the image.
The Raspberry Pi firmware has no A/B logic — it always loads the kernel from
the FAT partition and follows `cmdline.txt` — so the box uses RAUC's
**custom bootloader backend**:

* `compatible=iptvpi-pi1` / `iptvpi-pi2` — written per board by `post-build.sh`
  and mirrored in the bundle manifest, so RAUC refuses a bundle built for the
  other board before a slot is touched instead of writing an ARMv7 rootfs onto
  the ARMv6 Pi 1 — `bootloader=custom`,
  `bootloader-custom-backend=iptv-boot-slot` (`/etc/rauc/system.conf`);
* `/usr/bin/iptv-boot-slot` answers `get-primary`, `set-primary <A|B>`,
  `get-state`, `set-state <A|B> good|bad` and `get-current`; it rewrites
  `root=PARTUUID=…` and `rauc.slot=…` in `/boot/cmdline.txt` and keeps its
  record in `/boot/rauc/`;
* slot A is what the flashed image runs (`root=PARTUUID=49505456-02 rauc.slot=A`);
* `iptv-rauc-mark-good.service` marks the running slot `good` once the web UI
  is up, so a slot that never reaches a working system can be marked bad.

`post-image.sh` packs the same rootfs into a signed bundle next to the card
image:

```sh
ls output/pi2/images/
# sdcard.img  rootfs.ext4  iptvpi-2026.11.16-1200.raucb  iptvpi-2026.11.16-1200.raucb.sha256
```

The `.sha256` file is what the web UI compares against before it installs
anything: paste the number into the card for an upload, and for a bundle URL
the box fetches the file itself when that field is left empty. The card also
records what was installed, with which version and checksum, in its history
(§6).

Install it from the box itself (the bundle is verified against
`/etc/rauc/ca.cert.pem` before anything is written):

```sh
rauc status                      # booted slot, primary slot, both slot states
rauc status --detailed           # sizes, versions, devices
rauc install /var/lib/iptv/iptvpi-2026.11.16-1200.raucb
rauc install http://192.168.1.10/iptvpi-2026.11.16-1200.raucb   # needs RAUC_NETWORK
reboot
```

RAUC writes the inactive slot, marks it primary through `iptv-boot-slot` and
the next boot runs it; the previous system stays untouched in the other slot
as the rollback target. Check and roll back with:

```sh
rauc status mark-bad A           # refuse slot A as a future candidate
rauc status mark-good            # mark the running slot good
```

If a freshly installed system does not come up at all, put the card in a PC and
edit `cmdline.txt` on the FAT partition back to
`root=PARTUUID=49505456-02 rauc.slot=A` (or `-03`/`B` for the other slot) —
that file is the whole bootloader state, next to `/boot/rauc/` on the box.

**Signing keys.** An image trusts exactly the certificate it was built with, so
the pair has to survive the build: `post-build.sh` reuses one key instead of
generating a new one per board and per run. It resolves it in this order —
`IPTVPI_RAUC_CERT`/`IPTVPI_RAUC_KEY` when both are set, otherwise
`IPTVPI_RAUC_KEYDIR` (default `~/.cache/iptvpi-rauc`, created on first use,
RSA 4096, 10 years), otherwise a pair already staged in `binaries/rauc`, which
is then adopted into that directory. Only the certificate reaches the image
(`/etc/rauc/ca.cert.pem`); the private key stays outside `output/`, so `--clean`
and a fresh output directory do not rotate it. That is what makes an update from
a later release installable on a box that is already flashed — a per-build key
would be rejected by every existing box. Every build prints the certificate's
SHA-256, so a rotation shows up in the log instead of only when a box refuses an
update.

The development key is fine for testing and worthless for production: whoever
can read that directory can sign a bundle the box accepts. For a real deployment
pass your own pair with `IPTVPI_RAUC_CERT` and `IPTVPI_RAUC_KEY` and keep the
private key off the build machine. Replacing the key means rebuilding and
reflashing once, because the image carries the certificate it was built with.
In CI the pair lives in the Actions cache (`iptvpi-rauc-key-v1`) and is handed to
both matrix jobs as an artifact, so the pi1 and pi2 bundles of one release — and
of every later release — are signed with the same certificate. Bundles use
`format=plain`: no host-side `veritysetup` is needed, the bundle signature and
its hashes still cover every byte after installation.

---

### Power cuts, panics and freezes

Three small things make a box on a wall survive the ugly cases:

* **`/boot` is read-only.** The FAT partition is what the firmware reads, and an
  interrupted FAT write can cost the whole bootloader. `/etc/fstab` mounts it
  `ro`; the only writers — `iptv-boot-slot` (slot switch, slot state),
  `iptv-rauc-mark-good` (the good flag through RAUC) and `iptv-wifi-import`
  (consuming `wifi.txt`) — remount it read-write for the few bytes they change,
  `sync` and remount it read-only in an `EXIT` trap, so even a failing command
  leaves the partition clean.
* **`panic=10`** in `cmdline.txt`: a kernel panic reboots after ten seconds
  instead of leaving a hung box. `iptv-boot-slot` re-adds the token whenever it
  rewrites the command line, and `post-image.sh` keeps it in the image.
* **Hardware watchdog.** `CONFIG_BCM2835_WDT=y` (built in, in
  `kernel-iptv.fragment`) plus `RuntimeWatchdogSec=15` in
  `/etc/systemd/system.conf`: systemd pets `/dev/watchdog` and stops when PID 1
  or its own loop is wedged, so a frozen Kodi resets the board instead of
  waiting for someone to pull the power.

Uploads and downloads are spooled on the data partition, never in `/tmp` — see
§6.

---

## 6. The playlist web interface

`iptv-webui.service` runs the Flask app on **port 80** and listens on all
interfaces. It has no login: keep it on a trusted LAN.

The backend runs on the image's Python 2.7. Kodi 18 requires Python 2 and
Buildroot 2021.02 builds a single interpreter per image, so `webui/app.py` is
written to work on 2.7 and 3.x, and `webui/launch.sh` starts whichever
interpreter actually has Flask installed.

Three ways to replace the playlist:

| Action | What happens |
|---|---|
| **Upload a file** | the `.m3u`/`.m3u8` file is uploaded and stored |
| **Paste a playlist** | the textarea content is stored verbatim |
| **Load from a URL** | the **box** downloads the URL (no CORS issues), 25 s timeout, 32 MB limit |

Behaviour:

* The previous playlist is kept as `/var/lib/iptv/playlist.m3u.bak`.
* Writes are atomic (temp file + `fsync` + `rename`), so Kodi never sees a
  half-written playlist.
* Content is sanity-checked: it must contain `#EXTM3U`/`#EXTINF`, or at least
  stream URLs. Empty files and garbage are rejected with an error message.
* The unfiltered playlist is kept as `/var/lib/iptv/playlist.full.m3u`;
  `playlist.m3u` is always regenerated from it through the group filter.
* The selected groups are remembered in `/var/lib/iptv/state.json`.
* **Everything is streamed.** Uploads, pasted text and URL downloads are
  spooled into a temp file in 64 KB chunks, then parsed and written line by
  line; the playlist is never held in RAM as a whole. An 18.8 MB /
  200 000-channel playlist is stored and filtered without measurable memory
  growth, so a 50 MB list cannot spike a 256 MB Pi.
* **Reload Kodi after saving** (checked by default) disables and re-enables
  `pvr.iptvsimple` over Kodi's local JSON-RPC API
  (`http://127.0.0.1:8080/jsonrpc`), so the new channels appear without a
  restart. Only if JSON-RPC is unreachable does it fall back to
  `systemctl restart kodi.service`.
* `/playlist.m3u` always serves the current file for backup purposes.

### Channel groups

Every `group-title` attribute in the playlist becomes a checkbox in the web
UI. Deselect the groups you do not watch and press **Apply selection**: the
full playlist stays on the SD card, but `playlist.m3u` (what Kodi reads) only
contains the selected channels. On a 256 MB Pi 1 this is the difference
between a 4000-channel list that eats the RAM budget and a 40-channel list
that does not. Channels without a `group-title` land in `Ungrouped`.

### EPG (XMLTV)

The EPG field writes `epgPathType`/`epgUrl`/`epgCache` into the
`pvr.iptvsimple` settings, so Kodi downloads the programme guide itself.
Empty the field to disable the guide.

The on-disk EPG database cache is switched off (`epgCache=false`) on purpose:
the guide is fetched when Kodi needs it instead of growing an SQLite database
on the SD card, which saves both RAM and write cycles on a device that may be
unplugged at any moment.

API, if you want to script it:

```sh
curl -F mode=file -F restart=1 -F file=@channels.m3u http://iptvpi.local/api/playlist
curl -F mode=url  -F restart=1 -F url=http://provider/playlist.m3u http://iptvpi.local/api/playlist
curl -F mode=paste -F restart=0 -F content="$(cat channels.m3u)" http://iptvpi.local/api/playlist
curl -X POST http://iptvpi.local/api/restart
curl http://iptvpi.local/api/status
curl http://iptvpi.local/api/stats
curl http://iptvpi.local/api/groups
# keep only two groups in the active playlist
curl -H 'Content-Type: application/json' \
     -d '{"groups":["News","Sports"]}' http://iptvpi.local/api/groups
# set the XMLTV EPG URL
curl -H 'Content-Type: application/json' \
     -d '{"url":"http://provider/epg.xml.gz"}' http://iptvpi.local/api/epg
```

### Scheduled refresh (no browser needed)

Saving a playlist **from a URL** also stores that URL in
`/var/lib/iptv/state.json` and arms the refresh timer:

```
iptv-playlist-refresh.timer  -> iptv-playlist-refresh.service
                                 /usr/share/iptv-webui/launch.sh --refresh
```

Ten minutes after boot and then every 12 hours (with up to 30 minutes of
jitter, so a fleet of boxes does not hit the provider at the same second) the
job re-downloads the playlist, applies the group selection you made in the web
UI, writes `playlist.m3u` and asks Kodi to reload over JSON-RPC. It streams
exactly like the interactive path, so a 50 MB playlist costs no RAM.

The job is deliberately dumb about some edge cases and loud about one:

* no URL saved yet (`--refresh` with an empty `source_url`) — exits 0 and
  leaves everything alone, so the timer can stay enabled on a box that is fed
  playlists by hand;
* the provider renamed groups — groups that disappeared are dropped, and if
  *none* of your previous groups survive, the whole playlist is activated
  again instead of leaving Kodi with an empty channel list;
* saving an uploaded file or pasted text clears `source_url`, so the timer
  never overwrites a hand-made playlist;
* the provider answers a conditional request — the `ETag`/`Last-Modified` of
  the last successful download are remembered per URL in `state.json`, so the
  timer sends `If-None-Match`/`If-Modified-Since` and a `304` means no playlist
  bytes cross the network at all (reported as `provider reports no change
  (HTTP 304)`). An interactive save always fetches the body and just records
  the validators. Only the current URL's entry is kept, so saving another URL
  or a file prunes the old one and `state.json` cannot accumulate them;
* the provider still serves the same channels — every entry is hashed and the
  digests are summed across the file, so the comparison ignores the `#EXTM3U`
  header and the order of the list (providers reshuffle constantly), but not a
  renamed channel, a changed stream URL, or an added/removed entry. An
  unchanged result is reported as `playlist unchanged` without rewriting
  either playlist file and without touching Kodi, so a 12-hour timer costs
  nothing on a stable provider (a missing `playlist.m3u` is still rebuilt from
  that same download);
* the download failed — exits 1 without touching `playlist.m3u`.

The dashboard's **Scheduled refresh** card shows all of this without a shell:
whether a URL is saved, the source URL, the last run with its result, and when
the next run is due. The last result is stored in `state.json`, so a failure
stays visible in red — with its error message — even after a reboot; the next
run comes from `systemctl show iptv-playlist-refresh.timer`. If systemd cannot
be reached the card says *unknown* instead of inventing a time, and the card
only refreshes while the tab is visible like the telemetry tiles.

Its **Refresh now** button runs the same code path immediately instead of
waiting for the timer: the browser gets the result, the updated card and the
new channel count in the same request. The button is disabled while no URL is
saved, and a failed manual run is recorded and shown exactly like a failed
scheduled one. A run that finds the provider unchanged is shown as
`playlist unchanged` — nothing was rewritten and Kodi was left alone.

The last run's HTTP status is stored as its own field (`http_status`) and
rendered straight from `/api/status`: `200` when a body was transferred, `304`
when the provider had nothing new, the provider's own code (`500`, `404`, ...)
when it refused, and `null` when no response arrived at all. The card shows
`transferred (200)`, `not modified (304)`, `HTTP 500` or `no response` next to
the timestamp, so it never has to pick numbers out of the message text.

Failures are counted as well: `failures` in `state.json` counts failed refreshes
in a row, any successful run resets it, and saving a different URL starts a
fresh count. Once the streak reaches `REFRESH_FAILURE_LIMIT` (2 by default,
overridable with `IPTV_FAILURE_LIMIT`) the card shows a warning — `2 refreshes
in a row failed — check the playlist URL and whether the provider still
answers` — rendered server-side too, so it is there even before the JavaScript
poll runs. A single failed run stays quiet: one failure can be a provider hiccup,
and a warning on every blip would just train people to ignore it.

Once a streak is worth warning about, the box can also tell someone outside the
box. Put a webhook URL in **Failure alerts** (saved in `state.json`, or set
`IPTV_NOTIFY_URL` in the service environment) and the refresh POSTs one small
JSON body when the streak first reaches the limit: `text` and `content` (the
same sentence, so Slack and Discord webhooks work unchanged), plus `event`,
`failures`, `http_status`, `playlist_url` and `at` for anything custom. It is
edge-triggered — a week-long outage sends one message, not fourteen — and best
effort with a timeout of `IPTV_NOTIFY_TIMEOUT` seconds (10 by default): a
webhook that answers `503`
or refuses the connection is shown in the card as a failed alert, but never
changes the refresh result or its exit status.

### System health

The **System state** card (and `/api/health`) shows what fails below the app:
units `systemd` reports as failed, the error-priority journal lines of this
boot, `vcgencmd get_throttled` decoded into current and since-boot flags
(under-voltage is the usual reason a Pi 1 misbehaves), the OOM kill counter
from `/proc/vmstat`, and free space on `/` and on `/var/lib/iptv` (flagged
below 100 MB). Samples are cached for 30 s so rendering the page does not
shell out on every request; **Reload system state** forces a fresh one.

`iptv-health-watch.timer` runs the same check every 10 minutes and writes
exactly one line into `errors.log` when the state *changes* — a new failed
unit, new journal errors, throttling, an OOM kill — so the volatile `/var/log`
cannot hide a problem across a reboot while the SD card still sees one write
per event instead of 144 a day. The web UI takes one sample when it starts.

`OnFailure=` in `kodi.service` and `iptv-webui.service` starts
`iptv-unit-alert@<unit>.service`, which runs `app.py --alert-unit <unit>`: the
failure is logged as `[system]` and sent to the configured alert webhook as
`event: unit_failed` with the unit name and its state, at most once per unit
per 6 hours (`IPTV_UNIT_ALERT_COOLDOWN`), so a crash loop cannot flood the
endpoint. Without a webhook URL it is still logged locally.

### Logs

Two rotated files in `/var/lib/iptv/log`, each capped at 64 KB with one
previous copy kept (`IPTV_LOG_MAX_BYTES`), so the pair can never grow past
128 KB on the SD card:

* `errors.log` — failed refreshes, Kodi reload failures, alert delivery
  failures, rejected or failed updates and unhandled web errors, one line each
  with a timestamp and a scope tag (`[refresh]`, `[kodi]`, `[alert]`,
  `[update]`, `[webui]`).
* `hardware.log` — one sample every 30 minutes from
  `iptv-hardware-sample.timer`: CPU/GPU temperature, ARM and H.264 clocks,
  free/total memory, swap, zram usage, load average, governor and uptime.

Both are shown in the dashboard as collapsed sections (server-rendered, so
they work without JavaScript) with their current size, and `/api/logs?lines=N`
returns the tail for scripting. Writing is best effort: an unwritable log
directory never fails a refresh, and `python app.py --sample` records a single
sample for the timer.

### Updates

The **Update the app** card installs a `.tar.gz` of the web interface —
`app.py` at the root, optionally `launch.sh`, `templates/` and `static/` — so
bug fixes and new features can ship without rebuilding the rootfs. The bundle
is unpacked into `/var/lib/iptv/app`, where `launch.sh` prefers it over the
copy baked into the image; deleting that directory returns the box to the
flash-time version. The previous version is kept as `app.bak`, and the running
version keeps reporting its own `APP_VERSION` until the service restarts.

Everything is checked before anything is replaced: only the four path shapes
above are accepted (no absolute paths, no `..`, no symlinks, at most 64 files
and 4 MB unpacked), the archive must contain `app.py`, and the new `app.py`
must compile — a rejected bundle returns `400` with the reason, leaves the
installed version untouched and deletes its staging directory. After a
successful swap the unit is restarted once the response has been sent
(`IPTV_UPDATE_RESTART_DELAY`, 1.5 s), so the browser still gets its answer.
The whole endpoint is unauthenticated like the rest of the UI: keep it on the
LAN.

```sh
systemctl list-timers iptv-playlist-refresh.timer
systemctl start iptv-playlist-refresh.service    # refresh right now
journalctl -u iptv-playlist-refresh -n 30        # what happened last time
systemctl edit iptv-playlist-refresh.timer       # e.g. OnUnitActiveSec=6h
systemctl disable --now iptv-playlist-refresh.timer   # opt out
```

### System updates from the browser

The **System update (A/B)** card does what `rauc install` does, without a
shell:

* it shows the booted slot, the slot for the next boot and each slot's
  `good`/`bad` state — read through the same helper RAUC's bootloader backend
  uses (`iptv-boot-slot get-current`, `get-primary`, `get-state`), so the page
  and the bootloader can never disagree;
* it accepts a signed `.raucb` bundle and refuses anything else: a foreign
  extension, an empty file, a bundle larger than `IPTV_RAUC_MAX_BYTES`
  (512 MB), one that does not fit next to the free space of the data
  partition, and a second upload while an install is running (`409`);
* instead of uploading, the bundle can come from a **URL on your LAN** — the
  box downloads it itself in 64 KB chunks, exactly like the playlist URL, so a
  few hundred MB never travel through the browser. The card shows the download
  phase and percentage from the bytes received (`Content-Length`), then the
  RAUC steps; size cap, free-space check and the single-install rule are the
  same. This works even when RAUC was built without its own network support
  (`BR2_PACKAGE_RAUC_NETWORK`), because the app does the fetching;
* **checksum (optional, recommended):** both forms have a SHA-256 field. Paste
  the number from `iptvpi-<version>.raucb.sha256`, which `post-image.sh` writes
  next to the bundle, and the file is hashed on the box **before** RAUC sees
  it: a mismatch answers `400`, deletes the spooled file again and never starts
  an install, so a truncated or mangled transfer is caught before a slot is
  written. A `sha256:` prefix and upper case are accepted (`sha256sum` output
  can be pasted as is), anything that is not 64 hex characters is refused with
  a message saying so. Leaving the field empty keeps the old behaviour — RAUC's
  signature check against `/etc/rauc/ca.cert.pem` still decides. The digest the
  box computed is kept in the history entry next to version and origin.
  **For a URL the field can stay empty:** the box then fetches
  `<bundle>.raucb.sha256` next to it (the file `post-image.sh` writes, query
  string preserved), takes the first SHA-256 in it and compares the download
  against that. A published file that contradicts the bundle aborts the install
  like a typed mismatch; no file at all only means the download is verified by
  RAUC's signature alone. The number that was used shows up as `expected` in
  `/api/rauc`, and the history entry says **where it came from**: `typed` when
  it was pasted into the form, `sidecar` when the box fetched it (shown as
  “from .sha256” next to the hash);
* the install runs in the background (`rauc install` output goes to
  `/var/lib/iptv/updates/install.log`), and the card polls `/api/rauc` every
  two seconds — only while this tab is visible — showing the percentage and the
  current step (checking the bundle → writing the inactive slot → switching the
  primary slot). The page stays usable while the slot is written;
* the uploaded bundle lives in `/var/lib/iptv/updates` and is deleted once the
  install finished; the log line keeps the outcome. **Nothing goes through
  `/tmp`:** `iptv-webui.service` sets
  `TMPDIR=/var/lib/iptv/updates/tmp`, `launch.sh` creates that directory and
  removes leftovers older than five minutes, and `app.py` hands it to
  `tempfile` (`webui_temp_dir()`). Werkzeug spools the request body there, the
  bundle itself is written to `/var/lib/iptv/updates` in 64 KB chunks, and the
  running process never holds the payload: a 350 MB bundle works with
  `MemoryMax=96M` and a 32 MB `/tmp`. The test suite pushes a 34 MB bundle —
  larger than `/tmp` — and asserts that `/tmp` neither grows nor gains a file;
* **reboot on request:** tick “reboot automatically when the install finished”
  before uploading or fetching, and the box reboots `IPTV_RAUC_REBOOT_DELAY`
  (5 s) after a *successful* install — long enough for the page to show the
  result, and never after a failure. Without the tick nothing restarts; the
  previous system stays in the other slot as the rollback target. The **Reboot
  now** button next to it does the same manually (browser confirmation first,
  `IPTV_RAUC_MANUAL_DELAY`, 2 s) — it is refused with `409` while RAUC is still
  writing a slot and with `400` when systemd is disabled, so a reboot can never
  interrupt an install or pretend to happen;
* **it checks the slot afterwards:** the slot that was running and the kernel
  boot id are recorded in `/var/lib/iptv/updates/pending-update.json` before
  the install starts. After the reboot the card compares the running slot with
  that record: a different boot id on the other slot shows “Update verified:
  slot B is running”, a different boot id on the same slot warns “still running
  slot A after the reboot: the switch did not happen, or RAUC rolled back” and
  writes that to `errors.log`. The verdict is stored once, so polling the page
  never keeps writing to the SD card; a failed install closes the record as
  `failed` instead of promising a verification that cannot happen;
* **history:** the card lists the last five updates, newest first, with time,
  state (`installed`, `verified`, `failed`, `warning`), bundle name, the
  bundle's own `Version` and `Compatible` read from its manifest
  (`rauc info --output-format=shell`, falling back to the readable output; an
  unreadable manifest leaves the fields empty instead of failing the install),
  the origin (`upload` or `url`, the URL as tooltip) and the message that goes
  with it. The block opens by itself when the newest entry is a problem
  (`failed` or `warning`, count in red) and stays collapsed otherwise; once you
  toggle it yourself, polling leaves it alone (`history_alert` in the API is
  the same decision). A checkbox inside the block, **only failed and warned**,
  narrows the list to `failed`/`warning` entries and shows the count as
  `n/m` (`no failed or warned updates` when nothing matches); it only filters
  the listing — the counter colour and the auto-open still follow the newest
  entry, so a problem never hides behind the filter. The filter is client-side,
  it re-applies itself after every poll and needs no extra request. Next to the
  total the summary carries a second, red counter with the number of entries
  that report a problem (`history_problems` in `/api/rauc`), visible whether or
  not the filter is on and hidden when there is none. That counter is also the
  shortcut for the filter: clicking it (or Enter/Space while it has focus)
  ticks the checkbox, opens the block and stops polling from closing it again,
  so a glance at the number leads straight to the entries behind it. It lives in
  `/var/lib/iptv/updates/history.json` and is rewritten exactly twice per
  update (install finished, boot verdict known), so it costs two small writes
  and never more than five entries (`IPTV_RAUC_HISTORY`,
  `IPTV_RAUC_HISTORY_LIMIT`).

Endpoints: `GET /api/rauc` (slots + install progress) and
`POST /api/rauc/install`, which takes either a multipart field `bundle` or a
form field `url` (`http://`/`https://` ending in `.raucb`), and
`POST /api/rauc/reboot` (`confirm=1`). Tuning knobs:
`IPTV_RAUC`, `IPTV_RAUC_BOOT_SLOT`, `IPTV_RAUC_CONF`, `IPTV_RAUC_DIR`,
`IPTV_RAUC_MAX_BYTES`, `IPTV_RAUC_TIMEOUT`, `IPTV_RAUC_HEADROOM`,
`IPTV_RAUC_REBOOT_DELAY`, `IPTV_RAUC_MANUAL_DELAY`, `IPTV_RAUC_PENDING`,
`IPTV_RAUC_HISTORY`, `IPTV_RAUC_HISTORY_LIMIT`, `IPTV_BOOT_ID_FILE`. Like every
other endpoint it is unauthenticated: a signature is still required for the
bundle to install, but anyone on the LAN can start an install — keep the UI on
a trusted network.

### Live hardware telemetry

`/api/stats` (and the tile row at the top of the dashboard, refreshed every
five seconds) reports the values that tell you whether the box is coping:

| Field | Source |
|---|---|
| `cpu_temp_c` | `/sys/class/thermal/thermal_zone0/temp` |
| `gpu_temp_c` | `vcgencmd measure_temp` |
| `arm_mhz` | `vcgencmd measure_clock arm` |
| `h264_mhz` | `vcgencmd measure_clock h264` |
| `governor`, `cpufreq_min_mhz`, `cpufreq_max_mhz` | `/sys/devices/system/cpu/cpu0/cpufreq/*` |
| `mem_total_mb`, `mem_available_mb` | `/proc/meminfo` |
| `swap_total_mb`, `swap_free_mb`, `zram` | `/proc/meminfo`, `/sys/block/zram0/*` |
| `load`, `uptime_s` | `/proc/loadavg`, `/proc/uptime` |

Watch `arm_mhz` while the UI is idle and while a stream plays: with
`force_turbo=0` and the `ondemand` governor it should sit near 600 MHz and
climb towards 900 MHz under load. It should *never* be pinned at 900 MHz
while idle — that would mean DVFS is disabled. `h264_mhz` behaves the same
way between 250 and 400 MHz, driven by the firmware.

The dashboard is free while nobody is looking at it: it uses the Page
Visibility API, so the five-second `/api/stats` poll (and the slower status
poll) only run while the tab is in the foreground. Switch to another tab,
minimise the browser or close it and the box stops polling, running
`vcgencmd` and touching the SD card entirely — no background CPU wakeups
during normal TV viewing. Coming back to the tab refreshes immediately.

Paths used inside the image:

```
/var/lib/iptv/playlist.m3u        # the file Kodi reads (filtered)
/var/lib/iptv/playlist.full.m3u   # unfiltered playlist as saved
/var/lib/iptv/playlist.m3u.bak    # previous version
/var/lib/iptv/state.json          # groups, EPG URL, playlist URL, validators, failures, last run
/usr/share/iptv-webui/            # app.py, templates/, static/, launch.sh
```

---

## 7. Kodi autostart and the IPTV add-on

`kodi.service` (in `rootfs-overlay/etc/systemd/system/`) starts Kodi at boot:

```ini
Environment=HOME=/
Environment=KODI_HOME=/usr/share/kodi
Environment=AE_SINK=ALSA
TTYPath=/dev/tty1
ExecStart=/usr/bin/br-kodi /usr/lib/kodi/kodi.bin --standalone -fs
Restart=always
```

* `HOME=/` makes Kodi use `/.kodi`, which Buildroot symlinks to `/var/kodi`
  (the writable profile directory). The pre-seeded profile lives there:
  `/var/kodi/userdata/autoexec.py` and
  `/var/kodi/userdata/addon_data/pvr.iptvsimple/settings.xml`.
* `-fs` is full screen; there is no window manager or desktop underneath.
* `br-kodi` is Buildroot's launcher: it forwards `SIGTERM` to Kodi and maps
  Kodi's exit codes to `poweroff`/`reboot`, so the Kodi power menu works.
* `kodi.service` declares `Conflicts=getty@tty1.service`, and `post-build.sh`
  removes the tty1 getty. The login console is on the serial port instead.
* `Restart=always` means a Kodi crash returns to Kodi, not to a shell.

The add-on is **pre-installed** in the image by Buildroot
(`BR2_PACKAGE_KODI_PVR_IPTVSIMPLE` → `/usr/share/kodi/addons/pvr.iptvsimple`),
**pre-configured** by the seeded `settings.xml` (local file
`/var/lib/iptv/playlist.m3u`, cache off), and **enabled at every start** by
`autoexec.py`, which also starts the PVR manager. `post-build.sh` warns during
the build if either the Kodi binary or the add-on is missing.

Two more profile files are seeded, because Kodi only exposes their settings
in its GUI:

| File | Contents |
|---|---|
| `advancedsettings.xml` | `<splash>false</splash>`, `<loglevel hide="true">0</loglevel>`, `<gui>` with `algorithmdirtyregions=3` / `nofliptimeout=0` / `smartredraw=true`, stream `<cache>` with `buffermode=1`, `memorysize=20971520` (20 MB) and `readfactor=4.0` |
| `guisettings.xml` | local web server/JSON-RPC on port 8080 bound to localhost, no media metadata or chapter-thumbnail extraction, and the background services switched off: RSS feeds, EventServer, UPnP/DLNA (+announce), AirPlay, Zeroconf, weather |

The 20 MB stream cache is the largest single RAM consumer on a 256 MB board
(Kodi wants roughly 3× that free). Lower `memorysize` if you see the OOM
killer in `dmesg`; the value is in bytes, so 12 MB would be `12582912`.

Switching Zeroconf off means the box no longer answers mDNS lookups, so
reach the web UI by IP address (`http://<board-ip>/`) rather than by
`http://iptvpi.local/`.
HDMI-CEC comes from `libcec` plus `BR2_PACKAGE_KODI_LIBCEC`; enable the CEC
adapter in Kodi → Settings → System → Input → Peripherals once, and the TV
remote works over the HDMI cable.

---

## 8. Networking

* `systemd-networkd` brings up `eth0` and `wlan0` with DHCP
  (`/etc/systemd/network/*.network`), with IPv4 link-local as a fallback.
* Wi-Fi: edit `/etc/wpa_supplicant/wpa_supplicant-wlan0.conf` (SSID, PSK and
  the two-letter `country=`), then `systemctl restart wpa_supplicant-wlan0`.
  There is no `wpa_cli`: Buildroot 2021.02 builds wpa_supplicant without its
  control interface, so the configuration is static (restart to apply).
  Pi 1/2 have no built-in radio, so a USB dongle is required; the image ships
  the `brcmfmac` firmware and `wireless-regdb`.
* **Headless Wi-Fi setup:** with the SD card in a computer, create
  `wifi.txt` in the root of the FAT boot partition (the only partition
  Windows/macOS can read):

  ```
  ssid=MyNetwork
  psk=MyPassword
  country=DE
  ```

  `iptv-wifi-import.service` runs at boot, writes
  `wpa_supplicant-wlan0.conf`, restarts the unit once and renames the file to
  `wifi.txt.applied` so it is not imported again. A bare two-line file (SSID
  then password) also works. The boot partition is FAT and **not** encrypted,
  so delete the file once the box is on your network.
* SSH: `dropbear` is installed but **not enabled**. To enable it:

  ```sh
  passwd root                       # set a password first
  systemctl enable --now dropbear
  ```

* Serial console: `getty` on `ttyAMA0`, 115200 8N1, on GPIO 14/15.

---

## 9. Tuning

| What | Where |
|---|---|
| GPU memory (H.264 decode buffers) | `board/raspberrypi/config-pi1.txt` / `config-pi2.txt` → `gpu_mem_*` |
| CPU range (600 ↔ 900 MHz) | same files → `force_turbo=0`, `arm_freq`, `arm_freq_min`, `over_voltage` |
| H.264 block (250 ↔ 400 MHz) | same files → `h264_freq`, `h264_freq_min` |
| SD host clock | same files → `dtparam=sd_overclock=80` |
| Scaling governor | `rootfs-overlay/usr/bin/iptv-cpufreq-setup` (`IPTV_CPUFREQ_GOVERNOR`, default `ondemand`) |
| `ondemand` ramp threshold | same script (`IPTV_CPUFREQ_UP_THRESHOLD`, default `75`) |
| Kernel VM tunables | `rootfs-overlay/etc/sysctl.d/60-iptv-vm.conf` |
| Service priorities | `kodi.service` (`Nice=-10`, `OOMScoreAdjust=-500`) vs `iptv-webui.service` (`Nice=19`, idle I/O, `MemoryMax=96M`) |
| GUI resolution | Kodi → Settings → System → Display → Resolution (720p UI, 1080p video untouched) |
| Kernel cpufreq/thermal support | `board/raspberrypi/kernel-iptv.fragment` |
| HDMI mode, overscan, hotplug | same `config.txt` files |
| Kernel command line | `board/raspberrypi/cmdline.txt` |
| Timezone (EPG times) | `BR2_TARGET_LOCALTIME` in the defconfig, plus `BR2_TARGET_TZ_INFO=y` |
| Playlist path | `IPTV_PLAYLIST` in `iptv-webui.service` **and** `m3uPath` in the seeded `settings.xml` |
| zram size | `rootfs-overlay/usr/bin/iptv-zram-setup` (50 % of RAM, 64–512 MiB) |
| Stream cache | `advancedsettings.xml` → `cache/memorysize` (bytes) |
| Refresh interval | `iptv-playlist-refresh.timer` (`OnUnitActiveSec=12h`; override with `systemctl edit`) |
| tmpfs sizes | `rootfs-overlay/etc/fstab` (`/tmp` 32 M, `/var/log` 16 M) |
| Root filesystem size | `BR2_TARGET_ROOTFS_EXT2_SIZE` (default 1 GB image) |

`gpu_mem` is the single most important knob for playback: too little causes
stuttering or green frames at 1080p, too much starves the CPU side on a 256 MB
board. Do **not** add `dtoverlay=vc4-kms-v3d`: it switches the display to KMS
and Kodi's Pi platform will fail to start.

### Dynamic clocking (and why nothing is pinned)

`force_turbo=0` is written explicitly into both `config.txt` files: it keeps
the firmware's dynamic voltage/frequency scaling (DVFS) active. Together with
`arm_freq=900` / `arm_freq_min=600` and the `ondemand` governor selected at
boot by `iptv-cpufreq.service`, the CPU idles at 600 MHz and ramps to 900 MHz
when Kodi or the web UI needs it. `over_voltage=2` is applied *dynamically*
by the firmware only at the higher operating points — there is no locked
frequency anywhere, and `force_turbo=1` (which would lock the clock and void
the firmware's thermal behaviour) is never set.

The VideoCore H.264 block is scaled by the firmware on its own:
`h264_freq=400` is the ceiling during decode, `h264_freq_min=250` the idle
point. The SD reader runs at 80 MHz via `dtparam=sd_overclock=80`; the param
is applied by `start.elf` to the `sdhost` node of the base DTB, which
`post-image.sh` stages next to `config.txt`. If your card cannot take 80 MHz
(boot stalls or `mmc0: timeout` in `dmesg`), lower the value to 50 or remove
the line — everything else keeps working.

### GUI rendering, 720p and idle CPU

`advancedsettings.xml` enables dirty-region rendering so a static menu is not
re-pushed 60 times a second: `algorithmdirtyregions=3` (render only when
something actually changed; mode 2 can flicker on VideoCore), `nofliptimeout=0`
(no extra flip when nothing moved) and `smartredraw=true` (Kodi 18 skips the
GUI render call entirely while a video plays).

About the requested `<guires>720</guires>`: **Kodi has no such tag** in
`advancedsettings.xml` — that name comes from Enigma2/streamer boxes, and Kodi
would silently ignore it. The supported way to render the GUI at 720p while
keeping 1080p video decoding untouched is Kodi's own setting,
*Settings → System → Display → Resolution → 1280x720*, which persists in
`guisettings.xml` (`videoscreen.resolution`) and scales only the user
interface. On a 256 MB Pi the Raspberry Pi platform already defaults to a
reduced GUI resolution, so most users need to change nothing.

### SD card longevity

* The root filesystem and the data partition are mounted `noatime`, so reading
  files does not write access times back to the card.
* `/tmp` and `/var/log` are `tmpfs`: log churn and Kodi's transient files stay
  in RAM, and a power cut cannot corrupt them.
* Playlist writes are atomic (temp file + `fsync` + `rename`), and zram keeps
  swapping in RAM instead of on the card.
* `journald` is capped (see `etc/systemd/journald.conf.d/iptv.conf`).

Since `/var/log` is volatile, journal output is lost on reboot. For a
persistent log, remove the `/var/log` line from `fstab` and rebuild.

---

## 10. Troubleshooting

| Symptom | Check |
|---|---|
| Black screen, no Kodi | serial console; `systemctl status kodi`; `journalctl -u kodi -b` |
| `kodi.service` restart loop | `AE_SINK=ALSA` needs `snd-bcm2835`; see `dmesg | grep -i alsa` |
| No channels in Kodi | `ls -l /var/lib/iptv/playlist.m3u`, `journalctl -u kodi | grep -i iptv` |
| Add-on not enabled | open Kodi → Add-ons → My add-ons → PVR clients → IPTV Simple Client → Enable, then Post-process/PVR manager restart |
| Web UI not reachable | `systemctl status iptv-webui`; `journalctl -u iptv-webui -b`; port 80 needs root (the unit runs as root) |
| No network | `networkctl status`; `journalctl -u systemd-networkd -b` |
| Playback stutters on Pi 1 | lower the stream resolution, raise `gpu_mem_256`, or use a 512 MB board |
| Out of memory | `free -m`, `zramctl`; reduce `gpu_mem`, avoid 1080p UI skin |
| Update refused: “does not match the checksum” | the number belongs to a different bundle: compare `sha256sum iptvpi-<version>.raucb` on the build host; clear the field to rely on the signature only, and delete a stale `<bundle>.raucb.sha256` next to the URL |
| Update refused: “the checksum has to be 64 hex characters” | paste only the hash (from `iptvpi-<version>.raucb.sha256`, `sha256:` prefix and upper case are fine); the file name after the hash is only used when the box fetches the sidecar itself |
| History says `warning` after a reboot | `cat /proc/cmdline` and `iptv-boot-slot get-current` show the booted slot; `rauc status --detailed` shows both slot states — a `warning` means RAUC rolled back and the old slot is still running |
| The update card shows no history | `ls -l /var/lib/iptv/updates/` and `journalctl -u iptv-webui -b`; the history is written on the data partition, so it starts empty on a fresh card |

Useful commands on the device:

```sh
systemctl status kodi iptv-webui
journalctl -u kodi -b -n 200
cat /proc/swaps
cat /etc/wpa_supplicant/wpa_supplicant-wlan0.conf
```

---

## 11. Project status / verification

Verified locally while writing this project:

* `bash -n` / `sh -n` on all shell scripts, `python -m py_compile` on the
  backend, and a real run of the Flask app with each endpoint exercised
  (upload, paste, URL fetch, restart in `IPTV_NO_SYSTEMD=1` mode, status,
  playlist download, oversized/rejected input);
* the scheduled refresh was exercised end to end against a local HTTP server:
  `app.py --refresh` re-downloads the saved URL, keeps the chosen group filter,
  handles provider changes (vanished groups are dropped; if none survive the
  whole playlist is activated), reloads Kodi over JSON-RPC, exits 1 on a
  provider 404 without touching the playlist, and exits 0 as a no-op when no
  URL has been configured. The dashboard card is covered by the same suite: it
  starts as "off (no URL saved)", follows a URL save, renders the recorded
  timestamp and message of a successful run, flags a failed run in red with its
  error text, and reports "unknown" for the next run when systemd is
  unreachable. The "Refresh now" button is covered as well: disabled without a
  saved URL, `400` when pressed without one, the same refresh path with the
  group filter preserved, and `500` with the recorded failure when the provider
  is gone;
* the last run's status is structured: `http_status` is `200` after a real
  transfer (including a `304` run that had to rebuild missing files), `304`
  for an unchanged provider, the provider's own code (`500`, `404`) when it
  refuses, and `null` when nothing answered; legacy `state.json` files without
  the field and non-integer values both load as `null`;
* a failure streak warns: the second consecutive failed run (timer or button,
  any HTTP error or no response at all) raises a warning in `/api/status` and
  in the server-rendered card, while one failure stays quiet; a success and a
  change of URL both clear the streak, legacy state files without the counter
  and garbage values (`"3"`, `-1`, `true`) all load as zero;
* alerts leave the box: the second consecutive failure POSTs exactly one JSON
  body to the saved webhook (or `IPTV_NOTIFY_URL`), carrying `text`, `content`,
  `event`, `failures`, `http_status` and `playlist_url`; a third failure sends
  nothing, a box that recovers and then fails again starts a new streak,
  clearing the URL disables alerts, and an endpoint answering `503` or refusing
  the connection is recorded as a failed alert without touching the refresh
  result or its exit code;
* logging is bounded and safe: `--sample` writes exactly one hardware line,
  failed refreshes land in `errors.log` with their HTTP status, rotation keeps
  one backup under a tiny cap, `--version` prints the version, an unwritable
  log directory returns false instead of raising, and `/api/logs` honours its
  line limit and tolerates a bad one;
* updates are validated before anything is swapped: a good bundle lands in
  `app/` with the previous version in `app.bak` and a restart request, a
  `webui/`-prefixed bundle is accepted, while a syntax error, a foreign file,
  a `../` path, a symlink, a missing `app.py`, a non-tar upload and an
  oversized bundle are all rejected with the installed version unchanged and
  no staging directory left behind;
* system health is visible and persistent: `/api/health` reports failed units,
  journal errors, decoded throttling flags, OOM kills and free space, a cached
  sample answers repeats while `?fresh=1` bypasses the cache, `--health` writes
  one line per changed state and stays silent when the state is unchanged or
  recovered, and `--alert-unit` logs and POSTs `unit_failed` once per unit
  before the cooldown suppresses the repeat;
* an unchanged provider is skipped: `--refresh` and the button both report
  `playlist unchanged`, leave both playlist files byte- and mtime-identical
  and make no JSON-RPC calls, while a deliberately deleted `playlist.m3u` is
  still rebuilt from the identical download. Reordering the provider's list,
  rewriting its `#EXTM3U` header, CRLF line endings and trailing whitespace
  all still count as unchanged, while a changed stream URL, a renamed channel,
  an added entry, a removed entry or a duplicated channel do not. Conditional
  requests are covered too: the remembered validators produce a `304` that
  transfers nothing and skips the reload, a provider that sends only
  `Last-Modified` works, a `304` with missing local files triggers exactly one
  unconditional retry that rebuilds them, and a provider that stops sending
  validators has its stale entry dropped; and switching URLs or saving a file
  prunes the previous URL's entry, so a box that has been pointed at many URLs
  still stores exactly one;
* a streaming benchmark that generates an 18.8 MB playlist with 200 000
  channels in 50 groups: storing it (validate, count groups, move into place,
  write the filtered output) takes ~1 s with a **0.0 MB peak RSS delta**, and
  filtering one group out of it streams the 18.8 MB file again without growth;
  oversized input is refused mid-stream rather than after buffering;
* a second Flask run against stubbed `vcgencmd`/`systemctl` binaries and a
  fake Kodi JSON-RPC server, covering the round-2 features end to end:
  `/api/stats` parsing (`arm_mhz` 900, `h264_mhz` 400, `gpu_temp_c` 51.3,
  memory/zram/load/uptime, graceful `null`s when `vcgencmd` is absent),
  `group-title` parsing (`News` ×2, `Sports` ×1, `Ungrouped` ×1), group
  filtering down to 2 channels in `playlist.m3u` while
  `playlist.full.m3u` keeps all 4, `state.json` remembering the selection,
  the EPG URL landing in `pvr.iptvsimple/settings.xml` as
  `epgUrl`/`epgPathType=1`/`epgCache`, rejection of `ftp://` EPG URLs and
  non-list group payloads, the soft reload using JSON-RPC **without** any
  `systemctl` call, and the fallback restarting `kodi.service` when the RPC
  port is closed;
* `advancedsettings.xml`, `guisettings.xml` and the add-on `settings.xml`
  parse as well-formed XML, and the EPG cache is written as `epgCache=false`;
* the new `kernel-iptv.fragment`, the extra Kodi/network packages and the
  `iptv-cpufreq`/`iptv-wifi-import` units are all accepted by Kconfig
  (`BR2_PACKAGE_LIBCEC`, `BR2_PACKAGE_KODI_LIBCEC`, `BR2_PACKAGE_OPENSSL`,
  `BR2_PACKAGE_CA_CERTIFICATES`, `BR2_PACKAGE_KODI_INPUTSTREAM_ADAPTIVE` are
  `=y` in both boards' `.config`, nothing was dropped, and
  `BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES` points at the fragment);
* `post-build.sh` and `post-image.sh` were executed against throw-away
  target/binary trees (with `genimage` stubbed out): the web UI files land in
  `/usr/share/iptv-webui`, the `*.wants` symlinks are created (including
  `iptv-cpufreq.service`, `iptv-wifi-import.service` and
  `systemd-sysctl.service`, which applies the VM tunables), the tty1 getty is
  removed, `/etc/resolv.conf` points at systemd-resolved, the boot partition
  is staged with our `config.txt`/`cmdline.txt` plus firmware and DTBs (the
  base DTB that `dtparam=sd_overclock=80` is applied to), and the generated
  genimage description only lists those files;
* the defconfigs were loaded with **real Buildroot 2021.02.x** (`make
  iptvpi1_defconfig` and `make iptvpi2_defconfig`, both exit 0), and the
  resulting `.config` of both boards checked symbol by symbol: every option
  the defconfigs request survives Kconfig, and Kodi
  (`BR2_PACKAGE_KODI`, `BR2_PACKAGE_KODI_PLATFORM_RBPI`,
  `BR2_PACKAGE_KODI_ALSA_LIB`, `BR2_PACKAGE_KODI_PVR_IPTVSIMPLE`), libEGL/
  libGLES/OpenMAX from `rpi-userland`, Flask and the systemd networking
  options are all enabled. This is how the Python 2 `PYC_ONLY` trap that made
  Kodi silently disappear was found and fixed.

* the system update card is covered by `tests/verify_rauc_ui.py`
  (`python3 tests/verify_rauc_ui.py`), running the real `webui/app.py` against
  a stubbed `rauc` and the real `iptv-boot-slot` helper: `/api/rauc` reports
  the booted slot, the primary slot and both slot states (a `bad` set through
  the bootloader shows up as `bad`), the card renders the form, both slots and
  the `booted`/`next boot` marks; a wrong extension, an empty request and an
  oversized bundle are rejected without leaving a file behind, a valid
  `.raucb` is answered with `202` in well under a second while the install
  keeps running, and polling shows monotonic progress with the intermediate
  percentages and the "writing the inactive slot" step before ending in
  `success` at 100%. The URL route is covered against a real HTTP server: the
  box fetches the bundle itself, the `download` phase with its percentage is
  visible before the install phase starts, a URL without `.raucb` and a
  `ftp://` URL are refused at once, a `404` ends in `error` naming the status,
  a second URL install is refused with `409`, and no `.part` file is left
  behind. The checksum field is covered from both sides: a matching SHA-256
  (given as `sha256:` plus upper case) installs and lands in the history entry,
  a mismatching one answers `400` naming the checksum, leaves no file in the
  bundle directory and starts no install, a value that is not a checksum is
  refused with the “64 hex characters” message, and a fetched bundle with a
  wrong checksum ends in `error` instead of writing a slot. `post-image.sh`
  writes the `.sha256` file next to the bundle and the test recomputes the
  digest of the bundle to compare it. The automatic sidecar lookup is covered by
  the test HTTP server: without a typed checksum a published, matching
  `.sha256` is fetched and used (`expected` and the history entry carry it), a
  published wrong one stops the install with a checksum error, and a 404 for the
  sidecar just leaves the download unverified. `checksum_source` tells the two
  apart: `typed` after a pasted number, `sidecar` after the automatic lookup (in
  the live status and in the history entry), empty when no checksum was used at
  all — the card renders that as “typed” or “from .sha256”. The history counters
  are checked too: after a failed install the problem counter is rendered
  unhidden with the number of problem entries, and `/api/rauc`'s
  `history_problems` always equals the count of `failed`/`warning` entries. The
  same tag carries the `role="button"` shortcut and its hint text, so the
  counter cannot drift away from the click handler that belongs to it. The reboot option and the slot check are covered as well: an install
  without the tick calls neither `systemctl reboot` nor anything else, one with
  it asks for exactly one reboot after a successful install and none after a
  failing one (which closes the pending record as `failed`); the record keeps
  the running slot and the boot id, the same boot reports `waiting`, a new boot
  id on the other slot reports `ok` with the slot name and is written once (a
  second poll leaves file content and mtime untouched), and a new boot id on
  the same slot reports `warning`, logs `update verification failed` and names
  both possible causes. The manual button is covered too: without `confirm=1`
  it is refused, with systemd disabled it is refused with a reason and runs
  nothing, a confirmed click calls `systemctl reboot` exactly once and names
  the slot it will boot, and while an install is running it returns `409`
  without touching the reboot path. The history is checked as well: a finished
  install appears with its bundle name and timestamp as `installed`, a failed
  one as `failed`, the verified boot turns that entry into `verified` with the
  slot that is now running (`B` in the test), and the list never grows past
  `history_limit` even after eleven installs. Version and origin come from a
  stubbed `rauc info`: an upload is recorded as `upload` with the file name as
  origin, a fetched bundle as `url` with the whole URL, both with version
  `2.0.0` and `compatible=iptvpi`; when `rauc info` fails the install still
  succeeds and only the version stays empty. The auto-open rule is checked from
  both sides: after a failed install the `<details>` carries `open` and
  `history_alert` is true, after a verified boot neither is. The bundle is deleted afterwards, `install.log` keeps
  rauc's output, a failing `rauc` ends in `error` with its last line and an
  `errors.log` entry, a second upload during an install returns `409`, and an
  image without the `rauc` binary reports the card as unavailable instead of
  offering a button that cannot work;

* the A/B layout and the data partition are covered by
  `tests/verify_ab_update.py` (`python3 tests/verify_ab_update.py`):
  `iptv-boot-slot` is executed for all five RAUC commands — a fresh boot
  reports slot A, `set-primary B` rewrites `root=PARTUUID=49505456-03` and
  `rauc.slot=B` in `cmdline.txt` while keeping every other option and exactly
  one `root=`, repeated switches do not accumulate tokens, slot states default
  to `good`, `bad` is recorded and read back per slot, and unknown commands,
  slots, states, a missing `slots.conf` or a missing `cmdline.txt` all exit
  non-zero instead of reporting success. `iptv-data-init` seeds the shipped
  profile once, creates the placeholder playlist and the log directory, and
  leaves an existing playlist, a modified profile and a newer rootfs' defaults
  alone on the next boot; it refuses to write into the rootfs when the data
  partition is not a mountpoint. `post-image.sh` ran end to end against a fake
  binaries tree with stubbed `genimage`/`mke2fs`/`rauc`: the generated genimage
  description has `disk-signature = 0x49505456`, the four partitions in the
  order boot/rootfs_a/rootfs_b/data, two 1100 MB slots that fit the 1024 MB
  rootfs image, the data size from
  `IPTVPI_DATA_SIZE`, and a `boot.vfat` file list that contains firmware,
  `zImage`, DTBs and our own `config.txt` but neither `rootfs.ext4` nor
  `data.ext4`; the staged `cmdline.txt` is exactly one line with
  `root=PARTUUID=49505456-02`, the original options and `rauc.slot=A`;
  `mke2fs -L iptv-data` is called; the bundle manifest contains
  `compatible=iptvpi-pi1`, the version, `format=plain` and
  `filename=rootfs.ext4`, and `rauc bundle` is invoked with the certificate and
  key. A rootfs larger than a slot fails the build with a message naming the
  sizes, and a missing key still produces the card image and only skips the
  bundle. Statically checked as well: `fstab` mounts `LABEL=iptv-data` on
  `/var/lib/iptv` with `noatime`, `/var/kodi` is the expected symlink, every
  unit that touches the data partition carries `RequiresMountsFor=/var/lib/iptv`,
  and both defconfigs enable `BR2_PACKAGE_RAUC` and
  `BR2_PACKAGE_RAUC_NETWORK`;

**Not** verified here: a complete image build and a boot on real hardware.
That requires a Linux build host with ~20 GB free and several hours (glibc
toolchain + kernel + ffmpeg + Kodi), which is outside what this environment
can do. The first build on your machine is therefore the first full
end-to-end test — if it fails, `build_os.sh` stops at the configuration check
with a precise message rather than producing a broken image.

---

## 12. Continuous integration

`.github/workflows/build-iptvpi.yml` runs three jobs:

| Job | When | What |
|---|---|---|
| `verify` | every push and pull request | Python 3.11: compiles the sources, `node --check` on the inline JavaScript, then both suites |
| `verify-py27` | every push and pull request | the same two suites in a real `python:2.7-slim` container with Flask 1.1.4 — the image runs Kodi 18, which brings Python 2.7, so `webui/app.py` is proven there instead of merely declared compatible |
| `rauc-key` | `workflow_dispatch` or a `v*` tag | restores the shared RAUC signing pair from the Actions cache (creating it once), prints its fingerprint and hands it to both build jobs, so every image and every release is signed with the same certificate |
| `build-images` | `workflow_dispatch` or a `v*` tag | matrix over `pi1`/`pi2`: frees runner disk, restores the `dl/` cache, runs `./build_os.sh <board>` and uploads `sdcard.img`, `iptvpi-*.raucb` and `iptvpi-*.raucb.sha256` as artifacts |

The download cache is keyed on the defconfigs, so a rebuild reuses the
Buildroot tarballs and only re-runs what actually changed. `verify` and
`verify-py27` run on every branch push, so a broken Python 2.7 path is caught
before an image is ever built.

Everything at once — Python 3 compile, the inline JavaScript, both suites,
shell syntax and the 2.7 job — is `./tests/run-checks.sh` (add `--no-py27` to
skip the container). It exits 0 only when nothing failed.

It runs automatically before every commit: the hook lives in the repository as
`.githooks/pre-commit`, so one `git config --local core.hooksPath .githooks`
per clone is enough and the hook cannot drift from the script it calls. The
hook checks the working tree, so commit whole files; `git commit --no-verify`
is the deliberate way past it, and `IPTV_SKIP_CHECKS=1` skips it while still
saying so. On a machine with neither `python2.7` nor a running `docker` the
hook sets `IPTV_ALLOW_SKIP` and the run ends with `EVERY AVAILABLE CHECK PASSED
(Python 2.7 skipped)` instead of blocking every commit; CI never sets that
variable, so a missing 2.7 runtime still fails there.

Run the 2.7 job alone with `./tests/run-py27.sh`: it uses the same image and the
same pins through Docker, and falls back to a local 2.7 with Flask. Order of
preference: Docker, then the interpreter built by `./tests/setup-py27.sh`, then
whatever `python2.7` is on PATH (the box itself looks like that, because Kodi 18
brings 2.7 along).

Without Docker and without a 2.7, run `./tests/setup-py27.sh` once: it unpacks
CPython 2.7.16 plus `libssl1.1`/`libffi6` from the Debian buster archive into
`~/.cache/iptvpi-py27` (override with `IPTV_PY27_PREFIX`), bootstraps pip and
installs exactly the pins below — no root, no `apt-get`, about 40 MB. It lives
outside `/tmp` and a plain `python2.7` wrapper is left behind, so the job works
again after a reboot without rebuilding anything. The script is idempotent:
with the interpreter in place it only reports the version.

`./tests/run-py27.sh` is also what the workflow step calls, so CI and a laptop
run one implementation and cannot drift apart.

The 2.7 job is not decoration: the same suites against CPython 2.7.16 with
Flask 1.1.4 found real 2.7-only bugs — `shutil.which` in the test harness
(Python 3.3+) and a bare `raise` in `webui/app.py` that re-raised the inner
`OSError` about a missing `.part` file instead of the actual download error
(a 404 was reported as "No such file or directory"). Both are fixed; the fix
is `except ... as failure` + `raise failure` in the four write helpers. The
bundled run also exposed a race in `_finish()` (status went to success before
the history entry was written), which is fixed as well.

---

## 13. Licences

The project files (scripts, defconfigs, web UI) are provided as-is, MIT
style. The resulting image contains GPL software (Kodi, Linux, Buildroot
components) and the proprietary Raspberry Pi VideoCore firmware, distributed
under Broadcom's licence. If you redistribute images, honour those licences.
