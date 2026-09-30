// ---------------------------------------------------------------------------
//  80-bit software fsqrt, matching x87 `fsqrt` under `finit` (64-bit mantissa,
//  round to nearest, ties to even).  This is the one primitive the oneKpaq
//  decoder needs that the intro's x87.S does not already provide.
//
//  Same value representation as ../../fzn_east4k/ports/macos-arm64/src/x87.S:
//
//      A:  x0 mantissa   w1 exponent   w2 sign      value = (-1)^s * m * 2^k
//
//  with 2^63 <= m < 2^64, or m == 0 for zero.  A leaf: only x0-x17.
//
//  sqrt(m * 2^e):  scale to a 128-bit radicand R = m << (64 - (e & 1)), which
//  lies in [2^126, 2^128), so its integer square root M is a normalised 64-bit
//  mantissa in [2^63, 2^64).  The remainder R - M*M rounds it:
//
//      round up  iff  rem > M   (the exact-half case rem == M + 1/4 cannot
//                                occur for integer R, so ties never arise and
//                                nearest-even collapses to nearest)
//
//  exponent  f = (e >> 1) + (e & 1) - 32   (arithmetic shift = floor).
//
//  fsqrt of a negative is the x87 "real indefinite"; the decoder never asks for
//  one (probabilities are positive), so a negative input is treated as its
//  magnitude rather than spending bytes on a NaN nobody sees.
// ---------------------------------------------------------------------------

    .text
    .p2align 2
    .globl  _xsqrt
_xsqrt:
xsqrt:
    cbz     x0, 9f                      // sqrt(0) = 0 (fields already 0-ish)

    // R = m << (64 - (e & 1)), as the 128-bit pair x6:x7 (hi:lo)
    and     w8, w1, #1                  // p = e & 1
    cbnz    w8, 1f
    mov     x6, x0                      // p == 0: R = m << 64  -> hi=m, lo=0
    mov     x7, #0
    b       2f
1:  lsr     x6, x0, #1                  // p == 1: R = m << 63
    lsl     x7, x0, #63
2:
    // 128-bit integer square root of x6:x7 by digit recurrence.  Two radicand
    // bits enter the remainder each step; root grows one bit; 64 steps.
    //   rem (x10:x11) : running remainder, up to ~2^66
    //   root (x12)    : result so far, ends in [2^63, 2^64)
    //   t   (x14:x13) : trial 2*root+1, up to 2^65
    mov     x10, #0                     // rem hi
    mov     x11, #0                     // rem lo
    mov     x12, #0                     // root
    mov     w9, #64                     // step counter
3:  // rem = (rem << 2) | (top two bits of R);  R <<= 2
    extr    x10, x10, x11, #62          // rem <<= 2, pulling from lo
    lsl     x11, x11, #2
    orr     x11, x11, x6, lsr #62       // bring in R's top two bits
    extr    x6, x6, x7, #62             // R <<= 2
    lsl     x7, x7, #2

    // trial subtrahend t = (root << 2) | 1  (128-bit: x14:x13), from the OLD
    // root: rem_{i+1} = 4*rem_i + b - x*(4*P_i + 1)
    lsr     x14, x12, #62               // t hi = root >> 62
    lsl     x13, x12, #2
    orr     x13, x13, #1                // t lo = (root << 2) | 1

    lsl     x12, x12, #1                // root <<= 1 (x = 0 case)

    // if rem >= t : rem -= t ; root |= 1
    subs    x15, x11, x13               // rem - t (128-bit)
    sbcs    x16, x10, x14
    b.cc    4f                          // rem < t: bit stays 0
    mov     x11, x15
    mov     x10, x16
    orr     x12, x12, #1
4:  subs    w9, w9, #1
    b.ne    3b

    // rounding: round up iff rem > root.  rem is x10:x11, root is x12.
    cbnz    x10, 5f                     // rem hi != 0 -> rem > root (root < 2^64)
    cmp     x11, x12
    b.ls    6f                          // rem <= root: truncate
5:  adds    x12, x12, #1                // round up
    b.cc    6f
    mov     x12, #0x8000000000000000    // carried out of bit 63: 2^64 -> 2^63
    // exponent gets its +1 below via the carry-adjusted path; simplest is to
    // bump e so the shared f computation stays correct
    add     w1, w1, #2                  // +1 to f == +2 to e before the >>1
6:  mov     x0, x12

    // f = (e >> 1) + (e & 1) - 32
    and     w8, w1, #1
    asr     w1, w1, #1
    add     w1, w1, w8
    sub     w1, w1, #32
    mov     w2, #0                      // result is non-negative
9:  ret

// C-callable shim for the test harness:
//   xsqrt_c(uint64_t m, int32_t e, int32_t s, uint64_t *out_m, int32_t *out_e)
    .globl  _xsqrt_c
    .p2align 2
_xsqrt_c:
    stp     x29, x30, [sp, #-16]!
    bl      xsqrt
    str     x0, [x3]
    str     w1, [x4]
    ldp     x29, x30, [sp], #16
    ret
