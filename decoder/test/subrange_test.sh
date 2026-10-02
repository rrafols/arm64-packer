#!/bin/bash
# Validate the AArch64 CalculateSubRange (subrange.S) against the encoder's exact
# x87 long-double computation (subrange_ref.c, x86-64 under Rosetta), over random
# valid model profiles: counts >= 1, weights non-increasing powers of two, range
# in the coder's [2, 2^30) band.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
clang -arch x86_64 -O2 -o "$HERE/subrange_ref" "$HERE/subrange_ref.c"
clang -arch arm64  -O2 -o "$HERE/subrange_drv" "$HERE/subrange_drv.c" \
      "$HERE/../subrange.S" "$HERE/../x87.S" "$HERE/../xsqrt.S"

python3 - > "$HERE/sr_cases.txt" <<'PY'
import random
random.seed(7)
N = 20000
for _ in range(N):
    n = random.randint(1, 6)
    range_ = random.randint(3, (1 << 30) - 1)
    # weights: non-increasing powers of two
    s = random.randint(0, 6)
    triples = []
    for _ in range(n):
        s = random.randint(0, s)              # <= previous exponent
        w = 1 << s
        c0 = random.randint(1, 1 << 24)
        c1 = random.randint(1, 1 << 24)
        triples.append((c0, c1, w))
    print(n, range_)
    for c0, c1, w in triples:
        print(c0, c1, w)
PY

"$HERE/subrange_ref" < "$HERE/sr_cases.txt" > "$HERE/sr.ref"
"$HERE/subrange_drv" < "$HERE/sr_cases.txt" > "$HERE/sr.asm"

total=$(wc -l < "$HERE/sr.ref" | tr -d ' ')
if diff -q "$HERE/sr.ref" "$HERE/sr.asm" >/dev/null; then
    echo "calc_subrange: $total/$total match the encoder's x87 computation"
    rm -f "$HERE/subrange_ref" "$HERE/subrange_drv" "$HERE/sr_cases.txt" "$HERE/sr.ref" "$HERE/sr.asm"
else
    echo "calc_subrange: MISMATCH"
    paste <(cat "$HERE/sr.ref") <(cat "$HERE/sr.asm") | awk '$1!=$2' | head -10
    echo "(first differing case indices above: ref vs asm)"
    exit 1
fi
