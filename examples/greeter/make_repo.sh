#!/bin/sh
# Create a fresh greeter repo at $1 with `main` NOT checked out (SEAM moves it).
set -eu
dest="$1"
[ -e "$dest" ] && { echo "refusing: $dest exists" >&2; exit 1; }
mkdir -p "$dest"
cp "$(dirname "$0")"/seed/*.py "$dest"/
cd "$dest"
git init -q -b main
git add -A
git -c user.name=seed -c user.email=seed@localhost commit -q -m "greeter seed"
git checkout -q --detach
echo "greeter repo ready at $dest (HEAD detached; main is free for SEAM)"
