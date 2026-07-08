#!/usr/bin/env python3
"""Prototype of the self-contained cave resolver to be ported into hiber_fixer.py.
Verifies it reproduces the hand-validated cave for THIS libsynoscemd.so.1.
Uses only stdlib (struct/re) -- no capstone/pyelftools -- so it ports 1:1."""
import re, struct

DATA = open('bins/libsynoscemd.so.1','rb').read()

# ---- minimal ELF parsing (program headers + dynamic + plt + rodata) ----
def elf_segments(d):
    assert d[:4]==b'\x7fELF' and d[4]==2
    phoff=struct.unpack_from('<Q',d,32)[0]; phes=struct.unpack_from('<H',d,54)[0]; phn=struct.unpack_from('<H',d,56)[0]
    segs=[]
    for i in range(phn):
        o=phoff+i*phes
        p_type=struct.unpack_from('<I',d,o)[0]
        p_flags=struct.unpack_from('<I',d,o+4)[0]
        p_off,p_va=struct.unpack_from('<QQ',d,o+8)
        p_filesz,p_memsz=struct.unpack_from('<QQ',d,o+32)
        segs.append((p_type,p_flags,p_off,p_va,p_filesz,p_memsz))
    return segs

def foff_to_va(segs,foff):
    for t,fl,off,va,fsz,msz in segs:
        if t==1 and off<=foff<off+fsz: return va+(foff-off)
    return None

def va_to_foff(segs,va):
    for t,fl,off,va0,fsz,msz in segs:
        if t==1 and va0<=va<va0+fsz: return off+(va-va0)
    return None

def exec_seg(segs):
    for t,fl,off,va,fsz,msz in segs:
        if t==1 and (fl&1): return (off,va,fsz,msz)
    return None

def read_va(segs,d,va,n):
    fo=va_to_foff(segs,va)
    if fo is None: return b'\x00'*n
    return d[fo:fo+n]

def dyn_tags(segs,d):
    tags={}
    for t,fl,off,va,fsz,msz in segs:
        if t==2:  # PT_DYNAMIC
            p=off
            while True:
                tag,val=struct.unpack_from('<qQ',d,p); p+=16
                if tag==0: break
                tags.setdefault(tag,val)  # first wins; use lists if needed
            break
    return tags

def dyn_all(segs,d):
    out=[]
    for t,fl,off,va,fsz,msz in segs:
        if t==2:
            p=off
            while True:
                tag,val=struct.unpack_from('<qQ',d,p); p+=16
                out.append((tag,val))
                if tag==0: break
            break
    return out

# DT_* : STRTAB=5 SYMTAB=6 SYMENT=11 JMPREL=23 PLTRELSZ=2
def plt_stub_va(segs,d,symname):
    tg=dyn_tags(segs,d)
    symtab=tg.get(6); strtab=tg.get(5); syment=tg.get(11,24); jmprel=tg.get(23); pltsz=tg.get(2)
    if not all((symtab,strtab,jmprel,pltsz)): return None
    # find sym index by name
    def name_of(idx):
        st_name=struct.unpack_from('<I',d,va_to_foff(segs,symtab)+idx*syment)[0]
        base=va_to_foff(segs,strtab)+st_name
        end=d.index(b'\x00',base)
        return d[base:end].decode('latin1')
    # walk rela.plt: each Elf64_Rela = r_offset(8) r_info(8) r_addend(8)
    got_slot=None
    jf=va_to_foff(segs,jmprel)
    for off in range(jf,jf+pltsz,24):
        r_off,r_info=struct.unpack_from('<QQ',d,off)
        sym=r_info>>32
        if name_of(sym)==symname:
            got_slot=r_off; break
    if got_slot is None: return None
    # scan exec seg for `(f2) ff 25 disp32` whose target == got_slot; prefer endbr64 stub
    eo,eva,efsz,emsz=exec_seg(segs)
    code=d[eo:eo+efsz]
    cands=[]
    i=0
    while True:
        j=code.find(b'\xff\x25',i)
        if j<0: break
        va=eva+j
        disp=struct.unpack_from('<i',code,j+2)[0]
        tgt=va+6+disp
        if tgt==got_slot:
            stub=(va//16)*16
            cands.append(stub)
        # also bnd form f2 ff 25 (va-1)
        if j>=1 and code[j-1]==0xf2:
            va2=eva+j-1; tgt2=va2+7+disp
            if tgt2==got_slot: cands.append((va2//16)*16)
        i=j+2
    for c in cands:
        fo=va_to_foff(segs,c)
        if fo is not None and d[fo:fo+4]==b'\xf3\x0f\x1e\xfa':  # endbr64
            return c
    return cands[0] if cands else None

def find_string_va(segs,d,s):
    needle=s.encode()+b'\x00'
    for t,fl,off,va,fsz,msz in segs:
        if t==1 and not (fl&1):  # non-exec load seg (rodata/data)
            idx=d.find(needle,off,off+fsz)
            if idx>=0: return va+(idx-off)
    return None

# ---- locate hook + skip via byte patterns ----
def find_one(d, parts):
    rx=bytearray()
    for p in parts:
        if isinstance(p,(bytes,bytearray)): rx+=re.escape(bytes(p))
        else: rx+=b'('+b'.'*p[1]+b')'
    ms=list(re.finditer(bytes(rx),d,re.DOTALL))
    return ms

HOOK_PARTS=[b'\x85\xC0\x0F\x85',('any',4),
            b'\x4C\x8B\x6C\x24\x18\xBA\x02\x00\x00\x00\x49\x89\xE9\x4C\x8D\x05',('any',4),
            b'\xB9\x00\x10\x00\x00\xBE\x00\x10\x00\x00\x4C\x89\xEF']
SKIP_PARTS=[b'\x39\x44\x24\x14\x0F\x8F',('any',4),
            b'\x45\x31\xED\x83\xC3\x01\x41\x39\x5E\x04']

def build_cave_patch(d):
    segs=elf_segments(d)
    hm=find_one(d,HOOK_PARTS); sm=find_one(d,SKIP_PARTS)
    if len(hm)!=1: return f"hook pattern matched {len(hm)} times"
    if len(sm)!=1: return f"skip pattern matched {len(sm)} times"
    hook_foff=hm[0].start()+8
    skip_foff=sm[0].start()+10
    assert d[hook_foff:hook_foff+5]==b'\x4C\x8B\x6C\x24\x18', d[hook_foff:hook_foff+5].hex()
    assert d[skip_foff:skip_foff+3]==b'\x45\x31\xED', d[skip_foff:skip_foff+3].hex()
    hook_va=foff_to_va(segs,hook_foff); skip_va=foff_to_va(segs,skip_foff)
    cont_va=hook_va+5
    # cave = 16-aligned start of exec-segment tail slack
    eo,eva,efsz,emsz=exec_seg(segs)
    cave_va=(eva+efsz+15)&~15
    page_end=(eva+emsz+0xfff)&~0xfff
    devfmt=find_string_va(segs,d,'/dev/%s')
    snpr=plt_stub_va(segs,d,'snprintf')
    isssd=plt_stub_va(segs,d,'SYNODiskIsSSD')
    if devfmt is None: return "'/dev/%s' not found"
    if snpr is None or isssd is None: return "plt resolve failed"
    # assemble. op() appends opcode bytes first, THEN (if given) a rel32 to
    # `target` computed from the end of the instruction -- so evaluation order
    # can't silently offset the displacement.
    code=bytearray()
    def op(opcodes, target=None):
        code.extend(opcodes)
        if target is not None:
            code.extend(struct.pack('<i', target-(cave_va+len(code)+4)))
    op(bytes.fromhex('4c8b6c2418'))         # mov r13,[rsp+0x18]  (displaced)
    op(bytes.fromhex('4c89ef'))             # mov rdi,r13
    op(bytes.fromhex('be00100000'))         # mov esi,0x1000
    op(b'\x48\x8d\x15', devfmt)             # lea rdx,[rip+d] -> "/dev/%s"
    op(bytes.fromhex('4c89e1'))             # mov rcx,r12
    op(bytes.fromhex('31c0'))               # xor eax,eax
    op(b'\xe8', snpr)                        # call snprintf@plt
    op(bytes.fromhex('4c89ef'))             # mov rdi,r13
    op(b'\xe8', isssd)                       # call SYNODiskIsSSD@plt
    op(bytes.fromhex('83f801'))             # cmp eax,1
    op(b'\x0f\x84', skip_va)                # je skip
    op(b'\xe9', cont_va)                     # jmp cont
    cave_new=bytes(code)
    if cave_va+len(cave_new) > page_end: return "cave does not fit in slack"
    hook_new=b'\xe9'+struct.pack('<i', cave_va-(hook_va+5))
    return dict(hook_va=hook_va,skip_va=skip_va,cave_va=cave_va,cont_va=cont_va,
                devfmt=devfmt,snpr=snpr,isssd=isssd,
                hook_orig=d[hook_foff:hook_foff+5],hook_new=hook_new,
                cave_orig=b'\x00'*len(cave_new),cave_new=cave_new)

r=build_cave_patch(DATA)
if isinstance(r,str):
    print("ERROR:",r)
else:
    for k in ('hook_va','cont_va','skip_va','cave_va','devfmt','snpr','isssd'):
        print(f"{k:10}= 0x{r[k]:x}")
    print("hook_orig =", r['hook_orig'].hex())
    print("hook_new  =", r['hook_new'].hex())
    print("cave_new  =", r['cave_new'].hex())
    # compare to hand-validated golden values
    GOLD_CAVE="4c8b6c24184c89efbe00100000488d15e81700004c89e131c0e872fcfeff4c89efe8eaf4feff83f8010f847328ffffe9f427ffff"
    GOLD_HOOK="e9d8d70000"
    print("\ncave matches golden:", r['cave_new'].hex()==GOLD_CAVE)
    print("hook matches golden:", r['hook_new'].hex()==GOLD_HOOK)
    print("addrs match golden :", (r['hook_va'],r['skip_va'],r['cave_va'],r['devfmt'],r['snpr'],r['isssd'])==(0xb9e3,0xba62,0x191c0,0x1a9bc,0x8e50,0x86d0))
