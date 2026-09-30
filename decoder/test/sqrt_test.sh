#!/bin/bash
# Round-trip test for the 80-bit software fsqrt: compare xsqrt.S (arm64) against
# real x87 sqrtl (x86-64 under Rosetta) over a spread of normalised 80-bit
# values. Any mismatch in mantissa or exponent fails.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
clang -arch x86_64 -O2 -o "$HERE/ref80" "$HERE/ref80.c"
clang -arch arm64  -O2 -o "$HERE/drv"   "$HERE/drv.c" "$HERE/../xsqrt.S"

# Exponents are capped to +/-16000: outside ~2^16384 the x87 long double
# reference under/overflows, while the software float (32-bit exponent) does
# not. The decoder only ever uses tiny exponents, so this covers it amply.
python3 - > "$HERE/cases.txt" <<'PY'
import random
random.seed(1)
cases = []
# edge mantissas
for m in (1<<63, (1<<64)-1, (1<<63)+1, 0xC000000000000000, 0xFFFFFFFFFFFFFFFF,
          0x8000000000000001, 0xB504F333F9DE6484):   # last ~ sqrt(2)/2 * 2^64
    for e in (-1, -63, -64, -65, 0, 1, 62, -16000, 16000):
        cases.append((m, e))
# perfect squares: m = k^2 forms
for k in (1<<32, (1<<32)-1, 3<<31, 0x123456789):
    m = k*k
    while m >= (1<<64): m >>= 1
    while m < (1<<63): m <<= 1
    cases.append((m, 0)); cases.append((m, -1))
# random
for _ in range(50000):
    m = random.getrandbits(64) | (1<<63)
    e = random.randint(-16000, 16000)
    cases.append((m, e))
for m, e in cases:
    print("%x %d" % (m, e))
PY

"$HERE/ref80" < "$HERE/cases.txt" > "$HERE/out.ref"
"$HERE/drv"   < "$HERE/cases.txt" > "$HERE/out.asm"

n=$(wc -l < "$HERE/cases.txt" | tr -d ' ')
if diff -q "$HERE/out.ref" "$HERE/out.asm" >/dev/null; then
    echo "xsqrt: $n/$n match real x87 fsqrt"
    rm -f "$HERE/ref80" "$HERE/drv" "$HERE/cases.txt" "$HERE/out.ref" "$HERE/out.asm"
else
    echo "xsqrt: MISMATCH  (cols: m e | ref_m ref_e | asm_m asm_e)"
    paste "$HERE/cases.txt" "$HERE/out.ref" "$HERE/out.asm" | awk '$3!=$5 || $4!=$6' | head -15
    exit 1
fi
