#!/usr/bin/env bash
# Regenerate the lockfiles from pyproject.toml.
#
# Without --upgrade, uv keeps the versions already pinned here as preferences and
# only moves what pyproject.toml forces it to, so a dependency change does not drag
# the whole tree forward with it. Pass --upgrade through to do that deliberately:
#
#     requirements/refresh.sh --upgrade
#
# The resolution is universal — one file per extra, valid for every Python and
# platform this project supports, rather than one per interpreter. --python-version
# names the floor from requires-python explicitly: universal resolution is already
# version-independent, but the "# via" annotations are computed against the markers
# of whatever interpreter happens to be running, so without it the same pins are
# written with different annotations on a 3.11 runner and a 3.14 laptop.
set -euo pipefail

cd "$(dirname "$0")/.."

passthrough=("$@")

compile_lock() {
    name=$1
    shift
    echo "compiling requirements/$name.txt"
    uv pip compile --universal --generate-hashes --quiet \
        --python-version 3.11 \
        "$@" \
        --output-file "requirements/$name.txt" \
        ${passthrough[@]+"${passthrough[@]}"} \
        pyproject.toml
}

compile_lock base
compile_lock rpi --extra=rpi
compile_lock dev --extra=dev
compile_lock dev-rpi --extra=dev --extra=rpi

echo "done"
