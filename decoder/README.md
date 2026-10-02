# AArch64 oneKpaq mode-3 decoder — design notes

Port of oneKpaq's mode-3 (single section, fast) decompressor to AArch64. The
x86-64 port in `fzn_east4k/packers/onekpaq64/onekpaq_decompressor64.asm` (171
bytes) is the spec: same register roles, same algorithm, round-trip tested
against the real encoder.

## The precision question, answered: 80-bit is required

The arithmetic coder itself is **pure integer** (`ArithDecoder.cpp`: `u32 range`,
`value`, `subRange`). No floating point there. The floating point is only in the
probability model that produces `subRange`, in `BlockCodec.cpp::CalculateSubRange`:

```cpp
long double p = 1;
...
    asm volatile("fsqrt\n" : "=t"(p) : "0"(p));         // p = sqrt(p)
...
    p = (long double)(int)it.first.c0 / p;               // fidivr
    p = (long double)(int)it.first.c1 / p;               // fidivr
...
    asm volatile("fsqrt\n" : "=t"(p) : "0"(p));
...
u32 ret;
asm volatile("fistl %0\n" : "=m"(ret) : "t"((long double)(int)range / (1 + p)));
```

`long double` on x86 is **80-bit x87 extended precision**, and the code uses
explicit `fsqrt`/`fistl`. Because the coder is an integer arithmetic coder, the
encoder and decoder must compute the **exact same** `subRange` for every bit or
they desync — so the decoder must reproduce this 80-bit computation bit-for-bit.
AArch64 has no 80-bit float, so the decoder needs software extended precision,
exactly as the intro's synth did.

This is the pessimistic outcome of the M2 investigation, and it is now certain,
not a guess: reading the encoder settled it before a line of the decoder was
written.

## Primitives needed (80-bit, round-to-nearest-even, as `finit` leaves the x87)

| op in the decoder | 80-bit primitive | source |
|---|---|---|
| `p = 1` | load 1.0 | trivial |
| `fidivr dword` : `p = int / p` | int→80-bit, divide | reuse `east/x87.S` |
| `1 + p` (`faddp`) | add | reuse `east/x87.S` |
| `range / (1+p)` (`fidivr`) | divide | reuse `east/x87.S` |
| `fistp dword` | round 80-bit → int32, ties to even | reuse `east/x87.S` round-to-int |
| **`fsqrt`** | **80-bit square root, ties to even** | **new — must be written** |

So most of the datapath is already implemented and bit-verified in
`fzn_east4k/ports/macos-arm64/src/x87.S` (add, multiply, divide, round-to-integer,
fsin). The one new primitive is an 80-bit `fsqrt`: compute the integer square
root of the 126-bit mantissa field and round once to a 64-bit mantissa, ties to
even — the same "exact in 128 bits, round once" model `x87.S` already uses for
its other ops.

## The encoder, for round-trip testing

`tools/build_encoder.sh` builds it. It only builds for x86-64 (the `fsqrt`/`fistl`
inline asm), and runs under Rosetta 2, whose x87 emulation gives the 80-bit
results the arm64 decoder must match. Usage:

    ./build/onekpaq 3 1 input output.okp     # prints  offset=N shift=M

`offset` points into the compressed data (the decoder walks backwards into the
bytes before it); `shift` parameterises the weight upload. Both feed the decoder
exactly as in the x86-64 port.

## Register roles (from the x86-64 port, to be reassigned to AArch64)

    x86-64:  rax range / rbx src / rcx dest bit shift, ch model / rdx header
             rsi dest / rdi window start / ebp value
    x87 stack for the subrange maths -> software 80-bit here

Requirements inherited from the algorithm (see the x86-64 README):
* the compressed data must be writable — it is used as scratch and destroyed;
* the destination must be zero-filled and writable from −13 bytes to length+1;
* the decoder does not know the output length; it is taken from elsewhere.

## Verification plan

The context-mixing decoder produces plausible garbage rather than crashing when
it is subtly wrong, so the only real test is a byte-exact round trip over inputs
that exercise different shift values: code, text, random, zeros, repetitive data
— the same spread the x86-64 port's `test/run_tests.sh` uses. Build the arm64
decoder with clang's integrated assembler (no nasm needed), decompress each
encoder output, and compare to the original.

## Implementation steps (resumable — each is a commit with a passing test)

- [x] **A. 80-bit `fsqrt`** — `xsqrt.S`, the one new primitive. Bit-exact vs real
  x87 `sqrtl` (Rosetta) over 50k random + edge cases: `bash test/sqrt_test.sh`.
- [x] **B. x87 subset for the decoder** — `x87.S` has `xadd`, `xdiv`, `xfist32`
  (verbatim from the intro's bit-verified `x87.S`, minus `xsin`/`xmul` which
  drag in libm) plus a new `xfromint` (`(long double)(int)`), tested bit-exact
  vs the long-double reference over 30k cases. `xadd`/`xdiv`/`xfist32` get their
  end-to-end check against the encoder in step C.
- [x] **C. `CalculateSubRange` in AArch64** — `subrange.S` (`calc_subrange`).
  Validated bit-exact (20k random model profiles) against the encoder's own x87
  `long double` computation: `bash test/subrange_test.sh`. This also exercises
  `xadd`/`xdiv`/`xfist32`/`xsqrt`/`xfromint` together end to end.
- [ ] **D. the decoder control flow** — see the design notes below.
- [ ] **E. full round-trip** — decode the encoder's output for the code/text/
  random/zeros/repetitive spread and compare byte-for-byte.

## Step D design notes (ready to implement)

Port the **clean C++ decode path**, not the dense flag-threaded asm — the arm64
file rounds up to a 16 KB page regardless, so a compact C decoder that calls the
80-bit asm ops fits with room to spare and is far easier to get right. Hand-asm
is a later size optimisation if ever needed.

The algorithm (`BlockCodec::Decode` + `ArithDecoder` in the oneKpaq source), for
mode 3 = `Single` = `NoLimitQWContextModel` + `SingleAsm` arithmetic decoder:

    DecodeHeader(header) -> rawLength, models[] = {model_byte, weight}
    ret = zeroed(rawLength); bitLength = rawLength*8
    PreDecode( CalculateSubRange(ret, -1,       models, range, shift) )
    for i in 0..bitLength-1:
        if Decode( CalculateSubRange(ret, i,    models, range, shift) ):
            ret[i>>3] |= 0x80 >> (i&7)
    ProcessEndOfSection( CalculateSubRange(ret, bitLength, models, range, shift) )

Pieces to port (all integer except the FP, which `subrange.S` already does):
* **ArithDecoder (SingleAsm)** — pure integer, from `ArithDecoder.cpp`. Normalize
  checks the start/anchor/filler bits; Decode/ProcessEndOfSection subtract the
  subrange. ~40 lines.
* **DecodeHeader** — `header[0..1]` = size, `header[2]` = header length,
  `header[3..]` = model bytes; reconstruct `{model, weight}` by the `>=model`
  weight-doubling rule. ~15 lines.
* **NoLimitQWContextModel iterator + PAQ1CountBooster + CreateWeightProfile** —
  scans the already-decoded `ret` for context matches to build the per-model
  `{c0, c1, weight}` list. ~40 lines. This is what feeds `calc_subrange`.
* **CalculateSubRange** — `subrange.S`'s `calc_subrange` given that list, plus the
  `bitPos == -1` special case (NoLimit: Initialize + Increment(false) + Finalize).

On-disk format the real decoder consumes (so the arm64 one matches the packer):
`file = src1 ++ src2 ++ 4 zero bytes`, and the encoder prints `offset = len(src1)`.
`src2` (from `offset`) is the arith bitstream, read forward from `offset+4`
(the stream has a -4 bias; normalize reads `[base+4]`). `src1` is the header,
stored reversed: the asm walks a pointer back from `offset+3` into it. The clean
C++ keeps the header as a forward `_header` block, so the C port can read src1 in
natural order with the documented layout rather than the asm's backward walk.

Bring-up: `tools/roundtrip_ref.sh <file>` encodes and decodes through the real
x86-64 decompressor (ground truth). For debugging, dump per-bit (range, value,
subrange, bit) from both the reference and the arm64 port and diff — a
context-mixing decoder fails silently, so stage-by-stage comparison is the way.
Note: the encoder's own asm-stream self-verify aborts on some larger inputs
("End of section not detected"); small inputs are clean, so bring up on those.

## Status

**Steps A, B, C done** — the whole floating-point half of the decoder is ported
and validated bit-exact against the encoder's x87: the 80-bit primitives
(`xsqrt.S`, `x87.S`) and the full `CalculateSubRange` (`subrange.S`).

Step D is fully scoped and de-risked (design notes above): the x86-64 reference
decoder runs (`tools/roundtrip_ref.sh`, ground truth), the clean C++ algorithm to
port is identified, the on-disk format is mapped, and the C-callable 80-bit
wrappers (`x87_c.S`) are in place. What remains is writing the integer decode
(ArithDecoder + DecodeHeader + context-model scan) in C and round-tripping it.
