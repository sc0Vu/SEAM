#!/bin/sh
# Clone boltons at the pinned benchmark commit into $1, `master` NOT checked out.
set -eu
dest="$1"
[ -e "$dest" ] && { echo "refusing: $dest exists" >&2; exit 1; }
git clone -q https://github.com/mahmoud/boltons.git "$dest"
cd "$dest"
git checkout -q -B master d31d821
git checkout -q --detach
echo "boltons ready at $dest (d31d821, HEAD detached; master is free for SEAM)"
