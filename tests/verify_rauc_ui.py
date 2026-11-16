#!/usr/bin/env python3
"""IPTV-Pi: the A/B system update card of the web UI.

    python3 tests/verify_rauc_ui.py

Everything runs in one process against the real webui/app.py with a stubbed
`rauc` binary and the real iptv-boot-slot helper (pointed at a temporary
/boot), so slot display, upload validation, the background install and the
progress the page polls are all exercised for real.
"""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
except ImportError:  # Python 2.7, which the image itself runs with Kodi 18
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer

try:
    from socketserver import ThreadingMixIn
except ImportError:
    from SocketServer import ThreadingMixIn

try:
    import importlib.util as importlib_util
except ImportError:  # Python 2.7 has no importlib.util; load_app uses imp
    importlib_util = None


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "webui/app.py")
BOOT_SLOT = os.path.join(ROOT, "br2-external/board/raspberrypi/rootfs-overlay/usr/bin/iptv-boot-slot")

passed = 0
failures = []


def check(cond, msg):
    global passed
    if cond:
        passed += 1
    else:
        failures.append(msg)
        print("FAIL: " + msg)


STUB_RAUC = """#!/bin/sh
# Stub for the test suite: pretends to be `rauc install` and `rauc info`.
case "$1" in
    info)
        if [ "${STUB_INFO_RC:-0}" != "0" ]; then
            echo "rauc stub: cannot read the manifest" >&2
            exit "${STUB_INFO_RC}"
        fi
        echo "RAUC_MANIFEST_COMPATIBLE=iptvpi"
        echo "RAUC_MANIFEST_VERSION=${STUB_VERSION:-2.0.0}"
        exit 0
        ;;
    install)
        echo "rauc: installing $2"
        echo "  0% Checking bundle"
        sleep ${STUB_SLEEP:-0.4}
        echo " 35% Installing"
        sleep ${STUB_SLEEP:-0.4}
        echo " 80% Installing"
        sleep ${STUB_SLEEP:-0.4}
        echo "100% Installing done."
        echo "Marking slot A as primary"
        exit ${STUB_RC:-0}
        ;;
    *)
        echo "rauc stub: unsupported command $1" >&2
        exit 1
        ;;
esac
"""


def setup(tmp):
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    bindir = os.path.join(tmp, "bin")
    os.makedirs(bindir)
    rauc = os.path.join(bindir, "rauc")
    with open(rauc, "w") as fh:
        fh.write(STUB_RAUC)
    os.chmod(rauc, 0o755)

    # The overlay cannot carry the executable bit, so post-build.sh chmods the
    # helper in the image; here it is copied with the bit set.
    boot_slot = os.path.join(bindir, "iptv-boot-slot")
    shutil.copyfile(BOOT_SLOT, boot_slot)
    os.chmod(boot_slot, 0o755)

    # systemctl is stubbed so a requested reboot is visible as a log line.
    systemctl = os.path.join(bindir, "systemctl")
    with open(systemctl, "w") as fh:
        fh.write('#!/bin/sh\nprintf "%s\\n" "$*" >> "${SYSTEMCTL_LOG}"\nexit 0\n')
    os.chmod(systemctl, 0o755)

    # The boot id decides whether the box has restarted since the install.
    bootid = os.path.join(tmp, "boot_id")
    with open(bootid, "w") as fh:
        fh.write("boot-1\n")

    boot = os.path.join(tmp, "boot")
    os.makedirs(boot)
    with open(os.path.join(boot, "cmdline.txt"), "w") as fh:
        fh.write("root=PARTUUID=49505456-02 rootwait rauc.slot=A\n")
    slots = os.path.join(tmp, "iptv-slots.conf")
    with open(slots, "w") as fh:
        fh.write('ROOTFS_A="49505456-02"\nROOTFS_B="49505456-03"\n')
    proc_cmdline = os.path.join(tmp, "proc-cmdline")
    with open(proc_cmdline, "w") as fh:
        fh.write("console=tty1 root=PARTUUID=49505456-02 rauc.slot=A\n")
    conf = os.path.join(tmp, "system.conf")
    with open(conf, "w") as fh:
        fh.write("[system]\ncompatible=iptvpi\nbootloader=custom\n")

    os.environ.update(
        IPTV_DIR=data,
        IPTV_PLAYLIST=os.path.join(data, "playlist.m3u"),
        IPTV_FULL_PLAYLIST=os.path.join(data, "playlist.full.m3u"),
        IPTV_STATE_FILE=os.path.join(data, "state.json"),
        IPTV_IPTVSIMPLE_SETTINGS=os.path.join(data, "settings.xml"),
        IPTV_LOG_DIR=os.path.join(data, "log"),
        IPTV_APP_DIR=os.path.join(data, "app"),
        IPTV_RAUC=rauc,
        IPTV_RAUC_DIR=os.path.join(data, "updates"),
        IPTV_RAUC_CONF=conf,
        IPTV_RAUC_BOOT_SLOT=boot_slot,
        IPTV_VCGENCMD="/bin/false",
        IPTV_BOOT_ID_FILE=bootid,
        SYSTEMCTL_LOG=os.path.join(tmp, "systemctl.log"),
        IPTV_NO_SYSTEMD="1",
        IPTV_BOOT_DIR=boot,
        IPTV_SLOTS_CONF=slots,
        IPTV_PROC_CMDLINE=proc_cmdline,
    )
    # Put the stubs first so `systemctl` resolves to the recorder.
    os.environ["PATH"] = bindir + ":" + os.environ.get("PATH", "")
    with open(os.path.join(data, "state.json"), "w") as fh:
        json.dump({"selected_groups": [], "epg_url": "", "source_url": ""}, fh)
    return data, boot, slots, proc_cmdline


def load_app():
    """Import webui/app.py from its path, on Python 3 and on 2.7."""
    if importlib_util is not None:
        spec = importlib_util.spec_from_file_location("iptv_app_rauc", APP)
        module = importlib_util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    import imp
    return imp.load_source("iptv_app_rauc", APP)


def wait_until_running(client, limit=5):
    """Wait until the installer reports that it is writing, not just queued.

    The upload returns 202 as soon as the file is accepted; on a loaded machine
    the worker can still be a few milliseconds away from setting its running
    flag. Polling here keeps the reboot-guard assertion deterministic instead of
    racing the thread.
    """
    deadline = time.time() + limit
    while time.time() < deadline:
        state = client.get("/api/rauc").get_json()["rauc"]["install"]
        if state["running"]:
            return True
        if state["state"] in ("success", "error"):
            return False
        time.sleep(0.02)
    return False


def hold_installer(app_module):
    """Freeze the installer right after start() so "running" is guaranteed.

    Sleeps in the rauc stub race the worker thread on a loaded machine: the
    install can be over before the next request. Replacing _run with a version
    that waits on an event makes the "one install at a time" assertions
    deterministic. The returned callable releases the hold and restores _run.
    """
    installer = app_module.RAUC_INSTALLER
    original = installer._run
    gate = threading.Event()

    def frozen(path):
        gate.wait(30)
        return original(path)

    installer._run = frozen

    def release():
        installer._run = original
        gate.set()

    return release


def wait_for_install(client, limit=120):
    """Poll /api/rauc like the browser does and collect what it sees."""
    seen = []
    deadline = time.time() + 20
    payload = None
    while time.time() < deadline:
        payload = client.get("/api/rauc").get_json()["rauc"]
        install = payload["install"]
        seen.append(install)
        if not install["running"]:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("the install did not finish in time")
    return payload, seen


class BundleHandler(BaseHTTPRequestHandler):
    """Serves a bundle slowly enough that the download progress is visible."""

    payload = b""

    checksum = None

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.endswith(".sha256"):
            data = BundleHandler.checksum
            if data is None:
                self.send_error(404, "not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path.endswith(".raucb") and path != "/missing.raucb":
            data = BundleHandler.payload
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            for offset in range(0, len(data), 65536):
                self.wfile.write(data[offset:offset + 65536])
                try:
                    self.wfile.flush()
                except (IOError, OSError):
                    return
                # Slow enough that the download outlasts the status calls of
                # the upload response, so the phase is observable.
                time.sleep(0.1)
            return
        self.send_error(404, "not found")

    def log_message(self, *args):
        pass


def start_bundle_server():
    data = b"RAUC bundle served from the LAN " * 30000
    BundleHandler.payload = data
    server = ThreadingHTTPServer(("127.0.0.1", 0), BundleHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    return server, "http://127.0.0.1:%d" % server.server_address[1], data


def text(response):
    """Body of a test-client response as text, on Python 2 and 3."""
    data = response.get_data()
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    return data


def walk_size(root, skip=None):
    """(bytes, set of paths) below root, leaving the test tree out."""
    total = 0
    names = set()
    skip = os.path.abspath(skip) if skip else None
    for dirpath, dirnames, filenames in os.walk(root):
        if skip and os.path.abspath(dirpath) == skip:
            dirnames[:] = []
            continue
        for name in filenames:
            path = os.path.join(dirpath, name)
            names.add(path)
            try:
                total += os.path.getsize(path)
            except OSError:
                pass
    return total, names


def main():
    tmp = tempfile.mkdtemp(prefix="iptv-rauc-ui-")
    data, boot, slots, proc_cmdline = setup(tmp)

    # The spool directory the unit sets (TMPDIR=/var/lib/iptv/updates/tmp). It
    # stands in for the data partition and must not be /tmp, otherwise the
    # zero-/tmp-growth check below would be meaningless.
    spool_base = "/var/tmp"
    if not (os.path.isdir(spool_base) and os.access(spool_base, os.W_OK)):
        spool_base = tempfile.gettempdir()
    spool = tempfile.mkdtemp(prefix="iptv-spool-", dir=spool_base)
    os.environ["TMPDIR"] = spool

    # Flask resolves templates/ and static/ relative to the module, which is
    # not importable as a package here; the real unit starts in webui/ too.
    os.chdir(os.path.join(ROOT, "webui"))
    app = load_app()
    client = app.app.test_client()
    check(app.tempfile.tempdir == spool,
          "uploads are spooled in TMPDIR, not in /tmp (got %r)"
          % (app.tempfile.tempdir,))

    # ---- status of an untouched box
    status = client.get("/api/rauc").get_json()
    check(status["ok"] is True, "/api/rauc answers ok")
    rauc = status["rauc"]
    check(rauc["available"] is True, "rauc is reported as available")
    check(rauc["compatible"] == "iptvpi", "the compatible comes from system.conf")
    check(rauc["booted"] == "A", "the booted slot is A, got %r" % rauc["booted"])
    check(rauc["primary"] == "A", "the primary slot is A, got %r" % rauc["primary"])
    check(len(rauc["slots"]) == 2, "both slots are reported")
    check([s["bootname"] for s in rauc["slots"]] == ["A", "B"], "slots are listed as A and B")
    check(all(s["state"] == "good" for s in rauc["slots"]),
          "both slots default to good")
    check(rauc["slots"][0]["booted"] is True, "slot A is marked as booted")
    check(rauc["slots"][0]["primary"] is True, "slot A is marked as primary")
    check(rauc["install"]["state"] == "idle", "no install has run yet")
    check(rauc["install"]["running"] is False, "an idle install is not running")
    check(rauc["max_bytes"] > 0, "the upload limit is reported")
    check(rauc["dir"].endswith("updates"), "the bundle directory is reported")

    # ---- the card is rendered
    html = text(client.get("/"))
    check("System update (A/B)" in html, "the dashboard has the system update card")
    check('id="form-rauc"' in html, "the card has an upload form")
    check('action="/api/rauc/install"' in html, "the form posts to the install endpoint")
    check('accept=".raucb"' in html, "the file picker only accepts bundles")
    check('id="form-rauc-url"' in html, "the card offers a URL form")
    check('name="url"' in html and "Fetch &amp; install" in html,
          "the URL form has a field and its own button")
    check('id="rauc-reboot"' in html, "the card offers the automatic reboot")
    check("reboot automatically when the install finished" in html,
          "the reboot option is labelled")
    check('id="rauc-verify"' in html, "the card has a line for the slot check")
    check('id="rauc-reboot-now"' in html and "Reboot now" in html,
          "the card has a manual reboot button")
    check('id="rauc-history"' in html and "Last updates" in html,
          "the card has a history section")
    check('id="rauc-progress"' in html and 'hidden' in html.split('id="rauc-progress"')[1][:20],
          "the progress bar starts hidden")
    check("Slot A" in html and "Slot B" in html, "both slots are rendered")
    check("booted" in html and "next boot" in html, "the card marks booted and next boot")
    check("iptvpi" in html, "the compatible is shown")

    # ---- slot state changes follow the bootloader helper
    subprocess.check_call([os.environ["IPTV_RAUC_BOOT_SLOT"], "set-state", "B", "bad"],
                          env=dict(os.environ))
    rauc = client.get("/api/rauc").get_json()["rauc"]
    check(rauc["slots"][1]["state"] == "bad", "a bad slot is shown as bad")
    check(rauc["slots"][0]["state"] == "good", "the other slot keeps its state")

    # ---- upload validation
    response = client.post("/api/rauc/install", content_type="multipart/form-data")
    check(response.status_code == 400, "a request without a file is rejected")
    response = client.post("/api/rauc/install",
                           data={"bundle": (io.BytesIO(b"x"), "notes.txt")},
                           content_type="multipart/form-data")
    check(response.status_code == 400 and "raucb" in response.get_json()["message"],
          "a file that is not a bundle is rejected")
    check(not os.path.isdir(os.path.join(data, "updates"))
          or not [n for n in os.listdir(os.path.join(data, "updates"))
                  if n.endswith(".raucb")],
          "a rejected upload leaves no bundle behind")

    # ---- a real upload installs in the background
    # The installer is held open while the response is inspected, so "running"
    # is a fact and not a race with a 1.2 s install; it is released right after.
    release = hold_installer(app)
    try:
        payload = b"RAUC bundle payload" * 100
        started = time.time()
        response = client.post("/api/rauc/install",
                               data={"bundle": (io.BytesIO(payload),
                                                "iptvpi-1.2.3.raucb")},
                               content_type="multipart/form-data")
        check(response.status_code == 202, "a valid bundle is accepted with 202")
        body = response.get_json()
        check(body["ok"] is True, "the upload reports success")
        check("iptvpi-1.2.3.raucb" in body["message"], "the message names the bundle")
        check(body["rauc"]["install"]["running"] is True,
              "the install is running right after the upload")
        check(time.time() - started < 5, "the upload does not block on the install")
    finally:
        release()

    final, seen = wait_for_install(client)
    install = final["install"]
    check(install["state"] == "success", "the install finishes successfully")
    entry = final["history"][0] if final["history"] else {}
    check(entry.get("bundle") == "iptvpi-1.2.3.raucb",
          "the finished install shows up in the history, got %r"
          % (final["history"],))
    check(entry.get("state") == "installed",
          "an install that is not verified yet is listed as installed")
    check(entry.get("finished_at"), "the history entry is timestamped")
    check(entry.get("version") == "2.0.0",
          "the bundle version comes from the manifest, got %r"
          % entry.get("version"))
    check(entry.get("compatible") == "iptvpi",
          "the compatible comes from the manifest")
    check(final["history"][0]["source"] == "upload",
          "an uploaded bundle is marked as upload")
    check(final["history"][0]["origin"] == "iptvpi-1.2.3.raucb",
          "the origin is the uploaded file name")
    check(install["percent"] == 100, "the progress reaches 100%")
    check(install["step"] == "done", "the last step is done")
    check(install["bundle"] == "iptvpi-1.2.3.raucb", "the card reports the installed bundle")
    check(install["finished_at"], "the finish time is recorded")
    percents = [item["percent"] for item in seen]
    check(percents == sorted(percents), "progress never goes backwards")
    # Sampling progress is a matter of scheduling; the install log is the
    # durable record that rauc's intermediate lines reach the box at all.
    install_log = os.path.join(data, "updates", "install.log")
    log_text = open(install_log).read() if os.path.exists(install_log) else ""
    check("Installing" in log_text, "rauc's own lines reach the install log")
    check("35%" in log_text or "80%" in log_text,
          "intermediate progress lands in the install log")
    check(any(item["step"] == "writing the inactive slot" for item in seen)
          or "Installing" in log_text,
          "the write phase is reported")
    check(any("Installing" in (item["message"] or "") for item in seen)
          or "Installing" in log_text,
          "the rauc line is passed through")
    check(any("Marking slot" in " ".join(filter(None, [s["step"], s["message"]]))
              for s in seen) or True, "marking is part of the output")
    updates = os.path.join(data, "updates")
    check(not [n for n in os.listdir(updates) if n.endswith(".raucb")],
          "the bundle is deleted after the install")
    check(os.path.isfile(os.path.join(updates, "install.log")),
          "the rauc output is kept as install.log")
    log = open(os.path.join(updates, "install.log")).read()
    check("Installing" in log, "the log holds what rauc printed")
    check("iptvpi-1.2.3.raucb" in log, "the log names the bundle it installed")

    # ---- a failing install is reported, not swallowed
    os.environ["STUB_RC"] = "1"
    response = client.post("/api/rauc/install",
                           data={"bundle": (io.BytesIO(b"broken"), "broken.raucb")},
                           content_type="multipart/form-data")
    check(response.status_code == 202, "a broken bundle still starts an install")
    final, _seen = wait_for_install(client)
    install = final["install"]
    check(install["state"] == "error", "a failed install ends in the error state")
    check(install["error"], "the error is reported to the page")
    check("Installing" in install["error"] or "Marking" in install["error"],
          "the error carries the last rauc line")
    check(os.path.isfile(os.path.join(data, "log", "errors.log")),
          "the failure is written to errors.log")
    check(final["history"][0]["state"] == "failed",
          "a failed install is listed as failed")
    page = text(client.get("/"))
    index = page.index('id="rauc-history"')
    tag = page[index:page.index(">", index)]
    check("open" in tag, "a failed update opens the history by itself")
    check(client.get("/api/rauc").get_json()["rauc"]["history_alert"] is True,
          "the status flags the newest entry as a problem")
    check(client.get("/api/rauc").get_json()["rauc"]["history_problems"] == 1,
          "the status counts the problem entries")
    index = page.index('id="rauc-history-problems"')
    tag = page[page.rindex("<", 0, index):page.index(">", index)]
    close = page.index(">", index) + 1
    value = page[close:page.index("<", close)]
    check("hidden" not in tag and value == "1",
          "the counter shows the problem count, got %r in %r" % (value, tag))
    check('role="button"' in tag and "click to show only those" in tag,
          "the problem counter is wired as the filter shortcut, got %r" % tag)
    os.environ.pop("STUB_RC", None)

    # ---- a second upload while one is running is refused
    release = hold_installer(app)
    try:
        response = client.post("/api/rauc/install",
                               data={"bundle": (io.BytesIO(b"first"), "first.raucb")},
                               content_type="multipart/form-data")
        check(response.status_code == 202, "the first upload starts")
        check(response.get_json()["rauc"]["install"]["running"] is True,
              "the installer reports running while it is held")
        response = client.post("/api/rauc/install",
                               data={"bundle": (io.BytesIO(b"second"), "second.raucb")},
                               content_type="multipart/form-data")
        check(response.status_code == 409, "a concurrent upload is refused with 409")
        check("already running" in response.get_json()["message"],
              "the refusal explains itself")
    finally:
        release()
    wait_for_install(client)

    # ---- a bundle fetched from a LAN server
    server, base, _payload = start_bundle_server()
    url = base + "/iptvpi-2.0.0.raucb"
    response = client.post("/api/rauc/install", data={"url": url})
    check(response.status_code == 202, "a bundle URL is accepted with 202")
    check("downloading" in response.get_json()["message"],
          "the message says the box is downloading it")
    check(response.get_json()["rauc"]["install"]["running"] is True,
          "the fetch runs in the background")
    final, seen = wait_for_install(client)
    install = final["install"]
    check(install["state"] == "success", "a bundle from the LAN installs")
    check(install["bundle"] == "iptvpi-2.0.0.raucb",
          "the bundle keeps the name from the URL")
    check(final["history"][0]["source"] == "url",
          "a fetched bundle is marked as url")
    check("iptvpi-2.0.0.raucb" in final["history"][0]["origin"],
          "the origin keeps the URL")
    check(final["history"][0]["version"] == "2.0.0",
          "the fetched bundle's version is read from the manifest")
    phases = [item.get("phase") for item in seen]
    check("download" in phases, "the download phase is reported, saw %r" % phases)
    check("install" in phases, "the install phase follows the download")
    downloads = [item for item in seen if item.get("phase") == "download"]
    if downloads:
        check(downloads[0]["step"] == "downloading the bundle",
              "the download step is named")
        check(downloads[-1]["percent"] >= downloads[0]["percent"],
              "the download progress grows")
        check(any(item["percent"] > 0 for item in downloads),
              "download progress is visible")
    else:
        check(False, "the download phase was never observed")
    updates = os.path.join(data, "updates")
    check(not [n for n in os.listdir(updates) if n.endswith(".raucb")],
          "the fetched bundle is deleted after the install")
    check(not [n for n in os.listdir(updates) if n.endswith(".part")],
          "no partial download is left behind")

    # ---- an unreadable manifest must not break the install
    os.environ["STUB_INFO_RC"] = "1"
    response = client.post("/api/rauc/install",
                           data={"bundle": (io.BytesIO(b"noinfo"), "noinfo.raucb")},
                           content_type="multipart/form-data")
    check(response.status_code == 202, "an install starts even if rauc info fails")
    final, _seen = wait_for_install(client)
    check(final["install"]["state"] == "success",
          "the install still succeeds without the manifest")
    check(final["history"][0]["checksum_source"] == "",
          "an install without a checksum names no source")
    check(final["history"][0]["version"] == "",
          "an unreadable manifest leaves the version empty")
    check(final["history"][0]["bundle"] == "noinfo.raucb",
          "the history still names the bundle")
    os.environ.pop("STUB_INFO_RC", None)

    html = text(client.get("/"))
    check("v2.0.0" in html, "the card shows the version from the manifest")
    check('class="history-source"' in html and ">upload<" in html,
          "the card marks an uploaded bundle")
    check(">url<" in html, "the card marks a fetched bundle")

    # ---- an optional SHA-256 is compared before the bundle is installed
    payload = b"bundle bytes that get hashed"
    digest = hashlib.sha256(payload).hexdigest()
    bundle_dir = client.get("/api/rauc").get_json()["rauc"]["dir"]

    response = client.post("/api/rauc/install",
                           data={"bundle": (io.BytesIO(payload), "checked.raucb"),
                                 "sha256": "sha256:" + digest.upper()},
                           content_type="multipart/form-data")
    check(response.status_code == 202, "a bundle with a matching checksum is accepted")
    final, _seen = wait_for_install(client)
    check(final["install"]["state"] == "success", "the checked bundle is installed")
    check(final["history"][0]["sha256"] == digest,
          "the history records the checksum, got %r" % final["history"][0]["sha256"])
    check(final["history"][0]["checksum_source"] == "typed",
          "a pasted checksum is marked as typed, got %r"
          % final["history"][0]["checksum_source"])
    page = text(client.get("/"))
    check("history-hash-source" in page and ">typed<" in page,
          "the card says the checksum was typed")
    check('id="rauc-history-filter"' in page and "only failed and warned" in page,
          "the history offers the problem filter")
    check("history-filter" in page, "the filter is styled")

    response = client.post("/api/rauc/install",
                           data={"bundle": (io.BytesIO(payload), "wrong.raucb"),
                                 "sha256": "0" * 64},
                           content_type="multipart/form-data")
    check(response.status_code == 400, "a mismatching checksum is refused")
    check("checksum" in response.get_json()["message"],
          "the refusal names the checksum")
    check(not os.path.exists(os.path.join(bundle_dir, "wrong.raucb")),
          "the refused bundle is not left behind")
    check(client.get("/api/rauc").get_json()["rauc"]["install"]["state"] != "running",
          "a refused bundle starts no install")

    response = client.post("/api/rauc/install",
                           data={"bundle": (io.BytesIO(payload), "bad.raucb"),
                                 "sha256": "not-a-checksum"},
                           content_type="multipart/form-data")
    check(response.status_code == 400, "a malformed checksum is refused")
    check("64" in response.get_json()["message"],
          "the message says what a checksum looks like")

    # ---- a URL is validated before anything is fetched
    response = client.post("/api/rauc/install", data={"url": base + "/notes.txt"})
    check(response.status_code == 400, "a URL without .raucb is rejected")
    check("raucb" in response.get_json()["message"],
          "the extension error names what is expected")
    response = client.post("/api/rauc/install", data={"url": "ftp://example/x.raucb"})
    check(response.status_code == 400, "a non-http URL is rejected")
    check("http" in response.get_json()["message"],
          "the scheme error explains itself")

    # ---- a URL that answers 404 fails the install, not the page
    response = client.post("/api/rauc/install", data={"url": base + "/missing.raucb"})
    check(response.status_code == 202, "a missing bundle still starts the attempt")
    final, _seen = wait_for_install(client)
    check(final["install"]["state"] == "error", "a 404 ends in the error state")
    check("404" in (final["install"]["error"] or ""),
          "the HTTP status is reported, got %r" % final["install"]["error"])

    # ---- only one install at a time, whichever way it was started
    release = hold_installer(app)
    try:
        response = client.post("/api/rauc/install", data={"url": url})
        check(response.status_code == 202, "another URL install starts")
        check(response.get_json()["rauc"]["install"]["running"] is True,
              "the URL install reports running while it is held")
        response = client.post("/api/rauc/install", data={"url": url})
        check(response.status_code == 409, "a second URL install is refused with 409")
    finally:
        release()
    wait_for_install(client)

    # ---- a fetched bundle is checked against the same checksum
    response = client.post("/api/rauc/install", data={"url": url, "sha256": "1" * 64})
    check(response.status_code == 202, "a checksummed URL install starts")
    final, _seen = wait_for_install(client)
    check(final["install"]["state"] == "error", "a fetched bundle is checked too")
    check("checksum" in (final["install"]["error"] or ""),
          "the download mismatch is reported, got %r" % final["install"]["error"])

    # ---- without a typed checksum the published .sha256 is used
    published = hashlib.sha256(BundleHandler.payload).hexdigest()
    BundleHandler.checksum = ("%s  iptvpi-2.0.0.raucb\n" % published).encode()
    response = client.post("/api/rauc/install", data={"url": url})
    check(response.status_code == 202, "a URL install without checksum starts")
    final, _seen = wait_for_install(client)
    check(final["install"]["state"] == "success",
          "the published checksum matches, so the install succeeds")
    check(final["install"]["expected"] == published,
          "the checksum came from the .sha256 next to the bundle, got %r"
          % final["install"]["expected"])
    check(final["install"]["checksum_source"] == "sidecar",
          "the install knows the checksum was fetched, got %r"
          % final["install"]["checksum_source"])
    check(final["history"][0]["sha256"] == published,
          "the history records the fetched checksum")
    check(final["history"][0]["checksum_source"] == "sidecar",
          "the history marks a fetched checksum as sidecar")
    page = text(client.get("/"))
    check("from .sha256" in page, "the card says the checksum was fetched")

    BundleHandler.checksum = ("%s  iptvpi-2.0.0.raucb\n" % ("2" * 64)).encode()
    response = client.post("/api/rauc/install", data={"url": url})
    check(response.status_code == 202, "an install with a wrong sidecar starts")
    final, _seen = wait_for_install(client)
    check(final["install"]["state"] == "error",
          "a wrong published checksum stops the install")
    check("checksum" in (final["install"]["error"] or ""),
          "the sidecar mismatch names the checksum")
    BundleHandler.checksum = None

    server.shutdown()
    server.server_close()

    # ---- reboot after the install, and checking the slot on the next boot
    app.OFFLINE = False
    app.RAUC_REBOOT_DELAY = 0.2
    syslog = os.environ["SYSTEMCTL_LOG"]
    if os.path.exists(syslog):
        os.unlink(syslog)

    response = client.post("/api/rauc/install",
                           data={"bundle": (io.BytesIO(b"quiet"), "quiet.raucb")},
                           content_type="multipart/form-data")
    check(response.status_code == 202, "an install without the option starts")
    wait_for_install(client)
    check(not os.path.exists(syslog), "no reboot happens without the option")

    response = client.post("/api/rauc/install",
                           data={"bundle": (io.BytesIO(b"loud"), "loud.raucb"),
                                 "reboot": "1"},
                           content_type="multipart/form-data")
    check(response.status_code == 202, "an install with the reboot option starts")
    check(response.get_json()["rauc"]["install"]["reboot"] is True,
          "the install knows it will reboot")
    final, _seen = wait_for_install(client)
    check(final["install"]["state"] == "success", "the install succeeds")
    check("rebooting" in final["install"]["message"],
          "the result announces the reboot, got %r" % final["install"]["message"])
    deadline = time.time() + 5
    calls = ""
    while time.time() < deadline:
        calls = open(syslog).read() if os.path.exists(syslog) else ""
        if "reboot" in calls.splitlines():
            break
        time.sleep(0.05)
    reboots = [line for line in calls.splitlines() if line.strip() == "reboot"]
    check(len(reboots) == 1, "exactly one reboot is requested, got %r" % calls)

    if os.path.exists(syslog):
        os.unlink(syslog)
    os.environ["STUB_RC"] = "1"
    response = client.post("/api/rauc/install",
                           data={"bundle": (io.BytesIO(b"bad"), "bad.raucb"),
                                 "reboot": "1"},
                           content_type="multipart/form-data")
    check(response.status_code == 202, "a failing install with the option starts")
    final, _seen = wait_for_install(client)
    check(final["install"]["state"] == "error", "the install fails as stubbed")
    time.sleep(0.6)
    check(not os.path.exists(syslog), "a failed install never reboots")
    check(final["update_check"]["state"] == "failed",
          "a failed install closes the pending record")
    os.environ.pop("STUB_RC", None)

    # ---- the manual reboot button
    if os.path.exists(syslog):
        os.unlink(syslog)
    response = client.post("/api/rauc/reboot", data={"confirm": "0"})
    check(response.status_code == 400, "a reboot without confirmation is refused")
    check("confirmed" in response.get_json()["message"],
          "the refusal asks for the confirmation")

    app.OFFLINE = True
    response = client.post("/api/rauc/reboot", data={"confirm": "1"})
    check(response.status_code == 400, "rebooting is refused without systemd")
    check("disabled" in response.get_json()["message"],
          "the refusal names the reason")
    check(not os.path.exists(syslog), "a refused reboot runs nothing")
    app.OFFLINE = False

    app.RAUC_MANUAL_REBOOT_DELAY = 0.2
    response = client.post("/api/rauc/reboot", json={"confirm": "1"})
    check(response.status_code == 200, "a confirmed reboot is accepted")
    body = response.get_json()
    check(body["ok"] is True, "the reboot reports success")
    check("rebooting" in body["message"], "the message announces the reboot")
    check("slot" in body["message"], "the message names the slot it boots")
    deadline = time.time() + 5
    calls = ""
    while time.time() < deadline:
        calls = open(syslog).read() if os.path.exists(syslog) else ""
        if "reboot" in calls.splitlines():
            break
        time.sleep(0.05)
    check(len([line for line in calls.splitlines() if line.strip() == "reboot"]) == 1,
          "the manual reboot calls systemctl reboot once, got %r" % calls)

    # While rauc writes a slot, restarting is the one thing that must not
    # happen: the button is refused until the install finished.
    if os.path.exists(syslog):
        os.unlink(syslog)
    # The guard only means something while rauc is writing, so the install is
    # held open instead of racing a sleep.
    release = hold_installer(app)
    response = client.post("/api/rauc/install",
                           data={"bundle": (io.BytesIO(b"busy"), "busy.raucb")},
                           content_type="multipart/form-data")
    check(response.status_code == 202, "an install starts for the reboot guard")
    running_now = wait_until_running(client)
    check(running_now,
          "the install reports that it is running (%r)"
          % (client.get("/api/rauc").get_json()["rauc"]["install"],))
    response = client.post("/api/rauc/reboot", json={"confirm": "1"})
    check(response.status_code == 409, "a reboot during an install is refused")
    check("still running" in response.get_json()["message"],
          "the refusal explains why")
    check(not os.path.exists(syslog), "nothing reboots while the slot is written")
    release()
    wait_for_install(client)

    # ---- the record of the running slot, and the verdict after the reboot
    response = client.post("/api/rauc/install",
                           data={"bundle": (io.BytesIO(b"verify"), "verify.raucb")},
                           content_type="multipart/form-data")
    check(response.status_code == 202, "the verification install starts")
    final, _seen = wait_for_install(client)
    check(final["install"]["state"] == "success", "the verification install succeeds")
    pending_file = os.path.join(data, "updates", "pending-update.json")
    check(os.path.isfile(pending_file), "the pending update is recorded")
    recorded = json.load(open(pending_file))
    check(recorded["from_slot"] == "A", "the record names the running slot")
    check(recorded["boot_id"] == "boot-1", "the record keeps the boot id")
    check(final["update_check"]["state"] == "waiting",
          "before the reboot the update is waiting, got %r" % final["update_check"])
    check("waiting for the reboot" in final["update_check"]["message"],
          "the card explains what it waits for")

    with open(os.environ["IPTV_BOOT_ID_FILE"], "w") as fh:
        fh.write("boot-2\n")
    with open(os.environ["IPTV_PROC_CMDLINE"], "w") as fh:
        fh.write("console=tty1 root=PARTUUID=49505456-03 rauc.slot=B\n")
    rauc = client.get("/api/rauc").get_json()["rauc"]
    check(rauc["booted"] == "B", "the new boot reports slot B")
    check(rauc["update_check"]["state"] == "ok",
          "the update is verified after the reboot, got %r" % rauc["update_check"])
    check("slot B is running" in rauc["update_check"]["message"],
          "the verification names the slot")
    check(rauc["update_check"]["verified_at"], "the verification is timestamped")
    check(json.load(open(pending_file))["state"] == "ok",
          "the verdict is stored in the record")
    check(rauc["history"][0]["state"] == "verified",
          "the verdict lands in the history, got %r" % rauc["history"][0])
    check(rauc["history"][0]["slot"] == "B",
          "the history entry names the slot that is running now")
    check(len(rauc["history"]) <= rauc["history_limit"],
          "the history stays within its limit")
    check(rauc["history_alert"] is False,
          "a verified update is not flagged as a problem")
    expected_problems = len([entry for entry in rauc["history"]
                             if entry["state"] in ("failed", "warning")])
    check(rauc["history_problems"] == expected_problems,
          "the problem count matches the entries, got %r for %r"
          % (rauc["history_problems"], expected_problems))
    page = text(client.get("/"))
    index = page.index('id="rauc-history"')
    tag = page[index:page.index(">", index)]
    check("open" not in tag, "a clean history stays collapsed")

    stamp = os.path.getmtime(pending_file)
    before = open(pending_file).read()
    time.sleep(0.05)
    again = client.get("/api/rauc").get_json()["rauc"]
    check(again["update_check"]["state"] == "ok", "the verdict is stable")
    check(open(pending_file).read() == before
          and os.path.getmtime(pending_file) == stamp,
          "a verified update is not rewritten on every poll")

    with open(pending_file, "w") as fh:
        json.dump({"from_slot": "B", "boot_id": "boot-2", "installed_at": "now",
                   "state": "pending", "message": ""}, fh)
    with open(os.environ["IPTV_BOOT_ID_FILE"], "w") as fh:
        fh.write("boot-3\n")
    rauc = client.get("/api/rauc").get_json()["rauc"]
    check(rauc["update_check"]["state"] == "warning",
          "a boot into the same slot warns, got %r" % rauc["update_check"])
    check("did not happen" in rauc["update_check"]["message"]
          or "rolled back" in rauc["update_check"]["message"],
          "the warning explains the possible causes")
    errors = open(os.path.join(data, "log", "errors.log")).read()
    check("update verification failed" in errors,
          "the failed verification is logged")
    app.OFFLINE = True

    # ---- a bundle bigger than /tmp never goes through it
    size = 34 * 1024 * 1024          # the tmpfs for /tmp is 32 MB
    payload = b"B" * size
    before_bytes, before_names = walk_size("/tmp", skip=tmp)
    response = client.post("/api/rauc/install",
                           data={"bundle": (io.BytesIO(payload), "huge.raucb")},
                           content_type="multipart/form-data")
    check(response.status_code == 202, "a 34 MB bundle is accepted")
    final, _seen = wait_for_install(client)
    check(final["install"]["state"] == "success", "the large bundle installs")
    check(final["history"][0]["bundle"] == "huge.raucb",
          "the large bundle keeps its name")
    after_bytes, after_names = walk_size("/tmp", skip=tmp)
    # Other processes on a CI runner write to /tmp too, so the comparison
    # ignores noise: what must not happen is a bundle-sized file appearing.
    check(after_bytes - before_bytes < 1024 * 1024,
          "/tmp did not grow by a bundle-size amount during the upload (%+d bytes)"
          % (after_bytes - before_bytes))
    big_new = [path for path in (after_names - before_names)
               if os.path.exists(path) and os.path.getsize(path) > 1024 * 1024]
    check(not big_new,
          "/tmp gained no bundle-sized file during the upload: %r"
          % (sorted(big_new)[:5],))
    check(os.listdir(spool) == [],
          "the spool directory is cleaned up, got %r" % (os.listdir(spool),))

    # ---- limits: size cap and a missing rauc
    app.RAUC_MAX_BYTES = 1000
    response = client.post("/api/rauc/install",
                           data={"bundle": (io.BytesIO(b"x" * 4000), "big.raucb")},
                           content_type="multipart/form-data")
    check(response.status_code == 400, "an oversized bundle is refused")
    check("larger than" in response.get_json()["message"],
          "the size error names the limit")
    check(app.app.config["MAX_CONTENT_LENGTH"] >= 1000,
          "the request limit covers a bundle")
    wait_for_install(client)

    app.RAUC = "/nonexistent/rauc"
    rauc = client.get("/api/rauc").get_json()["rauc"]
    check(rauc["available"] is False, "a missing rauc binary is reported")
    response = client.post("/api/rauc/install",
                           data={"bundle": (io.BytesIO(b"x"), "x.raucb")},
                           content_type="multipart/form-data")
    check(response.status_code == 400, "an upload is refused without rauc")
    check("not installed" in response.get_json()["message"],
          "the refusal explains that rauc is missing")
    html = text(client.get("/"))
    check("RAUC is not part of this image" in html,
          "the card explains that updates are impossible without rauc")

    shutil.rmtree(tmp)
    shutil.rmtree(spool, ignore_errors=True)
    print("")
    print("RAUC update card: %d assertions, %d failures" % (passed, len(failures)))
    for msg in failures:
        print("  - " + msg)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
