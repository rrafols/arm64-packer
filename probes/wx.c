/* M1: which W^X path does arm64 macOS allow for an ad-hoc-signed binary with no
 * hardened runtime and no entitlements?  The packer stub has to write the
 * unpacked intro somewhere and then execute it, and Apple Silicon has no RWX to
 * unpack into the way the x86-64 stub used __TEXT.
 *
 * Tests, cheapest-for-the-stub first:
 *   A  mmap RW  -> write -> mprotect RX -> call        (one mmap, one mprotect)
 *   B  mmap RWX directly -> write -> call              (would be simplest of all)
 *   C  MAP_JIT -> toggle write off -> call             (needs the JIT path)
 *
 * The winner decides the stub. A is the target: no MAP_JIT, no entitlement.
 * Build:  clang -arch arm64 -o build/wx probes/wx.c  &&  ./build/wx
 */
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <pthread.h>

/* arm64: mov w0,#42 ; ret  -> returns 42 */
static const unsigned char CODE[8] = {0x40,0x05,0x80,0x52, 0xc0,0x03,0x5f,0xd6};

typedef int (*fn)(void);

static int run(void *p) {
    __builtin___clear_cache((char*)p, (char*)p + sizeof(CODE));
    return ((fn)p)();
}

static void A(void) {
    void *p = mmap(0, 0x4000, PROT_READ|PROT_WRITE, MAP_ANON|MAP_PRIVATE, -1, 0);
    if (p == MAP_FAILED) { printf("A mmap RW      : mmap failed\n"); return; }
    memcpy(p, CODE, sizeof(CODE));
    if (mprotect(p, 0x4000, PROT_READ|PROT_EXEC) != 0) {
        printf("A mmap RW->RX  : mprotect failed\n"); return;
    }
    printf("A mmap RW->RX  : returned %d (expect 42)\n", run(p));
}

static void B(void) {
    void *p = mmap(0, 0x4000, PROT_READ|PROT_WRITE|PROT_EXEC,
                   MAP_ANON|MAP_PRIVATE, -1, 0);
    if (p == MAP_FAILED) { printf("B mmap RWX     : mmap failed (expected on arm64)\n"); return; }
    memcpy(p, CODE, sizeof(CODE));
    printf("B mmap RWX     : returned %d (expect 42)\n", run(p));
}

static void C(void) {
    void *p = mmap(0, 0x4000, PROT_READ|PROT_WRITE|PROT_EXEC,
                   MAP_ANON|MAP_PRIVATE|MAP_JIT, -1, 0);
    if (p == MAP_FAILED) { printf("C MAP_JIT      : mmap failed\n"); return; }
    pthread_jit_write_protect_np(0);          /* writable */
    memcpy(p, CODE, sizeof(CODE));
    pthread_jit_write_protect_np(1);          /* executable */
    printf("C MAP_JIT      : returned %d (expect 42)\n", run(p));
}

int main(void) { A(); B(); C(); return 0; }
