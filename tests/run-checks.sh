#!/bin/sh
#
# IPTV-Pi: every local check in one run.
#
#     ./tests/run-checks.sh              # Python 3, JavaScript, shell syntax, 2.7
#     ./tests/run-checks.sh --no-py27    # skip the Python 2.7 job
#
# The Python 3 steps mirror the CI job "verify" (compile, inline JavaScript,
# both suites); the 2.7 part is delegated to tests/run-py27.sh, which is the
# command the CI job "verify-py27" runs. Exit status is 0 only when every check
# passed, so it can be used as a pre-commit hook or in a script.
#
#     PYTHON3=python3.11 ./tests/run-checks.sh

set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
PYTHON3=${PYTHON3:-python3}
SKIP_PY27=0
for argument in "$@"; do
    case "$argument" in
        --no-py27) SKIP_PY27=1 ;;
        -h|--help)
            sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            printf 'run-checks: unknown argument %s\n' "$argument" >&2
            exit 64
            ;;
    esac
done

cd "$ROOT"

failures=0
step() { printf '\n=== %s\n' "$*"; }
# Both interpreters are in play here: a PYTHONHOME that belongs to the 2.7
# interpreter must not leak into the 3.x steps.
py3() { env -u PYTHONHOME "$PYTHON3" "$@"; }
run() {
    if "$@"; then
        return 0
    fi
    failures=$((failures + 1))
    printf 'FAILED: %s\n' "$*" >&2
    return 0
}

step "Python 3: compile"
run py3 -m py_compile webui/app.py tests/verify_ab_update.py tests/verify_rauc_ui.py

step "JavaScript: syntax of the inline dashboard script"
# node picks the module type from the file name, so the extraction goes into a
# dashboard.js inside a throwaway directory rather than into a bare temp file.
js_dir=$(mktemp -d) || exit 1
js_file="$js_dir/dashboard.js"
trap 'rm -rf "$js_dir"' EXIT
if py3 - "$js_file" <<'PY'
import re
import sys

page = open("webui/templates/index.html").read()
with open(sys.argv[1], "w") as handle:
    handle.write(re.findall(r"<script>(.*?)</script>", page, re.S)[-1])
PY
then
    if command -v node >/dev/null 2>&1; then
        run node --check "$js_file"
    else
        printf 'node is not installed: skipping the JavaScript syntax check\n' >&2
    fi
else
    failures=$((failures + 1))
    printf 'FAILED: extracting the inline JavaScript\n' >&2
fi

step "Python 3: A/B update suite"
run py3 tests/verify_ab_update.py

step "Python 3: system update card suite"
run py3 tests/verify_rauc_ui.py

step "Shell: syntax of the image scripts"
for script in br2-external/board/raspberrypi/rootfs-overlay/usr/bin/* webui/launch.sh; do
    run sh -n "$script"
done
for script in br2-external/board/raspberrypi/post-image.sh \
              br2-external/board/raspberrypi/post-build.sh \
              build_os.sh; do
    run bash -n "$script"
done

if [ "$SKIP_PY27" -eq 1 ]; then
    step "Python 2.7: skipped (--no-py27)"
else
    step "Python 2.7: both suites (tests/run-py27.sh)"
    # The output is captured so the summary can tell "green" from "skipped",
    # and the exit status is kept instead of taking tee's.
    py27_log=$(mktemp)
    if sh tests/run-py27.sh > "$py27_log" 2>&1; then
        py27_rc=0
    else
        py27_rc=$?
    fi
    cat "$py27_log"
    if [ "$py27_rc" -ne 0 ]; then
        failures=$((failures + 1))
        printf 'FAILED: sh tests/run-py27.sh\n' >&2
    elif grep -q 'SKIPPED' "$py27_log"; then
        py27_skipped=1
    fi
    rm -f "$py27_log"
fi

printf '\n'
if [ "$failures" -eq 0 ]; then
    if [ "${py27_skipped:-0}" -eq 1 ]; then
        printf 'EVERY AVAILABLE CHECK PASSED (Python 2.7 skipped)\n'
    else
        printf 'ALL CHECKS PASSED\n'
    fi
    exit 0
fi
printf '%d CHECK(S) FAILED\n' "$failures" >&2
exit 1
