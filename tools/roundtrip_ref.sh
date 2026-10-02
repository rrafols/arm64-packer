#!/bin/bash
# Reference round-trip through the real x86-64 oneKpaq decompressor, for bringing
# up and debugging the arm64 decoder (step D). Encodes an input with the built
# encoder, then decodes it with onekpaq_decompressor64.asm (x86-64, Rosetta) via
# the fzn_east4k test harness, and checks the bytes match.
#
#   tools/roundtrip_ref.sh <input-file>
#
# Prints the offset/shift the encoder chose (the arm64 decoder will need the same
# entry contract: src = packed + offset, and the shift baked into the decoder).
set -eu
HERE="$(cd "$(dirname "$0")/.." && pwd)"
OK="$HERE/build/onekpaq"
OKPDIR="/Users/rrafols/dev/fzn_east4k/packers/onekpaq64"
IN="${1:?usage: roundtrip_ref.sh <input-file>}"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

[ -x "$OK" ] || "$HERE/tools/build_encoder.sh"
out="$(cd "$(dirname "$OK")" && "$OK" 3 1 "$IN" "$T/in.okp" 2>&1)" || { echo "$out" | tail -3; exit 1; }
line="$(echo "$out" | grep -o 'offset=[0-9]* shift=[0-9]*')"
off="${line#offset=}"; off="${off%% *}"; sh="${line##*shift=}"
echo "encoder: $line"

nasm -f macho64 -I"$OKPDIR" -DONEKPAQ_DECOMPRESSOR_SHIFT="$sh" \
     "$OKPDIR/test/wrap.asm" -o "$T/wrap.o"
clang -arch x86_64 -o "$T/harness" "$OKPDIR/test/harness.c" "$T/wrap.o" -Wl,-w
"$T/harness" "$IN" "$T/in.okp" "$off"
