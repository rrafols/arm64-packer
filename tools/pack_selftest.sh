#!/bin/bash
# Self-test for the packer skeleton (tools/pack.py): build small self-contained
# arm64 payloads, pack each, run, and check stdout + exit code. Exercises the
# whole chain - signed Mach-O, bound compression_decode_buffer, mmap RW ->
# mprotect RX -> jump - that M3 is built on.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
python3 - <<'PY'
import importlib.util, sys
spec=importlib.util.spec_from_file_location("pack","tools/pack.py")
pack=importlib.util.module_from_spec(spec); spec.loader.exec_module(pack)

def prog(msg, exit_code, pre=0, filler=0):
    # self-contained arm64: write(1,msg,len(msg)); exit(exit_code). entry at `pre`.
    assert len(msg)==8
    code=bytearray()
    msg_off=pre+40
    code+=pack.adrp_add(1,msg_off,pre)
    code+=pack.movz(0,1)+pack.movz(2,8)+pack.movz(16,4)+pack.svc0x80()
    code+=pack.movz(0,exit_code)+pack.movz(16,1)+pack.svc0x80()
    body=bytearray(b'\0'*pre)+code
    while len(body)<msg_off: body+=b'\0'
    body+=msg+(b'\xAB'*filler)
    return bytes(body), pre

cases=[(b"packed!\n",42,0,0),(b"entryOK\n",7,64,0),(b"big ok!\n",9,0,4000)]
for i,(m,ec,pre,fil) in enumerate(cases):
    body,entry=prog(m,ec,pre,fil)
    open("build/st%d.bin"%i,"wb").write(body)
    pack.build(body, entry, "build/st%d"%i)
PY
set +e
fail=0
check() { # file expected_out expected_exit
  out="$("$1")"; ec=$?
  if [ "$out" = "$2" ] && [ "$ec" = "$3" ]; then echo "  OK   $(basename "$1"): '$out' exit=$ec"
  else echo "  FAIL $(basename "$1"): got '$out' exit=$ec, want '$2' exit=$3"; fail=1; fi
}
check build/st0 "packed!" 42
check build/st1 "entryOK" 7
check build/st2 "big ok!" 9
rm -f build/st*.bin build/st0 build/st1 build/st2
[ "$fail" -eq 0 ] && echo "pack skeleton: all pass" || { echo "pack skeleton: FAILURES"; exit 1; }
