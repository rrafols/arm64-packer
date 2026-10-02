// ---------------------------------------------------------------------------
//  oneKpaq CalculateSubRange, in AArch64 with the 80-bit software x87.
//
//  The exact computation from BlockCodec.cpp (QWContextModel specialisation):
//
//      long double p = 1;
//      uint weight = def[0].weight;
//      for (auto &it : def) {
//          while (weight != it.weight) { p = sqrtl(p); weight >>= 1; }
//          p = (long double)(int)it.c0 / p;
//          p = (long double)(int)it.c1 / p;
//      }
//      while (weight != 1) { p = sqrtl(p); weight >>= 1; }
//      return (u32) lrintl( (long double)(int)range / (1 + p) );   // fistl
//
//  Must be bit-identical to the encoder or the arithmetic coder desyncs, so the
//  whole thing runs through xadd/xdiv/xsqrt/xfist32 (80-bit, round-to-nearest-
//  even) rather than doubles.
//
//  C ABI:  uint32_t calc_subrange(const int32_t *triples, int n, uint32_t range)
//  triples is n consecutive {c0, c1, weight} int32 groups.
//
//      x19:w20:w21  p  (mantissa / exponent / sign), kept across the leaf calls
//      x22  triples   w23  n remaining   w24  running weight
//      w25  range     w26  this triple's weight
// ---------------------------------------------------------------------------

    .text
    .p2align 2
    .globl  _calc_subrange
_calc_subrange:
    stp     x29, x30, [sp, #-80]!
    mov     x29, sp
    stp     x19, x20, [sp, #16]
    stp     x21, x22, [sp, #32]
    stp     x23, x24, [sp, #48]
    stp     x25, x26, [sp, #64]

    mov     x22, x0
    mov     w23, w1
    mov     w25, w2

    mov     x19, #0x8000000000000000     // p = 1.0
    mov     w20, #-63
    mov     w21, #0

    cbz     w23, 6f                      // empty def: fall through to 1 + p
    ldr     w24, [x22, #8]               // weight = def[0].weight

1:  ldr     w26, [x22, #8]               // it.weight
2:  cmp     w24, w26
    b.eq    3f
    mov     x0, x19                      // p = sqrt(p)
    mov     w1, w20
    mov     w2, w21
    bl      _xsqrt
    mov     x19, x0
    mov     w20, w1
    mov     w21, w2
    lsr     w24, w24, #1
    b       2b

3:  ldrsw   x0, [x22, #0]                // p = c0 / p
    bl      _xfromint
    mov     x3, x19
    mov     w4, w20
    mov     w5, w21
    bl      _xdiv
    mov     x19, x0
    mov     w20, w1
    mov     w21, w2
    ldrsw   x0, [x22, #4]                // p = c1 / p
    bl      _xfromint
    mov     x3, x19
    mov     w4, w20
    mov     w5, w21
    bl      _xdiv
    mov     x19, x0
    mov     w20, w1
    mov     w21, w2

    add     x22, x22, #12
    subs    w23, w23, #1
    b.ne    1b

5:  cmp     w24, #1                      // while (weight != 1) p = sqrt(p)
    b.eq    6f
    mov     x0, x19
    mov     w1, w20
    mov     w2, w21
    bl      _xsqrt
    mov     x19, x0
    mov     w20, w1
    mov     w21, w2
    lsr     w24, w24, #1
    b       5b

6:  mov     x0, #0x8000000000000000      // t = 1 + p
    mov     w1, #-63
    mov     w2, #0
    mov     x3, x19
    mov     w4, w20
    mov     w5, w21
    bl      _xadd
    mov     x19, x0                      // reuse p slot for t
    mov     w20, w1
    mov     w21, w2

    sxtw    x0, w25                      // range / t
    bl      _xfromint
    mov     x3, x19
    mov     w4, w20
    mov     w5, w21
    bl      _xdiv
    bl      _xfist32                      // w0 = round(range / t)

    ldp     x25, x26, [sp, #64]
    ldp     x23, x24, [sp, #48]
    ldp     x21, x22, [sp, #32]
    ldp     x19, x20, [sp, #16]
    ldp     x29, x30, [sp], #80
    ret
