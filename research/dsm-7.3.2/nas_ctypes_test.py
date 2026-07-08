#!/usr/bin/env python3
"""Definitive function-level proof of the SSD-slot patch, in a throwaway process.

dlopen libsynoscemd, build the internal-SATA disk list the way scemd does, then call
DiskListIdleEnough(list, threshold) directly -- once unpatched, once after applying the
cave to THIS process's own mapping (mprotect + memmove; a bug only crashes this test).
With syno_idle_time forced to "SSD busy, HDDs idle", expect unpatched=0, patched=1; and
with a real HDD busy, expect 0 both ways (the patch must not ignore HDDs)."""
import ctypes, sys, importlib.util, os

spec = importlib.util.spec_from_file_location("hf", "/tmp/hiber_fixer_test.py")
hf = importlib.util.module_from_spec(spec); sys.modules["hf"] = hf
spec.loader.exec_module(hf)

LIB = "/usr/lib/libsynoscemd.so.1"
libc = ctypes.CDLL("libc.so.6", use_errno=True)
lib = ctypes.CDLL(LIB, mode=ctypes.RTLD_GLOBAL)   # pulls in its deps (SzList / DiskPortEnum)
g = ctypes.CDLL(None)                             # global symbol namespace

alloc = g.SLIBCSzListAlloc;         alloc.restype = ctypes.c_void_p; alloc.argtypes = [ctypes.c_int]
enum = g.SYNODiskPortEnum;          enum.restype = ctypes.c_int; enum.argtypes = [ctypes.c_int, ctypes.c_void_p]
get = g.SLIBCSzListGet;             get.restype = ctypes.c_char_p; get.argtypes = [ctypes.c_void_p, ctypes.c_int]
idle_enough = lib.DiskListIdleEnough; idle_enough.restype = ctypes.c_int; idle_enough.argtypes = [ctypes.c_void_p, ctypes.c_int]

# build the list exactly like polling_hibernation_timer: alloc, enum(1), enum(2)
lst = ctypes.c_void_p(alloc(0x400))
enum(1, ctypes.byref(lst)); enum(2, ctypes.byref(lst))
count = ctypes.cast(lst, ctypes.POINTER(ctypes.c_int))[1]   # list->count at offset 4
names = [get(lst, i).decode() for i in range(count)]
print("disk list built: count=%d names=%s" % (count, names))


def set_idle(**vals):
    for d, v in vals.items():
        open("/sys/block/%s/device/syno_idle_time" % d, "w").write(str(v))


def self_patch():
    # find our own libsynoscemd base (offset 0 mapping)
    base = None
    for line in open("/proc/self/maps"):
        p = line.split()
        if len(p) >= 6 and os.path.basename(p[5]) == "libsynoscemd.so.1" and p[2] == "00000000":
            base = int(p[0].split("-")[0], 16); break
    plan = hf.build_ssd_cave_patch(open(LIB, "rb").read())
    assert not isinstance(plan, str), plan
    PS = 0x1000

    def poke(vaddr, data):
        addr = base + vaddr
        s = addr & ~(PS - 1); e = (addr + len(data) + PS - 1) & ~(PS - 1)
        assert libc.mprotect(ctypes.c_void_p(s), e - s, 7) == 0, os.strerror(ctypes.get_errno())
        ctypes.memmove(addr, data, len(data))
        libc.mprotect(ctypes.c_void_p(s), e - s, 5)
    poke(plan.cave_vaddr, plan.cave_new)     # cave first
    poke(plan.hook_vaddr, plan.hook_new)     # then the hop
    return base, plan


THRESH = 5
print("\n== scenario A: SSD busy (idle=1), HDDs idle (idle=9999) ==")
set_idle(sata1=1, sata2=9999, sata3=9999)
a_unp = idle_enough(lst, THRESH)
base, plan = self_patch()
set_idle(sata1=1, sata2=9999, sata3=9999)
a_pat = idle_enough(lst, THRESH)
print("  DiskListIdleEnough: unpatched=%d  patched=%d   (expect 0 then 1)" % (a_unp, a_pat))

print("\n== scenario B: SSD idle (idle=9999), a real HDD busy (sata2 idle=1) ==")
set_idle(sata1=9999, sata2=1, sata3=9999)
b_pat = idle_enough(lst, THRESH)
print("  DiskListIdleEnough: patched=%d   (expect 0 -- a busy HDD must still block)" % b_pat)

print("\n== scenario C: everything idle (idle=9999) ==")
set_idle(sata1=9999, sata2=9999, sata3=9999)
c_pat = idle_enough(lst, THRESH)
print("  DiskListIdleEnough: patched=%d   (expect 1)" % c_pat)

ok = (a_unp == 0 and a_pat == 1 and b_pat == 0 and c_pat == 1)
print("\nVERDICT: %s" % ("PASS - the patch skips the SSD and only the SSD" if ok else "FAIL - see values above"))
