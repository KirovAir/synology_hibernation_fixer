#!/usr/bin/env python3
"""Build & validate the SSD-skip code-cave patch for libsynoscemd.so.1.

Hook: at 0xb9e3 in DiskListIdleEnough (the instruction `mov r13,[rsp+0x18]`,
5 bytes) we write `jmp CAVE`. The cave:
  1. re-executes the displaced `mov r13,[rsp+0x18]`  (r13 = &local_1048 scratch)
  2. snprintf(r13, 0x1000, "/dev/%s", r12)            (r12 = bare disk name)
  3. eax = SYNODiskIsSSD(r13)
  4. if (eax == 1) jmp 0xba62  (skip disk = treat as idle, don't block)
     else          jmp 0xb9e8  (continue normal syno_idle_time check)

Only caller-saved regs are touched (dead at the hook); r12/r13 are callee-saved
and preserved across the two calls. rsp is unchanged so call alignment is kept.
Non-SSD / error from SYNODiskIsSSD (eax!=1) falls through to original behavior
=> fail-safe (worst case == current behavior, never a crash from our logic).
"""
import sys, struct
from elftool import Elf

L = Elf('bins/libsynoscemd.so.1')

HOOK      = 0xb9e3          # patch site (mov r13,[rsp+0x18], 5 bytes)
HOOK_ORIG = bytes.fromhex('4c8b6c2418')
CONT      = 0xb9e8          # continue normal idle check
SKIP      = 0xba62          # skip disk (xor r13d,r13d; add ebx,1; loop)
DEVFMT    = 0x1a9bc         # "/dev/%s"
SNPRINTF  = 0x8e50          # snprintf@plt
ISSSD     = 0x86d0          # SYNODiskIsSSD@plt
CAVE      = 0x191c0         # in exec-segment tail slack (0x191b5..0x1a000)

assert L.read(HOOK,5) == HOOK_ORIG, "hook bytes changed!"

def rel32(frm_end, target):
    d = target - frm_end
    assert -0x80000000 <= d < 0x80000000
    return struct.pack('<i', d)

# --- assemble the cave, tracking addresses so rel32 is exact ---
code = bytearray()
def emit(b): code.extend(b)
def here(): return CAVE + len(code)

emit(bytes.fromhex('4c8b6c2418'))                 # mov r13,[rsp+0x18]   (displaced)
emit(bytes.fromhex('4c89ef'))                     # mov rdi, r13
emit(bytes.fromhex('be00100000'))                 # mov esi, 0x1000
emit(b'\x48\x8d\x15'); emit(rel32(here()+4, DEVFMT))   # lea rdx,[rip+d] -> "/dev/%s"
emit(bytes.fromhex('4c89e1'))                     # mov rcx, r12
emit(bytes.fromhex('31c0'))                       # xor eax, eax
emit(b'\xe8'); emit(rel32(here()+4, SNPRINTF))    # call snprintf
emit(bytes.fromhex('4c89ef'))                     # mov rdi, r13
emit(b'\xe8'); emit(rel32(here()+4, ISSSD))       # call SYNODiskIsSSD
emit(bytes.fromhex('83f801'))                     # cmp eax, 1
emit(b'\x0f\x84'); emit(rel32(here()+4, SKIP))    # je 0xba62 (skip SSD)
emit(b'\xe9'); emit(rel32(here()+4, CONT))        # jmp 0xb9e8 (continue)

cave_bytes = bytes(code)
hook_jmp = b'\xe9' + rel32(HOOK+5, CAVE)          # jmp CAVE at the hook

print(f"CAVE @0x{CAVE:x}  len={len(cave_bytes)}  ends@0x{CAVE+len(cave_bytes):x} (slack to 0x1a000)")
print(f"cave bytes : {cave_bytes.hex()}")
print(f"hook jmp   : {hook_jmp.hex()}  (replaces {HOOK_ORIG.hex()} @0x{HOOK:x})")

# --- build a patched image and disassemble to VERIFY every target resolves ---
raw = bytearray(L.raw)
# hook site
raw[HOOK:HOOK+5] = hook_jmp
# cave: file offset == vaddr for exec seg here; but CAVE is beyond filesz.
# Extend the buffer with zeros up to CAVE, then place cave bytes (validation only).
if len(raw) < CAVE + len(cave_bytes):
    raw.extend(b'\x00' * (CAVE + len(cave_bytes) - len(raw)))
raw[CAVE:CAVE+len(cave_bytes)] = cave_bytes

# reload as an Elf-like for disasm using capstone directly
from capstone import Cs, CS_ARCH_X86, CS_MODE_64
from capstone.x86 import X86_OP_IMM, X86_OP_MEM, X86_REG_RIP
md = Cs(CS_ARCH_X86, CS_MODE_64); md.detail = True

def name_for(addr):
    if addr in L.sym_by_addr: return L.sym_by_addr[addr]
    if addr in L.plt_by_addr: return L.plt_by_addr[addr]+'@plt'
    named = {CONT:'CONT(0xb9e8)', SKIP:'SKIP(0xba62)', CAVE:'CAVE'}
    return named.get(addr)

def show(vaddr, n):
    for insn in md.disasm(bytes(raw[vaddr:vaddr+n*12]), vaddr):
        ann=''
        for op in insn.operands:
            if op.type==X86_OP_IMM and (insn.mnemonic=='call' or insn.mnemonic.startswith('j')):
                nm=name_for(op.imm); ann=f'   ; -> {nm} (0x{op.imm:x})' if nm else f'   ; -> 0x{op.imm:x}'
            if op.type==X86_OP_MEM and op.mem.base==X86_REG_RIP:
                ea=insn.address+insn.size+op.mem.disp
                s=L._maybe_str(ea) if ea<len(L.raw) else None
                if s: ann=f'   ; "{s}"'
        print(f'  0x{insn.address:06x}: {insn.bytes.hex():<20} {insn.mnemonic} {insn.op_str}{ann}')
        if insn.address+insn.size >= vaddr+n: break

print("\n=== patched hook site (0xb9de..) ===")
show(0xb9de, 14)
print("\n=== cave (disassembled from patched image) ===")
show(CAVE, len(cave_bytes))

# sanity checks
errs=[]
# reconstruct expected targets by re-decoding
def call_targets():
    tgts={}
    for insn in md.disasm(cave_bytes, CAVE):
        if insn.mnemonic in ('call','jmp','je') or insn.mnemonic.startswith('j'):
            for op in insn.operands:
                if op.type==X86_OP_IMM: tgts[insn.address]=op.imm
    return tgts
tg=call_targets()
want={ 'snprintf':SNPRINTF, 'isssd':ISSSD, 'skip':SKIP, 'cont':CONT }
resolved=set(tg.values())
for label,addr in want.items():
    if addr not in resolved: errs.append(f"target {label} 0x{addr:x} NOT reached by any cave branch")
# hook jmp lands on cave
hj=list(md.disasm(hook_jmp, HOOK))[0]
if hj.operands[0].imm != CAVE: errs.append("hook jmp does not target CAVE")
print("\n=== VALIDATION:", "ALL OK" if not errs else "ERRORS: "+"; ".join(errs), "===")
