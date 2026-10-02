#!/bin/bash
# Build the oneKpaq encoder for use on Apple Silicon.
#
# oneKpaq's CalculateSubRange computes the arithmetic-coder subrange in x87 80-bit
# `long double` with inline `fsqrt`/`fistl` (see decoder/README.md), so the
# encoder only builds for x86-64 - there is no arm64 x87. We build it as an
# x86-64 Mach-O and run it under Rosetta 2, whose x87 emulation gives the same
# 80-bit results the arm64 decoder's software float must match.
#
# The 64-bit build drops AsmDecode.cpp and the 32-bit cfunc stubs (the Makefile
# only links those for BITS=32); they are the in-process asm decode-verify path,
# which we do our own round-trip for instead.
#
#   tools/build_encoder.sh            # clones to /tmp, builds -> build/onekpaq
#   ONEKPAQ=build/onekpaq ...         # then point the tests at it
set -eu
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${ONEKPAQ_SRC:-/tmp/onekpaq_src}"
[ -d "$SRC" ] || git clone --depth 1 https://github.com/temisu/oneKpaq.git "$SRC"
cd "$SRC"
# Emit the clean header block (rawLength + model bytes) on stdout as "H <hex>",
# alongside "P offset= shift=". pack.py and the decoder need the models and raw
# length, which the asm header does not carry in recoverable form.
python3 - <<'PY'
h=open('StreamCodec.hpp').read()
if 'GetHeaders' not in h:
    h=h.replace('\tuint GetShift() const { return _shift; }',
                '\tuint GetShift() const { return _shift; }\n'
                '\tconst std::vector<std::vector<u8>> &GetHeaders() const { return _header; }')
    open('StreamCodec.hpp','w').write(h)
m=open('onekpaq_main.cpp').read()
if 'GetHeaders()' not in m:
    a='fprintf(stdout/* not stderr */, "P offset=%zu shift=%u\\n", src1.size(), s.GetShift());'
    m=m.replace(a, a+'\n\t\t{ const auto &H=s.GetHeaders(); if(!H.empty()){ '
                'fprintf(stdout,"H "); for(auto b:H[0]) fprintf(stdout,"%02x",b); fprintf(stdout,"\\n"); } }')
    open('onekpaq_main.cpp','w').write(m)
PY
mkdir -p obj
clang -arch x86_64 -Os -c log.c -o obj/log.o
for f in ArithDecoder ArithEncoder BlockCodec CacheFile StreamCodec onekpaq_main; do
    clang++ -arch x86_64 -Os -std=c++14 -Wno-shift-op-parentheses \
        -DONEKPAQ_VERSION='"1.1"' -c "$f.cpp" -o "obj/$f.o"
done
clang++ -arch x86_64 -o "$HERE/build/onekpaq" obj/*.o
echo "built $HERE/build/onekpaq"
file "$HERE/build/onekpaq"
