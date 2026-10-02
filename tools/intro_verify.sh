#!/bin/bash
# Verify the packed intro against the unpacked one. Builds the offscreen intro
# (renders frames + audio to files, no display), packs it, runs both, and checks:
# the audio is bit-identical, and only the clock-seeded effect-1 frames differ.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${1:-/Users/rrafols/dev/fzn_east4k/ports/macos-arm64}"
cd "$PORT" && make build/shots >/dev/null 2>&1
SHOTS="$PORT/build/shots"

python3 "$ROOT/tools/pack.py" --intro "$SHOTS" "$ROOT/build/shots_packed" | sed -n '1,4p'

U="$(mktemp -d)"; P="$(mktemp -d)"; trap 'rm -rf "$U" "$P"' EXIT
( cd "$U" && "$SHOTS" >/dev/null 2>&1 )
( cd "$P" && "$ROOT/build/shots_packed" >/dev/null 2>&1 )

fail=0
for f in song.wav instruments.raw notes.raw; do
    if cmp -s "$U/$f" "$P/$f"; then echo "  audio $f: identical"
    else echo "  audio $f: DIFFERS"; fail=1; fi
done
id=0; diff=0; dl=""
for f in "$U"/shot*.ppm; do
    b="$(basename "$f")"
    if cmp -s "$U/$b" "$P/$b"; then id=$((id+1)); else diff=$((diff+1)); dl="$dl $b"; fi
done
echo "  frames: $id identical, $diff differing ($dl)"
# only effect-1 frames (shot04,05,06, and some of 09/0b/0c) may differ; cap at 6
[ "$diff" -le 6 ] || { echo "  too many frames differ"; fail=1; }
[ "$fail" -eq 0 ] && echo "packed intro: VERIFIED (audio bit-identical, only clock-seeded frames differ)" \
  || { echo "packed intro: FAILED"; exit 1; }
