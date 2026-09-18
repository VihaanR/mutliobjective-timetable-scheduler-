#!/usr/bin/env bash
# Clone OR-Tools at the exact commit this fork was written against and apply the patch.
#
#   ./apply_fork.sh /path/to/workdir
#
# Leaves a buildable tree at <workdir>/or-tools. See README.md for the build step (needs MSVC
# or GCC/Clang plus CMake >= 3.18 -- this script does not build anything).
set -euo pipefail

BASE_COMMIT="98c165af62df62b3056c2ee0fca66b24e79097cb"
PATCH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/timetabling.patch"

WORKDIR="${1:-}"
if [ -z "$WORKDIR" ]; then
  echo "usage: $0 <workdir>" >&2
  exit 2
fi
[ -f "$PATCH" ] || { echo "patch not found: $PATCH" >&2; exit 1; }

mkdir -p "$WORKDIR"
cd "$WORKDIR"

if [ -e or-tools ]; then
  echo "refusing to overwrite existing $WORKDIR/or-tools -- move it aside first" >&2
  exit 1
fi

echo "==> cloning google/or-tools (full tree; this is a large download)"
git clone https://github.com/google/or-tools.git or-tools
cd or-tools

echo "==> checking out pinned base commit $BASE_COMMIT"
git checkout --detach "$BASE_COMMIT"

echo "==> verifying the patch applies cleanly"
git apply --check "$PATCH"

echo "==> applying"
git apply "$PATCH"

echo
echo "Done. Modified files:"
git diff --stat
echo
echo "Next:"
echo "  cmake -S . -B build -DBUILD_PYTHON=ON -DBUILD_DEPS=ON"
echo "  cmake --build build --config Release -j"
