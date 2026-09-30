// ---------------------------------------------------------------------------
//  80-bit software x87, the subset oneKpaq's CalculateSubRange needs.
//
//  Value representation, identical to the intro's x87.S:
//      A:  x0 mantissa   w1 exponent   w2 sign      value = (-1)^s * m * 2^k
//      B:  x3 mantissa   w4 exponent   w5 sign
//  with 2^63 <= m < 2^64, or m == 0 for zero.  Results in A.  Leaves touch x0-x17.
//
//  xadd, xdiv, xround, xfist and xfist32 are lifted verbatim from
//  ../../fzn_east4k/ports/macos-arm64/src/x87.S (bit-verified against real x87
//  there); xsin/xmul/xfromd/xtod are left out because the decoder does not use
//  them and xsin drags in libm. xfromint is new: (long double)(int) as the
//  encoder's CalculateSubRange does for the counts and the range. xsqrt lives in
//  xsqrt.S.
// ---------------------------------------------------------------------------

    .text
    .p2align 2

// A = (int64) x0, exactly. Sign from the input's sign bit; magnitude normalised.
// The encoder uses (long double)(int)count and (int)range, both non-negative
// here, but a full signed conversion costs nothing and is safer.
    .globl  _xfromint
_xfromint:
xfromint:
    cbz     x0, .Lxzero
    tbz     x0, #63, 1f                 // negative: sign 1, magnitude
    neg     x0, x0
    mov     w2, #1
    b       2f
1:  mov     w2, #0
2:  clz     x6, x0
    lsl     x0, x0, x6                  // normalise: bit 63 set
    neg     w1, w6                      // i = (i << clz) * 2^(-clz), so k = -clz
    ret

// A = A + B
    .globl  _xadd
_xadd:
xadd:
    cbz     x3, 9f                      // b == 0: a
    cbz     x0, 8f                      // a == 0: b
    cmp     w1, w4                      // put the larger exponent in A
    b.ge    1f
    mov     x6, x0
    mov     x0, x3
    mov     x3, x6
    mov     w6, w1
    mov     w1, w4
    mov     w4, w6
    mov     w6, w2
    mov     w2, w5
    mov     w5, w6
1:  sub     w6, w1, w4                  // d = exponent gap, >= 0

    cbz     w6, 2f
    cmp     w6, #64
    b.lt    3f
    b.eq    4f
    cmp     w6, #128
    b.ge    5f
    sub     w9, w6, #64
    lsr     x8, x3, x9
    mov     w10, #128
    sub     w10, w10, w6
    lsl     x11, x3, x10
    cmp     x11, #0
    cset    x11, ne
    orr     x8, x8, x11
    mov     x7, #0
    b       6f
2:  mov     x7, x3
    mov     x8, #0
    b       6f
3:  lsr     x7, x3, x6
    neg     w9, w6
    lsl     x8, x3, x9
    b       6f
4:  mov     x7, #0
    mov     x8, x3
    b       6f
5:  mov     x7, #0
    mov     x8, #1

6:  cmp     w2, w5
    b.ne    .Lxsub
    mov     x9, x8
    adds    x10, x0, x7
    b.cc    xround
    and     x11, x9, #1
    extr    x9, x10, x9, #1
    orr     x9, x9, x11
    lsr     x10, x10, #1
    orr     x10, x10, #0x8000000000000000
    add     w1, w1, #1
    b       xround

.Lxsub:
    cbnz    w6, 7f
    cmp     x0, x3
    b.eq    .Lxzero
    b.hi    7f
    mov     w2, w5
    sub     x10, x3, x0
    mov     x9, #0
    b       .Lxnorm
7:  negs    x9, x8
    sbc     x10, x0, x7
.Lxnorm:
    clz     x11, x10
    cbz     x11, xround
    lsl     x10, x10, x11
    neg     x12, x11
    lsr     x12, x9, x12
    orr     x10, x10, x12
    lsl     x9, x9, x11
    sub     w1, w1, w11

xround:
    mov     x0, x10
    tbz     x9, #63, 2f
    lsl     x11, x9, #1
    cbnz    x11, 1f
    tbz     x0, #0, 2f
1:  adds    x0, x0, #1
    b.cc    2f
    mov     x0, #0x8000000000000000
    add     w1, w1, #1
2:  ret

.Lxzero:
    mov     x0, #0
    mov     w1, #0
    mov     w2, #0
    ret
8:  mov     x0, x3
    mov     w1, w4
    mov     w2, w5
9:  ret

// A = A / B.  Plain long division: a 64-bit quotient, remainder rounds.
    .globl  _xdiv
_xdiv:
xdiv:
    cbz     x0, .Lxzero
    eor     w2, w2, w5
    sub     w1, w1, w4
    cmp     x0, x3
    b.lo    1f
    lsr     x6, x0, #1
    lsl     x7, x0, #63
    sub     w1, w1, #63
    b       2f
1:  mov     x6, x0
    mov     x7, #0
    sub     w1, w1, #64
2:  mov     x8, #0
    mov     w9, #64
3:  lsr     x10, x6, #63
    extr    x6, x6, x7, #63
    lsl     x7, x7, #1
    lsl     x8, x8, #1
    cbnz    x10, 4f
    cmp     x6, x3
    b.lo    5f
4:  sub     x6, x6, x3
    orr     x8, x8, #1
5:  subs    w9, w9, #1
    b.ne    3b
    mov     x0, x8
    lsr     x10, x6, #63
    lsl     x11, x6, #1
    cbnz    x10, 6f
    cmp     x11, x3
    b.hi    6f
    b.lo    7f
    tbz     x0, #0, 7f
6:  adds    x0, x0, #1
    b.cc    7f
    mov     x0, #0x8000000000000000
    add     w1, w1, #1
7:  ret

// x0 = A rounded to nearest integer, ties to even, or INT64_MIN if it overflows.
    .globl  _xfist
_xfist:
xfist:
    cbz     x0, 5f
    tbz     w1, #31, 4f
    neg     w6, w1
    cmp     w6, #64
    b.hi    6f
    b.eq    1f
    lsr     x7, x0, x6
    neg     w8, w6
    lsl     x8, x0, x8
    b       2f
1:  mov     x7, #0
    mov     x8, x0
2:  tbz     x8, #63, 3f
    lsl     x9, x8, #1
    cbnz    x9, 7f
    tbz     x7, #0, 3f
7:  add     x7, x7, #1
3:  tbnz    x7, #63, 4f
    cbz     w2, 8f
    neg     x7, x7
8:  mov     x0, x7
5:  ret
4:  mov     x0, #0x8000000000000000
    ret
6:  mov     x0, #0
    ret

// fistp dword: w0 = A as int32, or 0x80000000 on overflow.
    .globl  _xfist32
_xfist32:
xfist32:
    mov     x15, x30
    bl      xfist
    mov     x30, x15
    sxtw    x6, w0
    cmp     x6, x0
    mov     w7, #0x80000000
    csel    w0, w0, w7, eq
    ret
