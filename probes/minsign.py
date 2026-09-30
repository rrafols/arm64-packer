#!/usr/bin/env python3
"""
M0: the smallest signed arm64 Mach-O the kernel will exec.

Hand-builds the whole file - header, load commands, code, and an ad-hoc code
signature - with no clang and no /usr/bin/codesign, then runs it and checks the
exit code (42) and that the system verifier agrees. The packer's output is a
file no linker ever touches, so we have to produce all of this ourselves.

What M0 established (each the result of a probe that failed first):

  * dyld is mandatory. A purely static executable - LC_UNIXTHREAD, no dylinker -
    is SIGKILLed at exec on Apple Silicon, unlike x86-64 which ran it. So the
    binary must carry LC_LOAD_DYLINKER + LC_MAIN + LC_LOAD_DYLIB (libSystem),
    the same set the working x86-64 packer uses. This costs nothing extra for
    the intro, which needs dlopen/dlsym regardless.

  * A code signature is mandatory, and it must be ad-hoc-flagged. The kernel
    refuses an unsigned arm64 binary. The signature is an LC_CODE_SIGNATURE
    pointing at a CSMAGIC_EMBEDDED_SIGNATURE SuperBlob whose CodeDirectory
    (v20400, 88-byte header) holds one SHA-256 per 4096 bytes of the file up to
    codeLimit = the signature's own offset. The CodeDirectory flags field must
    have CS_ADHOC set; with flags = 0 the kernel still runs it but `codesign`
    reports "not signed at all" because it expects a CMS blob.

  * The file floor is one 16 KB VM page. A 702-byte file is SIGKILLed; the file
    must be at least the page its first segment maps. But it need be no more:
    header, code and signature all fit in that single page, and the signature
    ending exactly at EOF keeps `codesign -v` happy. So the smallest signed
    arm64 executable is exactly 16384 bytes - four times the x86-64 floor,
    purely because arm64 pages are four times larger.
"""
import struct, hashlib, os, subprocess, sys

VM      = 0x100000000
PAGE    = 0x4000                          # arm64 VM page = 16 KB
CS_PAGE = 4096                            # CodeDirectory hashing unit

LC_SEGMENT_64     = 0x19
LC_SYMTAB         = 0x02
LC_DYSYMTAB       = 0x0b
LC_LOAD_DYLINKER  = 0x0e
LC_LOAD_DYLIB     = 0x0c
LC_BUILD_VERSION  = 0x32
LC_MAIN           = 0x80000028
LC_DYLD_INFO_ONLY = 0x80000022
LC_CODE_SIGNATURE = 0x1d

CSMAGIC_EMBEDDED_SIGNATURE = 0xfade0cc0
CSMAGIC_CODEDIRECTORY      = 0xfade0c02
CSSLOT_CODEDIRECTORY       = 0
CS_ADHOC                   = 0x0000002
CS_EXECSEG_MAIN_BINARY     = 0x1
CS_HASHTYPE_SHA256         = 2
PLATFORM_MACOS             = 1
CPU_TYPE_ARM64             = 0x0100000c


def seg(name, vmaddr, vmsize, fileoff, filesize, maxprot, initprot):
    return struct.pack('<II16sQQQQIIII', LC_SEGMENT_64, 72, name.encode(),
                       vmaddr, vmsize, fileoff, filesize, maxprot, initprot, 0, 0)


def cstr_cmd(cmd, path, extra=b''):
    off = 12 + len(extra)
    data = path.encode() + b'\0'
    size = (off + len(data) + 7) & ~7
    out = struct.pack('<III', cmd, size, off) + extra + data
    return out + b'\0' * (size - len(out))


def build_codesig(file_bytes, text_filesize, ident=b'm'):
    """Ad-hoc signature over the whole of file_bytes; codeLimit = its length.
    file_bytes must already be padded up to where the signature will sit."""
    code_limit = len(file_bytes)
    n = (code_limit + CS_PAGE - 1) // CS_PAGE
    hashes = b''.join(hashlib.sha256(file_bytes[i * CS_PAGE:(i + 1) * CS_PAGE]).digest()
                      for i in range(n))
    ident_b = ident + b'\0'
    HDR = 88                              # v20400 header, up to identOff
    ident_off = HDR
    hash_off = ident_off + len(ident_b)
    cd_len = hash_off + len(hashes)
    cd  = struct.pack('>IIII', CSMAGIC_CODEDIRECTORY, cd_len, 0x20400, CS_ADHOC)
    cd += struct.pack('>IIIII', hash_off, ident_off, 0, n, code_limit)
    cd += struct.pack('>BBBB', 32, CS_HASHTYPE_SHA256, PLATFORM_MACOS, 12)
    cd += struct.pack('>I', 0)                                 # spare2
    cd += struct.pack('>III', 0, 0, 0)                         # scatter/team/spare3
    cd += struct.pack('>QQQQ', 0, 0, text_filesize, CS_EXECSEG_MAIN_BINARY)
    assert len(cd) == ident_off, (len(cd), ident_off)
    cd += ident_b + hashes
    assert len(cd) == cd_len
    sb = struct.pack('>III', CSMAGIC_EMBEDDED_SIGNATURE, 12 + 8 + len(cd), 1)
    sb += struct.pack('>II', CSSLOT_CODEDIRECTORY, 20)
    return sb + cd


def loadcmds(entry, cs_off, cs_len):
    """The single-page layout: __TEXT covers the whole file, __LINKEDIT is empty
    and based just past it, and the signature lives inside __TEXT ending at EOF.
    LC_DYLD_INFO_ONLY / SYMTAB / DYSYMTAB are present but empty - the binary has
    no imports to bind."""
    dylink = cstr_cmd(LC_LOAD_DYLINKER, '/usr/lib/dyld')
    dylib  = cstr_cmd(LC_LOAD_DYLIB, '/usr/lib/libSystem.B.dylib',
                      struct.pack('<III', 0, 0x10000, 0x10000))
    parts = [
        seg('__PAGEZERO', 0, VM, 0, 0, 0, 0),
        seg('__TEXT', VM, PAGE, 0, PAGE, 5, 5),               # r-x
        seg('__LINKEDIT', VM + PAGE, PAGE, PAGE, 0, 1, 1),    # r--
        struct.pack('<IIIIIIIIIIII', LC_DYLD_INFO_ONLY, 48, *([0] * 10)),
        struct.pack('<IIIIII', LC_SYMTAB, 24, 0, 0, 0, 0),
        struct.pack('<II18I', LC_DYSYMTAB, 80, *([0] * 18)),
        struct.pack('<IIIIII', LC_BUILD_VERSION, 24, PLATFORM_MACOS,
                    13 << 16, 13 << 16, 0),
        dylink,
        struct.pack('<IIQQ', LC_MAIN, 24, entry, 0),
        dylib,
        struct.pack('<IIII', LC_CODE_SIGNATURE, 16, cs_off, cs_len),
    ]
    cmds = b''.join(parts)
    hdr = struct.pack('<IiiIIIII', 0xfeedfacf, CPU_TYPE_ARM64, 0, 2,
                      len(parts), len(cmds), 0x00200085, 0)
    return hdr + cmds


def main():
    # return 42; with dyld + LC_MAIN, returning goes into dyld's start -> exit(w0)
    code = bytes.fromhex('40058052') + bytes.fromhex('c0035fd6')   # mov w0,#42 ; ret

    hdr_size = len(loadcmds(0, 0, 0))
    code_off = hdr_size

    # The signature ends exactly at the end of the single page. Its length
    # depends only on the file length (fixed at PAGE here), so a couple of
    # passes settle cs_off.
    sig_len = 256
    for _ in range(4):
        cs_off = PAGE - sig_len
        page = bytearray(b'\0' * PAGE)
        page[0:hdr_size] = loadcmds(code_off, cs_off, sig_len)
        page[code_off:code_off + len(code)] = code
        sig = build_codesig(bytes(page[:cs_off]), PAGE)
        if len(sig) == sig_len:
            break
        sig_len = len(sig)
    page[cs_off:cs_off + len(sig)] = sig

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'build', 'minsign')
    open(path, 'wb').write(bytes(page))
    os.chmod(path, 0o755)

    print("file size      : %d bytes  (one 16 KB page)" % len(page))
    print("header + LCs   : %d bytes" % hdr_size)
    print("code           : %d bytes at file offset %d" % (len(code), code_off))
    print("signature      : %d bytes at file offset %d" % (len(sig), cs_off))
    r = subprocess.run([path])
    ran = r.returncode == 42
    print("exit code      : %d  (expect 42) -> %s" % (r.returncode, 'PASS' if ran else 'FAIL'))
    v = subprocess.run(['codesign', '-v', path], capture_output=True, text=True)
    ok = v.returncode == 0
    print("codesign -v    : %s" % ('OK' if ok else (v.stderr.strip() or 'FAIL')))
    return 0 if (ran and ok) else 1


if __name__ == '__main__':
    sys.exit(main())
