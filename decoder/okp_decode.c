/* AArch64 oneKpaq mode-3 decoder (step D), a faithful port of the clean C++
 * decode path: ArithDecoder(SingleAsm) + DecodeHeader + NoLimitQWContextModel +
 * CreateWeightProfile, with the x87 subrange maths done by calc_subrange
 * (subrange.S, 80-bit, bit-exact vs the encoder).
 *
 * This is C, not hand-asm: the arm64 binary rounds up to a 16 KB page regardless,
 * so a compact C decoder that calls the 80-bit asm ops fits with room to spare
 * and is far easier to get right than the dense flag-threaded asm.
 *
 * Entry contract matches what pack.py will provide (no stream parsing here):
 *   okp_decode(arith, arith_len, header, header_len, shift, out, raw_len)
 * where arith = combine[offset+4 : len-4], header = the clean header block.
 */
#include <stdint.h>
#include <string.h>
#ifdef OKP_DBG
#include <stdio.h>
#endif

/* ---- 80-bit subrange, from subrange.S ---- */
extern uint32_t calc_subrange(const int32_t *triples, int n, uint32_t range);

#define MAX_MODELS 128

typedef struct { uint8_t model; uint32_t weight; } Model;

/* ---- header ---- */
/* header[0..1] = rawLength, header[2] = header length, header[3..] = model bytes.
 * Reconstruct models with the weight-doubling rule. Returns model count. */
static int decode_header(const uint8_t *h, int hlen, Model *models, uint32_t *raw_len)
{
    *raw_len = h[0] | ((uint32_t)h[1] << 8);
    int n = 0;
    uint32_t weight = 1, model = 0x100;
    for (int i = 3; i < (int)h[2] && i < hlen; i++) {
        if (h[i] >= model) {
            for (int k = 0; k < n; k++) models[k].weight <<= 1;
            if (h[i] == model) continue;
        }
        models[n].model = h[i];
        models[n].weight = weight;
        n++;
        model = h[i];
    }
    return n;
}

/* ---- ArithDecoder, SingleAsm ---- */
typedef struct {
    const uint8_t *src; size_t src_size;
    unsigned src_pos, dest_pos;
    uint32_t value, range;
    int has_pre_bit;
} ArithDec;

static int ad_bit(ArithDec *d)
{
    int ret = ((d->src_pos >> 3) < d->src_size) &&
              (d->src[d->src_pos >> 3] & (0x80U >> (d->src_pos & 7)));
    d->src_pos++;
    return ret;
}

static void ad_normalize(ArithDec *d)
{
    while (d->range < 0x40000000U) {
        d->range <<= 1;
        d->value <<= 1;
        /* SingleAsm start/anchor/filler bits */
        if (!d->src_pos)       ad_bit(d);       /* start  = 0 */
        if (d->src_pos == 6)   ad_bit(d);       /* anchor = 1 */
        if (d->src_pos == 7)   ad_bit(d);       /* filler = 0 */
        if (ad_bit(d)) d->value++;
    }
}

static void ad_init(ArithDec *d, const uint8_t *src, size_t n)
{
    memset(d, 0, sizeof *d);
    d->src = src; d->src_size = n; d->range = 1;
    ad_normalize(d);
}

static int ad_decode(ArithDec *d, uint32_t sub)
{
    int ret;
    d->range -= sub;
    if (d->value >= d->range) { d->value -= d->range + 1; d->range = sub; ret = 0; }
    else ret = 1;
    ad_normalize(d);
    d->dest_pos++;
    return ret;
}

static void ad_predecode(ArithDec *d, uint32_t sub)
{
    if (!d->has_pre_bit) { ad_decode(d, sub); d->has_pre_bit = 1; }
}

/* ---- NoLimitQWContextModel scan + PAQ1CountBooster -> weight profile ----
 * Builds the {c0, c1, weight} triples for a bit position, then calc_subrange. */
static uint8_t u8rol(uint8_t b, uint8_t c) { c &= 7; return (uint8_t)((b >> (8 - c)) | (b << c)); }

static uint32_t subrange_for(const uint8_t *data, uint32_t data_size, int bitPos,
                             const Model *models, int nm, uint32_t shift, uint32_t range)
{
    int32_t triples[MAX_MODELS * 3];

    if (bitPos < 0) {
        /* bitPos == -1, NoLimit: Init; Increment(false); Finalize */
        int32_t c0 = (int32_t)((1u << shift) + 1);   /* c0=1 -> (1<<shift)+1 */
        int32_t c1 = 1;                              /* c1=0 -> 1 */
        for (int i = 0; i < nm; i++) {
            triples[i*3] = c0; triples[i*3+1] = c1; triples[i*3+2] = (int32_t)models[i].weight;
        }
        return calc_subrange(triples, nm, range);
    }

    uint32_t c0[MAX_MODELS], c1[MAX_MODELS];
    for (int i = 0; i < nm; i++) { c0[i] = 0; c1[i] = 0; }

    unsigned bitpos = (unsigned)bitPos;
    unsigned bytePos = bitpos >> 3;
    unsigned maxPos = (bitpos + 8) >> 3;
    int overRun = (bytePos == data_size);
    uint8_t mask = (uint8_t)(0xff00U >> (bitpos & 7));

    for (unsigned cur = 0; cur < maxPos; cur++) {
        unsigned noMatch; int bit; int matched = 0;
        if (bytePos < 9 && cur == 0) {
            uint8_t xShift = (uint8_t)(8 - (bitpos & 7));
            /* byteLookup: pos is UNSIGNED, as in the C++ original - bytePos-i
             * underflows to a huge value when i>bytePos, so BL returns 0 rather
             * than reading out of bounds. */
            #define BL(pos) ( ((uint32_t)(pos) > bytePos || overRun) ? 0u : \
                              ((uint32_t)(pos) == bytePos ? (unsigned)(data[(uint32_t)(pos)] >> xShift) \
                                                          : (unsigned)data[(uint32_t)(pos)]) )
            uint8_t xByte = (uint8_t)(BL(8) ^ u8rol((uint8_t)BL(bytePos), xShift));
            if (!(xByte >> xShift)) {
                noMatch = 0;
                for (uint32_t i = 1; i < 9; i++)
                    if (BL(8 - i) != BL(bytePos - i)) noMatch |= 0x80u >> (i - 1);
                bit = (xByte >> (xShift - 1)) & 1;
                matched = 1;
            }
            #undef BL
        } else {
            if (cur >= 8 && cur < bytePos &&
                (overRun || !((data[cur] ^ data[bytePos]) & mask))) {
                noMatch = 0;
                for (int i = 1; i < 9; i++)
                    if (data[cur - i] != data[bytePos - i]) noMatch |= 0x80u >> (i - 1);
                unsigned bp = (cur << 3) + (bitpos & 7);
                bit = (data[bp >> 3] & (0x80U >> (bp & 7))) != 0;
                matched = 1;
            }
        }
        if (matched) {
            for (int i = 0; i < nm; i++) {
                if (!(noMatch & models[i].model)) {
                    c0[i]++; c1[i]++;
                    if (bit) c0[i] >>= 1; else c1[i] >>= 1;
                }
            }
        }
    }

    for (int i = 0; i < nm; i++) {
        triples[i*3]   = (int32_t)((c0[i] << shift) + 1);
        triples[i*3+1] = (int32_t)((c1[i] << shift) + 1);
        triples[i*3+2] = (int32_t)models[i].weight;
    }
    return calc_subrange(triples, nm, range);
}

/* ---- top level ---- */
void okp_decode(const uint8_t *arith, size_t arith_len,
                const uint8_t *header, int header_len, uint32_t shift,
                uint8_t *out, uint32_t raw_len)
{
    Model models[MAX_MODELS];
    uint32_t hdr_raw;
    int nm = decode_header(header, header_len, models, &hdr_raw);
    if (!raw_len) raw_len = hdr_raw;

    memset(out, 0, raw_len);
    uint32_t bitLength = raw_len * 8;

    ArithDec dc;
    ad_init(&dc, arith, arith_len);

    ad_predecode(&dc, subrange_for(out, raw_len, -1, models, nm, shift, dc.range));
    for (uint32_t i = 0; i < bitLength; i++) {
        uint32_t sub = subrange_for(out, raw_len, (int)i, models, nm, shift, dc.range);
#ifdef OKP_DBG
        fprintf(stderr, "b%u range=%x sub=%x\n", i, dc.range, sub);
#endif
        if (ad_decode(&dc, sub))
            out[i >> 3] |= 0x80U >> (i & 7);
    }
}
