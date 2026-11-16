#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""IPTV-Pi playlist manager and hardware dashboard.

Small Flask app that runs on the appliance (systemd unit iptv-webui.service)
behind port 80. It lets anyone on the LAN:

  * replace the M3U playlist Kodi reads (upload, paste or remote URL),
  * keep only the channel groups they care about (low-RAM friendly),
  * configure the XMLTV EPG URL for pvr.iptvsimple,
  * re-fetch the playlist from its URL on a schedule (systemd timer, run with
    --refresh; it keeps the group filter the user chose),
  * ask Kodi to reload the playlist through JSON-RPC (falling back to a full
    service restart),
  * watch live CPU/GPU temperature, clock and memory telemetry.

Deliberately tiny: no database, no sessions, no authentication.

Compatibility note: this file must run on Python 2.7 *and* Python 3, because
Buildroot installs its Flask package for the image's default interpreter and
Kodi 18 forces Python 2 into the image.
"""

from __future__ import print_function

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import traceback
from xml.sax.saxutils import escape as xml_escape

from flask import Flask, jsonify, render_template, request

try:  # Python 3
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen
except ImportError:  # Python 2
    from urllib2 import HTTPError, URLError, Request, urlopen


# Python 2 needs explicit handling here: json.loads() and the WSGI layer hand
# back unicode, and str(u'\u041d\u043e\u0432\u043e\u0441\u0442\u0438') raises UnicodeEncodeError. Group names in
# IPTV playlists are frequently non-ASCII, so normalise everything to text.
if sys.version_info[0] == 2:  # noqa: SIM108  (kept explicit for Python 2)
    string_types = (str, unicode)  # noqa: F821

    def to_text(value):
        if isinstance(value, unicode):  # noqa: F821
            return value
        if isinstance(value, str):
            return value.decode("utf-8", "replace")
        return unicode(value)  # noqa: F821
else:
    string_types = (str,)

    def to_text(value):
        return value if isinstance(value, str) else str(value)


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

IPTV_DIR = os.environ.get("IPTV_DIR", "/var/lib/iptv")
PLAYLIST = os.environ.get("IPTV_PLAYLIST", os.path.join(IPTV_DIR, "playlist.m3u"))
FULL_PLAYLIST = os.environ.get(
    "IPTV_FULL_PLAYLIST", os.path.join(IPTV_DIR, "playlist.full.m3u"))
STATE_FILE = os.environ.get("IPTV_STATE_FILE", os.path.join(IPTV_DIR, "state.json"))
ADDON_SETTINGS = os.environ.get(
    "IPTV_IPTVSIMPLE_SETTINGS",
    "/var/kodi/userdata/addon_data/pvr.iptvsimple/settings.xml")

KODI_UNIT = os.environ.get("IPTV_KODI_UNIT", "kodi.service")
KODI_RPC = os.environ.get("IPTV_KODI_RPC", "http://127.0.0.1:8080/jsonrpc")
REFRESH_TIMER = os.environ.get("IPTV_REFRESH_TIMER",
                               "iptv-playlist-refresh.timer")
ADDON = "pvr.iptvsimple"
VCGENCMD = os.environ.get("IPTV_VCGENCMD", "/usr/bin/vcgencmd")

MAX_BYTES = int(os.environ.get("IPTV_MAX_BYTES", str(32 * 1024 * 1024)))
BIND_ADDR = os.environ.get("IPTV_WEBUI_BIND", "0.0.0.0")
BIND_PORT = int(os.environ.get("IPTV_WEBUI_PORT", "80"))
FETCH_TIMEOUT = float(os.environ.get("IPTV_FETCH_TIMEOUT", "25"))
OFFLINE = os.environ.get("IPTV_NO_SYSTEMD") == "1"
USER_AGENT = "IPTV-Pi/1.0"

# How many failed refreshes in a row before the dashboard warns. A single
# failure can be a provider hiccup; two in a row on a 12-hour timer means an
# outage nobody would otherwise see for a day.
REFRESH_FAILURE_LIMIT = int(os.environ.get("IPTV_FAILURE_LIMIT", "2"))

# How long an alert POST may take before it is given up on. The refresh itself
# must never wait on a notification service for long.
NOTIFY_TIMEOUT = float(os.environ.get("IPTV_NOTIFY_TIMEOUT", "10"))

APP_VERSION = "1.0.0"
WEBUI_UNIT = os.environ.get("IPTV_WEBUI_UNIT", "iptv-webui.service")

# Updates are unpacked here rather than over /usr/share, so the shipped image
# stays untouched and "undo" is one rename (app.bak). launch.sh prefers this
# directory when it exists.
APP_DIR = os.environ.get("IPTV_APP_DIR", os.path.join(IPTV_DIR, "app"))
APP_BACKUP = APP_DIR + ".bak"
UPDATE_MAX_BYTES = int(os.environ.get("IPTV_UPDATE_MAX_BYTES", str(4 * 1024 * 1024)))
UPDATE_RESTART_DELAY = float(os.environ.get("IPTV_UPDATE_RESTART_DELAY", "1.5"))
# Only these paths may appear in an update bundle.
UPDATE_FILE_RE = re.compile(
    r"^(app\.py|launch\.sh|(templates|static)/[A-Za-z0-9._/-]+)$")

# Logs live in the data directory, one rotated file each, so they cannot grow
# without bound on an SD card.
LOG_DIR = os.environ.get("IPTV_LOG_DIR", os.path.join(IPTV_DIR, "log"))
ERROR_LOG = os.path.join(LOG_DIR, "errors.log")
HARDWARE_LOG = os.path.join(LOG_DIR, "hardware.log")
LOG_MAX_BYTES = int(os.environ.get("IPTV_LOG_MAX_BYTES", str(64 * 1024)))

# System health: the watchdog timer runs --health every 10 minutes, the page
# reuses a sample for a short while instead of shelling out on every request.
HEALTH_TIMER = os.environ.get("IPTV_HEALTH_TIMER", "iptv-health-watch.timer")
HEALTH_CACHE_TTL = float(os.environ.get("IPTV_HEALTH_CACHE_TTL", "30"))
# One alert per unit per window, so a crash loop cannot flood the webhook.
UNIT_ALERT_COOLDOWN = float(os.environ.get(
    "IPTV_UNIT_ALERT_COOLDOWN", str(6 * 3600)))

# A/B system updates through RAUC. The page never parses `rauc status` text:
# the slot it shows comes from the same helper RAUC's bootloader backend uses,
# so the dashboard and the bootloader cannot disagree.
RAUC = os.environ.get("IPTV_RAUC", "/usr/bin/rauc")
RAUC_BOOT_SLOT = os.environ.get("IPTV_RAUC_BOOT_SLOT", "/usr/bin/iptv-boot-slot")
RAUC_SYSTEM_CONF = os.environ.get("IPTV_RAUC_CONF", "/etc/rauc/system.conf")
RAUC_BUNDLE_DIR = os.environ.get("IPTV_RAUC_DIR", os.path.join(IPTV_DIR, "updates"))
# A bundle is the compressed rootfs image: a few hundred MB, not a few KB.
RAUC_MAX_BYTES = int(os.environ.get("IPTV_RAUC_MAX_BYTES",
                                    str(512 * 1024 * 1024)))
RAUC_INSTALL_TIMEOUT = float(os.environ.get("IPTV_RAUC_TIMEOUT", str(30 * 60)))
RAUC_FREE_HEADROOM = int(os.environ.get("IPTV_RAUC_HEADROOM",
                                        str(32 * 1024 * 1024)))
# An update only proves itself after the reboot, so the slot that was running
# when it started is recorded (with the boot id), and the boot that comes back
# is compared against it. RAUC_REBOOT_DELAY is the grace period between the
# finished install and `systemctl reboot`, so the browser still sees the result.
RAUC_REBOOT_DELAY = float(os.environ.get("IPTV_RAUC_REBOOT_DELAY", "5"))
# The manual button only has to get its answer out, so it may restart sooner.
RAUC_MANUAL_REBOOT_DELAY = float(os.environ.get("IPTV_RAUC_MANUAL_DELAY", "2"))
RAUC_PENDING_FILE = os.environ.get(
    "IPTV_RAUC_PENDING", os.path.join(RAUC_BUNDLE_DIR, "pending-update.json"))
# The card shows the last few updates; the file is rewritten once per update
# and once per verified boot, so its size and its write count stay tiny.
RAUC_HISTORY_FILE = os.environ.get(
    "IPTV_RAUC_HISTORY", os.path.join(RAUC_BUNDLE_DIR, "history.json"))
RAUC_HISTORY_LIMIT = max(1, int(os.environ.get("IPTV_RAUC_HISTORY_LIMIT", "5")))
BOOT_ID_FILE = os.environ.get("IPTV_BOOT_ID_FILE",
                              "/proc/sys/kernel/random/boot_id")
RAUC_PROGRESS_RE = re.compile(r"(\d{1,3})\s*%")
# SHA-256 as printed by sha256sum (optionally with the "sha256:" prefix used by
# container tooling), so a checksum can be pasted straight from the build host.
SHA256_RE = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})$")
# Any SHA-256 inside a checksum file (sha256sum prints "<hash>  <name>").
SHA256_ANY_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")
RAUC_STEPS = (
    ("check", "checking the bundle"),
    ("verif", "verifying the image"),
    ("install", "writing the inactive slot"),
    ("copy", "writing the inactive slot"),
    ("mark", "switching the primary slot"),
    ("done", "done"),
)

UNGROUPED = "Ungrouped"
GROUP_RE = re.compile(r"""group-title\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)

def webui_temp_dir():
    """Spool directory for request bodies: TMPDIR, else the data partition.

    Werkzeug spools uploads into temporary files. /tmp on the box is a 32 MB
    tmpfs and a real rootfs bundle is 150-350 MB, so it must not go there; the
    unit sets TMPDIR=/var/lib/iptv/updates/tmp (launch.sh creates and cleans
    it). Without TMPDIR the data partition is the fallback, and only a box
    without a data partition falls back to the system temp directory.
    """
    candidates = [os.environ.get("TMPDIR", "").strip(), IPTV_DIR]
    for path in candidates:
        if not path:
            continue
        try:
            if not os.path.isdir(path):
                os.makedirs(path)
            return path
        except (IOError, OSError):
            continue
    return tempfile.gettempdir()


app = Flask(__name__)
# The hard limit is the larger of the two; each endpoint still enforces its own
# cap while reading (a playlist is 32 MB, a RAUC bundle a few hundred MB).
app.config["MAX_CONTENT_LENGTH"] = max(MAX_BYTES, RAUC_MAX_BYTES)

# Werkzeug spools uploads into temporary files: keep them on the data
# partition (TMPDIR), never in the 32 MB /tmp tmpfs.
tempfile.tempdir = webui_temp_dir()


# --------------------------------------------------------------------------
# generic helpers
# --------------------------------------------------------------------------

def read_text(path, limit=8192):
    """Read a small text file, returning None when it is not readable."""
    try:
        handle = io.open(path, "r")
        try:
            return handle.read(limit)
        finally:
            handle.close()
    except (IOError, OSError):
        return None


def write_atomic(path, data, mode=0o644):
    """Write bytes atomically: temp file in the same directory + rename."""
    directory = os.path.dirname(path) or "."
    if not os.path.isdir(directory):
        os.makedirs(directory)
    handle_fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        handle = os.fdopen(handle_fd, "wb")
        try:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.chmod(tmp_path, mode)
        os.rename(tmp_path, path)
    except Exception as failure:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        # `raise failure`, not a bare `raise`: in Python 2 a bare raise after an
        # inner except re-raises the *inner* exception (here the OSError of a
        # missing temp file), which would hide the real error from the caller.
        raise failure


def ensure_dir(path):
    try:
        if path and not os.path.isdir(path):
            os.makedirs(path)
    except OSError:
        pass


def timestamp():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def rotate_log(path):
    """Keep one previous file, so each log stays below 2 x LOG_MAX_BYTES."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size < LOG_MAX_BYTES:
        return
    try:
        backup = path + ".1"
        if os.path.exists(backup):
            os.unlink(backup)
        os.rename(path, backup)
    except OSError:
        pass


def append_log(path, line):
    """Append one line, rotating first. Logging must never raise."""
    try:
        ensure_dir(os.path.dirname(path))
        rotate_log(path)
        data = to_text(line).rstrip("\n") + "\n"
        if not isinstance(data, bytes):
            data = data.encode("utf-8", "replace")
        handle = open(path, "ab")
        try:
            handle.write(data)
        finally:
            handle.close()
        return True
    except (IOError, OSError):
        return False


def log_error(scope, message, detail=None):
    """Record a failure for the dashboard and for later debugging."""
    text = "[%s] %s" % (scope, message)
    if detail:
        text = "%s: %s" % (text, detail)
    return append_log(ERROR_LOG, "%s %s" % (timestamp(), text))


def _num(value):
    return "n/a" if value is None else value


def hardware_log_line(stats=None):
    """One compact line: enough for a trend, cheap to write to an SD card."""
    stats = stats or hardware_stats()
    zram = stats.get("zram") or {}
    total = stats.get("swap_total_mb")
    free = stats.get("swap_free_mb")
    swap_used = None
    if isinstance(total, (int, float)) and isinstance(free, (int, float)):
        swap_used = round(total - free, 1)
    load = stats.get("load") or [None]
    return ("cpu=%sC gpu=%sC arm=%sMHz h264=%sMHz mem=%s/%sMB "
            "swap=%s/%sMB zram=%sMB load=%s gov=%s uptime=%ss"
            % (_num(stats.get("cpu_temp_c")), _num(stats.get("gpu_temp_c")),
               _num(stats.get("arm_mhz")), _num(stats.get("h264_mhz")),
               _num(stats.get("mem_available_mb")),
               _num(stats.get("mem_total_mb")), _num(swap_used),
               _num(total), _num(zram.get("used_mb")), _num(load[0]),
               _num(stats.get("governor")), _num(stats.get("uptime_s"))))


def sample_hardware():
    """Write one hardware sample; returns (written, line)."""
    line = hardware_log_line()
    return append_log(HARDWARE_LOG, "%s %s" % (timestamp(), line)), line


def tail_lines(path, count=40, window=16384):
    """Last `count` non-empty lines, read from the tail of the file only."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    try:
        handle = open(path, "rb")
    except (IOError, OSError):
        return []
    try:
        handle.seek(max(0, size - window))
        data = handle.read()
    except (IOError, OSError):
        return []
    finally:
        handle.close()
    text = data.decode("utf-8", "replace")
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-count:]


def log_size_kb(path):
    try:
        return round(float(os.path.getsize(path)) / 1024.0, 1)
    except OSError:
        return 0.0


def log_status(lines=40):
    """Recent errors and hardware samples, for the page and /api/logs."""
    return {
        "dir": LOG_DIR,
        "max_kb": LOG_MAX_BYTES // 1024,
        "errors": {"path": ERROR_LOG, "size_kb": log_size_kb(ERROR_LOG),
                   "lines": tail_lines(ERROR_LOG, lines)},
        "hardware": {"path": HARDWARE_LOG, "size_kb": log_size_kb(HARDWARE_LOG),
                     "lines": tail_lines(HARDWARE_LOG, lines)},
    }


def run_cmd(argv, timeout=60.0):
    """Run a command and return (returncode, output).

    Poll-based so it behaves identically on Python 2 and 3 (no
    communicate(timeout=...)). Every command used here prints very little.
    """
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as exc:
        return 127, str(exc)

    deadline = time.time() + timeout
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.1)
    if proc.poll() is None:
        proc.kill()
        proc.wait()
        return 124, "command timed out: %s" % " ".join(argv)

    output = b""
    if proc.stdout is not None:
        output = proc.stdout.read() or b""
    return proc.returncode, output.decode("utf-8", "replace").strip()


# --------------------------------------------------------------------------
# M3U parsing, grouping and filtering
# --------------------------------------------------------------------------

def extract_group(extinf_line):
    match = GROUP_RE.search(extinf_line)
    if not match:
        return UNGROUPED
    value = match.group(1) if match.group(1) is not None else match.group(2)
    value = (value or "").strip()
    return value or UNGROUPED


def iter_lines(handle):
    """Yield decoded, stripped, non-empty lines from a binary stream.

    A file object iterates one line at a time, so a 50 MB playlist costs one
    line of memory instead of 50 MB. That is what keeps the web UI inside a
    256 MB Pi's RAM budget.
    """
    for raw in handle:
        if not isinstance(raw, bytes):
            raw = raw.encode("utf-8", "replace")
        line = raw.decode("utf-8", "replace").strip()
        if line:
            yield line


def iter_entries(path):
    """Stream a playlist as {"group", "lines"} entries, one at a time.

    Nothing but the current entry is ever held in memory, so filtering a 50 MB
    playlist is as cheap as filtering a 5 KB one.
    """
    seen_header = False
    current = None
    handle = io.open(path, "rb")
    try:
        for line in iter_lines(handle):
            upper = line.upper()
            if not seen_header and upper.startswith("#EXTM3U"):
                seen_header = True
                continue
            if upper.startswith("#EXTINF"):
                if current is not None:
                    yield current
                current = {"group": extract_group(line), "lines": [line]}
                continue
            if current is None:
                if line.startswith("#"):
                    continue
                # A bare stream URL without an #EXTINF header.
                yield {"group": UNGROUPED, "lines": [line]}
                continue
            current["lines"].append(line)
            if not line.startswith("#"):
                yield current
                current = None
        if current is not None:
            yield current
    finally:
        # Close the file even if the consumer stopped iterating early.
        handle.close()


def scan_playlist(path):
    """One streaming pass over a playlist file.

    Returns the header line, the line/channel counts and the ordered group
    list, without ever materialising the playlist in memory.
    """
    header = None
    counts = {}
    order = []
    channels = 0
    lines = 0
    handle = io.open(path, "rb")
    try:
        for line in iter_lines(handle):
            lines += 1
            upper = line.upper()
            if header is None and upper.startswith("#EXTM3U"):
                header = line
                continue
            if not upper.startswith("#EXTINF"):
                continue
            channels += 1
            group = extract_group(line)
            if group not in counts:
                counts[group] = 0
                order.append(group)
            counts[group] += 1
    finally:
        handle.close()

    named = sorted([name for name in order if name != UNGROUPED],
                   key=lambda name: name.lower())
    if UNGROUPED in counts:
        named.append(UNGROUPED)
    return {
        "header": header or "#EXTM3U",
        "lines": lines,
        "channels": channels,
        "groups": [{"name": name, "channels": counts[name]}
                   for name in named],
    }


def validate_playlist(path):
    """Check an M3U file by looking at its first lines only."""
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return False, "the playlist is empty"

    inspected = 0
    handle = io.open(path, "rb")
    try:
        for line in iter_lines(handle):
            upper = line.upper()
            if upper.startswith("#EXTM3U") or upper.startswith("#EXTINF"):
                return True, ""
            if not line.startswith("#") and not line.startswith("//"):
                scheme = line.split(":", 1)[0].lower()
                if scheme in ("http", "https", "rtmp", "rtsp", "udp", "rtp",
                              "file"):
                    return True, ""
                if line.startswith("/"):
                    return True, ""
            inspected += 1
            if inspected > 200:
                break
    finally:
        handle.close()
    return False, "no M3U content found (expected #EXTM3U/#EXTINF or stream URLs)"


def write_filtered(source, target, header, selected):
    """Stream `source` into `target`, keeping only the selected groups.

    The output is built in a temp file that is fsynced and renamed, so Kodi
    never reads a half-written playlist and a 50 MB input stays at a few
    kilobytes of resident memory. Returns the number of channels written.
    """
    keep = set(selected)
    directory = os.path.dirname(target) or "."
    if not os.path.isdir(directory):
        os.makedirs(directory)

    handle_fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    channels = 0
    try:
        out = os.fdopen(handle_fd, "wb")
        try:
            out.write((header + "\n").encode("utf-8"))
            for entry in iter_entries(source):
                if entry["group"] not in keep:
                    continue
                for line in entry["lines"]:
                    out.write((line + "\n").encode("utf-8"))
                channels += 1
            out.flush()
            os.fsync(out.fileno())
        finally:
            out.close()
        os.chmod(tmp_path, 0o644)
        os.rename(tmp_path, target)
    except Exception as failure:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        # `raise failure`, not a bare `raise`: in Python 2 a bare raise after an
        # inner except re-raises the *inner* exception (here the OSError of a
        # missing temp file), which would hide the real error from the caller.
        raise failure
    return channels


def read_chunks(handle, size=65536):
    """Yield byte chunks from a file-like object."""
    while True:
        chunk = handle.read(size)
        if not chunk:
            return
        yield chunk


def text_chunks(text, size=65536):
    """Yield a unicode/str blob as encoded chunks (no second full copy)."""
    for index in range(0, len(text), size):
        yield text[index:index + size].encode("utf-8")


def file_chunks(path, size=65536):
    """Yield a stored file to a WSGI response without buffering it."""
    handle = io.open(path, "rb")
    try:
        for chunk in read_chunks(handle, size):
            yield chunk
    finally:
        handle.close()


# 2**256: the modulus of the order-independent playlist fingerprint below.
FINGERPRINT_MODULUS = 1 << 256


def playlist_fingerprint(path):
    """Order-independent fingerprint of a playlist's channels.

    Providers reshuffle their lists and rewrite the #EXTM3U header constantly,
    so neither counts as a change here. What does count: every entry (#EXTINF
    plus its URL and any #KODIPROP/#EXTVLCOPT extras) is hashed on its own and
    the digests are summed modulo 2**256. Summing is commutative (blind to
    order) but, unlike XOR, does not cancel duplicated entries, and it needs
    constant memory however big the playlist is.

    Returns (entry_count, digest_sum).
    """
    total = 0
    entries = 0
    for entry in iter_entries(path):
        blob = "\n".join(line.strip() for line in entry["lines"])
        total += int(hashlib.sha256(blob.encode("utf-8")).hexdigest(), 16)
        total %= FINGERPRINT_MODULUS
        entries += 1
    return entries, total


def spool_chunks(chunks, limit):
    """Write incoming data to a temp file inside IPTV_DIR, chunk by chunk.

    Uploads, pasted text and remote downloads all pass through here, so the
    playlist never has to be held in RAM and a runaway source is stopped at
    `limit` bytes. Returns (path, size); the caller owns and deletes the file.
    """
    if not os.path.isdir(IPTV_DIR):
        os.makedirs(IPTV_DIR)
    handle_fd, tmp_path = tempfile.mkstemp(prefix=".incoming-", dir=IPTV_DIR)
    total = 0
    try:
        out = os.fdopen(handle_fd, "wb")
        try:
            for chunk in chunks:
                total += len(chunk)
                if total > limit:
                    raise ValueError(
                        "playlist is larger than the %d byte limit" % limit)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        finally:
            out.close()
        os.chmod(tmp_path, 0o644)
    except Exception as failure:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        # `raise failure`, not a bare `raise`: in Python 2 a bare raise after an
        # inner except re-raises the *inner* exception (here the OSError of a
        # missing temp file), which would hide the real error from the caller.
        raise failure
    return tmp_path, total


# --------------------------------------------------------------------------
# playlist / state persistence
# --------------------------------------------------------------------------

def load_state():
    state = {"selected_groups": None, "epg_url": "", "source_url": "",
             "last_refresh": None, "http_cache": {}, "failures": 0,
             "notify_url": "", "last_alert": None, "health": None,
             "unit_alerts": {}}
    text = read_text(STATE_FILE)
    if text:
        try:
            loaded = json.loads(text)
        except ValueError:
            loaded = None
        if isinstance(loaded, dict):
            groups = loaded.get("selected_groups")
            if isinstance(groups, list):
                state["selected_groups"] = [to_text(g) for g in groups]
            epg = loaded.get("epg_url")
            if isinstance(epg, string_types):
                state["epg_url"] = to_text(epg)
            source = loaded.get("source_url")
            if isinstance(source, string_types):
                state["source_url"] = to_text(source)
            cache = loaded.get("http_cache")
            if isinstance(cache, dict):
                parsed = {}
                for key, value in cache.items():
                    if not isinstance(key, string_types) or not isinstance(value, dict):
                        continue
                    parser = {"etag": "", "last_modified": ""}
                    for field in ("etag", "last_modified"):
                        raw = value.get(field)
                        if isinstance(raw, string_types):
                            parser[field] = to_text(raw)
                    parsed[to_text(key)] = parser
                state["http_cache"] = parsed
            last = loaded.get("last_refresh")
            if isinstance(last, dict):
                at = last.get("at")
                message = last.get("message")
                if isinstance(at, string_types):
                    http_status = last.get("http_status")
                    if not isinstance(http_status, int) or isinstance(http_status, bool):
                        http_status = None
                    state["last_refresh"] = {
                        "at": to_text(at),
                        "ok": bool(last.get("ok")),
                        "message": to_text(message or ""),
                        "http_status": http_status,
                    }
            failures = loaded.get("failures")
            if (isinstance(failures, int) and not isinstance(failures, bool)
                    and failures >= 0):
                state["failures"] = failures
            notify = loaded.get("notify_url")
            if isinstance(notify, string_types):
                state["notify_url"] = to_text(notify)
            health = loaded.get("health")
            if isinstance(health, dict) and isinstance(health.get("signature"),
                                                      string_types):
                state["health"] = {
                    "at": to_text(health.get("at") or ""),
                    "boot_id": to_text(health.get("boot_id") or ""),
                    "signature": to_text(health.get("signature")),
                }
            unit_alerts = loaded.get("unit_alerts")
            if isinstance(unit_alerts, dict):
                parsed_alerts = {}
                for key, value in unit_alerts.items():
                    if (isinstance(key, string_types)
                            and isinstance(value, (int, float))
                            and not isinstance(value, bool)):
                        parsed_alerts[to_text(key)] = float(value)
                state["unit_alerts"] = parsed_alerts
            alert = loaded.get("last_alert")
            if isinstance(alert, dict):
                sent_at = alert.get("at")
                if isinstance(sent_at, string_types):
                    state["last_alert"] = {
                        "at": to_text(sent_at),
                        "ok": bool(alert.get("ok")),
                        "message": to_text(alert.get("message") or ""),
                    }
    return state


def save_state(state):
    # Invariant: at most one HTTP validator entry, and only for the current
    # playlist URL. A box that has been pointed at several URLs would
    # otherwise keep collecting them, and a stale validator can only produce
    # wrong 304 answers. Enforcing it here means no caller can forget.
    cache = state.get("http_cache")
    if not isinstance(cache, dict):
        cache = {}
    keep = state.get("source_url") or ""
    if keep and keep in cache:
        state["http_cache"] = {keep: cache[keep]}
    else:
        state["http_cache"] = {}

    payload = json.dumps(state, sort_keys=True, indent=2, ensure_ascii=False)
    write_atomic(STATE_FILE, payload.encode("utf-8"))


def stored_validators(state, url):
    """HTTP validators remembered for a playlist URL (may be empty)."""
    entry = state.get("http_cache", {}).get(url)
    if not isinstance(entry, dict):
        return {}
    return {"etag": entry.get("etag") or None,
            "last_modified": entry.get("last_modified") or None}


def remember_validators(url, meta):
    """Remember the ETag/Last-Modified of the last successful download.

    Servers that send neither (or a 304 without validators) get their entry
    dropped, so the next request is a plain unconditional GET instead of
    carrying a validator the provider has forgotten about.
    """
    etag = (meta or {}).get("etag") or ""
    modified = (meta or {}).get("last_modified") or ""
    state = load_state()
    if etag or modified:
        state["http_cache"] = {
            url: {"etag": to_text(etag), "last_modified": to_text(modified)}}
    else:
        state["http_cache"] = {}
    save_state(state)


def set_source_url(url):
    """Remember (or forget) the remote playlist URL for the timer refresh.

    Saving from a URL arms the scheduled refresh; saving a file or pasted text
    disarms it, so the timer never overwrites a hand-made playlist.
    """
    state = load_state()
    new_source = to_text(url or "").strip()
    if new_source != state["source_url"]:
        # A different provider deserves a clean slate: the old failure streak
        # says nothing about the new URL.
        state["failures"] = 0
    state["source_url"] = new_source
    save_state(state)


def active_source():
    """The playlist to read groups from: the full one, else the active one."""
    if os.path.isfile(FULL_PLAYLIST):
        return FULL_PLAYLIST
    if os.path.isfile(PLAYLIST):
        return PLAYLIST
    return None


def active_state_groups():
    """(state, groups, selected) for the dashboard and the JSON API."""
    state = load_state()
    groups = []
    source = active_source()
    if source is not None:
        try:
            groups = scan_playlist(source)["groups"]
        except (IOError, OSError):
            groups = []
    selected = state["selected_groups"]
    if selected is None:
        selected = [group["name"] for group in groups]
    return state, groups, selected


def playlist_stats():
    stats = {
        "path": PLAYLIST,
        "full_path": FULL_PLAYLIST,
        "exists": os.path.isfile(PLAYLIST),
        "bytes": 0,
        "lines": 0,
        "channels": 0,
        "groups": 0,
        "modified": None,
        "backup": os.path.exists(PLAYLIST + ".bak"),
        "kodi_unit": KODI_UNIT,
    }
    if not stats["exists"]:
        return stats
    try:
        stats["bytes"] = os.path.getsize(PLAYLIST)
        stats["modified"] = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(PLAYLIST)))
        scan = scan_playlist(PLAYLIST)
    except (IOError, OSError):
        return stats
    stats["lines"] = scan["lines"]
    stats["channels"] = scan["channels"]
    stats["groups"] = len(scan["groups"])
    return stats


def store_playlist_from_file(source_path, keep_selection=False):
    """Adopt a spooled playlist file as the new full playlist.

    Everything here streams from disk: validation, group counting and the
    filtered write. The temp file is moved (not copied) into place.

    With keep_selection=True the group filter the user picked is kept; this is
    what the scheduled refresh uses. Groups that vanished from the provider's
    list are dropped, and if none of the old groups survive (the provider
    renamed everything) the whole playlist is activated again rather than
    leaving Kodi with an empty list.
    """
    try:
        size = os.path.getsize(source_path)
    except (IOError, OSError) as exc:
        raise ValueError("could not read the uploaded playlist: %s" % exc)
    if size > MAX_BYTES:
        # Defence in depth: spool_chunks already enforces this, but a direct
        # file must never slip past the RAM/disk budget either.
        raise ValueError("playlist is larger than the %d byte limit" % MAX_BYTES)

    ok, reason = validate_playlist(source_path)
    if not ok:
        raise ValueError(reason)

    scan = scan_playlist(source_path)
    if not scan["channels"]:
        raise ValueError("no channels found in the playlist")

    if os.path.isfile(FULL_PLAYLIST):
        shutil.copy2(FULL_PLAYLIST, FULL_PLAYLIST + ".bak")
    if os.path.isfile(PLAYLIST):
        shutil.copy2(PLAYLIST, PLAYLIST + ".bak")

    if os.path.abspath(source_path) != os.path.abspath(FULL_PLAYLIST):
        shutil.move(source_path, FULL_PLAYLIST)

    groups = scan["groups"]
    available = [group["name"] for group in groups]
    selected = available
    if keep_selection:
        previous = load_state()["selected_groups"]
        if previous:
            wanted = set(previous)
            kept = [name for name in available if name in wanted]
            if kept:
                selected = kept

    channels = write_filtered(FULL_PLAYLIST, PLAYLIST, scan["header"],
                              selected)

    state = load_state()
    state["selected_groups"] = selected
    save_state(state)
    return groups, selected, channels


def apply_groups(selected_names):
    source = active_source()
    if source is None:
        raise ValueError("no playlist stored yet")

    scan = scan_playlist(source)
    if not scan["channels"]:
        raise ValueError("the stored playlist has no channels")

    available = set(group["name"] for group in scan["groups"])
    chosen = [name for name in selected_names if name in available]

    if os.path.isfile(PLAYLIST):
        shutil.copy2(PLAYLIST, PLAYLIST + ".bak")
    channels = write_filtered(source, PLAYLIST, scan["header"], chosen)

    state = load_state()
    state["selected_groups"] = chosen
    save_state(state)
    return scan["groups"], chosen, channels


def set_epg(url):
    """Store the XMLTV URL and push it into pvr.iptvsimple."""
    url = to_text(url or "").strip()
    if url and not (url.lower().startswith("http://")
                    or url.lower().startswith("https://")):
        raise ValueError("the EPG URL must start with http:// or https://")

    state = load_state()
    state["epg_url"] = url
    save_state(state)

    values = {
        "m3uPathType": "0",          # local file
        "m3uPath": PLAYLIST,
        "m3uCache": "false",
        "epgPathType": "1",          # remote URL
        "epgUrl": url,
        # No on-disk EPG database cache: the guide is streamed from the
        # provider when Kodi needs it, which saves RAM and SD card writes
        # (and re-downloads are cheap compared to a dying SD card).
        "epgCache": "false",
    }
    write_iptvsimple_settings(values)
    for key in ("epgPathType", "epgUrl", "epgCache"):
        jsonrpc("Addons.SetAddonSetting",
                {"addonid": ADDON, "setting": key, "value": values[key]})
    return url


def write_iptvsimple_settings(values):
    """Write the add-on settings in the Kodi 18 (pvr.iptvsimple 3.9) format."""
    lines = ['<?xml version="1.0" encoding="utf-8" standalone="yes"?>',
             '<settings version="2">']
    for key in sorted(values):
        lines.append('    <setting id="%s" value="%s" />'
                     % (key, xml_escape(values[key], {'"': "&quot;"})))
    lines.append('</settings>')
    write_atomic(ADDON_SETTINGS, ("\n".join(lines) + "\n").encode("utf-8"))


class FetchError(ValueError):
    """A failed download, carrying the HTTP status when there was one.

    Kept separate from ValueError so the refresh can record `http_status`
    (200/304/an error code/None) for the dashboard without parsing the
    human-readable message.
    """

    def __init__(self, message, status=None):
        ValueError.__init__(self, message)
        self.status = status


def download_to_spool(url, validators=None):
    """Download a playlist into a temp file inside IPTV_DIR, streaming.

    With `validators` ({etag, last_modified}) the request becomes conditional,
    so a provider that has not changed answers 304 and no playlist bytes are
    transferred at all. Returns (spool_path, meta); spool_path is None for a
    304 and meta carries {"not_modified", "etag", "last_modified"}.
    """
    url = to_text(url or "").strip()
    if not url:
        raise ValueError("no URL given")
    lowered = url.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        raise ValueError("the URL must start with http:// or https://")

    validators = validators or {}
    headers = {"User-Agent": USER_AGENT}
    if validators.get("etag"):
        headers["If-None-Match"] = validators["etag"]
    if validators.get("last_modified"):
        headers["If-Modified-Since"] = validators["last_modified"]

    request_obj = Request(url, headers=headers)
    try:
        response = urlopen(request_obj, timeout=FETCH_TIMEOUT)
    except HTTPError as exc:
        if exc.code == 304:
            return None, {"not_modified": True, "status": 304,
                          "etag": validators.get("etag"),
                          "last_modified": validators.get("last_modified")}
        raise FetchError("the server answered HTTP %s" % exc.code, exc.code)
    except URLError as exc:
        raise FetchError("could not reach the URL: %s" % exc.reason)
    except Exception as exc:  # timeouts, TLS problems, ...
        raise FetchError("could not download the playlist: %s" % exc)

    try:
        path, _size = spool_chunks(read_chunks(response), MAX_BYTES)
        info = response.info()
        meta = {"not_modified": False, "status": 200,
                "etag": info.get("ETag"),
                "last_modified": info.get("Last-Modified")}
    except ValueError:
        raise
    except Exception as exc:  # socket timeouts mid-transfer, ...
        raise ValueError("could not download the playlist: %s" % exc)
    finally:
        response.close()
    return path, meta


# --------------------------------------------------------------------------
# hardware telemetry (/proc, /sys and vcgencmd)
# --------------------------------------------------------------------------

def cpu_temperature():
    raw = read_text("/sys/class/thermal/thermal_zone0/temp", 64)
    if not raw:
        return None
    try:
        return round(float(raw.strip()) / 1000.0, 1)
    except ValueError:
        return None


def gpu_temperature():
    returncode, output = run_cmd([VCGENCMD, "measure_temp"], timeout=5)
    if returncode != 0:
        return None
    match = re.search(r"([0-9]+\.[0-9]+)", output)
    if not match:
        return None
    try:
        return round(float(match.group(1)), 1)
    except ValueError:
        return None


def vcgencmd_clock_mhz(clock):
    """vcgencmd measure_clock arm -> 'frequency(48)=900000000' -> 900.0"""
    returncode, output = run_cmd([VCGENCMD, "measure_clock", clock], timeout=5)
    if returncode != 0:
        return None
    match = re.search(r"=(\d+)", output)
    if not match:
        return None
    try:
        return round(float(match.group(1)) / 1000000.0, 1)
    except ValueError:
        return None


def meminfo():
    info = {}
    text = read_text("/proc/meminfo", 16384) or ""
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        parts = value.split()
        if not parts:
            continue
        try:
            info[key] = int(parts[0])
        except ValueError:
            continue
    return info


def kb_to_mb(value):
    if value is None:
        return None
    return round(float(value) / 1024.0, 1)


def zram_stats():
    result = {"present": False, "size_mb": None, "used_mb": None,
              "compressed_mb": None}
    size = read_text("/sys/block/zram0/disksize", 64)
    if not size:
        return result
    result["present"] = True
    try:
        result["size_mb"] = round(float(size.strip()) / (1024.0 * 1024.0), 1)
    except ValueError:
        pass
    stat = read_text("/sys/block/zram0/mm_stat", 256)
    if stat:
        fields = stat.split()
        if len(fields) >= 2:
            try:
                result["used_mb"] = round(float(fields[0]) / (1024.0 * 1024.0), 1)
                result["compressed_mb"] = round(
                    float(fields[1]) / (1024.0 * 1024.0), 1)
            except ValueError:
                pass
    return result


def cpufreq_info():
    base = "/sys/devices/system/cpu/cpu0/cpufreq"
    info = {"governor": read_text(base + "/scaling_governor", 64),
            "available_governors": read_text(
                base + "/scaling_available_governors", 256),
            "cur_mhz": None, "min_mhz": None, "max_mhz": None}
    if info["governor"]:
        info["governor"] = info["governor"].strip()
    if info["available_governors"]:
        info["available_governors"] = info["available_governors"].split()
    for key, filename in (("cur_mhz", "scaling_cur_freq"),
                          ("min_mhz", "scaling_min_freq"),
                          ("max_mhz", "scaling_max_freq")):
        raw = read_text(base + "/" + filename, 64)
        if not raw:
            continue
        try:
            info[key] = round(float(raw.strip()) / 1000.0, 1)
        except ValueError:
            pass
    return info


def hardware_stats():
    mem = meminfo()
    zram = zram_stats()
    cpufreq = cpufreq_info()
    try:
        load = [round(value, 2) for value in os.getloadavg()]
    except OSError:
        load = [None, None, None]
    uptime_raw = read_text("/proc/uptime", 64)
    uptime = None
    if uptime_raw:
        try:
            uptime = int(float(uptime_raw.split()[0]))
        except (ValueError, IndexError):
            uptime = None

    stats = {
        "cpu_temp_c": cpu_temperature(),
        "gpu_temp_c": gpu_temperature(),
        "arm_mhz": vcgencmd_clock_mhz("arm"),
        "h264_mhz": vcgencmd_clock_mhz("h264"),
        "governor": cpufreq["governor"],
        "available_governors": cpufreq["available_governors"],
        "cpufreq_mhz": cpufreq["cur_mhz"],
        "cpufreq_min_mhz": cpufreq["min_mhz"],
        "cpufreq_max_mhz": cpufreq["max_mhz"],
        "mem_total_mb": kb_to_mb(mem.get("MemTotal")),
        "mem_available_mb": kb_to_mb(mem.get("MemAvailable", mem.get("MemFree"))),
        "swap_total_mb": kb_to_mb(mem.get("SwapTotal")),
        "swap_free_mb": kb_to_mb(mem.get("SwapFree")),
        "zram": zram,
        "load": load,
        "uptime_s": uptime,
    }
    return stats


# --------------------------------------------------------------------------
# system health (below the app: units, journal, power, OOM, disk)
# --------------------------------------------------------------------------

THROTTLE_FLAGS = (
    (0, "under-voltage right now"),
    (1, "ARM frequency capped right now"),
    (2, "currently throttled"),
    (3, "soft temperature limit reached"),
    (16, "under-voltage since boot"),
    (17, "ARM frequency capping since boot"),
    (18, "throttling since boot"),
    (19, "soft temperature limit since boot"),
)


def disk_usage(path):
    """Free/total space, so a full SD card shows up before Kodi breaks."""
    try:
        stats = os.statvfs(path)
    except OSError:
        return {"path": path, "total_mb": None, "free_mb": None}
    return {
        "path": path,
        "total_mb": round((stats.f_blocks * stats.f_frsize) / 1048576.0, 1),
        "free_mb": round((stats.f_bavail * stats.f_frsize) / 1048576.0, 1),
    }


def oom_kills():
    """Count from /proc/vmstat; None when the kernel does not report it."""
    text = read_text("/proc/vmstat", 65536) or ""
    match = re.search(r"^oom_kill (\d+)$", text, re.MULTILINE)
    return int(match.group(1)) if match else None


def throttled_state():
    """Decode `vcgencmd get_throttled` into current and since-boot flags."""
    returncode, output = run_cmd([VCGENCMD, "get_throttled"], timeout=5)
    raw = None
    if returncode == 0:
        match = re.search(r"throttled=(0x[0-9a-fA-F]+|\d+)", output or "")
        if match:
            try:
                raw = int(match.group(1), 0)
            except ValueError:
                raw = None
    current = []
    since_boot = []
    if raw:
        for bit, label in THROTTLE_FLAGS:
            if raw & (1 << bit):
                (current if bit < 16 else since_boot).append(label)
    return {"raw": raw,
            "hex": ("0x%x" % raw) if raw is not None else "",
            "current": current, "since_boot": since_boot}


def failed_units():
    """Units systemd reports as failed ([] when systemd is unreachable)."""
    returncode, output = run_cmd(
        ["systemctl", "--failed", "--no-legend", "--no-pager"], timeout=20)
    if returncode != 0:
        return []
    units = []
    for line in output.splitlines():
        fields = line.split()
        if fields and "." in fields[0]:
            units.append(fields[0])
    return units


def journal_errors(limit=20):
    """Error-priority journal lines of this boot, oldest first."""
    returncode, output = run_cmd(
        ["journalctl", "-p", "3", "-b", "--no-pager", "-n", str(limit),
         "-o", "short-iso"], timeout=20)
    if returncode != 0 or not output:
        return []
    lines = [line for line in output.splitlines()
             if line.strip() and not line.startswith("-- ")]
    return lines[-limit:]


def boot_id():
    return (read_text(BOOT_ID_FILE, 64) or "").strip()


def health_status(journal_limit=20):
    """Everything that can go wrong underneath the app, in one dict."""
    units = failed_units()
    errors = journal_errors(journal_limit)
    throttle = throttled_state()
    oom = oom_kills()
    root = disk_usage("/")
    data = disk_usage(IPTV_DIR)

    problems = []
    if units:
        problems.append("%d failed unit(s): %s" % (len(units), ", ".join(units)))
    if throttle["current"]:
        problems.append("power/thermal: %s" % ", ".join(throttle["current"]))
    if oom:
        problems.append("%d OOM kill(s) since boot" % oom)
    for usage in (root, data):
        if usage["free_mb"] is not None and usage["free_mb"] < 100:
            problems.append("%s has only %.0f MB free"
                            % (usage["path"], usage["free_mb"]))
    if errors:
        problems.append("%d error line(s) in the journal" % len(errors))

    return {
        "ok": not problems,
        "at": timestamp(),
        "boot_id": boot_id(),
        "failed_units": units,
        "journal_errors": errors,
        "journal_error_count": len(errors),
        "throttled": throttle,
        "oom_kills": oom,
        "disk": {"root": root, "data": data},
        "timer": HEALTH_TIMER,
        "problems": problems,
        "summary": "; ".join(problems) if problems else "all quiet",
        "signature": hashlib.sha1(("|".join(units)
                                   + "|%s|%s|%s" % (oom, throttle["hex"],
                                                     "\n".join(errors))
                                   ).encode("utf-8")).hexdigest(),
    }


_HEALTH_CACHE = {"at": 0.0, "value": None}


def cached_health(fresh=False):
    """health_status() with a short cache, so rendering the page is cheap."""
    now = time.time()
    cached = _HEALTH_CACHE["value"]
    if (not fresh and cached is not None
            and now - _HEALTH_CACHE["at"] < HEALTH_CACHE_TTL):
        return cached
    value = health_status()
    _HEALTH_CACHE["at"] = now
    _HEALTH_CACHE["value"] = value
    return value


def health_main():
    """Watchdog for the timer: persist a line only when the state changed."""
    health = health_status()
    state = load_state()
    previous = state.get("health") or {}
    rebooted = bool(previous.get("boot_id")) \
        and previous.get("boot_id") != health["boot_id"]
    if (rebooted or previous.get("signature") != health["signature"]) \
            and health["problems"]:
        log_error("system", health["summary"])
    state["health"] = {"at": health["at"], "boot_id": health["boot_id"],
                       "signature": health["signature"]}
    save_state(state)
    print(health["summary"])
    return 0


def alert_unit(unit):
    """OnFailure handler: report a failed unit, at most once per cooldown."""
    unit = to_text(unit or "").strip()
    if not unit:
        print("no unit name given")
        return 2

    state = load_state()
    now = time.time()
    alerts = state.get("unit_alerts") or {}
    last = alerts.get(unit)
    if isinstance(last, (int, float)) and now - float(last) < UNIT_ALERT_COOLDOWN:
        print("%s was already reported, staying quiet" % unit)
        return 0

    active = "unknown"
    if not OFFLINE:
        returncode, output = run_cmd(["systemctl", "is-active", unit], timeout=10)
        active = output or ("inactive" if returncode == 0 else "unknown")
    message = "%s failed (state: %s)" % (unit, active)
    log_error("system", message)

    target = notify_target(state)
    if target:
        text = "IPTV-Pi: %s" % message
        ok, detail = send_notification(target, {
            "text": text, "content": text, "event": "unit_failed",
            "unit": unit, "state": active,
            "at": time.strftime("%Y-%m-%d %H:%M"),
        })
        state["last_alert"] = {"at": time.strftime("%Y-%m-%d %H:%M"),
                               "ok": ok, "message": detail}
        if not ok:
            log_error("alert", "could not deliver the unit alert", detail)
    alerts[unit] = now
    state["unit_alerts"] = alerts
    save_state(state)
    print(message)
    return 0


# --------------------------------------------------------------------------
# Kodi control (JSON-RPC first, service restart as fallback)
# --------------------------------------------------------------------------

def jsonrpc(method, params=None, timeout=5.0):
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params or {},
    })
    request_obj = Request(
        KODI_RPC,
        data=payload.encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        response = urlopen(request_obj, timeout=timeout)
        try:
            body = response.read()
        finally:
            response.close()
    except Exception as exc:
        return False, "kodi JSON-RPC unreachable: %s" % exc

    try:
        parsed = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return False, "invalid answer from kodi JSON-RPC"
    if "error" in parsed:
        error = parsed["error"]
        if isinstance(error, dict):
            return False, error.get("message", "kodi JSON-RPC error")
        return False, str(error)
    return True, parsed.get("result", {})


def kodi_state():
    if OFFLINE:
        return "not managed here"
    returncode, output = run_cmd(["systemctl", "is-active", KODI_UNIT], timeout=10)
    if output:
        return output.splitlines()[0]
    return "unknown"


def clean_systemd_time(value):
    """systemd prints 'n/a' / 'infinity' for timers that never ran."""
    if not value:
        return None
    value = value.strip()
    if value in ("n/a", "0", "infinity"):
        return None
    return value


def timer_info():
    """Next and last run of the refresh timer, straight from systemd."""
    info = {"next_run": None, "last_trigger": None, "timer_active": None}
    if OFFLINE:
        return info
    returncode, output = run_cmd(
        ["systemctl", "show", REFRESH_TIMER,
         "--property=NextElapseUSecRealtime",
         "--property=LastTriggerUSec",
         "--property=ActiveState"], timeout=10)
    if returncode != 0:
        return info

    properties = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            properties[key.strip()] = value.strip()
    info["next_run"] = clean_systemd_time(properties.get("NextElapseUSecRealtime"))
    info["last_trigger"] = clean_systemd_time(properties.get("LastTriggerUSec"))
    info["timer_active"] = properties.get("ActiveState") in ("active", "waiting")
    return info


def read_app_version(path):
    """Version of an app.py on disk (the running one may be older)."""
    text = read_text(path, 8192) or ""
    match = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else ""


def update_status():
    """What is installed where, so the card can show both versions."""
    overlay_app = os.path.join(APP_DIR, "app.py")
    backup_app = os.path.join(APP_BACKUP, "app.py")
    return {
        "running": APP_VERSION,
        "installed": (read_app_version(overlay_app) or APP_VERSION),
        "overlay": os.path.isfile(overlay_app),
        "dir": APP_DIR,
        "backup": read_app_version(backup_app),
        "max_bytes": UPDATE_MAX_BYTES,
        "unit": WEBUI_UNIT,
    }


def extract_update(archive_path, target_dir):
    """Unpack an app bundle into target_dir, refusing anything unexpected.

    Only app.py, launch.sh and files under templates/ and static/ are allowed;
    no symlinks, no absolute paths, no `..`, and a size and file-count cap.
    """
    ensure_dir(target_dir)
    try:
        bundle = tarfile.open(archive_path, "r:gz")
    except (tarfile.TarError, IOError, OSError) as exc:
        raise ValueError("the file is not a readable .tar.gz archive: %s" % exc)

    count = 0
    total = 0
    try:
        for member in bundle:
            name = member.name.replace("\\", "/")
            while name.startswith("./"):
                name = name[2:]
            if name.startswith("webui/"):
                name = name[len("webui/"):]
            if name in ("", ".", "./"):
                continue
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError(
                    "the archive contains %r, which is not a regular file"
                    % member.name)
            if name.startswith("/") or ".." in name.split("/"):
                raise ValueError("the archive contains an unsafe path: %r"
                                 % member.name)
            if not UPDATE_FILE_RE.match(name):
                raise ValueError(
                    "the archive contains %r, which is not part of the app"
                    % member.name)
            total += member.size
            if total > UPDATE_MAX_BYTES:
                raise ValueError("the unpacked update is larger than %d bytes"
                                 % UPDATE_MAX_BYTES)
            count += 1
            if count > 64:
                raise ValueError("the archive contains too many files")

            source = bundle.extractfile(member)
            if source is None:
                raise ValueError("could not read %r from the archive" % name)
            destination = os.path.join(target_dir, *name.split("/"))
            ensure_dir(os.path.dirname(destination))
            try:
                handle = open(destination, "wb")
                try:
                    shutil.copyfileobj(source, handle, 65536)
                finally:
                    handle.close()
            finally:
                source.close()
    finally:
        bundle.close()

    if not os.path.isfile(os.path.join(target_dir, "app.py")):
        raise ValueError("the archive has no app.py at its root")
    return count


def check_python_source(path):
    """Compile the uploaded app.py so a syntax error can never be installed."""
    python = sys.executable or "python"
    snippet = ("import py_compile, sys; "
               "py_compile.compile(sys.argv[1], cfile=sys.argv[2], doraise=True)")
    compiled = path + ".pyc"
    returncode, output = run_cmd([python, "-c", snippet, path, compiled], timeout=60)
    try:
        os.unlink(compiled)
    except OSError:
        pass
    if returncode != 0:
        detail = (output or "").strip().splitlines()
        raise ValueError("the new app.py does not compile: %s"
                         % (detail[-1] if detail else "exit %d" % returncode))


def apply_update(archive_path):
    """Validate a bundle, then swap it in and keep the old app as app.bak."""
    staging = APP_DIR + ".new"
    shutil.rmtree(staging, ignore_errors=True)
    ensure_dir(staging)
    try:
        count = extract_update(archive_path, staging)
        check_python_source(os.path.join(staging, "app.py"))
    except Exception:
        # A rejected bundle must not leave a half-unpacked directory behind.
        shutil.rmtree(staging, ignore_errors=True)
        raise
    version = read_app_version(os.path.join(staging, "app.py"))
    if not version:
        raise ValueError("the new app.py has no APP_VERSION")

    launcher = os.path.join(staging, "launch.sh")
    if os.path.isfile(launcher):
        try:
            os.chmod(launcher, 0o755)
        except OSError:
            pass

    shutil.rmtree(APP_BACKUP, ignore_errors=True)
    try:
        if os.path.isdir(APP_DIR):
            os.rename(APP_DIR, APP_BACKUP)
        os.rename(staging, APP_DIR)
    except OSError as exc:
        # Put the old app back if the swap broke halfway through.
        if not os.path.isdir(APP_DIR) and os.path.isdir(APP_BACKUP):
            try:
                os.rename(APP_BACKUP, APP_DIR)
            except OSError:
                pass
        shutil.rmtree(staging, ignore_errors=True)
        raise ValueError("could not swap the update into place: %s" % exc)
    return version or "%d files" % count


def schedule_webui_restart(delay=None):
    """Restart the web UI once the current response has been written."""
    if OFFLINE:
        return "restart skipped (IPTV_NO_SYSTEMD=1)"
    delay = UPDATE_RESTART_DELAY if delay is None else delay

    def worker():
        time.sleep(delay)
        returncode, output = run_cmd(["systemctl", "restart", WEBUI_UNIT],
                                     timeout=90)
        if returncode != 0:
            log_error("update", "could not restart %s" % WEBUI_UNIT,
                      output or "exit %d" % returncode)

    thread = threading.Thread(target=worker)
    thread.daemon = True
    thread.start()
    return "restarting %s in %.1fs" % (WEBUI_UNIT, delay)


# --------------------------------------------------------------------------
# A/B system updates (RAUC)
# --------------------------------------------------------------------------

class RaucRefused(ValueError):
    """The request itself is wrong, so the browser gets a 400, not a 409."""


def sha256_file(path):
    """SHA-256 of a file, streamed so a big bundle never lands in RAM."""
    digest = hashlib.sha256()
    try:
        handle = open(path, "rb")
    except (IOError, OSError):
        return ""
    try:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        handle.close()
    return digest.hexdigest()


def wanted_checksum():
    """Optional expected SHA-256 from the form ("" when none was given)."""
    raw = to_text(request.form.get("sha256", "")).strip()
    if not raw:
        return ""
    match = SHA256_RE.match(raw)
    if not match:
        raise RaucRefused("the checksum has to be 64 hex characters (SHA-256)")
    return match.group(1).lower()


def checksum_mismatch(expected, observed, name):
    """Message for a bundle that does not match, or "" when it does."""
    if not expected or not observed or expected == observed:
        return ""
    return ("%s does not match the checksum: expected %s, got %s"
            % (name, expected, observed))


def sidecar_url(url):
    """The .sha256 next to a bundle URL, keeping a query string intact."""
    if "?" in url:
        base, query = url.split("?", 1)
        return "%s.sha256?%s" % (base, query)
    return url + ".sha256"


def fetch_checksum(url):
    """Published SHA-256 for a bundle URL, or "" when there is none.

    `post-image.sh` writes `<bundle>.raucb.sha256` next to every bundle, so a
    download can be checked without anyone copying the number by hand. A
    missing sidecar just means the download stays unverified by checksum; only
    an unexpected answer from the server is worth a log line.
    """
    target = sidecar_url(url)
    try:
        request_obj = Request(target, headers={"User-Agent": USER_AGENT})
        try:
            handle = urlopen(request_obj, timeout=FETCH_TIMEOUT)
        except HTTPError as exc:
            if exc.code != 404:
                log_error("rauc", "could not read the checksum file",
                          "HTTP %d for %s" % (exc.code, target))
            return ""
        except (URLError, IOError, OSError):
            return ""
        try:
            raw = handle.read(4096)
        finally:
            handle.close()
    except (URLError, IOError, OSError, ValueError):
        return ""
    match = SHA256_ANY_RE.search(to_text(raw))
    return match.group(1).lower() if match else ""


def free_bytes(path):
    """Free space in bytes on the filesystem holding path (None if unknown)."""
    try:
        stats = os.statvfs(path)
    except OSError:
        return None
    return stats.f_bavail * stats.f_frsize


def download_bundle(url, target, progress=None):
    """Copy a .raucb from an http(s) URL into target, keeping memory flat.

    The box fetches the bundle itself, so a server on the LAN is enough and no
    browser has to push a few hundred MB through the page. Everything is
    streamed in 64 KB chunks; the size cap and the free space of the data
    partition are enforced while writing, so a wrong URL cannot fill the card.
    """
    scheme = url.split(":", 1)[0].lower()
    if scheme not in ("http", "https"):
        raise ValueError("the bundle URL must start with http:// or https://")

    free = free_bytes(RAUC_BUNDLE_DIR)
    partial = target + ".part"
    try:
        request = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            handle = urlopen(request, timeout=FETCH_TIMEOUT)
        except HTTPError as exc:
            raise ValueError("the server answered HTTP %d" % exc.code)
        except (URLError, IOError, OSError) as exc:
            raise ValueError("could not reach the server: %s" % exc)

        total = None
        try:
            length = handle.headers.get("Content-Length")
        except AttributeError:  # Python 2 response objects without headers
            length = None
        if length and to_text(length).strip().isdigit():
            total = int(to_text(length).strip())
            if total > RAUC_MAX_BYTES:
                handle.close()
                raise ValueError("the bundle is larger than %d MB"
                                 % (RAUC_MAX_BYTES // 1048576))
            if free is not None and total + RAUC_FREE_HEADROOM > free:
                handle.close()
                raise ValueError("not enough free space in %s for a %d MB bundle"
                                 % (RAUC_BUNDLE_DIR, total // 1048576))

        written = 0
        try:
            destination = open(partial, "wb")
            try:
                while True:
                    chunk = handle.read(65536)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > RAUC_MAX_BYTES:
                        raise ValueError("the bundle is larger than %d MB"
                                         % (RAUC_MAX_BYTES // 1048576))
                    if free is not None and written + RAUC_FREE_HEADROOM > free:
                        raise ValueError(
                            "not enough free space in %s for this bundle"
                            % RAUC_BUNDLE_DIR)
                    destination.write(chunk)
                    if progress is not None:
                        progress(written, total)
            finally:
                destination.close()
        finally:
            try:
                handle.close()
            except (IOError, OSError):
                pass
    except Exception as failure:
        try:
            os.unlink(partial)
        except OSError:
            pass
        # Python 2 would re-raise the OSError of the missing .part file here
        # instead of the download error (see write_text above).
        raise failure

    if written == 0:
        try:
            os.unlink(partial)
        except OSError:
            pass
        raise ValueError("the server sent an empty file")
    os.rename(partial, target)
    return written


def rauc_available():
    return bool(RAUC) and os.path.isfile(RAUC) and os.access(RAUC, os.X_OK)


def bundle_info(path):
    """Version and compatible from the bundle's manifest, best effort.

    `rauc info` reads only the manifest, so this costs no significant time even
    for a few hundred MB. The shell output is preferred because it is stable;
    older RAUC versions without that flag fall back to parsing the readable
    output. Anything unexpected leaves the fields empty instead of failing the
    install.
    """
    info = {"version": "", "compatible": ""}
    if not path or not rauc_available():
        return info

    attempts = (["info", "--output-format=shell", path], ["info", path])
    for arguments in attempts:
        returncode, output = run_cmd([RAUC] + arguments, timeout=30)
        if returncode != 0:
            continue
        for line in (output or "").splitlines():
            line = line.strip()
            if line.startswith("RAUC_MANIFEST_VERSION="):
                info["version"] = line.split("=", 1)[1].strip("'\"")
            elif line.startswith("RAUC_MANIFEST_COMPATIBLE="):
                info["compatible"] = line.split("=", 1)[1].strip("'\"")
            else:
                match = re.match(r"^version\s*:\s*(\S+)", line, re.IGNORECASE)
                if match:
                    info["version"] = match.group(1)
                    continue
                match = re.match(r"^compatible\s*:\s*(\S+)", line,
                                 re.IGNORECASE)
                if match:
                    info["compatible"] = match.group(1)
        if info["version"] or info["compatible"]:
            return info
    return info


def rauc_compatible():
    """The `compatible` the image was built with, straight from system.conf."""
    text = read_text(RAUC_SYSTEM_CONF, 8192) or ""
    match = re.search(r"^compatible\s*=\s*(\S+)", text, re.MULTILINE)
    return match.group(1) if match else ""


def run_boot_slot(arguments):
    """Ask iptv-boot-slot something; None when it cannot answer."""
    if not os.path.isfile(RAUC_BOOT_SLOT):
        return None
    returncode, output = run_cmd([RAUC_BOOT_SLOT] + list(arguments), timeout=10)
    if returncode != 0:
        return None
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    return lines[-1] if lines else None


def rauc_slot_state():
    """Booted slot, primary slot and the state of both, as the bootloader sees it."""
    booted = run_boot_slot(["get-current"])
    primary = run_boot_slot(["get-primary"])
    slots = []
    for name in ("A", "B"):
        state = run_boot_slot(["get-state", name]) or "unknown"
        if state not in ("good", "bad"):
            state = "unknown"
        slots.append({
            "bootname": name,
            "state": state,
            "booted": name == booted,
            "primary": name == primary,
        })
    return booted, primary, slots


def read_pending_update():
    """The record of the last update that has not been verified yet."""
    text = read_text(RAUC_PENDING_FILE, 8192) or ""
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_pending_update(payload):
    try:
        ensure_dir(os.path.dirname(RAUC_PENDING_FILE))
        write_atomic(RAUC_PENDING_FILE,
                     json.dumps(payload, sort_keys=True).encode("utf-8"))
        return True
    except (IOError, OSError, ValueError) as exc:
        log_error("rauc", "could not record the pending update", exc)
        return False


def record_pending_update(booted):
    """Remember which slot is running now, so the next boot can be verified."""
    if not booted:
        return None
    payload = {
        "from_slot": booted,
        "boot_id": boot_id(),
        "installed_at": timestamp(),
        "state": "pending",
        "message": "",
    }
    return payload if write_pending_update(payload) else None


def read_history():
    """The last updates, newest first, so the card can list them."""
    text = read_text(RAUC_HISTORY_FILE, 16384) or ""
    try:
        entries = json.loads(text)
    except (ValueError, TypeError):
        return []
    if not isinstance(entries, list):
        return []
    fields = ("bundle", "state", "installed_at", "started_at", "finished_at",
              "message", "version", "compatible", "source", "origin",
              "sha256", "checksum_source", "slot", "verified_at")
    items = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        # Entries written by an older build may lack the newer fields; the
        # card should never have to guess.
        items.append(dict((key, entry.get(key) or "") for key in fields))
    return list(reversed(items[-RAUC_HISTORY_LIMIT:]))


def write_history(entries):
    trimmed = [entry for entry in entries if isinstance(entry, dict)]
    trimmed = trimmed[-RAUC_HISTORY_LIMIT:]
    try:
        ensure_dir(os.path.dirname(RAUC_HISTORY_FILE))
        write_atomic(RAUC_HISTORY_FILE, json.dumps(trimmed).encode("utf-8"))
        return True
    except (IOError, OSError, ValueError) as exc:
        log_error("rauc", "could not write the update history", exc)
        return False


def record_history(entry):
    """Append one finished update, keeping the last RAUC_HISTORY_LIMIT."""
    text = read_text(RAUC_HISTORY_FILE, 16384) or ""
    try:
        entries = json.loads(text)
    except (ValueError, TypeError):
        entries = []
    if not isinstance(entries, list):
        entries = []
    entries.append(entry)
    return write_history(entries)


def update_history_verdict(pending, state, message, slot):
    """Attach the boot verdict to the history entry that belongs to it.

    Matched by the install timestamp the pending record and the history entry
    share, so a verdict can never be attached to some older update.
    """
    installed_at = (pending or {}).get("installed_at")
    if not installed_at:
        return False
    text = read_text(RAUC_HISTORY_FILE, 16384) or ""
    try:
        entries = json.loads(text)
    except (ValueError, TypeError):
        return False
    if not isinstance(entries, list):
        return False
    for entry in reversed(entries):
        if (isinstance(entry, dict)
                and entry.get("installed_at") == installed_at
                and not entry.get("verified_at")):
            entry.update(state=state, message=message,
                         verified_at=timestamp(), slot=slot or "")
            return write_history(entries)
    return False


def check_pending_update(booted):
    """Compare the running slot with what the last update promised.

    Same boot id: the box has not restarted yet, nothing to verify. A different
    boot id with a different slot means the switch worked; a different boot id
    with the same slot means the bootloader did not follow the update or RAUC
    rolled back. The verdict is written exactly once, so polling the page does
    not keep writing to the SD card.
    """
    pending = read_pending_update()
    if not pending:
        return {"state": "none"}

    result = {
        "state": pending.get("state") or "pending",
        "from_slot": pending.get("from_slot"),
        "booted": booted,
        "installed_at": pending.get("installed_at"),
        "verified_at": pending.get("verified_at"),
        "message": pending.get("message") or "",
    }
    if result["state"] != "pending":
        return result

    current = boot_id()
    if current and pending.get("boot_id") and current == pending.get("boot_id"):
        result["state"] = "waiting"
        result["message"] = "installed, waiting for the reboot"
        return result

    from_slot = pending.get("from_slot")
    if not booted:
        result["state"] = "waiting"
        result["message"] = "the running slot could not be determined"
        return result
    if from_slot and booted != from_slot:
        result["state"] = "ok"
        result["message"] = ("slot %s is running, the update took effect"
                             % booted)
    else:
        result["state"] = "warning"
        result["message"] = ("still running slot %s after the reboot: the switch "
                             "did not happen, or RAUC rolled back" % booted)
        log_error("rauc", "update verification failed", result["message"])

    pending.update(state=result["state"],
                   booted=booted,
                   verified_at=timestamp(),
                   message=result["message"])
    if write_pending_update(pending):
        result["verified_at"] = pending["verified_at"]
    update_history_verdict(pending,
                           "verified" if result["state"] == "ok" else "warning",
                           result["message"], booted)
    return result


def close_pending_update(state, message):
    """Close the record when there is nothing left to verify (e.g. a failure)."""
    pending = read_pending_update()
    if not pending or pending.get("state") != "pending":
        return
    pending.update(state=state, message=message, verified_at=timestamp())
    write_pending_update(pending)


def schedule_reboot(delay=None):
    """Reboot after the install result has had a chance to reach the browser."""
    if OFFLINE:
        return "reboot skipped (IPTV_NO_SYSTEMD=1)"
    delay = RAUC_REBOOT_DELAY if delay is None else delay

    def worker():
        time.sleep(delay)
        returncode, output = run_cmd(["systemctl", "reboot"], timeout=60)
        if returncode != 0:
            log_error("rauc", "could not reboot after the update",
                      output or "exit %d" % returncode)

    thread = threading.Thread(target=worker)
    thread.daemon = True
    thread.start()
    return "rebooting in %.0fs" % delay


class RaucInstaller(object):
    """Runs `rauc install <bundle>` in the background and tracks its progress.

    RAUC prints one line per step plus a percentage while it writes; the output
    goes into a file that is read incrementally. That behaves identically on
    Python 2.7 and 3 and never blocks on a pipe the way readline() would.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._state = "idle"
        self._percent = 0
        self._step = ""
        self._message = ""
        self._error = ""
        self._bundle = ""
        self._version = ""
        self._compatible = ""
        self._source = ""
        self._origin = ""
        self._sha256 = ""
        self._expected = ""
        self._checksum_source = ""
        self._pending = {}
        self._reboot = False
        self._phase = ""
        self._started = None
        self._finished = None

    def status(self):
        with self._lock:
            return {
                "state": self._state,
                "running": self._state == "running",
                "phase": self._phase,
                "reboot": self._reboot,
                "percent": self._percent,
                "step": self._step,
                "message": self._message,
                "error": self._error,
                "bundle": self._bundle,
                "version": self._version,
                "compatible": self._compatible,
                "source": self._source,
                "origin": self._origin,
                "sha256": self._sha256,
                "expected": self._expected,
                "checksum_source": self._checksum_source,
                "started_at": self._started,
                "finished_at": self._finished,
            }

    def busy(self):
        return self.status()["running"]

    def start(self, path, reboot=False, expected=""):
        if self.busy():
            raise ValueError("an update is already running")
        observed = sha256_file(path)
        mismatch = checksum_mismatch(expected, observed, os.path.basename(path))
        if mismatch:
            raise RaucRefused(mismatch)
        pending = record_pending_update(run_boot_slot(["get-current"]))
        info = bundle_info(path)
        with self._lock:
            self._state = "running"
            self._pending = pending or {}
            self._reboot = bool(reboot)
            self._phase = "install"
            self._percent = 0
            self._step = "starting rauc"
            self._message = ""
            self._error = ""
            self._bundle = os.path.basename(path)
            self._version = info.get("version", "")
            self._compatible = info.get("compatible", "")
            self._source = "upload"
            self._origin = os.path.basename(path)
            self._sha256 = observed
            self._expected = expected
            self._checksum_source = "typed" if expected else ""
            self._started = timestamp()
            self._finished = None
        thread = threading.Thread(target=self._run, args=(path,))
        thread.daemon = True
        thread.start()

    def start_url(self, url, reboot=False, expected=""):
        """Download the bundle from a LAN URL, then install it like an upload."""
        if self.busy():
            raise ValueError("an update is already running")
        pending = record_pending_update(run_boot_slot(["get-current"]))
        name = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        with self._lock:
            self._state = "running"
            self._pending = pending or {}
            self._reboot = bool(reboot)
            self._phase = "download"
            self._percent = 0
            self._step = "downloading the bundle"
            self._message = url
            self._error = ""
            self._bundle = name or url
            self._version = ""
            self._compatible = ""
            self._source = "url"
            self._origin = url
            self._sha256 = ""
            self._expected = expected
            self._checksum_source = "typed" if expected else ""
            self._started = timestamp()
            self._finished = None
        thread = threading.Thread(target=self._fetch_then_install, args=(url, name))
        thread.daemon = True
        thread.start()

    def _fetch_then_install(self, url, name):
        target = os.path.join(RAUC_BUNDLE_DIR, name)
        try:
            ensure_dir(RAUC_BUNDLE_DIR)
            download_bundle(url, target, self._download_progress)
        except (ValueError, IOError, OSError) as exc:
            log_error("rauc", "could not download the bundle", exc)
            self._finish(False, to_text(exc))
            return
        expected = self._expected
        if not expected:
            # Nothing typed in: use the number published next to the bundle.
            expected = fetch_checksum(url)
            if expected:
                with self._lock:
                    self._expected = expected
                    self._checksum_source = "sidecar"
        observed = sha256_file(target)
        mismatch = checksum_mismatch(expected, observed, os.path.basename(target))
        if mismatch:
            log_error("rauc", "bundle rejected", mismatch)
            try:
                os.unlink(target)
            except OSError:
                pass
            self._finish(False, mismatch)
            return
        # The manifest is only readable once the file is complete.
        info = bundle_info(target)
        with self._lock:
            self._sha256 = observed
            self._version = info.get("version", "")
            self._compatible = info.get("compatible", "")
        self._run(target)

    def _download_progress(self, done, total):
        if total:
            percent = min(100, int(done * 100 / total))
        else:
            percent = 0
        if total:
            message = "%d of %d MB" % (done // 1048576, total // 1048576)
        else:
            message = "%d MB" % (done // 1048576)
        with self._lock:
            if percent > self._percent:
                self._percent = percent
            self._message = message

    def _note(self, line):
        match = RAUC_PROGRESS_RE.search(line)
        lowered = line.lower()
        step = ""
        for keyword, label in RAUC_STEPS:
            if keyword in lowered:
                step = label
                break
        with self._lock:
            if match:
                value = min(100, int(match.group(1)))
                if value > self._percent:
                    self._percent = value
            if step:
                self._step = step
            self._message = line[:200]

    def _drain(self, log_path, offset):
        """Feed every complete line written since the last call into _note."""
        try:
            handle = open(log_path, "rb")
        except (IOError, OSError):
            return offset
        try:
            handle.seek(offset)
            data = handle.read()
        finally:
            handle.close()
        if not data:
            return offset
        for raw in data.splitlines():
            text = raw.decode("utf-8", "replace").strip()
            if text:
                self._note(text)
        return offset + len(data)

    def _tail(self, log_path):
        lines = [line.strip() for line in
                 (read_text(log_path, 8192) or "").splitlines() if line.strip()]
        return lines[-1] if lines else ""

    def _finish(self, ok, message):
        # Only a successful install may reboot, and only when it was asked for.
        if ok and self._reboot:
            hint = schedule_reboot()
            message = "%s; %s" % (message, hint) if message else hint
        if not ok:
            close_pending_update("failed", "the install failed, nothing to verify")
        finished = timestamp()
        with self._lock:
            pending = self._pending or {}
            history_entry = {
                "bundle": self._bundle,
                "state": "installed" if ok else "failed",
                # The pending record and this entry share the timestamp, which
                # is how the boot verdict finds the right entry later.
                "installed_at": pending.get("installed_at") or self._started,
                "started_at": self._started,
                "finished_at": finished,
                "message": message[:200],
                "version": self._version,
                "compatible": self._compatible,
                "source": self._source,
                "origin": self._origin,
                "sha256": self._sha256,
                "checksum_source": self._checksum_source,
                "slot": "",
                "verified_at": "",
            }
        # The entry is written before the outcome becomes visible: a poll that
        # already sees success/error must also find the history line, otherwise
        # the card shows "done" with an empty history for a moment.
        record_history(history_entry)
        with self._lock:
            self._state = "success" if ok else "error"
            self._finished = finished
            self._message = message
            self._error = "" if ok else message
            if ok:
                self._percent = 100
                self._step = "done"

    def _run(self, path):
        with self._lock:
            self._phase = "install"
            self._percent = 0
            self._step = "starting rauc"
            self._bundle = os.path.basename(path)
        log_path = os.path.join(RAUC_BUNDLE_DIR, "install.log")
        try:
            ensure_dir(RAUC_BUNDLE_DIR)
            handle = open(log_path, "wb")
        except (IOError, OSError) as exc:
            self._finish(False, "could not open %s: %s" % (log_path, exc))
            return

        try:
            try:
                proc = subprocess.Popen([RAUC, "install", path],
                                        stdout=handle,
                                        stderr=subprocess.STDOUT)
            except OSError as exc:
                self._finish(False, "could not run %s: %s" % (RAUC, exc))
                return

            deadline = time.time() + RAUC_INSTALL_TIMEOUT
            offset = 0
            timed_out = False
            while True:
                time.sleep(0.2)
                offset = self._drain(log_path, offset)
                if proc.poll() is not None:
                    break
                if time.time() > deadline:
                    timed_out = True
                    proc.kill()
                    proc.wait()
                    break
            offset = self._drain(log_path, offset)
            returncode = proc.returncode
        finally:
            try:
                handle.close()
            except (IOError, OSError):
                pass

        if timed_out:
            message = "the install timed out after %d minutes" % (
                RAUC_INSTALL_TIMEOUT / 60)
            log_error("rauc", message, self._tail(log_path))
            self._finish(False, message)
        elif returncode == 0:
            self._finish(True, "update installed; reboot to boot the new slot")
        else:
            message = self._tail(log_path) or ("rauc exited with status %d"
                                               % returncode)
            log_error("rauc", "install failed", message)
            self._finish(False, message)

        # A bundle is a few hundred MB and is worthless here once it has been
        # written to the inactive slot; the log line keeps the outcome.
        try:
            os.unlink(path)
        except OSError:
            pass


RAUC_INSTALLER = RaucInstaller()


def rauc_status():
    """Everything the card shows: slots, the bootloader view and the install."""
    booted, primary, slots = rauc_slot_state()
    install = RAUC_INSTALLER.status()
    # Checked before the history is read: the verdict of a finished update is
    # written into it, so the same response already shows it.
    update_check = check_pending_update(booted)
    # Before the first upload the bundle directory does not exist yet; the
    # space it will get is the space of the data partition it lives on.
    free = free_bytes(RAUC_BUNDLE_DIR)
    if free is None:
        free = free_bytes(IPTV_DIR)
    history = read_history()
    # A problem in the newest entry is worth opening the history for; a clean
    # one stays collapsed so the card does not grow for no reason.
    history_alert = bool(history) and history[0].get("state") in (
        "failed", "warning")
    history_problems = len([entry for entry in history
                            if entry.get("state") in ("failed", "warning")])
    return {
        "available": rauc_available(),
        "compatible": rauc_compatible(),
        "booted": booted,
        "primary": primary,
        "slots": slots,
        "install": install,
        "history": history,
        "history_limit": RAUC_HISTORY_LIMIT,
        "history_alert": history_alert,
        "history_problems": history_problems,
        "update_check": update_check,
        "reboot_delay": RAUC_REBOOT_DELAY,
        "max_bytes": RAUC_MAX_BYTES,
        "dir": RAUC_BUNDLE_DIR,
        "free_mb": None if free is None else round(free / 1048576.0, 1),
    }


def notify_target(state=None):
    """Where failure alerts go: the URL saved in the UI, else IPTV_NOTIFY_URL."""
    state = state if state is not None else load_state()
    saved = to_text(state.get("notify_url") or "").strip()
    if saved:
        return saved
    return to_text(os.environ.get("IPTV_NOTIFY_URL") or "").strip()


def send_notification(url, payload, timeout=None):
    """POST a small JSON body to the alert endpoint; returns (ok, message).

    Never raises: a notification problem must not turn into a refresh failure.
    """
    body = json.dumps(payload)
    if not isinstance(body, bytes):  # Python 2 hands back str, Python 3 bytes
        body = body.encode("utf-8")
    request_obj = Request(url, data=body, headers={
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    })
    try:
        response = urlopen(request_obj, timeout=timeout or NOTIFY_TIMEOUT)
        try:
            response.read(256)  # drain so the connection can be reused/closed
        finally:
            response.close()
    except HTTPError as exc:
        return False, "the alert endpoint answered HTTP %s" % exc.code
    except URLError as exc:
        return False, "could not reach the alert endpoint: %s" % exc.reason
    except Exception as exc:  # timeouts, TLS problems, bad payload, ...
        return False, "could not send the alert: %s" % exc
    return True, "alert sent"


def send_failure_alert(state, message, http_status):
    """Alert the configured endpoint about a failing refresh streak.

    The body carries `text` and `content` so Slack- and Discord-style webhooks
    work unchanged, plus the structured fields an own receiver would want.
    """
    target = notify_target(state)
    if not target:
        return None
    text = "IPTV-Pi: %s" % message
    payload = {
        "text": text,
        "content": text,
        "event": "refresh_failed",
        "failures": state.get("failures") or 0,
        "http_status": http_status,
        "playlist_url": state.get("source_url") or "",
        "at": time.strftime("%Y-%m-%d %H:%M"),
    }
    ok, detail = send_notification(target, payload)
    return {"at": payload["at"], "ok": ok, "message": detail}


def alerts_status():
    """Where failure alerts go, and how the most recent attempt went."""
    state = load_state()
    target = notify_target(state)
    saved = state["notify_url"]
    return {
        "url": saved,
        "target": target,
        "enabled": bool(target),
        "source": "saved" if saved else ("environment" if target else ""),
        "limit": REFRESH_FAILURE_LIMIT,
        "last": state["last_alert"],
    }


def http_status_label(status):
    """Human label for a recorded HTTP status.

    Computed here so the dashboard renders a finished string and never has to
    pick numbers out of the refresh message.
    """
    if status == 304:
        return "not modified (304)"
    if status == 200:
        return "transferred (200)"
    if isinstance(status, int) and not isinstance(status, bool):
        return "HTTP %d" % status
    return "no response"


def refresh_status():
    """State of the scheduled refresh, for the dashboard and /api/status.

    `last` is what this box recorded on its previous run (so a failure is still
    visible after a reboot), `next_run` comes from systemd and is None when the
    timer is not reachable. `failures` counts failed runs in a row and
    `warning` is the sentence to show once that streak is long enough to be a
    real outage rather than a blip.
    """
    state = load_state()
    failures = state["failures"]
    warning = ""
    if failures >= REFRESH_FAILURE_LIMIT:
        warning = ("%d refreshes in a row failed \u2014 check the playlist URL "
                   "and whether the provider still answers" % failures)
    last = state["last_refresh"]
    if last:
        last = dict(last)
        last["status_label"] = http_status_label(last.get("http_status"))
    info = {
        "timer": REFRESH_TIMER,
        "enabled": bool(state["source_url"]),
        "source_url": state["source_url"],
        "last": last,
        "failures": failures,
        "failure_limit": REFRESH_FAILURE_LIMIT,
        "warning": warning,
        "next_run": None,
        "last_trigger": None,
        "timer_active": None,
    }
    info.update(timer_info())
    return info


def record_refresh(ok, message, http_status=None):
    """Persist the outcome of a scheduled run so the UI can show it.

    `http_status` describes the download itself: 200 when a body was
    transferred, 304 when the provider had nothing new, an HTTP error code
    when the provider refused, and None when no response was obtained. The
    dashboard renders it directly instead of digging numbers out of the text.
    """
    if isinstance(http_status, bool) or not isinstance(http_status, int):
        http_status = None
    state = load_state()
    # A streak of failures is what the dashboard warns about, and any success
    # clears it.
    previous = state.get("failures") or 0
    if ok:
        state["failures"] = 0
    else:
        state["failures"] = previous + 1
    state["last_refresh"] = {
        "at": time.strftime("%Y-%m-%d %H:%M"),
        "ok": bool(ok),
        "message": to_text(message or ""),
        "http_status": http_status,
    }
    # Edge-triggered: exactly one alert per streak, when it first reaches the
    # limit. A week-long outage sends one message, not fourteen.
    if (not ok and previous < REFRESH_FAILURE_LIMIT
            and state["failures"] >= REFRESH_FAILURE_LIMIT):
        alert = send_failure_alert(state, state["last_refresh"]["message"],
                                   http_status)
        if alert is not None:
            state["last_alert"] = alert
            if not alert["ok"]:
                log_error("alert", "could not deliver the failure alert",
                          alert["message"])
    save_state(state)


def restart_kodi():
    """Full service restart; returns (ok, message)."""
    if OFFLINE:
        return True, "systemctl skipped (IPTV_NO_SYSTEMD=1)"
    returncode, output = run_cmd(["systemctl", "restart", KODI_UNIT], timeout=90)
    if returncode != 0:
        detail = output or ("exit status %d" % returncode)
        return False, "could not restart Kodi: %s" % detail
    time.sleep(1.0)
    return True, "Kodi restarted (state: %s)" % kodi_state()


def reload_kodi():
    """Soft-reload the PVR client through JSON-RPC, restart only if needed."""
    if OFFLINE:
        return True, "reload skipped (IPTV_NO_SYSTEMD=1)"

    ok, detail = jsonrpc("Addons.SetAddonEnabled",
                         {"addonid": ADDON, "enabled": False})
    if not ok:
        restarted, message = restart_kodi()
        if restarted:
            return True, "%s (fallback: %s)" % (message, detail)
        return False, message

    time.sleep(1.0)
    ok, detail = jsonrpc("Addons.SetAddonEnabled",
                         {"addonid": ADDON, "enabled": True})
    if not ok:
        restarted, message = restart_kodi()
        if restarted:
            return True, "%s (fallback: %s)" % (message, detail)
        return False, message
    return True, "playlist reloaded through Kodi JSON-RPC"


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

def dashboard_context():
    state, groups, selected = active_state_groups()
    return {
        "stats": playlist_stats(),
        "kodi": kodi_state(),
        "hardware": hardware_stats(),
        "refresh": refresh_status(),
        "groups": groups,
        "selected": selected,
        "epg_url": state["epg_url"],
        "alerts": alerts_status(),
        "health": cached_health(),
        "logs": log_status(40),
        "update": update_status(),
        "rauc": rauc_status(),
        "limit": MAX_BYTES,
        "unit": KODI_UNIT,
    }


@app.route("/")
def index():
    return render_template("index.html", **dashboard_context())


@app.route("/api/status")
def api_status():
    state, groups, selected = active_state_groups()
    return jsonify({
        "ok": True,
        "playlist": playlist_stats(),
        "kodi": kodi_state(),
        "refresh": refresh_status(),
        "groups": groups,
        "selected": selected,
        "epg_url": state["epg_url"],
        "alerts": alerts_status(),
        "update": update_status(),
    })


@app.route("/api/notify", methods=["POST"])
def api_notify():
    """Save (or clear) the URL that failure alerts are POSTed to."""
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        url = payload.get("url", "")
    else:
        url = request.form.get("url", "")
    url = to_text(url or "").strip()
    lowered = url.lower()
    if url and not (lowered.startswith("http://") or lowered.startswith("https://")):
        return jsonify({
            "ok": False,
            "message": "the alert URL must start with http:// or https://",
        }), 400

    state = load_state()
    if url != state["notify_url"]:
        # Another endpoint has not been sent anything yet.
        state["last_alert"] = None
    state["notify_url"] = url
    save_state(state)

    message = ("alerts will be sent to %s" % url) if url else "alerts disabled"
    return jsonify({"ok": True, "message": message, "notify_url": url,
                    "alerts": alerts_status()})


@app.route("/api/health")
def api_health():
    """System state below the app; ?fresh=1 bypasses the short cache."""
    payload = cached_health(fresh=request.args.get("fresh") == "1")
    return jsonify({"ok": True, "health": payload})


@app.route("/api/logs")
def api_logs():
    """Recent error and hardware log lines."""
    try:
        count = max(1, min(200, int(request.args.get("lines", "40"))))
    except (TypeError, ValueError):
        count = 40
    payload = log_status(count)
    payload["ok"] = True
    return jsonify(payload)


@app.route("/api/update", methods=["POST"])
def api_update():
    """Install an app update bundle (.tar.gz of app.py/templates/static)."""
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"ok": False, "message": "no update file was received"}), 400

    spooled = None
    try:
        stream = getattr(upload, "stream", None) or upload
        spooled, _size = spool_chunks(read_chunks(stream), UPDATE_MAX_BYTES)
        version = apply_update(spooled)
    except ValueError as exc:
        log_error("update", "update rejected", exc)
        return jsonify({"ok": False, "message": str(exc)}), 400
    except (IOError, OSError) as exc:
        log_error("update", "could not install the update", exc)
        return jsonify({
            "ok": False,
            "message": "could not install the update: %s" % exc,
        }), 500
    finally:
        if spooled and os.path.exists(spooled):
            try:
                os.unlink(spooled)
            except OSError:
                pass

    message = "version %s installed" % version
    if OFFLINE:
        message = "%s; restart the web UI to use it (systemd disabled)" % message
    else:
        message = "%s; %s" % (message, schedule_webui_restart())
    return jsonify({"ok": True, "message": message, "version": version,
                    "update": update_status()})


@app.route("/api/rauc")
def api_rauc():
    """A/B slots, bootloader state and the progress of a running install."""
    return jsonify({"ok": True, "rauc": rauc_status()})


def want_reboot():
    """Whether the install should reboot the box when it finished."""
    value = to_text(request.form.get("reboot", "")).strip().lower()
    return value in ("1", "true", "on", "yes")


@app.route("/api/rauc/install", methods=["POST"])
def api_rauc_install():
    """Accept a .raucb bundle and let RAUC write it to the inactive slot.

    The upload is capped and refused when the data partition cannot hold it;
    the install itself runs in the background because writing a slot takes
    minutes and the browser must not hang on it.
    """
    if not rauc_available():
        return jsonify({
            "ok": False,
            "message": "rauc is not installed in this image",
        }), 400
    if RAUC_INSTALLER.busy():
        return jsonify({
            "ok": False,
            "message": "an update is already running",
        }), 409

    # Optional: the checksum printed next to the bundle on the build host. When
    # it is given, a bundle that does not match is never handed to RAUC.
    try:
        expected = wanted_checksum()
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400

    # A bundle can also be fetched from a server in the LAN: the box downloads
    # it itself, which is the only practical way for a few hundred MB and
    # works even when RAUC was built without its own network support.
    url = to_text(request.form.get("url", "")).strip()
    if url:
        lowered = url.lower()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            return jsonify({
                "ok": False,
                "message": "the bundle URL must start with http:// or https://",
            }), 400
        name = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        if not name.lower().endswith(".raucb"):
            return jsonify({
                "ok": False,
                "message": "the URL has to point at a .raucb bundle",
            }), 400
        try:
            RAUC_INSTALLER.start_url(url, reboot=want_reboot(), expected=expected)
        except RaucRefused as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 409
        return jsonify({
            "ok": True,
            "message": "the box is downloading %s and will install it" % name,
            "rauc": rauc_status(),
        }), 202

    upload = request.files.get("bundle") or request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"ok": False, "message": "no bundle was received"}), 400

    name = os.path.basename(upload.filename.replace("\\", "/"))
    if not name.lower().endswith(".raucb"):
        return jsonify({
            "ok": False,
            "message": "a system update has to be a .raucb bundle",
        }), 400

    partial = os.path.join(RAUC_BUNDLE_DIR, name + ".part")
    written = 0
    try:
        ensure_dir(RAUC_BUNDLE_DIR)
        free = free_bytes(RAUC_BUNDLE_DIR)
        stream = getattr(upload, "stream", None) or upload
        handle = open(partial, "wb")
        try:
            for chunk in read_chunks(stream):
                written += len(chunk)
                if written > RAUC_MAX_BYTES:
                    raise ValueError(
                        "the bundle is larger than %d MB"
                        % (RAUC_MAX_BYTES // 1048576))
                if free is not None and written + RAUC_FREE_HEADROOM > free:
                    raise ValueError(
                        "not enough free space in %s for a %d MB bundle"
                        % (RAUC_BUNDLE_DIR, RAUC_MAX_BYTES // 1048576))
                handle.write(chunk)
        finally:
            handle.close()
        if written == 0:
            raise ValueError("the uploaded bundle is empty")
        target = os.path.join(RAUC_BUNDLE_DIR, name)
        os.rename(partial, target)
        mismatch = checksum_mismatch(expected, sha256_file(target), name)
        if mismatch:
            log_error("rauc", "upload rejected", mismatch)
            try:
                os.unlink(target)
            except OSError:
                pass
            raise RaucRefused(mismatch)
    except ValueError as exc:
        log_error("rauc", "bundle rejected", exc)
        try:
            os.unlink(partial)
        except OSError:
            pass
        return jsonify({"ok": False, "message": str(exc)}), 400
    except (IOError, OSError) as exc:
        log_error("rauc", "could not store the bundle", exc)
        try:
            os.unlink(partial)
        except OSError:
            pass
        return jsonify({
            "ok": False,
            "message": "could not store the bundle: %s" % exc,
        }), 500

    try:
        RAUC_INSTALLER.start(target, reboot=want_reboot(), expected=expected)
    except RaucRefused as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 409

    return jsonify({
        "ok": True,
        "message": ("%s (%d MB) received, install started; the progress below "
                    "comes from rauc itself" % (name, written // 1048576)),
        "rauc": rauc_status(),
    }), 202


@app.route("/api/rauc/reboot", methods=["POST"])
def api_rauc_reboot():
    """Reboot on request, so an install without the auto-reboot still helps.

    Refused while RAUC is writing a slot (a reboot in the middle of that is the
    one thing that can leave a half-written system) and when systemd is not
    there to do it. The browser asks for confirmation first.
    """
    if RAUC_INSTALLER.busy():
        return jsonify({
            "ok": False,
            "message": "an install is still running, wait for it to finish",
        }), 409
    if OFFLINE:
        return jsonify({
            "ok": False,
            "message": "rebooting is disabled (IPTV_NO_SYSTEMD=1)",
        }), 400

    payload = request.get_json(silent=True) or {} if request.is_json else {}
    confirm = payload.get("confirm") or request.form.get("confirm", "")
    if to_text(confirm or "").strip().lower() not in ("1", "true", "yes", "on"):
        return jsonify({
            "ok": False,
            "message": "the reboot has to be confirmed",
        }), 400

    booted, primary, _slots = rauc_slot_state()
    target = primary or booted
    hint = schedule_reboot(RAUC_MANUAL_REBOOT_DELAY)
    message = hint
    if target:
        message = "%s; the box comes back on slot %s" % (hint, target)
    return jsonify({"ok": True, "message": message, "rauc": rauc_status()})


@app.route("/api/stats")
def api_stats():
    payload = hardware_stats()
    payload["ok"] = True
    return jsonify(payload)


@app.route("/api/playlist", methods=["POST"])
def api_playlist():
    mode = request.form.get("mode", "")
    want_reload = request.form.get("restart", "1") == "1"
    spooled = None

    try:
        if mode == "file":
            upload = request.files.get("file")
            if upload is None or not upload.filename:
                raise ValueError("no file was received")
            source = upload.filename
            # Werkzeug spools the request body to disk; read it in chunks.
            stream = getattr(upload, "stream", None) or upload
            spooled, _size = spool_chunks(read_chunks(stream), MAX_BYTES)
        elif mode == "paste":
            spooled, _size = spool_chunks(
                text_chunks(request.form.get("content", "")), MAX_BYTES)
            source = "pasted text"
        elif mode == "url":
            url = (request.form.get("url") or "").strip()
            if not url:
                raise ValueError("no URL given")
            source = url
            # An interactive save always fetches the body (no conditional
            # GET), but remembers the validators for the scheduled refresh.
            spooled, fetched = download_to_spool(url)
            if spooled is None:
                raise ValueError("the provider refused the download (HTTP 304)")
        else:
            raise ValueError("unknown upload mode %r" % mode)

        groups, selected, channels = store_playlist_from_file(spooled)
        spooled = None
        # A URL save arms the scheduled refresh; file/paste disarms it.
        set_source_url(source if mode == "url" else "")
        # Then remember the validators: save_state keeps exactly the entry for
        # the current source URL, so this order is what makes that stick.
        if mode == "url":
            remember_validators(source, fetched)
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except (IOError, OSError) as exc:
        return jsonify({
            "ok": False,
            "message": "could not store the playlist: %s" % exc,
        }), 500
    finally:
        if spooled and os.path.exists(spooled):
            try:
                os.unlink(spooled)
            except OSError:
                pass

    message = "playlist saved from %s (%d channels in %d groups)" % (
        source, channels, len(groups))

    ok = True
    if want_reload:
        reloaded, reload_message = reload_kodi()
        message = "%s; %s" % (message, reload_message)
        ok = reloaded

    payload = {
        "ok": ok,
        "message": message,
        "playlist": playlist_stats(),
        "groups": groups,
        "selected": selected,
        "kodi": kodi_state(),
    }
    return jsonify(payload), (200 if ok else 500)


@app.route("/api/groups", methods=["GET", "POST"])
def api_groups():
    if request.method == "GET":
        state, groups, selected = active_state_groups()
        return jsonify({"ok": True, "groups": groups, "selected": selected})

    if request.is_json:
        payload = request.get_json(silent=True) or {}
        requested = payload.get("groups", [])
    else:
        requested = request.form.getlist("groups")
    if not isinstance(requested, list):
        return jsonify({"ok": False, "message": "expected a list of groups"}), 400
    requested = [to_text(name) for name in requested]

    try:
        groups, selected, channels = apply_groups(requested)
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except (IOError, OSError) as exc:
        return jsonify({
            "ok": False,
            "message": "could not write %s: %s" % (PLAYLIST, exc),
        }), 500

    reloaded, reload_message = reload_kodi()
    message = "active playlist now has %d channels from %d/%d groups; %s" % (
        channels, len(selected), len(groups), reload_message)
    payload = {
        "ok": reloaded,
        "message": message,
        "playlist": playlist_stats(),
        "groups": groups,
        "selected": selected,
        "kodi": kodi_state(),
    }
    return jsonify(payload), (200 if reloaded else 500)


@app.route("/api/epg", methods=["POST"])
def api_epg():
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        url = payload.get("url", "")
    else:
        url = request.form.get("url", "")

    try:
        stored = set_epg(url)
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except (IOError, OSError) as exc:
        return jsonify({
            "ok": False,
            "message": "could not write the add-on settings: %s" % exc,
        }), 500

    reloaded, reload_message = reload_kodi()
    if stored:
        message = "EPG URL set to %s; %s" % (stored, reload_message)
    else:
        message = "EPG URL cleared; %s" % reload_message
    return jsonify({
        "ok": reloaded,
        "message": message,
        "epg_url": stored,
    }), (200 if reloaded else 500)


@app.route("/api/restart", methods=["POST"])
def api_restart():
    restarted, message = restart_kodi()
    payload = {"ok": restarted, "message": message, "kodi": kodi_state()}
    return jsonify(payload), (200 if restarted else 500)


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Run the scheduled refresh right now (the "Refresh now" button).

    Same code path as the timer, but synchronous so the browser gets the
    outcome immediately instead of waiting for the next status poll.
    """
    if not load_state()["source_url"]:
        return jsonify({
            "ok": False,
            "message": "save a playlist URL first, there is nothing to refresh",
        }), 400

    result = perform_refresh()
    _state, groups, selected = active_state_groups()
    payload = {
        "ok": result["ok"],
        "unchanged": result.get("unchanged", False),
        "http_status": result.get("http_status"),
        "message": result["message"],
        "playlist": playlist_stats(),
        "refresh": refresh_status(),
        "groups": groups,
        "selected": selected,
        "kodi": kodi_state(),
    }
    return jsonify(payload), (200 if result["ok"] else 500)


@app.route("/playlist.m3u")
def download_playlist():
    if not os.path.isfile(PLAYLIST):
        return "no playlist available yet", 404
    # Streamed to the client in 64 KB chunks: a big playlist never has to be
    # buffered in the box's RAM just to be downloaded.
    response = app.response_class(file_chunks(PLAYLIST),
                                  mimetype="audio/x-mpegurl")
    response.headers["Content-Disposition"] = \
        "attachment; filename=playlist.m3u"
    return response


@app.errorhandler(500)
def server_error(error):
    """Log unhandled errors instead of only letting them reach stderr."""
    log_error("webui", "unhandled error on %s" % request.path,
              traceback.format_exc().strip().splitlines()[-1]
              if traceback.format_exc().strip() else error)
    if request.path.startswith("/api/"):
        return jsonify({"ok": False,
                        "message": "internal error, see the error log"}), 500
    return "internal error, see the error log", 500


@app.errorhandler(413)
def too_large(_error):
    return jsonify({
        "ok": False,
        "message": ("the upload is larger than the %d byte limit"
                    % app.config["MAX_CONTENT_LENGTH"]),
    }), 413


@app.errorhandler(404)
def not_found(_error):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "message": "not found"}), 404
    return "not found", 404


def perform_refresh():
    """Re-download the saved URL, keep the group filter, reload Kodi.

    Shared by the scheduled timer (--refresh) and the "Refresh now" button,
    so a manual run does exactly what the timer does and records the same
    result. Returns {"ok", "summary", "message"}.
    """
    url = load_state()["source_url"]
    if not url:
        return {"ok": False, "summary": "", "http_status": None,
                "message": "no playlist URL saved yet"}

    validators = stored_validators(load_state(), url)

    status = None
    spooled = None
    try:
        spooled, meta = download_to_spool(url, validators)
        status = meta.get("status")
        have_files = (os.path.isfile(FULL_PLAYLIST)
                      and os.path.isfile(PLAYLIST)
                      and os.path.getsize(PLAYLIST) > 0)

        if meta["not_modified"] and have_files:
            # Conditional GET answered with 304: not one playlist byte was
            # transferred, so there is certainly nothing to rewrite.
            message = ("playlist unchanged: provider reports no change "
                       "(HTTP 304), Kodi not reloaded")
            record_refresh(True, message, 304)
            return {"ok": True, "summary": message, "message": message,
                    "unchanged": True, "http_status": 304}
        if meta["not_modified"]:
            # Local playlist files are gone, so the body is needed: ask again
            # without validators.
            spooled, meta = download_to_spool(url, {})
            status = meta.get("status")

        # Nothing to do when the provider still serves the same channels: the
        # fingerprint ignores the M3U header and the channel order, so a
        # reshuffled list neither rewrites the SD card files nor reloads Kodi.
        # Both files are streamed, so this costs no RAM on a big playlist.
        if (have_files
                and playlist_fingerprint(spooled)
                == playlist_fingerprint(FULL_PLAYLIST)):
            cached = scan_playlist(spooled)
            message = ("playlist unchanged: %d channels in %d groups, "
                       "Kodi not reloaded"
                       % (cached["channels"], len(cached["groups"])))
            remember_validators(url, meta)
            record_refresh(True, message, status)
            return {"ok": True, "summary": message, "message": message,
                    "unchanged": True, "http_status": status}

        groups, selected, channels = store_playlist_from_file(
            spooled, keep_selection=True)
        spooled = None
        remember_validators(url, meta)
    except FetchError as exc:
        # The download itself failed: keep the status the provider answered
        # (None when the box never got a response at all).
        message = "refresh failed: %s" % exc
        log_error("refresh", message)
        record_refresh(False, message, exc.status)
        return {"ok": False, "summary": "", "message": message,
                "http_status": exc.status}
    except ValueError as exc:
        message = "refresh failed: %s" % exc
        log_error("refresh", message)
        record_refresh(False, message, status)
        return {"ok": False, "summary": "", "message": message,
                "http_status": status}
    except (IOError, OSError) as exc:
        message = "refresh failed: could not store the playlist: %s" % exc
        log_error("refresh", message)
        record_refresh(False, message, status)
        return {"ok": False, "summary": "", "message": message,
                "http_status": status}
    finally:
        if spooled and os.path.exists(spooled):
            try:
                os.unlink(spooled)
            except OSError:
                pass

    summary = "playlist updated: %d channels in %d groups, %d activated" % (
        sum(group["channels"] for group in groups), len(groups), channels)
    ok, detail = reload_kodi()
    # On a Kodi failure the reload message is the interesting one; it is shown
    # instead of the (already applied) playlist summary.
    if not ok:
        log_error("kodi", "could not reload Kodi", detail)
    record_refresh(ok, summary if ok else detail, status)
    if ok:
        return {"ok": True, "summary": summary, "http_status": status,
                "message": "%s; %s" % (summary, detail)}
    return {"ok": False, "summary": summary, "message": detail,
            "http_status": status}


def refresh_main():
    """Headless refresh for the scheduled systemd timer.

    Returns a process exit status so `systemctl status` shows failures: 0 when
    there was nothing to do or the playlist was updated and Kodi reloaded.
    """
    url = load_state()["source_url"]
    if not url:
        print("no playlist URL configured, nothing to refresh")
        return 0

    print("refreshing the playlist from %s" % url)
    result = perform_refresh()
    print(result["message"])
    return 0 if result["ok"] else 1


def sample_main():
    """Headless hardware sample for the systemd timer."""
    written, line = sample_hardware()
    print(line)
    if not written:
        print("could not write %s" % HARDWARE_LOG, file=sys.stderr)
        return 1
    return 0


def main():
    arguments = sys.argv[1:]
    if "--refresh" in arguments:
        return refresh_main()
    if "--sample" in arguments:
        return sample_main()
    if "--health" in arguments:
        return health_main()
    if "--alert-unit" in arguments:
        position = arguments.index("--alert-unit")
        return alert_unit(arguments[position + 1]
                          if len(arguments) > position + 1 else "")
    if "--version" in arguments:
        print(APP_VERSION)
        return 0

    # One persistent line per boot when something below the app is wrong, so a
    # failure survives the volatile /var/log.
    health_main()

    print("IPTV-Pi web UI on http://%s:%d/ (playlist: %s)"
          % (BIND_ADDR, BIND_PORT, PLAYLIST))
    if not os.path.isdir(os.path.dirname(PLAYLIST) or "."):
        print("warning: %s does not exist yet" % os.path.dirname(PLAYLIST))
    app.run(host=BIND_ADDR, port=BIND_PORT, threaded=True, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
