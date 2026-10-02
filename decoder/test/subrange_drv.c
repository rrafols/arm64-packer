/* arm64 driver: same contract as subrange_ref.c, via calc_subrange in
 * ../subrange.S. Build: clang -arch arm64 -o subrange_drv subrange_drv.c \
 *   ../subrange.S ../x87.S ../xsqrt.S
 */
#include <stdio.h>
#include <stdint.h>

extern uint32_t calc_subrange(const int32_t *triples, int n, uint32_t range);

int main(void) {
    int n; unsigned long range;
    while (scanf("%d %lu", &n, &range) == 2) {
        int32_t t[512 * 3];
        for (int i = 0; i < n; i++)
            scanf("%d %d %d", &t[i*3], &t[i*3+1], &t[i*3+2]);
        printf("%u\n", calc_subrange(t, n, (uint32_t)range));
    }
    return 0;
}
