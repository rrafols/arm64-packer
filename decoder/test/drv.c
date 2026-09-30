/* arm64 driver: same stdin/stdout contract as ref80.c, but computes the sqrt
 * with the software xsqrt in ../xsqrt.S. Build:
 *   clang -arch arm64 -o drv drv.c ../xsqrt.S
 */
#include <stdio.h>
#include <stdint.h>

extern void xsqrt_c(uint64_t m, int32_t e, int32_t s,
                    uint64_t *out_m, int32_t *out_e);

int main(void) {
    unsigned long long m;
    int e;
    while (scanf("%llx %d", &m, &e) == 2) {
        uint64_t om; int32_t oe;
        xsqrt_c((uint64_t)m, e, 0, &om, &oe);
        printf("%llx %d\n", (unsigned long long)om, oe);
    }
    return 0;
}
