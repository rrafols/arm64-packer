#!/usr/bin/env python3
"""
Parse the arm64 intro Mach-O into what the packer needs:
  - flat loadable image (segment contents laid out by vmaddr from the base)
  - entry offset (LC_MAIN)
  - __TEXT vmsize (the range to mprotect r-x after unpack)
  - total vmsize (the mmap size, covers the BSS tail of __DATA)
  - the imports to resolve: list of (got_offset, symbol_name)

The intro has no rebases (checked: all fixups are binds in __got), so nothing
needs base-relocation - the stub just fills the GOT slots via dlsym.

Usage: parse_intro.py <mach-o>   (prints a summary; import by other tools)
"""
import struct, sys

LC_SEGMENT_64, LC_MAIN, LC_DYLD_CHAINED_FIXUPS = 0x19, 0x80000028, 0x80000034


def parse(path):
    d = open(path, 'rb').read()
    magic, cpu, cst, ftype, ncmds, scmds, flags, rsv = struct.unpack_from('<IiiIIIII', d, 0)
    assert magic == 0xfeedfacf, "not a 64-bit Mach-O"
    off = 32
    segs = []           # (name, vmaddr, vmsize, fileoff, filesize)
    entry = None
    cf_off = cf_size = None
    vmbase = None
    for _ in range(ncmds):
        cmd, sz = struct.unpack_from('<II', d, off)
        if cmd == LC_SEGMENT_64:
            name = d[off+8:off+24].split(b'\0')[0].decode()
            vmaddr, vmsize, fileoff, filesize = struct.unpack_from('<QQQQ', d, off+24)
            segs.append((name, vmaddr, vmsize, fileoff, filesize))
            if name == '__TEXT':
                vmbase = vmaddr
        elif cmd == LC_MAIN:
            entry = struct.unpack_from('<Q', d, off+8)[0]
        elif cmd == LC_DYLD_CHAINED_FIXUPS:
            cf_off, cf_size = struct.unpack_from('<II', d, off+8)
        off += sz

    # flat image: from vmbase to the end of the last loadable segment's file content
    text_vmsize = next(s[2] for s in segs if s[0] == '__TEXT')
    loadable = [s for s in segs if s[0] != '__PAGEZERO' and s[0] != '__LINKEDIT']
    img_end = max((s[1] - vmbase) + s[4] for s in loadable)     # by filesize
    total_vmsize = max((s[1] - vmbase) + s[2] for s in loadable)  # by vmsize (BSS)
    image = bytearray(img_end)
    for name, vmaddr, vmsize, fileoff, filesize in loadable:
        o = vmaddr - vmbase
        image[o:o+filesize] = d[fileoff:fileoff+filesize]

    imports = parse_chained_fixups(d, cf_off, segs, vmbase)
    return {
        'image': bytes(image), 'entry': entry, 'text_vmsize': text_vmsize,
        'total_vmsize': total_vmsize, 'imports': imports, 'vmbase': vmbase,
    }


def parse_chained_fixups(d, cf_off, segs, vmbase):
    """Return [(got_offset_from_vmbase, name)] for every bind; assert no rebases."""
    (fv, starts_off, imports_off, symbols_off, imports_count,
     imports_format, symbols_format) = struct.unpack_from('<IIIIIII', d, cf_off)
    # imports table (format 1: DYLD_CHAINED_IMPORT = uint32 lib(8)|weak(1)|nameoff(23))
    names = []
    for i in range(imports_count):
        v = struct.unpack_from('<I', d, cf_off + imports_off + i*4)[0]
        name_off = v >> 9
        s = cf_off + symbols_off + name_off
        names.append(d[s:d.index(b'\0', s)].decode())
    # starts_in_image
    seg_count = struct.unpack_from('<I', d, cf_off + starts_off)[0]
    seg_starts = struct.unpack_from('<%dI' % seg_count, d, cf_off + starts_off + 4)
    binds = []
    for si, so in enumerate(seg_starts):
        if so == 0:
            continue
        base = cf_off + starts_off + so       # dyld_chained_starts_in_segment
        (size, page_size, ptr_format, seg_offset, max_valid,
         page_count) = struct.unpack_from('<IHHQII', d, base)
        assert ptr_format == 6, "only DYLD_CHAINED_PTR_64_OFFSET handled"
        page_starts = struct.unpack_from('<%dH' % page_count, d, base + 22)
        seg = segs[si]
        seg_fileoff = seg[3]
        seg_vmaddr = seg[1]
        for pi, ps in enumerate(page_starts):
            if ps == 0xffff:
                continue
            # offset WITHIN the segment; seg_offset just locates the segment and
            # equals seg_fileoff, so it must not be added again.
            cur = pi * page_size + ps
            while True:
                raw = struct.unpack_from('<Q', d, seg_fileoff + cur)[0]
                is_bind = (raw >> 63) & 1
                nxt = (raw >> 51) & 0xfff
                got_off = (seg_vmaddr + cur) - vmbase
                if is_bind:
                    ordinal = raw & 0xffffff            # import ordinal (24 bits)
                    binds.append((got_off, names[ordinal]))
                else:
                    raise AssertionError("rebase at %#x - not handled" % got_off)
                if nxt == 0:
                    break
                cur += nxt * 4
    return binds


def main():
    info = parse(sys.argv[1])
    print("entry off     : %#x" % info['entry'])
    print("__TEXT vmsize : %#x" % info['text_vmsize'])
    print("total vmsize  : %#x (mmap size)" % info['total_vmsize'])
    print("image bytes   : %d" % len(info['image']))
    print("imports       : %d" % len(info['imports']))
    for o, n in info['imports'][:5]:
        print("   %#x  %s" % (o, n))
    print("   ...")


if __name__ == '__main__':
    main()
