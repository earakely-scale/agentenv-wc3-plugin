#!/usr/bin/env bash
# Build wc3-worker:standin, a stand-in for wc3env's worker image on its fake game, from a wc3env checkout:
#   scripts/standin.sh path/to/wc3env && agent-env wc3 setup --base wc3-worker:standin && agent-env run wc3 --task smoke
set -euo pipefail
wc3env="${1:?usage: scripts/standin.sh path/to/wc3env}"
context="$(mktemp -d)"
trap 'rm -rf "$context"' EXIT
cp "$(dirname "$0")/standin/Dockerfile" "$context/"
cp "$wc3env/docker/with-display.sh" "$context/"
cp -r "$wc3env/src/wc3env" "$context/wc3env"
docker build --platform linux/amd64 -t wc3-worker:standin "$context"
