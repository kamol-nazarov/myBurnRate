#!/bin/sh
# Build the project outside the runtime and install only hashed runtime inputs.
set -eu
umask 077
build_root=$(mktemp -d)
trap 'rm -rf -- "$build_root"' EXIT
.venv/bin/python -m venv "$build_root/build"
build_python="$build_root/build/bin/python"
"$build_python" -m pip install --require-hashes -r requirements-build.txt
BURNRATE_PUBLIC_BUILD=1 "$build_python" -m build --wheel --no-isolation --outdir "$build_root/dist"
set -- "$build_root"/dist/*.whl
test "$#" -eq 1
test -f "$1"
.venv/bin/python -m pip install --require-hashes -r requirements-runtime.txt
.venv/bin/python -m pip install --no-deps --no-build-isolation "$1"
.venv/bin/python -m pip check
