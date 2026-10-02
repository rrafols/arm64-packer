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

def add_imm(dst, src, imm):              # add xdst, xsrc, #imm (imm<4096, or <<12)
    if imm < 4096:
        return struct.pack('<I', 0x91000000 | (imm << 10) | (src << 5) | dst)
    assert imm % 4096 == 0 and imm < (4096 << 12)
    return struct.pack('<I', 0x91400000 | ((imm >> 12) << 10) | (src << 5) | dst)
def add_reg(dst, a, b):                  return struct.pack('<I', 0x8B000000 | (b << 16) | (a << 5) | dst)
def sub_imm(dst, src, imm):              return struct.pack('<I', 0xD1000000 | (imm << 10) | (src << 5) | dst)
def ldrb_post(dst, base, imm=1):         return struct.pack('<I', 0x38400400 | ((imm & 0x1ff) << 12) | (base << 5) | dst)
def str_post(src, base, imm=8):          return struct.pack('<I', 0xF8000400 | ((imm & 0x1ff) << 12) | (base << 5) | src)
def cbz(reg, off, is64=True):            return struct.pack('<I', (0xB4000000 if is64 else 0x34000000) | (((off >> 2) & 0x7ffff) << 5) | reg)
def cbnz(reg, off, is64=True):           return struct.pack('<I', (0xB5000000 if is64 else 0x35000000) | (((off >> 2) & 0x7ffff) << 5) | reg)
def b(off):                              return struct.pack('<I', 0x14000000 | ((off >> 2) & 0x3ffffff))


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


def build_intro_stub(stub_va, dlsym_slot_va, cdb_slot_va, names_va, packed_va,
                     packed_len, image_len, total_vmsize, text_vmsize, got_base,
                     count, entry_off):
    """Stub for the real intro: mmap RW the whole vmsize, decompress the segment
    image, resolve `count` imports (names blob at names_va) via dlsym into the GOT
    (dst+got_base, consecutive 8-byte slots), mprotect __TEXT r-x, jump to entry."""
    c = bytearray()
    def here(): return stub_va + len(c)
    # dst = mmap(0, total_vmsize, RW, ANON|PRIV, -1, 0)
    c += movz(0, 0); c += mov_imm(1, total_vmsize); c += movz(2, 3)
    c += mov_imm(3, 0x1002); c += movn(4, 0); c += movz(5, 0)
    c += mov_imm(16, SYS_mmap); c += svc0x80(); c += mov_reg(19, 0)
    # compression_decode_buffer(dst, image_len, packed, packed_len, 0, LZMA)
    c += mov_reg(0, 19); c += mov_imm(1, image_len)
    c += adrp_add(2, packed_va, here()); c += mov_imm(3, packed_len)
    c += movz(4, 0); c += mov_imm(5, COMPRESSION_LZMA)
    c += adrp_add(9, cdb_slot_va, here()); c += ldr_x(9, 9); c += blr(9)
    # x24 = dlsym
    c += adrp_add(24, dlsym_slot_va, here()); c += ldr_x(24, 24)
    # x20 = names, x21 = dst + got_base, w22 = count
    c += adrp_add(20, names_va, here())
    c += add_imm(21, 19, got_base)
    c += mov_imm(22, count)
    # loop (all instructions 4 bytes, so offsets are len(c) from stub start):
    loop_at = len(c)
    cbz_at = len(c)
    c += b'\0\0\0\0'                          # placeholder: cbz w22, done
    c += movn(0, 1)                          # x0 = RTLD_DEFAULT (-2)
    c += mov_reg(1, 20)                       # x1 = name
    c += blr(24)                             # dlsym
    c += str_post(0, 21, 8)                  # got[i] = x0; x21 += 8
    skip_at = len(c)
    c += ldrb_post(9, 20, 1)                 # w9 = *x20++
    c += cbnz(9, skip_at - len(c), is64=False)   # if w9 != 0, back to ldrb
    c += sub_imm(22, 22, 1)                   # count--
    c += b(loop_at - len(c))                  # back to the cbz
    done_at = len(c)
    c[cbz_at:cbz_at+4] = cbz(22, done_at - cbz_at, is64=False)   # patch forward branch
    # mprotect(dst, text_vmsize, RX)
    c += mov_reg(0, 19); c += mov_imm(1, text_vmsize); c += movz(2, 5)
    c += mov_imm(16, SYS_mprotect); c += svc0x80()
    # jump to dst + entry
    if entry_off:
        c += mov_imm(10, entry_off); c += add_reg(19, 19, 10)
    c += br(19)
    return bytes(c)


def build_intro(macho_path, out_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("pi", os.path.join(os.path.dirname(__file__), "parse_intro.py"))
    pi = importlib.util.module_from_spec(spec); spec.loader.exec_module(pi)
    info = pi.parse(macho_path)
    imports = info['imports']
    got_base = imports[0][0]
    assert [o for o, _ in imports] == list(range(got_base, got_base + 8*len(imports), 8)), \
        "GOT slots not consecutive - stub assumes they are"
    # dlsym wants the C name without the leading underscore (it adds it itself).
    names_blob = b''.join(n.lstrip('_').encode() + b'\0' for _, n in imports)
    image = info['image']
    packed = lzma.compress(image, format=lzma.FORMAT_XZ, check=lzma.CHECK_NONE,
                           filters=[{'id': lzma.FILTER_LZMA2, 'preset': 9 | lzma.PRESET_EXTREME,
                                     'lc': 0, 'lp': 0, 'pb': 0}])

    data_va = VM + PAGE
    dlsym_slot_va = data_va + 0
    cdb_slot_va = data_va + 8

    dylink = cstr_cmd(LC_LOAD_DYLINKER, '/usr/lib/dyld')
    def dylib(path): return cstr_cmd(LC_LOAD_DYLIB, path, struct.pack('<III', 0, 0x10000, 0x10000))
    dylibs = [
        dylib('/usr/lib/libSystem.B.dylib'),                                          # ord 1 (dlsym)
        dylib('/usr/lib/libcompression.dylib'),                                       # ord 2 (cdb)
        dylib('/System/Library/Frameworks/OpenGL.framework/Versions/A/OpenGL'),       # ord 3
        dylib('/System/Library/Frameworks/AudioToolbox.framework/Versions/A/AudioToolbox'),  # ord 4
        dylib('/System/Library/Frameworks/CoreGraphics.framework/Versions/A/CoreGraphics'),  # ord 5
        dylib('/System/Library/Frameworks/ApplicationServices.framework/Versions/A/ApplicationServices'),  # ord 6
    ]

    def loadcmds(entry, bind_off, bind_size, cs_off, cs_len, linkedit_fs):
        parts = [
            seg('__PAGEZERO', 0, VM, 0, 0, 0, 0),
            seg('__TEXT', VM, PAGE, 0, PAGE, 5, 5),
            seg('__DATA', data_va, PAGE, PAGE, 0, 3, 3),   # filesize 0: zero-fill, dyld binds into the mapped tail
            seg('__LINKEDIT', VM + 2*PAGE, PAGE, PAGE, linkedit_fs, 1, 1),
            struct.pack('<IIIIIIIIIIII', LC_DYLD_INFO_ONLY, 48, 0, 0,
                        bind_off, bind_size, 0, 0, 0, 0, 0, 0),
            struct.pack('<IIIIII', LC_SYMTAB, 24, 0, 0, 0, 0),
            struct.pack('<II18I', LC_DYSYMTAB, 80, *([0]*18)),
            struct.pack('<IIIIII', LC_BUILD_VERSION, 24, 1, 13<<16, 13<<16, 0),
            dylink,
            struct.pack('<IIQQ', LC_MAIN, 24, entry, 0),
        ] + dylibs + [
            struct.pack('<IIII', LC_CODE_SIGNATURE, 16, cs_off, cs_len),
        ]
        cmds = b''.join(parts)
        hdr = struct.pack('<IiiIIIII', 0xfeedfacf, CPU_TYPE_ARM64, 0, 2,
                          len(parts), len(cmds), 0x00200085, 0)
        return hdr + cmds

    hdr_size = len(loadcmds(0, PAGE, 0, PAGE, 0, 0))
    stub_off = hdr_size
    stub_va = VM + stub_off

    # names blob right after the stub; packed right after names. Two passes for stub size.
    def layout(stub_len):
        names_off = stub_off + stub_len
        packed_off = names_off + len(names_blob)
        return names_off, packed_off
    stub = build_intro_stub(stub_va, dlsym_slot_va, cdb_slot_va, 0, 0,
                            len(packed), len(image), info['total_vmsize'],
                            info['text_vmsize'], got_base, len(imports), info['entry'])
    names_off, packed_off = layout(len(stub))
    stub = build_intro_stub(stub_va, dlsym_slot_va, cdb_slot_va,
                            VM + names_off, VM + packed_off, len(packed), len(image),
                            info['total_vmsize'], info['text_vmsize'], got_base,
                            len(imports), info['entry'])
    names_off, packed_off = layout(len(stub))

    # bind _dlsym (ord 1) -> slot0, _compression_decode_buffer (ord 2) -> slot1
    ops = bytearray([0x11, 0x51, 0x70 | 2]) + uleb(0)
    ops += bytearray([0x40]) + b'_dlsym\0' + bytearray([0x90])
    ops += bytearray([0x12, 0x70 | 2]) + uleb(8)
    ops += bytearray([0x40]) + b'_compression_decode_buffer\0' + bytearray([0x90, 0x00])

    cs_len = 400
    for _ in range(4):
        cs_off = PAGE + ((len(ops) + 15) & ~15)
        linkedit_fs = (cs_off - PAGE) + cs_len
        text = bytearray(b'\0' * PAGE)
        allc = loadcmds(stub_off, PAGE, len(ops), cs_off, cs_len, linkedit_fs)
        assert len(allc) == hdr_size
        text[0:len(allc)] = allc
        text[stub_off:stub_off+len(stub)] = stub
        text[names_off:names_off+len(names_blob)] = names_blob
        text[packed_off:packed_off+len(packed)] = packed
        assert packed_off + len(packed) <= PAGE, \
            "payload too big for one __TEXT page: %d" % (packed_off + len(packed))
        linkedit = bytearray(b'\0' * (cs_off - PAGE))
        linkedit[0:len(ops)] = ops
        file_wo_sig = bytes(text) + bytes(linkedit)   # __DATA has no file bytes
        sig = codesig(file_wo_sig, PAGE)
        if len(sig) == cs_len:
            break
        cs_len = len(sig)
    out = file_wo_sig + sig
    open(out_path, 'wb').write(out); os.chmod(out_path, 0o755)
    print("intro image : %d raw -> %d packed (%.1f%%)" % (len(image), len(packed), 100*len(packed)/len(image)))
    print("imports     : %d resolved via dlsym at runtime" % len(imports))
    print("stub        : %d bytes" % len(stub))
    print("file        : %d bytes  (%s)" % (len(out), os.path.basename(out_path)))


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == '--intro':
        out = sys.argv[3] if len(sys.argv) > 3 else os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'build', 'east_packed')
        build_intro(sys.argv[2], out)
        return 0
    if len(sys.argv) < 3:
        print("usage: pack.py <payload.bin> <entry_off> [out]")
        print("       pack.py --intro <mach-o> [out]"); return 1
    payload = open(sys.argv[1], 'rb').read()
    entry_off = int(sys.argv[2], 0)
    out = sys.argv[3] if len(sys.argv) > 3 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'build', 'packed')
    build(payload, entry_off, out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
