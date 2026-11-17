# IPTV-Pi — Zwischenstand

Stand: 16.11.2026 · Zweig `freebuff/cfc5ff8ea424e5ea8b07c7d5`

Kurzfassung: Web-UI, Update-Logik und die Robustheits-Schicht (Spool auf der
Datenpartition, `/boot` read-only, `panic=10`, Hardware-Watchdog) sind fertig und
automatisiert geprüft — **440 Assertions** (198 im UI-Test, 242 im A/B-Test),
0 Fehler. CI und Image-Build sind als GitHub-Actions-Workflow definiert. Offen
bleibt der Nachweis auf echter Hardware.

Kennzahlen: `webui/app.py` 3.327 Zeilen / 16 Routen ·
`webui/templates/index.html` 1.230 Zeilen · `webui/static/style.css` 430 Zeilen ·
19 systemd-Unit-Dateien + `/etc/systemd/system.conf` ·
`tests/verify_rauc_ui.py` 198 Assertions · `tests/verify_ab_update.py` 242
Assertions.

---

## 1. Funktionen (Web-UI und Appliance)

| Bereich | Stand |
|---|---|
| Kodi-Kiosk | Kodi 18.9 „Leia" auf tty1 (`--standalone -fs`), kein Desktop, kein Login; `Restart=always`, `Nice=-10`, `OOMScoreAdjust=-500` |
| Playlist | Upload, Einfügen als Text oder Abruf per URL (Box lädt selbst, 25 s, 32 MB), Gruppenfilter, EPG-URL, `playlist.m3u` zusätzlich unter `/playlist.m3u` |
| Automatik | `iptv-playlist-refresh.timer` alle 12 h, behält den Gruppenfilter, lädt Kodi neu |
| Betrieb | `/api/status`, `/api/health`, `/api/logs`, `/api/stats`, `/api/notify`, `/api/restart`, `/api/refresh` |
| Systemupdate | A/B-Karte: Slot-Anzeige aus `iptv-boot-slot`, Upload **oder** LAN-URL, optionaler SHA-256 (eingetippt oder automatisch aus `<bundle>.raucb.sha256`), Fortschritt für Download und Install, Auto-/manueller Reboot, Slot-Verifikation nach dem Boot, Historie (max. 5) mit Version, Herkunft, Hash + Quelle, Problem-Filter und rotem Problem-Zähler als Filter-Abkürzung |
| Historie | öffnet sich selbst bei `failed`/`warning` als neuestem Eintrag, Nutzer-Klick wird respektiert; `history.json` auf der Datenpartition, zwei Schreibvorgänge pro Update |
| Speicher-Disziplin | `TMPDIR=/var/lib/iptv/updates/tmp` (Datenpartition) statt `/tmp`; Body-Spooling über `tempfile`, Bundles in 64-KB-Chunks, `MemoryMax=96M` bleibt ausreichend für 350-MB-Bundles |
| Kompatibilität | `webui/app.py` und beide Testsuiten sind Python-2.7- und 3.x-tauglich (keine f-Strings, `%`-Formatierung, `except … as`, eigener HTTP-Server-Shim) |

**Belegt:**

* `tests/verify_rauc_ui.py` — **198 Assertions, 0 Fehler** (~60 s): echte
  `webui/app.py` gegen Stub-`rauc`, echten `iptv-boot-slot` und einen echten
  HTTP-Server. Neu: 34-MB-Upload (größer als die 32-MB-`/tmp`-tmpfs) installiert,
  `/tmp` wächst dabei um **0 Bytes** und bekommt keine neue Datei, der
  Spool-Ordner ist danach leer, `tempfile.tempdir` zeigt auf `TMPDIR`.
* `tests/verify_ab_update.py` — **242 Assertions, 0 Fehler**: Partitionierung,
  `cmdline.txt` inklusive `panic=10`, Daten-Partition, Bundle und `.sha256` aus
  einem echten `post-image.sh`-Lauf; neu geprüft werden `/boot` als `ro`-Mount,
  die Remount-/`sync`-/`trap`-Fenster in `iptv-boot-slot`, `iptv-wifi-import` und
  dem neuen `iptv-rauc-mark-good` (auch dessen Exit-Code-Weitergabe), der
  Watchdog im Kernelfragment und `RuntimeWatchdogSec=15`.
* `python3 -m py_compile` auf `app.py` und beiden Suiten, `node --check` auf dem
  Inline-JS, `sh -n`/`bash -n` auf allen Skripten.

**Python 2.7 belegt:** Mit einem echten CPython **2.7.16** (Debian-Buster-Pakete
aus dem Archiv) und dem CI-Pin **Flask 1.1.4 /
Werkzeug 1.0.1 / Jinja2 2.11.3** laufen beide Suiten grün: **242** Assertions
im A/B-Test, **198** im UI-Test, 0 Fehler — identische Zahlen wie unter 3.x
(mehrfach wiederholt, 3× grün in Folge nach den letzten Härtungen).
Dabei kamen die folgenden Punkte heraus, alle behoben:

1. `tests/verify_ab_update.py` nutzte `shutil.which` (erst ab 3.3) — ersetzt
   durch einen eigenen `which()`-Helfer.
2. Die Tests prüften "Install läuft noch" direkt nach dem Upload und rasten
   damit gegen den Worker-Thread (auf 2.7 sporadisch rot). Jetzt hält
   `hold_installer()` den Installer über ein `threading.Event` an, statt auf
   ein Timing zu hoffen; Progress wird gegen `install.log` geprüft, nicht
   gegen Stichproben.
3. `_finish()` setzte den Status auf `success`, **bevor** der
   Historieneintrag geschrieben war — ein Poll dazwischen sah "fertig" mit
   leerer Historie. Jetzt wird erst die Historie geschrieben, dann der Status
   gesetzt.
4. Die `/tmp`-Zusicherung verglich Bytes exakt und schlug an fremden
   Schreibern fehl (auf einem CI-Runner normal); sie prüft jetzt auf
   bundle-große Zuwächse.
5. `webui/app.py` verlor in `write_text`/`write_bundle` die ursprüngliche
   Ausnahme: ein nacktes `raise` nach einem inneren `except OSError` wirft in
   Python 2 die *innere* Ausnahme weiter. Ein 404-Download meldete deshalb
   „No such file or directory: …missing.raucb.part" statt „the server answered
   HTTP 404". Jetzt `except … as failure` + `raise failure` (vier Stellen).

Der 2.7-Interpreter dafür entsteht mit `./tests/setup-py27.sh` (einmalig, kein
Root): Buster-Pakete nach `${IPTV_PY27_PREFIX:-~/.cache/iptvpi-py27}`, pip und
die Pins obendrauf, plus `bin/python2.7`-Wrapper. Das liegt außerhalb von
`/tmp`, übersteht also einen Neustart; `tests/run-py27.sh` nimmt ihn vor einem
beliebigen `python2.7` aus dem PATH. Ein voller Lauf mit diesem Interpreter:
242 Assertions im A/B-Test, 198 im UI-Test, 0 Fehler.

Alle lokalen Prüfungen in einem Lauf: `./tests/run-checks.sh`
(`--no-py27` überspringt den Container). Der gleiche Lauf hängt als
`.githooks/pre-commit` vor jedem Commit; einmal pro Klon
`git config --local core.hooksPath .githooks`. `git commit --no-verify` bzw.
`IPTV_SKIP_CHECKS=1` sind die bewussten Ausnahmen. Ohne `python2.7` und ohne
laufenden Docker setzt der Hook `IPTV_ALLOW_SKIP=1`: der Lauf endet dann mit
`EVERY AVAILABLE CHECK PASSED (Python 2.7 skipped)` statt jeden Commit zu
blockieren. Die CI setzt die Variable nicht — dort fällt ein fehlender
2.7-Lauf weiterhin hart durch.
(`--no-py27` überspringt den Container) — Python-3-Kompilierung, Inline-JS,
beide Suiten, Shell-Syntax und der 2.7-Job; Exit 0 nur bei vollständig grün.

Reproduzierbar ist der 2.7-Teil über `./tests/run-py27.sh` — derselbe Aufruf, den der
Workflow verwendet: mit Docker läuft `python:2.7-slim` plus die Pins, ohne
Docker ein `python2.7` mit Flask auf dem PATH. Beide Zweige wurden hier
ausgeführt (Docker-Zweig mit gestubbtem `docker` für die Skriptprüfung, der
Fallback-Zweig echt gegen 2.7.16).

Der CI-Job `verify-py27` selbst ist auf GitHub noch nicht gelaufen. Kein
Klicktest im Browser; die UI-Prüfungen kommen aus HTTP-Aufrufen und dem
ausgelieferten Markup.

## 2. System (Image, Boot, Partitionen)

* **Basis:** Buildroot **2021.02.x** (getestet mit 2021.02.12); `build_os.sh`
  bricht bei anderen Serien ab und prüft `BR2_PACKAGE_KODI_PLATFORM_RBPI`.
* **Partitionen** (MBR, `disk-signature 0x49505456` → PARTUUIDs
  `49505456-01…-04`): p1 boot.vfat 64M · p2 rootfs_a 1100M · p3 rootfs_b
  1100M · p4 Daten (`LABEL=iptv-data`) 1024M; Rootfs exakt 1024M.
* **Boot:** `cmdline.txt` mit `root=PARTUUID=49505456-02 rauc.slot=A panic=10`;
  `iptv-boot-slot` schreibt Slot-Wechsel nach `cmdline.txt` und
  `/boot/rauc/state`, ergänzt `panic=10` falls es fehlt.
* **`/boot` read-only:** `fstab` mountet `ro,nofail`; `iptv-boot-slot`,
  `iptv-rauc-mark-good` und `iptv-wifi-import` öffnen ein kurzes
  `remount,rw`-Fenster, `sync`en und schließen es im `EXIT`-Trap wieder.
* **Updates:** RAUC 1.5.1 mit eigenem Bootloader-Backend, Zertifikat
  `/etc/rauc/ca.cert.pem`, `format=plain`, Bundle
  `iptvpi-<version>.raucb` **plus** `.sha256`; `iptv-rauc-mark-good.service`
  ruft den neuen Helper `/usr/bin/iptv-rauc-mark-good`. Das
  Signaturschlüsselpaar liegt außerhalb von `output/` (`IPTVPI_RAUC_KEYDIR`,
  Standard `~/.cache/iptvpi-rauc`; in CI der Actions-Cache) und wird von jedem
  Build wiederverwendet, damit Bundles aus späteren Releases auf bereits
  geflashten Boxen installierbar bleiben.
* **Datenhaltung:** Playlist, State, Logs, Update-Dateien, Spool-Verzeichnis und
  Kodi-Profil (`/var/kodi` → `/var/lib/iptv/kodi`) liegen auf p4 und überleben
  einen Slot-Tausch; Seeding einmalig via `iptv-data-init`.
* **SD-Schonung:** `noatime`, `/tmp` 32M und `/var/log` 16M tmpfs, journald
  `Storage=volatile` 8M, zram (lz4, Prio 100, `swappiness=100`), Historie max.
  5 Einträge, Verifikations-Urteil genau einmal geschrieben.
* **Zuverlässigkeit:** `panic=10` (Kernel-Panic → Reboot), Hardware-Watchdog
  `bcm2835_wdt` mit `RuntimeWatchdogSec=15` und `RebootWatchdogSec=10min` in
  `/etc/systemd/system.conf`.
* **Prioritäten:** Kodi `Nice=-10`/`OOMScoreAdjust=-500`; Web-UI `Nice=19`,
  I/O `idle`, `MemoryHigh=48M`, `MemoryMax=96M`.

**Nicht belegt:** kein vollständiger Image-Build mit dem aktuellen Baum (die
Defconfigs laden mit echtem Buildroot 2021.02.12, rc=0) und kein Boot auf
Hardware; der Watchdog ist konfiguriert, aber nicht auf einem Pi ausgelöst
worden.

## 3. Kernel

| Punkt | Wert |
|---|---|
| Version | Linux **5.10**, Raspberry-Pi-Foundation-Fork, Commit `967d45b29ca2902f031b867809d72e3b3d623e7a` |
| Defconfigs | `bcmrpi` (Pi 1, ARMv6) und `bcm2709` (Pi 2, Cortex-A7) |
| DTS | `bcm2708-rpi-b`, `-b-plus`, `-cm` (Pi 1); `bcm2709-rpi-2-b` (Pi 2) |
| Fragment | `CPU_FREQ` + `ondemand`/`performance`, `THERMAL`/`CPU_THERMAL`/`BCM2835_THERMAL`, jetzt zusätzlich `CONFIG_WATCHDOG=y`, `CONFIG_WATCHDOG_CORE=y`, `CONFIG_BCM2835_WDT=y` |
| Kommandozeile | `root=/dev/mmcblk0p2 rootwait rootfstype=ext4 console=ttyAMA0,115200 quiet loglevel=3 dwc_otg.lpm_enable=0 panic=10` (im Image: PARTUUID + `rauc.slot=A`) |
| `config.txt` | `gpu_mem` 100/128 (Pi 1) bzw. 128/192/256 (Pi 2); `arm_freq=900`/`600`, `over_voltage=2`; `h264_freq=400`/`250`; `sd_overclock=80`; `audio=on`; `hdmi_force_hotplug=1`, `disable_overscan=1` |
| Hardware-Decode | VideoCore IV über MMAL/OpenMAX und dispmanx (Kodi-18-RBPI-Plattform), Firmware `start.elf`/`fixup.dat`, kein `vc4-kms-v3d` |
| Treiber | VideoCore/VCHIQ, MMAL, `snd_bcm2835`, `brcmfmac`, USB, ext4/vfat, zram, Watchdog |

**Nicht belegt:** Kernel nicht gebaut/gebootet; Governor, Thermal-Zone und
Watchdog sind konfigurativ beschrieben, nicht auf dem Pi gemessen.

---

## 4. Continuous Integration

`.github/workflows/build-iptvpi.yml`:

| Job | Auslöser | Inhalt |
|---|---|---|
| `verify` | jeder Push/PR | Python 3.11: `py_compile`, `node --check`, beide Suiten |
| `verify-py27` | jeder Push/PR | dieselben Suiten in `python:2.7-slim` mit Flask 1.1.4 |
| `build-images` | `workflow_dispatch` oder Tag `v*` | Matrix `pi1`/`pi2`: Disk freiräumen, `dl/`-Cache, `./build_os.sh <board>`, Artefakte `sdcard.img`, `iptvpi-*.raucb`, `iptvpi-*.raucb.sha256` |

Der Workflow ist geschrieben, aber auf GitHub nie gelaufen (kein Repo/Remote,
kein `gh`-Login in dieser Umgebung). Sein `verify-py27`-Inhalt wurde lokal mit
CPython 2.7.16 + Flask 1.1.4 nachgestellt und ist dort grün.

## 5. Offene Punkte (Checkliste)

### Build und Boot

- [ ] Vollständiger Buildroot-Build mit dem aktuellen Baum für **pi1** und
      **pi2**, inklusive Bundle und `.sha256`
- [ ] `sdcard.img` flashen, Pi 1 booten: Kodi-Start, Senderliste, H.264, Ton
      über HDMI und Analog
- [ ] Dasselbe auf dem Pi 2
- [ ] Daten-Partition: Seeding beim ersten Boot, Kodi-Profil übersteht einen
      Slot-Tausch
- [ ] 256-MB-Pi 1 unter 1080p-Stream (OOM-Killer, zram, `kodi.service`)

### Robustheit auf Hardware

- [ ] `/boot` read-only: Slot-Wechsel, `mark-good` und WLAN-Import schreiben
      erfolgreich, `mount` zeigt danach wieder `ro`
- [ ] `panic=10`: Panic künstlich auslösen (`echo c > /proc/sysrq-trigger`),
      Reboot nach ~10 s prüfen
- [ ] Watchdog: `wdctl`/`systemctl show -p RuntimeWatchdogUSec`, dann einen
      Hänger erzwingen (`kill -STOP 1`) und den Reset abwarten

### Systemupdates

- [ ] Echtes RAUC mit dem gebauten Bundle: `rauc install`, Reboot in Slot B,
      `rauc status` gegen `iptv-boot-slot` vergleichen
- [ ] Rollback: unsigniertes/unpassendes Bundle, laufender Slot bleibt intakt
- [ ] Verifikation auf Hardware: `verified`, `warning` bei nicht erfolgtem Switch
- [ ] Prüfsumme über HTTPS (Sidecar-Abruf bisher nur über HTTP ausgeführt)

### Web-UI

- [ ] Bedienung im Browser: Upload, URL-Abruf, Prüfsummenfeld, Filter,
      Problem-Zähler, Auf-/Zuklappen der Historie
- [x] Python 2.7 lokal mit echtem 2.7.16 + Flask 1.1.4: beide Suiten grün
- [ ] CI-Job `verify-py27` einmal auf GitHub grün sehen
- [ ] Paralleler Upload gegen die Ein-Install-Regel

### Betrieb und Pflege

- [ ] SD-Schreiblast über 24 h messen
- [ ] WLAN-Import und Netzwerk nach Neustart
- [ ] `README.md`/`STATUS.md` nach den Hardwaretests nachziehen
- [ ] Ersten Commit auf dem Branch anlegen (folgt in diesem Arbeitsschritt)

### Bekannte Grenzen der bisherigen Nachweise

* Live-Nachweise liefen gegen eine Demo-Instanz mit Stub-`rauc`,
  Stub-`systemctl` und simulierter Boot-ID — kein echtes RAUC.
* Python 2.7 lag nur als nach `/tmp` entpacktes Debian-Paket vor, nicht als
  Teil des Repos; der CI-Container ist der reproduzierbare Weg.
* Keine Messung von Bootzeit, Wiedergabequalität, Temperaturen oder
  Speicherverbrauch auf Hardware; keine Messung der SD-Schreiblast.
* CI-Workflow und Image-Build-Job sind definiert, aber noch nie gelaufen.
