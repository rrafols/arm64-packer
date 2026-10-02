// ---------------------------------------------------------------------------
//  C-callable wrappers around the 80-bit software x87 (x87.S / xsqrt.S), for the
//  C decompressor. A value is a struct V { uint64_t m; int32_t e; int32_t s; }
//  (16 bytes: m at +0, e at +8, s at +12), matching the A/B register ABI.
//
//  The underlying routines are leaves touching x0-x17, so these save the live
//  pointer(s) in callee-saved registers across the call.
// ---------------------------------------------------------------------------

    .text
    .p2align 2

// void v_sqrt(V *a)   a = sqrt(a)
    .globl  _v_sqrt
_v_sqrt:
    stp     x29, x30, [sp, #-32]!
    str     x19, [sp, #16]
    mov     x19, x0
    ldr     x0, [x19]
    ldr     w1, [x19, #8]
    ldr     w2, [x19, #12]
    bl      _xsqrt
    str     x0, [x19]
    str     w1, [x19, #8]
    str     w2, [x19, #12]
    ldr     x19, [sp, #16]
    ldp     x29, x30, [sp], #32
    ret

// void v_div(V *a, const V *b)   a = a / b
    .globl  _v_div
_v_div:
    stp     x29, x30, [sp, #-32]!
    stp     x19, x20, [sp, #16]
    mov     x19, x0
    mov     x20, x1
    ldr     x0, [x19]
    ldr     w1, [x19, #8]
    ldr     w2, [x19, #12]
    ldr     x3, [x20]
    ldr     w4, [x20, #8]
    ldr     w5, [x20, #12]
    bl      _xdiv
    str     x0, [x19]
    str     w1, [x19, #8]
    str     w2, [x19, #12]
    ldp     x19, x20, [sp, #16]
    ldp     x29, x30, [sp], #32
    ret

// void v_add(V *a, const V *b)   a = a + b
    .globl  _v_add
_v_add:
    stp     x29, x30, [sp, #-32]!
    stp     x19, x20, [sp, #16]
    mov     x19, x0
    mov     x20, x1
    ldr     x0, [x19]
    ldr     w1, [x19, #8]
    ldr     w2, [x19, #12]
    ldr     x3, [x20]
    ldr     w4, [x20, #8]
    ldr     w5, [x20, #12]
    bl      _xadd
    str     x0, [x19]
    str     w1, [x19, #8]
    str     w2, [x19, #12]
    ldp     x19, x20, [sp, #16]
    ldp     x29, x30, [sp], #32
    ret

// void v_fromint(int64_t i, V *out)
    .globl  _v_fromint
_v_fromint:
    stp     x29, x30, [sp, #-32]!
    str     x19, [sp, #16]
    mov     x19, x1
    bl      _xfromint
    str     x0, [x19]
    str     w1, [x19, #8]
    str     w2, [x19, #12]
    ldr     x19, [sp, #16]
    ldp     x29, x30, [sp], #32
    ret

// uint32_t v_fist32(const V *a)
    .globl  _v_fist32
_v_fist32:
    stp     x29, x30, [sp, #-16]!
    ldr     w1, [x0, #8]
    ldr     w2, [x0, #12]
    ldr     x0, [x0]
    bl      _xfist32
    ldp     x29, x30, [sp], #16
    ret
