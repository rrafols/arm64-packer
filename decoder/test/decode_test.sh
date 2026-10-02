#!/bin/bash
# Full round-trip for the AArch64 oneKpaq decoder: encode a spread of inputs with
# the real encoder, decode each with okp_decode (arm64), and compare byte-for-byte.
# A context-mixing decoder fails silently, so the spread exercises different shift
# values and model profiles: repetitive, random, zeros, text, code, structured.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OK="$ROOT/build/onekpaq"
[ -x "$OK" ] || "$ROOT/tools/build_encoder.sh"

clang -arch arm64 -O2 -o "$HERE/dh" "$HERE/decode_harness.c" \
      "$ROOT/decoder/okp_decode.c" "$ROOT/decoder/subrange.S" \
      "$ROOT/decoder/x87.S" "$ROOT/decoder/xsqrt.S"

D="$(mktemp -d)"; trap 'rm -rf "$D"' EXIT
printf 'ABCDEFGH%.0s' {1..50}                                  > "$D/rep.bin"
head -c 256 /dev/urandom                                       > "$D/rand.bin"
python3 -c "open('$D/zeros.bin','wb').write(bytes(300))"
head -c 500 "$OK"                                              > "$D/code.bin"
printf 'the quick brown fox jumps over the lazy dog. %.0s' {1..12} > "$D/text.bin"
python3 -c "
import random
for s in range(30):
    random.seed(2000+s); n=random.randint(40,1600)
    open('$D/s%02d.bin'%s,'wb').write(bytes(
        (random.randint(0,255) if random.random()<0.3 else (i*7+13)&0xff) for i in range(n)))
"
pass=0; fail=0; enc_skip=0
for f in "$D"/*.bin; do
    out="$(cd "$(dirname "$OK")" && "$OK" 3 1 "$f" "$D/o.okp" 2>/dev/null)" || { enc_skip=$((enc_skip+1)); continue; }
    off="$(echo "$out" | sed -n 's/^P offset=\([0-9]*\).*/\1/p')"
    sh="$(echo "$out" | sed -n 's/^P.*shift=\([0-9]*\).*/\1/p')"
    hh="$(echo "$out" | sed -n 's/^H \(.*\)/\1/p')"
    r="$("$HERE/dh" "$D/o.okp" "$off" "$sh" "$hh" "$f")"
    if echo "$r" | grep -q 'DECODE OK'; then pass=$((pass+1))
    else fail=$((fail+1)); echo "  FAIL $(basename "$f") off=$off sh=$sh: $r"; fi
done
rm -f "$HERE/dh"
echo "round-trip: passed=$pass failed=$fail (encoder self-verify skipped $enc_skip)"
[ "$fail" -eq 0 ]
