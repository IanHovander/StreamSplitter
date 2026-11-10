#!/bin/sh
# StreamSplitter for macOS and Linux: install and set up with one command. In a terminal, in this folder:
#     sh run.sh                     first time: install, then the guided setup; later: a menu (start, status, stop,
#                                   change settings, listen for Slack commands)
#     sh run.sh --section slack     one setup step: event, tba, stream, slack, youtube, notify, listen, test
#     sh run.sh status              any streamsplitter command: start, status, stop, listen, live ..., record ...
#     sh run.sh --install-only      only install and download (no questions)
# It installs uv (astral.sh's official installer) into .tools/uv if uv is not on PATH, Python 3.12 and the light
# packages into .venv, then starts the setup, which downloads the rest (detection packages, ffmpeg, Deno) in the
# background. Everything stays inside this folder: no system settings, shell profile or PATH are changed. A second
# run skips what is done. Needs curl or wget (macOS has curl).
set -u
cd "$(dirname "$0")" || exit 1
ROOT=$(pwd)
TOOLS="$ROOT/.tools"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"
# uv keeps its Python and download cache here too; copy (not link) files, which works on every disk
export UV_PYTHON_INSTALL_DIR="$TOOLS/python" UV_CACHE_DIR="$TOOLS/uv-cache" UV_LINK_MODE=copy

fail() {
    echo
    echo "$1"
    exit 1
}

UV=$(command -v uv 2>/dev/null || true)
if [ -z "$UV" ] && [ -x "$TOOLS/uv/uv" ]; then
    UV="$TOOLS/uv/uv"
fi

# Up to date: the venv works and was installed from this very pyproject.toml.
if [ -x "$PY" ] && cmp -s "$ROOT/pyproject.toml" "$VENV/installed-pyproject.toml" \
        && "$PY" -c "import streamsplitter, tomli_w, yt_dlp" >/dev/null 2>&1; then
    :
else
    echo "StreamSplitter: installing (the first time takes a minute or two)"
    if [ -z "$UV" ]; then
        echo
        echo "[1/3] Getting uv, which installs Python and the packages (official installer from astral.sh) ..."
        if command -v curl >/dev/null 2>&1; then
            curl -LsSf https://astral.sh/uv/install.sh | env UV_UNMANAGED_INSTALL="$TOOLS/uv" sh
        elif command -v wget >/dev/null 2>&1; then
            wget -qO- https://astral.sh/uv/install.sh | env UV_UNMANAGED_INSTALL="$TOOLS/uv" sh
        else
            fail "Neither curl nor wget is installed. Install one (Linux: sudo apt install curl) and run this again."
        fi
        [ -x "$TOOLS/uv/uv" ] || fail "Could not get uv from astral.sh. Check the internet connection (a school or
company network may block downloads; a phone hotspot works) and run this again, or install uv yourself
(https://docs.astral.sh/uv/) and run this again."
        UV="$TOOLS/uv/uv"
    fi
    echo
    echo "[2/3] Python 3.12 in .venv (uv's own copy, the same on every computer) ..."
    if [ ! -x "$PY" ]; then
        "$UV" venv --python 3.12 --managed-python "$VENV" || fail "Could not make the Python 3.12 environment. Check the internet
connection and run this again. If it keeps failing, delete the .venv and .tools folders here and run it again."
    fi
    echo
    echo "[3/3] The setup's own packages (a few seconds) ..."
    "$UV" pip install --python "$PY" -e . || fail "Could not install StreamSplitter's packages. Check the internet
connection and run this again (it continues where it stopped)."
    cp "$ROOT/pyproject.toml" "$VENV/installed-pyproject.toml"
fi

if [ -n "$UV" ]; then
    export STREAMSPLITTER_UV="$UV"
fi
case "${1:-}" in
    ""|-*) exec "$PY" -m streamsplitter.cli setup "$@" ;;
    *) exec "$PY" -m streamsplitter.cli "$@" ;;
esac
