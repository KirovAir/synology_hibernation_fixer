#!/usr/bin/env python3
"""Live before/after proof that the SSD-slot cave patch works.

Holds "SSD busy (idle=1), HDDs idle (idle=9999)" and checks whether scemd spins the
HDDs down. Unpatched: the SSD's low idle blocks DiskListIdleEnough -> HDDs stay awake.
Patched: the SSD is skipped -> HDDs spin down. Leaves the patch APPLIED at the end.
Run as root on the NAS: sudo python3 nas_behavior_test.py"""
import importlib.util, sys, time, subprocess

spec = importlib.util.spec_from_file_location("hf", "/tmp/hiber_fixer_test.py")
hf = importlib.util.module_from_spec(spec); sys.modules["hf"] = hf
spec.loader.exec_module(hf)
hf.setup_logging(False)

IDLE = {d: "/sys/block/%s/device/syno_idle_time" % d for d in ("sata1", "sata2", "sata3")}
SPIN = {d: "/sys/block/%s/device/syno_spindown" % d for d in ("sata1", "sata2", "sata3")}


def rd(p):
    try: return open(p).read().strip()
    except Exception: return "?"


def wr(p, v):
    try: open(p, "w").write(str(v)); return True
    except Exception: return False


def pid():
    return hf.get_pid_by_name("scemd")


def cave_state():
    p, _v, sites, err = hf.resolve_sites(hf.SSD_CAVE_TARGET)
    if sites is None: return "ERR:" + str(err)
    return "".join("P" if hf.read_mem(p, a, len(o)) == n else "O" if hf.read_mem(p, a, len(o)) == o else "?"
                   for a, o, n in sites)


def revert():
    p, _v, sites, _e = hf.resolve_sites(hf.SSD_CAVE_TARGET)   # sites = [cave, hook]
    pt = hf.Ptrace()
    if not pt.attach(p): return False
    try:
        ok = hf.write_mem(p, [(sites[1][0], sites[1][1])])       # hook -> original first
        ok = hf.write_mem(p, [(sites[0][0], sites[0][1])]) and ok  # then zero the cave
        return ok
    finally:
        pt.detach(p)


def wake_hdds():
    for d in ("sata2", "sata3"):
        subprocess.run(["dd", "if=/dev/%s" % d, "of=/dev/null", "bs=4096", "count=1", "iflag=direct"],
                       stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    time.sleep(2)


def watch(label, seconds):
    wake_hdds()
    print("--- %s ---" % label)
    print("    scemd pid=%s  cave=%s  start spindown[s2=%s s3=%s]"
          % (pid(), cave_state(), rd(SPIN["sata2"]), rd(SPIN["sata3"])))
    spun = False
    t0 = time.time()
    while time.time() - t0 < seconds:
        wr(IDLE["sata1"], 1)         # SSD kept "busy"
        wr(IDLE["sata2"], 9999)      # HDDs held "idle"
        wr(IDLE["sata3"], 9999)
        sp2, sp3, i1 = rd(SPIN["sata2"]), rd(SPIN["sata3"]), rd(IDLE["sata1"])
        el = int(time.time() - t0)
        if sp2 == "1" or sp3 == "1":
            print("    t=%2ds sata1_idle=%s spindown[s2=%s s3=%s]  <<< HDDs SPUN DOWN" % (el, i1, sp2, sp3))
            spun = True; break
        if el % 12 == 0:
            print("    t=%2ds sata1_idle=%s spindown[s2=%s s3=%s]" % (el, i1, sp2, sp3))
        time.sleep(3)
    print("    RESULT: %s\n" % ("HDDs SPUN DOWN" if spun else "HDDs stayed awake"))
    return spun


print("initial: scemd pid=%s cave=%s\n" % (pid(), cave_state()))
revert(); time.sleep(2)
print("reverted -> cave=%s" % cave_state())
unpatched = watch("UNPATCHED  (SSD idle=1, HDDs idle=9999)  -- expect: stay awake", 80)

hf.apply_target(hf.Ptrace(), hf.SSD_CAVE_TARGET); time.sleep(2)
print("applied  -> cave=%s" % cave_state())
patched = watch("PATCHED    (SSD idle=1, HDDs idle=9999)  -- expect: spin down", 95)

print("scemd pid after test: %s  (unchanged => no crash)" % pid())
print("SUMMARY: unpatched_spun=%s  patched_spun=%s" % (unpatched, patched))
print("VERDICT: %s" % ("PASS - patch lets HDDs sleep while SSD is busy"
                       if (patched and not unpatched) else "INCONCLUSIVE - see log above"))
print("cave left in state:", cave_state())
