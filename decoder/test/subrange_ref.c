/* x86-64 reference for CalculateSubRange: the encoder's exact long-double path,
 * including its inline fsqrt/fistl, run under Rosetta 2.
 * Build: clang -arch x86_64 -O2 -o subrange_ref subrange_ref.c
 * Input per case:  "<n> <range>" then n lines "<c0> <c1> <weight>".  Output: subrange.
 */
#include <stdio.h>
#include <stdint.h>

int main(void) {
    int n; unsigned long range;
    while (scanf("%d %lu", &n, &range) == 2) {
        int c0[512], c1[512], w[512];
        for (int i = 0; i < n; i++) scanf("%d %d %d", &c0[i], &c1[i], &w[i]);
        long double p = 1;
        unsigned weight = n ? (unsigned)w[0] : 1;
        for (int i = 0; i < n; i++) {
            while (weight != (unsigned)w[i]) {
                asm volatile("fsqrt\n" : "=t"(p) : "0"(p));
                weight >>= 1;
            }
            p = (long double)(int)c0[i] / p;
            p = (long double)(int)c1[i] / p;
        }
        while (weight != 1) {
            asm volatile("fsqrt\n" : "=t"(p) : "0"(p));
            weight >>= 1;
        }
        unsigned ret;
        asm volatile("fistl %0\n" : "=m"(ret) : "t"((long double)(int)range / (1 + p)));
        printf("%u\n", ret);
    }
    return 0;
}
