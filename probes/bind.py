#!/usr/bin/env python3
"""
M3 probe: can a hand-built, self-signed arm64 Mach-O bind an imported symbol via
the legacy LC_DYLD_INFO_ONLY bind opcodes and call it? The packer stub needs to
reach compression_decode_buffer (and/or mmap/mprotect) in libSystem, and on arm64
the x86-64 trick of parking the bound pointer in an RWX __TEXT page is gone (W^X),
so the bound slot must live in a writable __DATA segment.

This binds `_write`, loads the bound pointer from __DATA, and calls
write(1, "bind ok\n", 8), then exit(0) via syscall. If it prints, dyld processed
our bind opcodes and the stub's import mechanism works.

Layout (each 16 KB): __PAGEZERO, __TEXT (r-x: header+code+string),
__DATA (rw-: one bound pointer slot), __LINKEDIT (bind opcodes + signature).
"""
import struct, hashlib, os, subprocess, sys

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


def codesig(file_bytes, text_filesize, ident=b'b'):
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


def main():
    # __DATA holds one 8-byte pointer slot that dyld binds to _write.
    data_slot_off = 0                       # offset of the slot within __DATA
    data_va = VM + 2 * PAGE                  # __DATA base
    text_va = VM

    dylink = cstr_cmd(LC_LOAD_DYLINKER, '/usr/lib/dyld')
    dylib  = cstr_cmd(LC_LOAD_DYLIB, '/usr/lib/libSystem.B.dylib',
                      struct.pack('<III', 0, 0x10000, 0x10000))

    def loadcmds(code_off, msg_off, bind_off, bind_size, cs_off, cs_len):
        parts = [
            seg('__PAGEZERO', 0, VM, 0, 0, 0, 0),
            seg('__TEXT', text_va, PAGE, 0, PAGE, 5, 5),
            seg('__DATA', data_va, PAGE, PAGE, PAGE, 3, 3),      # rw-
            seg('__LINKEDIT', VM + 3*PAGE, PAGE, 2*PAGE, (cs_off - 2*PAGE) + cs_len, 1, 1),
            struct.pack('<IIIIIIIIIIII', LC_DYLD_INFO_ONLY, 48, 0, 0,
                        bind_off, bind_size, 0, 0, 0, 0, 0, 0),
            struct.pack('<IIIIII', LC_SYMTAB, 24, 0, 0, 0, 0),
            struct.pack('<II18I', LC_DYSYMTAB, 80, *([0]*18)),
            struct.pack('<IIIIII', LC_BUILD_VERSION, 24, 1, 13<<16, 13<<16, 0),
            dylink,
            struct.pack('<IIQQ', LC_MAIN, 24, code_off, 0),
            dylib,
            struct.pack('<IIII', LC_CODE_SIGNATURE, 16, cs_off, cs_len),
        ]
        cmds = b''.join(parts)
        hdr = struct.pack('<IiiIIIII', 0xfeedfacf, CPU_TYPE_ARM64, 0, 2,
                          len(parts), len(cmds), 0x00200085, 0)
        return hdr + cmds

    hdr_size = len(loadcmds(0, 0, 2*PAGE, 0, 2*PAGE, 0))
    code_off = hdr_size

    # code: x19 = &slot; call write(1, msg, 8) via [slot]; exit(0)
    msg = b"bind ok\n"
    # assemble by hand (AArch64):
    def adrp_add(reg, target_va, pc_va):
        page_delta = (target_va & ~0xfff) - (pc_va & ~0xfff)
        immhi = (page_delta >> 14) & 0x7ffff; immlo = (page_delta >> 12) & 3
        adrp = 0x90000000 | (immlo << 29) | (immhi << 5) | reg
        add  = 0x91000000 | ((target_va & 0xfff) << 10) | (reg << 5) | reg
        return struct.pack('<II', adrp, add)

    # We need msg address and slot address, both PC-relative. Lay out code first
    # with placeholders, then patch, since adrp depends on the instruction's PC.
    # Simpler: fixed instruction schedule, compute each adrp against its own PC.
    code = bytearray()
    slot_va = data_va + data_slot_off
    # placeholder; fill after we know msg_off (msg placed right after code)
    # instruction plan (each 4 bytes):
    #  0: adrp x1, msg ; 4: add x1,x1,#msgoff   -> x1 = msg
    #  8: mov w0, #1                              -> fd 1
    # 12: mov w2, #8                              -> len
    # 16: adrp x3, slot ; 20: add x3,x3,#slotoff ; 24: ldr x3,[x3] ; 28: blr x3
    # 32: mov w0,#0 ; 36: mov x16,#1 ; 40: svc #0x80
    msg_off = code_off + 44                  # msg right after the 44-byte code
    msg_va = text_va + msg_off
    code += adrp_add(1, msg_va, text_va + code_off + 0)
    code += struct.pack('<I', 0x52800020)    # mov w0, #1
    code += struct.pack('<I', 0x52800102)    # mov w2, #8
    code += adrp_add(3, slot_va, text_va + code_off + 16)
    code += struct.pack('<I', 0xf9400063)    # ldr x3, [x3]
    code += struct.pack('<I', 0xd63f0060)    # blr x3
    code += struct.pack('<I', 0x52800000)    # mov w0, #0
    code += struct.pack('<I', 0xd2800030)    # mov x16, #1
    code += struct.pack('<I', 0xd4001001)    # svc #0x80
    assert len(code) == 44, len(code)

    # bind opcodes: bind the __DATA slot (segment index 2) to _write, libSystem(1)
    ops = bytearray([0x11, 0x51, 0x70 | 2]) + uleb(data_slot_off)
    ops += bytearray([0x40]) + b'_write\0' + bytearray([0x90, 0x00])

    # assemble file
    cs_len = 400
    for _ in range(4):
        bind_off = 2 * PAGE                   # bind opcodes at start of __LINKEDIT
        cs_off = 2 * PAGE + ((len(ops) + 15) & ~15)
        text = bytearray(b'\0' * PAGE)
        allc = loadcmds(code_off, msg_off, bind_off, len(ops), cs_off, cs_len)
        text[0:len(allc)] = allc
        text[code_off:code_off+len(code)] = code
        text[msg_off:msg_off+len(msg)] = msg
        data = bytearray(b'\0' * PAGE)        # slot zeroed; dyld fills it
        linkedit = bytearray(b'\0' * ((cs_off - 2*PAGE)))
        linkedit[0:len(ops)] = ops
        file_wo_sig = bytes(text) + bytes(data) + bytes(linkedit)
        sig = codesig(file_wo_sig, PAGE)
        if len(sig) == cs_len:
            break
        cs_len = len(sig)
    out = file_wo_sig + sig

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'build', 'bind')
    open(path, 'wb').write(out); os.chmod(path, 0o755)
    print("file size   : %d bytes" % len(out))
    r = subprocess.run([path], capture_output=True, text=True)
    print("stdout      : %r" % r.stdout)
    print("exit code   : %d" % r.returncode)
    v = subprocess.run(['codesign', '-v', path], capture_output=True, text=True)
    print("codesign -v : %s" % ('OK' if v.returncode == 0 else (v.stderr.strip() or 'FAIL')))
    ok = r.stdout == "bind ok\n"
    print("BIND VIA LC_DYLD_INFO: %s" % ('WORKS' if ok else 'does not work'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
