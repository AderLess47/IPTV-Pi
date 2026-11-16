#!/bin/sh
#
# IPTV-Pi: start the playlist web interface with whichever Python
# interpreter actually has Flask installed. Buildroot installs Flask for the
# image's default interpreter, which may be Python 2 (Kodi 18 pulls it in) or
# Python 3, depending on how the image was configured.

DATA_DIR="${IPTV_DIR:-/var/lib/iptv}"
APP="${IPTV_WEBUI_APP:-}"

# Spool directory for request bodies (Flask/werkzeug writes uploads and
# downloads to tempfile). It has to live on the data partition: /tmp is a
# 32 MB tmpfs and a rootfs bundle is a few hundred MB.
TMP_DIR="${TMPDIR:-${DATA_DIR}/updates/tmp}"
TMPDIR="$TMP_DIR"
export TMPDIR
mkdir -p "$TMP_DIR" 2>/dev/null || true
# Leftovers from a killed process (crash, reboot, power cut). Anything younger
# than five minutes may belong to a concurrently running helper (--refresh,
# --sample), so it is left alone.
if [ -d "$TMP_DIR" ]; then
	find "$TMP_DIR" -mindepth 1 -maxdepth 1 -mmin +5 -exec rm -rf {} \; 2>/dev/null || true
fi

# An update installed through the web UI lands in $DATA_DIR/app and wins over
# the copy baked into the rootfs, so fixes can be shipped without rebuilding
# the image. Deleting that directory (or renaming it back from app.bak) returns
# the box to the version it was flashed with.
if [ -z "$APP" ]; then
	for candidate in "${DATA_DIR}/app/app.py" /usr/share/iptv-webui/app.py; do
		if [ -f "$candidate" ]; then
			APP="$candidate"
			break
		fi
	done
fi

# Arguments are forwarded unchanged, so the same script serves the web server
# (no arguments), the scheduled refresh (--refresh), the hardware log sample
# (--sample) and --version.
for interpreter in python3 python2 python; do
	command -v "$interpreter" >/dev/null 2>&1 || continue
	if "$interpreter" -c 'import flask' >/dev/null 2>&1; then
		exec "$interpreter" "${APP}" "$@"
	fi
done

echo "iptv-webui: no Python interpreter with Flask found" >&2
exit 1
