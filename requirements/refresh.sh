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
# platform this project supports, rather than one per interpreter.
set -euo pipefail

cd "$(dirname "$0")/.."

for spec in "base:" "rpi:--extra=rpi" "dev:--extra=dev"; do
    name=${spec%%:*}
    extra=${spec#*:}
    echo "compiling requirements/$name.txt"
    uv pip compile --universal --generate-hashes --quiet \
        ${extra:+"$extra"} \
        --output-file "requirements/$name.txt" \
        "$@" \
        pyproject.toml
done

echo "done"
