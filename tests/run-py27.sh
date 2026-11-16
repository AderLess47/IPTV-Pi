#!/bin/sh
#
# IPTV-Pi: run the Python 2.7 verification (the CI job "verify-py27") locally.
#
#     ./tests/run-py27.sh
#
# Docker is used with the same image as CI (python:2.7-slim) and the same
# package pins, so a local run matches the runner. Without a Docker daemon the
# script falls back to a python2.7 on PATH that already has Flask — which is how
# the box itself looks, because Kodi 18 brings Python 2.7 along.
#
# Exit status is the test result: 0 = both suites green, non-zero = failure.

set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
IMAGE=${PY27_IMAGE:-python:2.7-slim}
SUITES="tests/verify_ab_update.py tests/verify_rauc_ui.py"
PINS='"flask==1.1.4" "werkzeug==1.0.1" "jinja2==2.11.3" "markupsafe==1.1.1" "itsdangerous==1.1.0" "click==7.1.2"'

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    echo ">>> Python 2.7 via Docker image ${IMAGE}"
    docker run --rm -v "${ROOT}":/src -w /src "${IMAGE}" sh -c '
        set -e
        python -m pip install -q --upgrade "pip<21" setuptools wheel
        python -m pip install -q '"${PINS}"'
        python -V
        python -m py_compile webui/app.py
        for suite in '"${SUITES}"'; do
            echo ">>> python $suite"
            python "$suite"
        done
    '
    echo ">>> Python 2.7: both suites green"
    exit 0
fi

# A 2.7 assembled by tests/setup-py27.sh wins over whatever "python2.7" happens
# to be on PATH: it is the version and the pins the CI job uses, and it lives
# outside /tmp, so it survives a reboot.
CACHE_PY27=${IPTV_PY27_PREFIX:-$HOME/.cache/iptvpi-py27}/bin/python2.7
PY27=
if [ -x "$CACHE_PY27" ]; then
    PY27=$CACHE_PY27
elif command -v python2.7 >/dev/null 2>&1; then
    PY27=$(command -v python2.7)
fi

if [ -n "$PY27" ] && "$PY27" -c 'import flask' >/dev/null 2>&1; then
    echo ">>> Python 2.7 found locally: $PY27"
    "$PY27" -V 2>&1
    "$PY27" -m py_compile webui/app.py
    for suite in ${SUITES}; do
        echo ">>> python2.7 $suite"
        "$PY27" "$suite"
    done
    echo ">>> Python 2.7: both suites green"
    exit 0
fi

# The pre-commit hook sets IPTV_ALLOW_SKIP: on a laptop without a container
# runtime a missing 2.7 must not block every commit. CI never sets it, so there
# the job still fails when the runtime is missing.
if [ "${IPTV_ALLOW_SKIP:-0}" = "1" ]; then
    echo ">>> Python 2.7: SKIPPED, no usable runtime (IPTV_ALLOW_SKIP=1)" >&2
    echo "    run ./tests/setup-py27.sh, or start docker, to run it" >&2
    exit 0
fi

cat >&2 <<'EOF'
No usable Python 2.7 found.

Either start Docker (the script then uses python:2.7-slim, the image the CI job
uses), or put a python2.7 with Flask 1.1.4 on PATH. The pins are:

  flask==1.1.4 werkzeug==1.0.1 jinja2==2.11.3 markupsafe==1.1.1 \
  itsdangerous==1.1.0 click==7.1.2

The suites are normal Python scripts, so any 2.7 works:
  python2.7 tests/verify_ab_update.py && python2.7 tests/verify_rauc_ui.py

Without Docker and without a python2.7, this sets one up once (~40 MB, no root)
and keeps it outside /tmp, where run-py27.sh finds it again:

  ./tests/setup-py27.sh
EOF
exit 1
