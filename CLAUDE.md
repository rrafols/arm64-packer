# CLAUDE.md — arm64-packer

Guidance for Claude Code sessions working in this repo.

## What this is

A size-coding packer + hand-built Mach-O header for **AArch64 macOS**, to shrink
the Apple Silicon port of the *Looking for the East* 4k intro
(`~/dev/fzn_east4k/ports/macos-arm64`, currently a ~52 KB linked binary) toward
the 4k class the other ports reach. It is the arm64 counterpart of
`~/dev/fzn_east4k/packers/onekpaq64` (x86-64) and that port's `tools/pack.py`.

Read `README.md` (milestones + what's settled) and `decoder/README.md` (the
decoder design and its verification) before starting. This file is the quick
operational guide.

## Status (keep this current)

- **M0 done** — `probes/minsign.py`: hand-built, self-signed 16384-byte arm64
  Mach-O that the kernel execs. dyld is mandatory (static binaries are SIGKILLed);
  the code signature is mandatory and must be `CS_ADHOC`-flagged; the file floor
  is one 16 KB page.
- **M1 done** — `probes/wx.c`: W^X unpack path is `mmap` RW → write → `mprotect`
  RX → call (no `MAP_JIT`, no entitlement).
- **M2 done** — the arm64 oneKpaq mode-3 decoder, round-trip verified. See below.
  (Not used by the shipping packer; see "oneKpaq vs LZMA".)
- **M3 + M4 done** — `tools/pack.py --intro <mach-o>` packs the intro into a
  signed, self-decompressing 16702-byte binary (from 52816), LZMA, verified
  bit-identical by `tools/intro_verify.sh`. See "The packer" below.

## The packer (M3/M4)

`tools/pack.py --intro <mach-o> [out]` builds a signed one-page Mach-O:
- `tools/parse_intro.py` extracts the flat image, entry, `__TEXT`/total vmsize, and
  the GOT imports from `LC_DYLD_CHAINED_FIXUPS` (no rebases → position-independent
  apart from the GOT).
- LZMA-compress the image; hand-build the signed Mach-O (M0 machinery) with the
  frameworks as `LC_LOAD_DYLIB`s and `_dlsym` / `_compression_decode_buffer` bound
  via `LC_DYLD_INFO` opcodes.
- Stub: `mmap` RW the full vmsize (syscall), decompress, `dlsym` each import into
  the GOT, `mprotect` `__TEXT` r-x, jump to entry.

Gotchas that cost debugging time (do not reintroduce):
- `dlsym` wants the C name WITHOUT the leading `_` (it adds it) — pass `glClear`,
  not `_glClear`, or every GOT slot resolves to 0 and the intro jumps to PC=0.
- The chained-fixups `segment_offset` already equals the segment file offset; do
  not add it again when walking the chain.
- `__DATA` is given `filesize 0` so it costs no file bytes (zero-fill, dyld binds
  into the mapped tail); that is what keeps the file at one page + `__LINKEDIT`.
- `compression_decode_buffer` is in libcompression (dyld shared cache, loads by
  path), not libSystem.

Tests: `tools/pack_selftest.sh` (skeleton on self-contained payloads),
`tools/intro_verify.sh` (packed intro vs unpacked: audio bit-identical, only
clock-seeded frames differ).

## oneKpaq vs LZMA for the arm64 packer (read before M3)

Testing M2 on the real intro payload surfaced a decision:

- **oneKpaq's encoder emits a broken single-section asm stream for intro-like
  content** (modes 1 and 3 both abort the self-verify with "End of section not
  detected"). Confirmed three ways: the encoder's own self-verify aborts; our
  decoder desyncs (byte 49); and the real x86-64 asm decoder *hangs* (never finds
  end-of-section). The *standard* (non-asm) stream verifies, but the compact asm
  decoders don't consume that. This is upstream, content-dependent (400 bytes of
  the intro fails; 400 bytes of repetitive text is fine), not our bug.
- **On arm64 the oneKpaq size advantage is moot.** The file rounds up to one
  16 KB page regardless. The intro payload (~11 KB dense) LZMA-compresses to
  ~5.2 KB; payload + stub + header + signature fits in one 16384-byte page — the
  same final file size oneKpaq would give if it worked. Here LZMA is also *smaller*
  than oneKpaq's (broken) ~6.5 KB for this payload.
- **LZMA needs no embedded decoder** — `dlopen`/`dlsym` `compression_decode_buffer`
  (`COMPRESSION_LZMA`), exactly the x86-64 pack.py fallback. Smaller, simpler stub
  than the oneKpaq decoder + 80-bit x87.

**Recommendation: M3 uses LZMA.** The M2 oneKpaq decoder stays as a verified
artifact (correct — it round-trips everything the encoder validly emits) and
could be used if the oneKpaq mode-3 encoder bug were ever fixed, but it is not
needed to hit the one-page floor.

## Layout

```
probes/        M0/M1 experiments (minsign.py, wx.c)
decoder/       the oneKpaq mode-3 decoder (M2), all verified
  xsqrt.S      80-bit software fsqrt (the one new x87 primitive)
  x87.S        80-bit xadd/xdiv/xfist32 (from the intro) + xfromint
  subrange.S   CalculateSubRange (calc_subrange) using the above
  okp_decode.c the full decoder (C): ArithDecoder + header + context scan
  x87_c.S      C-callable wrappers around the 80-bit ops
  test/        sqrt_test.sh, subrange_test.sh, decode_test.sh (+ harness .c)
tools/
  build_encoder.sh  build the oneKpaq encoder (x86-64, Rosetta); also emits "H"
  roundtrip_ref.sh  encode + decode through the real x86-64 decompressor
build/         gitignored; holds the encoder binary and scratch
```

## Build & test

Everything builds with the Xcode command-line clang; `nasm` (brew) is needed only
for the x86-64 reference decompressor. The oneKpaq **encoder only builds for
x86-64** (it uses x87 inline asm) and runs under Rosetta 2.

    tools/build_encoder.sh              # -> build/onekpaq  (clones oneKpaq to /tmp)
    bash decoder/test/sqrt_test.sh      # 80-bit fsqrt vs real x87  (50k cases)
    bash decoder/test/subrange_test.sh  # CalculateSubRange vs encoder (20k cases)
    bash decoder/test/decode_test.sh    # full decoder round-trip (spread of inputs)
    tools/roundtrip_ref.sh <file>       # ground-truth decode via the x86-64 asm

Run all three decoder tests after any change under `decoder/`. They compare
against real x87 / the real encoder, which is the only trustworthy check — a
context-mixing decoder fails *silently* (plausible garbage, not a crash).

To debug a decode divergence: build `okp_decode.c` with `-DOKP_DBG` for a per-bit
`range`/`sub` trace, and compare against the encoder's own decode (patch a dump
into `BlockCodec::Decode` in the cloned `/tmp/onekpaq_src`, rebuild with
`build_encoder.sh`'s compile lines). The encoder verifies **two** decoders in
sequence — Standard first, then SingleAsm; the decoder here is **SingleAsm**
(`value -= range + 1`), so compare against the *second* trace.

## Facts that bite

- **80-bit precision is mandatory.** oneKpaq's subrange is computed in x87
  `long double` with `fsqrt`/`fistl`, and the integer arithmetic coder needs the
  encoder and decoder to agree on every subrange. Plain `double` desyncs. This is
  why `decoder/` carries a software 80-bit x87. Do not "simplify" it to doubles.
- **The decoder is C on purpose.** arm64's file floor is one 16 KB page, so the
  decompressor need not be hyper-compact the way the x86-64 one is. C is far
  easier to keep correct; it calls the asm only for the 80-bit ops.
- **Unsigned matters in the context scan.** oneKpaq's `byteLookup` takes an
  *unsigned* pos; `bytePos - i` underflows to a huge value → returns 0. Porting
  it with signed ints reads out of bounds (this bug cost a debugging session).
- **The encoder's own asm-stream self-verify aborts on some inputs**
  ("End of section not detected"). That is upstream oneKpaq, not our decoder. Our
  decoder round-trips everything the encoder *successfully* emits. M3/M4 must
  confirm the real intro payload encodes cleanly, or chunk it.
- **On-disk format** (what the encoder writes): `file = src1 ++ src2 ++ 4 zeros`,
  `offset = len(src1)`. The arith stream the decoder consumes is
  `file[offset+4 : len-4]`. The clean header (models + rawLength) is emitted
  separately by the patched encoder as an `H <hex>` line; `pack.py` bakes it in,
  so the reversed on-disk asm header is never parsed.
- **Encoder is slow** on larger inputs (model search is O(bits × models ×
  iterations)); multi-KB inputs can take minutes. Run encodes in the background.

## M3 findings so far (probes/bind.py)

- **Import binding works via legacy `LC_DYLD_INFO_ONLY` bind opcodes on arm64** —
  no chained fixups needed. `probes/bind.py` binds `_write` into a `__DATA` slot
  and calls it from a hand-built, self-signed Mach-O. So the stub can bind
  `compression_decode_buffer` / `dlsym` directly through dyld.
- **W^X forces a writable `__DATA` segment** for the bound pointers — the x86-64
  trick of parking them in an RWX `__TEXT` page is gone. That pushes the minimum
  past one page (bind.py is 33 KB = __TEXT + __DATA + __LINKEDIT). A `__DATA` with
  `filesize 0` (zero-fill, dyld binds into the mapped tail) may shrink this toward
  ~17 KB — untested; worth trying.
- **The intro links directly against frameworks** (OpenGL/CGL/CoreAudio/CoreGraphics
  via dyld chained fixups), not `dlopen`/`dlsym` like x86-64. So its GOT must be
  bound. When the payload is decompressed into fresh memory, dyld has not bound it,
  so the **stub must resolve the intro's imports itself**: bind `dlsym` via dyld,
  then `dlsym` each intro import by name and write it into the decompressed GOT.
  pack.py must extract the intro's import names + GOT slot offsets from its
  `LC_DYLD_CHAINED_FIXUPS`. The intro's code is PC-relative (`adrp`), so the payload
  can load at any base as long as its segments stay contiguous; watch for absolute
  pointers in its data (AudioQueue callback, display-list/data pointers) that would
  need relocating too. This is more involved than the x86-64 packer, where the
  intro resolved its own imports.

## M3 plan (next) — LZMA path (recommended)

`tools/pack.py`, modelled on `~/dev/fzn_east4k/ports/macos-x86_64/tools/pack.py`
and the M0 header code in `probes/minsign.py`:

1. Get the intro payload (the loadable bytes to pack — the dense `__TEXT`/`__DATA`
   content, not the 16 KB-padded file).
2. Compress it with Python `lzma` (FORMAT_XZ, LZMA2, preset 9|EXTREME, lc=0 lp=0
   pb=0 — same as x86-64 pack.py).
3. Hand-build a signed arm64 Mach-O (one 16 KB page): dyld load commands (M0),
   `__TEXT` holding header + stub + packed payload, and the ad-hoc
   `LC_CODE_SIGNATURE` built with `hashlib` (M0).
4. Stub: `dlopen`/`dlsym` `compression_decode_buffer`; `mmap` RW the output size
   (M1), decode into it, `mprotect` RX, jump. (Simpler than embedding a decoder.)
5. Verify: the packed binary renders the same frames / tune as the linked build
   (use the intro's own `make verify` / offscreen path).

If instead using oneKpaq (only worth it if the mode-3 encoder bug is fixed): step
2 is `build/onekpaq 3 1` capturing packed stream + `offset` + `shift` + the `H`
header; step 4 is `okp_decode` with baked models/shift/rawLength.

## Conventions

- Commit per milestone/step with a passing test; keep `README.md`,
  `decoder/README.md`, and this file's Status in sync.
- Attribution on commits: `Co-Authored-By: Claude <noreply@anthropic.com>`
  (match the session's stated attribution).
- Don't commit `build/`. Don't commit the cloned oneKpaq source (`/tmp`).
- The project memory for cross-session context lives in the fzn_east4k project's
  memory dir (`arm64-packer-project.md`).
