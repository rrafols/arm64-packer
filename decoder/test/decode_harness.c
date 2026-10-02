/* Drives okp_decode over an encoder output and checks it against the original.
 *   decode_harness <packed> <offset> <shift> <header_hex> <orig>
 * arith stream = packed[offset+4 : len-4]; header comes from the encoder's "H" line.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

void okp_decode(const uint8_t *arith, size_t arith_len,
                const uint8_t *header, int header_len, uint32_t shift,
                uint8_t *out, uint32_t raw_len);

static uint8_t *readf(const char *p, long *n) {
    FILE *f = fopen(p, "rb"); fseek(f, 0, SEEK_END); *n = ftell(f); rewind(f);
    uint8_t *b = malloc(*n); fread(b, 1, *n, f); fclose(f); return b;
}

int main(int argc, char **argv) {
    long pn, on;
    uint8_t *packed = readf(argv[1], &pn);
    long offset = atol(argv[2]);
    uint32_t shift = (uint32_t)atol(argv[3]);
    const char *hhex = argv[4];
    uint8_t *orig = readf(argv[5], &on);

    int hlen = strlen(hhex) / 2;
    uint8_t header[256];
    for (int i = 0; i < hlen; i++) sscanf(hhex + 2*i, "%2hhx", &header[i]);

    const uint8_t *arith = packed + offset + 4;
    long arith_len = pn - offset - 4 - 4;        /* drop the trailing 4 zeros */
    if (arith_len < 0) arith_len = 0;

    uint8_t *out = calloc(1, on + 16);
    okp_decode(arith, arith_len, header, hlen, shift, out, (uint32_t)on);

    long bad = 0, first = -1;
    for (long i = 0; i < on; i++)
        if (out[i] != orig[i]) { if (first < 0) first = i; bad++; }
    if (!bad) printf("DECODE OK - all %ld bytes match\n", on);
    else printf("MISMATCH: %ld of %ld differ, first at %ld (%02x vs %02x)\n",
               bad, on, first, out[first], orig[first]);
    return bad != 0;
}
