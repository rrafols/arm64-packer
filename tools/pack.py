#!/usr/bin/env python3
"""
tools/pack.py - the arm64 packer (M3), LZMA path.

Skeleton stage: packs a self-contained flat arm64 payload (PC-relative, no
imports) into a signed, single-page-ish Mach-O that decompresses itself and runs.
Integrates M0 (hand-built signed header), M1 (mmap RW -> mprotect RX), and the
import binding proven in probes/bind.py (to reach compression_decode_buffer).

    pack.py <payload.bin> <entry_off> [out]

The stub: mmap RW a buffer, compression_decode_buffer (LZMA) the payload into it,
mprotect RX, jump to buffer+entry_off. mmap/mprotect are raw syscalls (no import
needed); only compression_decode_buffer is bound via dyld.

Layout (16 KB pages): __TEXT (r-x: header + stub + packed payload),
__DATA (rw-: one bound function pointer), __LINKEDIT (bind opcodes + signature).

The intro itself is not self-contained (it links against frameworks), so packing
it needs the import-resolving stage on top of this skeleton - see CLAUDE.md.
"""
import struct, hashlib, os, sys, lzma

VM      = 0x100000000
PAGE    = 0x4000
CS_PAGE = 4096

LC_SEGMENT_64     = 0x19
LC_SYMTAB         = 0x02
LC_DYSYMTAB       = 0x0b
LC_LOAD_DYLINKER  = 0x0e
LC_LOAD_DYLIB     = 0x0c
LC_MAIN           = 0x80000028
LC_DYLD_INFO_ONLY = 0x80000022
LC_CODE_SIGNATURE = 0x1d
LC_BUILD_VERSION  = 0x32
CPU_TYPE_ARM64    = 0x0100000c
CSMAGIC_EMBEDDED_SIGNATURE = 0xfade0cc0
CSMAGIC_CODEDIRECTORY      = 0xfade0c02
CS_ADHOC = 2
COMPRESSION_LZMA = 0x306
SYS_mmap, SYS_mprotect = 197, 74


def seg(name, vmaddr, vmsize, fileoff, filesize, maxprot, initprot):
    return struct.pack('<II16sQQQQIIII', LC_SEGMENT_64, 72, name.encode(),
                       vmaddr, vmsize, fileoff, filesize, maxprot, initprot, 0, 0)


def cstr_cmd(cmd, path, extra=b''):
    off = 12 + len(extra); data = path.encode() + b'\0'
    size = (off + len(data) + 7) & ~7
    out = struct.pack('<III', cmd, size, off) + extra + data
    return out + b'\0' * (size - len(out))


def uleb(v):
    out = bytearray()
    while True:
        b = v & 0x7f; v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


# ---- AArch64 instruction helpers ----
def movz(reg, imm16, shift=0):   return struct.pack('<I', 0xD2800000 | ((shift//16)<<21) | ((imm16 & 0xffff) << 5) | reg)
def movk(reg, imm16, shift):     return struct.pack('<I', 0xF2800000 | ((shift//16)<<21) | ((imm16 & 0xffff) << 5) | reg)
def movn(reg, imm16, shift=0):   return struct.pack('<I', 0x92800000 | ((shift//16)<<21) | ((imm16 & 0xffff) << 5) | reg)
def mov_reg(dst, src):           return struct.pack('<I', 0xAA0003E0 | (src << 16) | dst)   # orr dst, xzr, src
def svc0x80():                   return struct.pack('<I', 0xD4001001)
def ldr_x(dst, base):            return struct.pack('<I', 0xF9400000 | (base << 5) | dst)   # ldr xdst,[xbase]
def blr(reg):                    return struct.pack('<I', 0xD63F0000 | (reg << 5))
def br(reg):                     return struct.pack('<I', 0xD61F0000 | (reg << 5))

def mov_imm(reg, val):
    """Load an arbitrary 64-bit (here <=32-bit) immediate into reg via movz/movk."""
    out = movz(reg, val & 0xffff, 0)
    if val >> 16:
        out += movk(reg, (val >> 16) & 0xffff, 16)
    return out

def adrp_add(reg, target_va, pc_va):
    page_delta = (target_va & ~0xfff) - (pc_va & ~0xfff)
    immhi = (page_delta >> 14) & 0x7ffff; immlo = (page_delta >> 12) & 3
    adrp = 0x90000000 | (immlo << 29) | (immhi << 5) | reg
    add  = 0x91000000 | ((target_va & 0xfff) << 10) | (reg << 5) | reg
    return struct.pack('<II', adrp, add)


def codesig(file_bytes, text_filesize, ident=b'e'):
    cl = len(file_bytes); n = (cl + CS_PAGE - 1) // CS_PAGE
    hashes = b''.join(hashlib.sha256(file_bytes[i*CS_PAGE:(i+1)*CS_PAGE]).digest() for i in range(n))
    idb = ident + b'\0'; io = 88; ho = io + len(idb); cdlen = ho + len(hashes)
    cd  = struct.pack('>IIII', CSMAGIC_CODEDIRECTORY, cdlen, 0x20400, CS_ADHOC)
    cd += struct.pack('>IIIII', ho, io, 0, n, cl)
    cd += struct.pack('>BBBB', 32, 2, 1, 12) + struct.pack('>I', 0)
    cd += struct.pack('>III', 0, 0, 0) + struct.pack('>QQQQ', 0, 0, text_filesize, 1)
    cd += idb + hashes
    sb = struct.pack('>III', CSMAGIC_EMBEDDED_SIGNATURE, 12 + 8 + len(cd), 1)
    sb += struct.pack('>II', 0, 20)
    return sb + cd


def build_stub(stub_va, slot_va, packed_va, packed_len, raw_len, entry_off):
    """Hand-assembled stub. mmap RW (syscall), compression_decode_buffer (bound,
    via slot), mprotect RX (syscall), jump to decoded+entry_off. x19 holds dst."""
    code = bytearray()
    def here():  return stub_va + len(code)

    # dst = mmap(0, raw_len, PROT_READ|WRITE=3, MAP_ANON|PRIVATE=0x1002, -1, 0)
    code += movz(0, 0)                      # x0 = 0
    code += mov_imm(1, raw_len)             # x1 = len
    code += movz(2, 3)                      # w2 = RW
    code += mov_imm(3, 0x1002)              # w3 = MAP_ANON|MAP_PRIVATE
    code += movn(4, 0)                      # x4 = -1 (fd)
    code += movz(5, 0)                      # x5 = 0 (offset)
    code += mov_imm(16, SYS_mmap)           # x16 = 197
    code += svc0x80()
    code += mov_reg(19, 0)                  # x19 = dst

    # compression_decode_buffer(dst, raw_len, src, packed_len, 0, COMPRESSION_LZMA)
    code += mov_reg(0, 19)                  # x0 = dst
    code += mov_imm(1, raw_len)             # x1 = dst_size
    code += adrp_add(2, packed_va, here()); # x2 = src
    code += mov_imm(3, packed_len)          # x3 = src_size
    code += movz(4, 0)                      # x4 = scratch NULL
    code += mov_imm(5, COMPRESSION_LZMA)    # w5 = algo
    code += adrp_add(9, slot_va, here())    # x9 = &slot
    code += ldr_x(9, 9)                     # x9 = *slot (bound fn)
    code += blr(9)

    # mprotect(dst, raw_len, PROT_READ|EXEC=5)
    code += mov_reg(0, 19)
    code += mov_imm(1, raw_len)
    code += movz(2, 5)
    code += mov_imm(16, SYS_mprotect)
    code += svc0x80()

    # jump to dst + entry_off
    if entry_off:
        code += mov_imm(10, entry_off)
        code += struct.pack('<I', 0x8B0A0273)   # add x19, x19, x10
    code += br(19)
    return bytes(code)


def build(payload, entry_off, out_path):
    raw_len = len(payload)
    packed = lzma.compress(payload, format=lzma.FORMAT_XZ, check=lzma.CHECK_NONE,
                           filters=[{'id': lzma.FILTER_LZMA2,
                                     'preset': 9 | lzma.PRESET_EXTREME,
                                     'lc': 0, 'lp': 0, 'pb': 0}])

    data_va = VM + PAGE                       # __DATA slot
    slot_va = data_va

    dylink = cstr_cmd(LC_LOAD_DYLINKER, '/usr/lib/dyld')
    dylib  = cstr_cmd(LC_LOAD_DYLIB, '/usr/lib/libSystem.B.dylib',
                      struct.pack('<III', 0, 0x10000, 0x10000))
    # compression_decode_buffer is in libcompression (dyld shared cache, not on
    # disk, but loadable by path), not libSystem. Second dylib -> ordinal 2.
    dylibc = cstr_cmd(LC_LOAD_DYLIB, '/usr/lib/libcompression.dylib',
                      struct.pack('<III', 0, 0x10000, 0x10000))

    def loadcmds(entry, bind_off, bind_size, cs_off, cs_len, linkedit_fs):
        parts = [
            seg('__PAGEZERO', 0, VM, 0, 0, 0, 0),
            seg('__TEXT', VM, PAGE, 0, PAGE, 5, 5),
            seg('__DATA', data_va, PAGE, PAGE, PAGE, 3, 3),
            seg('__LINKEDIT', VM + 2*PAGE, PAGE, 2*PAGE, linkedit_fs, 1, 1),
            struct.pack('<IIIIIIIIIIII', LC_DYLD_INFO_ONLY, 48, 0, 0,
                        bind_off, bind_size, 0, 0, 0, 0, 0, 0),
            struct.pack('<IIIIII', LC_SYMTAB, 24, 0, 0, 0, 0),
            struct.pack('<II18I', LC_DYSYMTAB, 80, *([0]*18)),
            struct.pack('<IIIIII', LC_BUILD_VERSION, 24, 1, 13<<16, 13<<16, 0),
            dylink,
            struct.pack('<IIQQ', LC_MAIN, 24, entry, 0),
            dylib,
            dylibc,
            struct.pack('<IIII', LC_CODE_SIGNATURE, 16, cs_off, cs_len),
        ]
        cmds = b''.join(parts)
        hdr = struct.pack('<IiiIIIII', 0xfeedfacf, CPU_TYPE_ARM64, 0, 2,
                          len(parts), len(cmds), 0x00200085, 0)
        return hdr + cmds

    hdr_size = len(loadcmds(0, 2*PAGE, 0, 2*PAGE, 0, 0))
    stub_off = hdr_size
    stub_va = VM + stub_off

    stub = build_stub(stub_va, slot_va, 0, len(packed), raw_len, entry_off)
    packed_off = stub_off + len(stub)
    packed_va = VM + packed_off
    stub = build_stub(stub_va, slot_va, packed_va, len(packed), raw_len, entry_off)
    assert VM + stub_off + len(stub) == packed_va

    # bind _compression_decode_buffer into the __DATA slot (segment index 2),
    # from dylib ordinal 2 (libcompression)
    ops = bytearray([0x12, 0x51, 0x70 | 2]) + uleb(0)
    ops += bytearray([0x40]) + b'_compression_decode_buffer\0' + bytearray([0x90, 0x00])

    cs_len = 400
    for _ in range(4):
        cs_off = 2*PAGE + ((len(ops) + 15) & ~15)
        linkedit_fs = (cs_off - 2*PAGE) + cs_len
        text = bytearray(b'\0' * PAGE)
        allc = loadcmds(stub_off, 2*PAGE, len(ops), cs_off, cs_len, linkedit_fs)
        assert len(allc) == hdr_size
        text[0:len(allc)] = allc
        text[stub_off:stub_off+len(stub)] = stub
        text[packed_off:packed_off+len(packed)] = packed
        assert packed_off + len(packed) <= PAGE, "payload too big for one __TEXT page (chunk/extend)"
        data = bytearray(b'\0' * PAGE)
        linkedit = bytearray(b'\0' * (cs_off - 2*PAGE))
        linkedit[0:len(ops)] = ops
        file_wo_sig = bytes(text) + bytes(data) + bytes(linkedit)
        sig = codesig(file_wo_sig, PAGE)
        if len(sig) == cs_len:
            break
        cs_len = len(sig)
    out = file_wo_sig + sig

    open(out_path, 'wb').write(out); os.chmod(out_path, 0o755)
    print("payload   : %d raw -> %d packed (%.1f%%)" % (raw_len, len(packed), 100*len(packed)/raw_len))
    print("stub      : %d bytes" % len(stub))
    print("file      : %d bytes  (%s)" % (len(out), os.path.basename(out_path)))


def main():
    if len(sys.argv) < 3:
        print("usage: pack.py <payload.bin> <entry_off> [out]"); return 1
    payload = open(sys.argv[1], 'rb').read()
    entry_off = int(sys.argv[2], 0)
    out = sys.argv[3] if len(sys.argv) > 3 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'build', 'packed')
    build(payload, entry_off, out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
