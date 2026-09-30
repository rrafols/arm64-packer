# arm64-packer

A size-coding packer and hand-built Mach-O header for AArch64 macOS, aimed at
shrinking the Apple Silicon port of the *Looking for the East* 4k intro
(`~/dev/fzn_east4k/ports/macos-arm64`) from its current ~52 KB linked binary
toward the 4k class the other ports reach.

This is the arm64 counterpart of `fzn_east4k/packers/onekpaq64` (x86-64) and
`fzn_east4k/ports/macos-x86_64/tools/pack.py` (the hand-built header + loader
stub). Those work and are the reference; this project has to solve three things
they never had to.

## Why arm64 macOS is harder than x86-64 macOS

1. **Mandatory code signature.** The arm64e/arm64 kernel refuses to exec an
   unsigned Mach-O — there is no "ad-hoc is optional" as there effectively was
   for x86-64. So the file must carry an `LC_CODE_SIGNATURE` load command and a
   `__LINKEDIT` code-signature blob (a `CS_SuperBlob` with a `CodeDirectory`
   holding a SHA-256 per page). That is real, incompressible bytes we cannot
   avoid, and it constrains the layout: the signature must cover the whole file
   and live at the end of `__LINKEDIT`.

2. **W^X is enforced.** The x86-64 stub unpacks into an RWX `__TEXT` and jumps
   in. Apple Silicon does not allow a page to be both writable and executable
   under the default policy. Options, cheapest first:
   - unpack into a separate writable segment, then `mprotect` it to `r-x`
     before jumping (one syscall, needs the address/len);
   - `MAP_JIT` + `pthread_jit_write_protect_np()` toggling (more setup);
   - unpack in place in a page mapped `rw-`, `mprotect` to `r-x`.
   The mprotect route looks smallest. Whichever we pick, the "unpack into the
   zero-filled tail of an RWX __TEXT" trick from x86-64 is gone.

3. **16 KB pages.** `getpagesize()` is 16384. The Mach-O minimum file size is a
   page, so the floor is 16 KB, not 4 KB. A "4k intro" cannot mean a 4096-byte
   file here; the honest target is smallest-possible, and the interesting number
   is the packed payload size, not the file size the kernel rounds up to.

## The decompressor

Port oneKpaq's mode-3 (single-section, fast) decoder to AArch64, the way
`onekpaq64` ported it to x86-64. The x86-64 port is 171 bytes and is the spec:
same register roles, same algorithm, round-trip tested against the real encoder.

The fast decoder leans on SSE (`pcmpeqb`/`pmovmskb`) for the match model and on
the x87 FPU (`fld1`/`fidivr`/`fsqrt`/`fistp`) for the probability arithmetic.
On arm64:
- `pcmpeqb` + `pmovmskb` → NEON `cmeq` on 8 bytes + a mask reduction (`shrn` /
  `addv` trick), or a scalar compare since only equality-to-`-1` is needed.
- the x87 subrange maths → double-precision scalar `fdiv`/`fsqrt`/`fcvtns`.
  Care needed: oneKpaq's model uses the x87 stack and `fidivr` semantics; the
  arithmetic must stay bit-identical to the encoder's model or decode diverges
  silently (a context-mixing decoder that is subtly wrong produces plausible
  garbage, not a crash — same warning as the x86-64 port).

The encoder is unchanged: `onekpaq 3 1 in out.okp`, giving `offset=` and
`shift=`.

## Layout plan

```
probes/         standalone experiments, each independently runnable
  minsign.py    smallest signed arm64 Mach-O the kernel will exec (baseline)
  mprotect.s    unpack-then-mprotect W^X path, proven on a trivial payload
decoder/        the AArch64 oneKpaq mode-3 decompressor + round-trip tests
tools/
  pack.py       build the hand Mach-O: header, stub, packed payload, signature
README.md
```

## Milestones

- [x] M0  smallest signed arm64 Mach-O that execs and returns 42 (baseline size)
- [ ] M1  W^X probe: map rw-, write code, mprotect r-x, call it
- [ ] M2  AArch64 oneKpaq mode-3 decoder, round-trip vs real encoder
- [ ] M3  pack.py: hand header + stub + packed payload, self-signed, runs
- [ ] M4  apply to the intro payload; measure vs the 52 KB linked build

## Status

**M0 done** — `probes/minsign.py` builds, signs, and runs a 16384-byte arm64
executable with no clang and no `codesign`; the kernel runs it and `codesign -v`
verifies it. What it settled:

* **dyld is mandatory.** A static `LC_UNIXTHREAD` binary is SIGKILLed on Apple
  Silicon; x86-64 ran the same shape. The fix is the dyld set the x86-64 packer
  already uses (`LC_LOAD_DYLINKER` + `LC_MAIN` + `LC_LOAD_DYLIB`), which the
  intro needs anyway for `dlopen`/`dlsym`. No net cost.
* **The signature is mandatory and must be ad-hoc-flagged.** An
  `LC_CODE_SIGNATURE` → `CSMAGIC_EMBEDDED_SIGNATURE` SuperBlob → v20400
  CodeDirectory, one SHA-256 per 4096 bytes up to `codeLimit` = the signature's
  own offset. `pack.py` computes this with `hashlib`; no external tool. The CD
  `flags` field needs `CS_ADHOC` or `codesign` reports "not signed at all"
  (the kernel runs it either way).
* **The file floor is one 16 KB page.** A 702-byte file is SIGKILLed; a
  16384-byte file with header, code and signature all inside that one page runs
  and verifies. So the arm64 floor is 16384 bytes — 4× the x86-64 floor, purely
  because arm64 pages are 4× larger. The *packed payload* is the number worth
  minimising, not the file size the kernel rounds up to.

Next: M1, the W^X unpack path (arm64 has no RWX to unpack into, unlike the
x86-64 stub).
