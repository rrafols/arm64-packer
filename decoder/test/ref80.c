/* x86-64 reference for the 80-bit fsqrt: real x87 long double, run under
 * Rosetta 2. Reads "<mant_hex> <exp_dec>" lines (value = mant * 2^exp, mant a
 * normalised 64-bit significand), prints "<mant_hex> <exp_dec>" of the sqrt in
 * the same normalised form. Build: clang -arch x86_64 -O2 -o ref80 ref80.c
 */
#include <stdio.h>
#include <stdint.h>
#include <math.h>

int main(void) {
    unsigned long long m;
    int e;
    while (scanf("%llx %d", &m, &e) == 2) {
        if (m == 0) { printf("0 0\n"); continue; }
        long double v = ldexpl((long double)m, e);   /* exact: 64-bit signif. */
        long double r = sqrtl(v);
        int E;
        long double mant = frexpl(r, &E);            /* r = mant * 2^E, mant in [.5,1) */
        unsigned long long om = (unsigned long long)(mant * 18446744073709551616.0L); /* *2^64 */
        printf("%llx %d\n", om, E - 64);
    }
    return 0;
}
