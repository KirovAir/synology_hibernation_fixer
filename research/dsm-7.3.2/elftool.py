#!/usr/bin/env python3
"""Section-independent ELF analysis helper for the DSM hibernation RE.

These DSM binaries are stripped of section headers, so everything is parsed
from the program headers + the PT_DYNAMIC table:
  - .dynsym / .dynstr  (DT_SYMTAB / DT_STRTAB); symcount inferred from layout
  - .rela.plt          (DT_JMPREL / DT_PLTRELSZ)   -> JUMP_SLOT (imports)
  - .rela.dyn          (DT_RELA / DT_RELASZ)        -> RELATIVE / GLOB_DAT
PLT stubs are found by scanning the executable PT_LOAD for `jmp [rip+d]`
into a JUMP_SLOT GOT slot.

For this binary set PT_LOAD off == vaddr, so file offset == vaddr.
"""
import sys, struct
from elftools.elf.elffile import ELFFile
from capstone import Cs, CS_ARCH_X86, CS_MODE_64
from capstone.x86 import X86_OP_MEM, X86_OP_IMM, X86_REG_RIP


class Elf:
    def __init__(self, path):
        self.path = path
        self.raw = open(path, 'rb').read()
        self.f = open(path, 'rb')
        self.elf = ELFFile(self.f)
        self.md = Cs(CS_ARCH_X86, CS_MODE_64)
        self.md.detail = True
        self._load_segments()
        self._load_dynamic()
        self._load_symbols()
        self._load_relocs()
        self._load_plt()

    # ---- memory image (vaddr == fileoff here, but map generally) ----
    def _load_segments(self):
        self.loads = []  # (vaddr, memsz, filesz, off, flags)
        self.exec_ranges = []
        for seg in self.elf.iter_segments():
            if seg['p_type'] == 'PT_LOAD':
                v, msz, fsz, off, fl = (seg['p_vaddr'], seg['p_memsz'], seg['p_filesz'],
                                        seg['p_offset'], seg['p_flags'])
                self.loads.append((v, msz, fsz, off, fl))
                if fl & 1:  # X
                    self.exec_ranges.append((v, msz, off))
        self.text_addr, self.text_size, self.text_off = self.exec_ranges[0]

    def read(self, vaddr, n):
        for v, msz, fsz, off, fl in self.loads:
            if v <= vaddr < v + msz:
                start = off + (vaddr - v)
                avail = off + fsz - start
                if avail <= 0:
                    return b'\x00' * n
                chunk = self.raw[start:start + min(n, avail)]
                if len(chunk) < n:
                    chunk += b'\x00' * (n - len(chunk))
                return chunk
        return None

    def u(self, vaddr, size):
        d = self.read(vaddr, size)
        return int.from_bytes(d, 'little')

    # ---- dynamic table ----
    def _load_dynamic(self):
        self.dyn = {}       # tag -> value (last wins for singletons)
        self.dyn_all = []   # (tag, value)
        for seg in self.elf.iter_segments():
            if seg['p_type'] == 'PT_DYNAMIC':
                for t in seg.iter_tags():
                    self.dyn_all.append((t.entry.d_tag, t.entry.d_val))
                    self.dyn[t.entry.d_tag] = t.entry.d_val
                break

    # ---- symbols ----
    def _load_symbols(self):
        self.syms = []          # (name, value, size, info, other, shndx)
        self.sym_by_addr = {}
        self.sym_by_name = {}
        symtab = self.dyn.get('DT_SYMTAB')
        strtab = self.dyn.get('DT_STRTAB')
        syment = self.dyn.get('DT_SYMENT', 24)
        if symtab is None or strtab is None:
            return
        strsz = self.dyn.get('DT_STRSZ')
        # symcount: dynsym typically immediately precedes dynstr
        symcount = (strtab - symtab) // syment
        self.strtab = strtab
        self.strsz = strsz
        for i in range(symcount):
            base = symtab + i * syment
            st_name, st_info, st_other, st_shndx, st_value, st_size = struct.unpack(
                '<IBBHQQ', self.read(base, 24))
            name = self._str(strtab + st_name)
            self.syms.append((name, st_value, st_size, st_info, st_other, st_shndx))
            if name:
                self.sym_by_name.setdefault(name, st_value)
                if st_value != 0:
                    self.sym_by_addr.setdefault(st_value, name)

    def _str(self, vaddr):
        out = bytearray()
        while True:
            b = self.read(vaddr, 1)
            if not b or b == b'\x00':
                break
            out += b
            vaddr += 1
            if len(out) > 512:
                break
        return out.decode('latin1')

    def symname(self, idx):
        return self.syms[idx][0] if 0 <= idx < len(self.syms) else f'sym{idx}'

    # ---- relocations ----
    def _load_relocs(self):
        self.jmprel = {}   # got slot vaddr -> symname (JUMP_SLOT)
        self.globdat = {}  # got slot vaddr -> symname (GLOB_DAT)
        self.relative = {} # slot vaddr -> addend (RELATIVE)
        jr = self.dyn.get('DT_JMPREL'); jsz = self.dyn.get('DT_PLTRELSZ')
        if jr and jsz:
            for off in range(jr, jr + jsz, 24):
                r_off, r_info, r_add = struct.unpack('<QQq', self.read(off, 24))
                sym = r_info >> 32
                self.jmprel[r_off] = self.symname(sym)
        ra = self.dyn.get('DT_RELA'); rsz = self.dyn.get('DT_RELASZ')
        if ra and rsz:
            for off in range(ra, ra + rsz, 24):
                r_off, r_info, r_add = struct.unpack('<QQq', self.read(off, 24))
                typ = r_info & 0xffffffff
                sym = r_info >> 32
                if typ == 6:    # R_X86_64_GLOB_DAT
                    self.globdat[r_off] = self.symname(sym)
                elif typ == 8:  # R_X86_64_RELATIVE
                    self.relative[r_off] = r_add

    # ---- PLT stubs ----
    def _load_plt(self):
        self.plt_by_addr = {}   # stub vaddr -> symname
        for v, msz, off in self.exec_ranges:
            data = self.raw[off:off + msz]
            for insn in self.md.disasm(data, v):
                if insn.mnemonic in ('jmp', 'bnd jmp'):
                    for op in insn.operands:
                        if op.type == X86_OP_MEM and op.mem.base == X86_REG_RIP:
                            slot = insn.address + insn.size + op.mem.disp
                            if slot in self.jmprel:
                                stub = (insn.address // 16) * 16
                                self.plt_by_addr[stub] = self.jmprel[slot]
                                self.plt_by_addr[insn.address] = self.jmprel[slot]

    def name_for(self, addr):
        if addr in self.sym_by_addr:
            return self.sym_by_addr[addr]
        if addr in self.plt_by_addr:
            return self.plt_by_addr[addr] + '@plt'
        return None

    # ---- strings ----
    def strings(self, minlen=4):
        out = []
        for v, msz, fsz, off, fl in self.loads:
            if fl & 1:  # skip executable
                continue
            data = self.raw[off:off + fsz]
            cur = []; start = None
            for i, b in enumerate(data):
                if 0x20 <= b < 0x7f:
                    if start is None:
                        start = i
                    cur.append(b)
                else:
                    if start is not None and len(cur) >= minlen:
                        out.append((v + start, bytes(cur).decode('latin1')))
                    cur = []; start = None
            if start is not None and len(cur) >= minlen:
                out.append((v + start, bytes(cur).decode('latin1')))
        # dedup by vaddr
        seen = {}
        for a, s in out:
            seen.setdefault(a, s)
        return sorted(seen.items())

    def find_string(self, needle, minlen=3):
        return [(v, s) for v, s in self.strings(minlen=minlen) if needle in s]

    # ---- disassembly ----
    def disasm(self, vaddr, count=None, nbytes=None):
        if nbytes is None:
            nbytes = (count or 40) * 12
        data = self.read(vaddr, nbytes)
        out = []
        for insn in self.md.disasm(data, vaddr):
            out.append(insn)
            if count and len(out) >= count:
                break
        return out

    def fmt(self, insn):
        tgt = ''
        for op in insn.operands:
            if op.type == X86_OP_IMM and (insn.mnemonic == 'call' or insn.mnemonic.startswith('j')):
                nm = self.name_for(op.imm)
                if nm:
                    tgt = f'   ; -> {nm}'
            if op.type == X86_OP_MEM and op.mem.base == X86_REG_RIP:
                ea = insn.address + insn.size + op.mem.disp
                nm = self.sym_by_addr.get(ea) or self.globdat.get(ea)
                if nm:
                    tgt += f'   ; &{nm}'
                else:
                    # show string if it points into rodata
                    s = self._maybe_str(ea)
                    if s is not None:
                        tgt += f'   ; "{s}"'
        raw = insn.bytes.hex()
        return f'0x{insn.address:06x}: {raw:<22} {insn.mnemonic} {insn.op_str}{tgt}'

    def _maybe_str(self, ea, maxlen=48):
        d = self.read(ea, 1)
        if not d or not (0x20 <= d[0] < 0x7f):
            return None
        s = self._str(ea)
        if len(s) >= 3:
            return s[:maxlen]
        return None

    def print_range(self, vaddr, count=40):
        for insn in self.disasm(vaddr, count=count):
            print(self.fmt(insn))

    # ---- xref scanning ----
    def scan_text(self):
        if hasattr(self, '_insns'):
            return self._insns
        insns = []
        for v, msz, off in self.exec_ranges:
            data = self.raw[off:off + msz]
            insns += list(self.md.disasm(data, v))
        self._insns = insns
        return insns

    def xrefs_to_addr(self, target):
        hits = []
        for insn in self.scan_text():
            for op in insn.operands:
                if op.type == X86_OP_MEM and op.mem.base == X86_REG_RIP:
                    if insn.address + insn.size + op.mem.disp == target:
                        hits.append(insn)
                elif op.type == X86_OP_IMM and op.imm == target:
                    hits.append(insn)
        return hits

    def calls_to(self, target):
        hits = []
        for insn in self.scan_text():
            if insn.mnemonic == 'call' or insn.mnemonic.startswith('j'):
                for op in insn.operands:
                    if op.type == X86_OP_IMM and op.imm == target:
                        hits.append(insn)
        return hits

    def calls_to_symbol(self, symname):
        stubs = [a for a, n in self.plt_by_addr.items() if n == symname]
        hits = []
        for st in stubs:
            hits += self.calls_to(st)
        if self.sym_by_name.get(symname):
            hits += self.calls_to(self.sym_by_name[symname])
        return sorted(set(hits), key=lambda i: i.address), stubs

    def imports(self):
        return sorted(set(self.plt_by_addr.values()))


if __name__ == '__main__':
    e = Elf(sys.argv[1])
    print(f'loaded {e.path}')
    print(f'exec ranges: {[(hex(v),hex(s)) for v,s,o in e.exec_ranges]}')
    print(f'dynsym: {len(e.syms)} entries, plt stubs: {len(set(e.plt_by_addr.values()))} imports')
    print(f'jmprel: {len(e.jmprel)}, globdat: {len(e.globdat)}, relative: {len(e.relative)}')
