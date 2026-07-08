# DSM HDD hibernation internals & the byte patches

This is the long-form companion to the code. It records how DSM decides whether the HDDs may
hibernate, why NVMe and an SSD-in-a-HDD-bay each keep them awake, and exactly what the two
in-memory patches do, down to the bytes, so anyone (including future me) can re-derive and
re-verify them after a DSM update. Everything here was recovered by decompiling the DSM 7.3.2
binaries with Ghidra and cross-checking with capstone; nothing depends on Synology source.

> The proprietary DSM binaries themselves are **not** in this repo (they're kept locally under a
> git-ignored `research/` folder). The addresses below are for the DSM 7.3.2-86009 build on a
> DS920+ (Geminilake, x86-64); treat them as a worked example, not constants.

## Contents
1. [How disk hibernation is decided](#1-how-disk-hibernation-is-decided)
2. [Why NVMe keeps the HDDs awake, and the NVMe patch](#2-why-nvme-keeps-the-hdds-awake-and-the-nvme-patch)
3. [Why an SSD in a HDD bay keeps them awake, and the SSD-slot cave patch](#3-why-an-ssd-in-a-hdd-bay-keeps-them-awake-and-the-ssd-slot-cave-patch)
4. [Safety of the cave patch](#4-safety-of-the-cave-patch)
5. [How each patch is applied at runtime](#5-how-each-patch-is-applied-at-runtime)
6. [Re-deriving the patches after a DSM update](#6-re-deriving-the-patches-after-a-dsm-update)
7. [Empirical notes](#7-empirical-notes)

---

## 1. How disk hibernation is decided

On an x86 DSM box the internal drive bays form **one hibernation group**: the HDDs only spin
down once *every* internal disk has been idle past the standby timer. Two userspace daemons are
involved:

- **`scemd`** runs the hibernation *decision* loop. Its private helper library
  `libsynoscemd.so.1` exports `DiskListIdleEnough(list, threshold)`, which is the actual gate.
- **`synostgd-disk`** (a forked child of `synostoraged`) runs `disk_monitor.c`, a health poller.
  It reads SMART data from *active* disks and explicitly skips idle/spun-down ones, so it does
  not itself gate or wake the HDDs. (It is patched for NVMe only for completeness.)

The kernel exposes a per-disk idle counter at `/sys/block/<dev>/device/syno_idle_time` (seconds
since the last I/O to that physical disk; it free-runs at ~1/s and resets to 0 on I/O).

### `scemd`'s decision loop (`polling_hibernation_timer.c`)

The polling loop (function `FUN_00120280` in the 7.3.2 build) does, roughly:

```c
for (;;) {
    ScemdSleep(29);
    if (IsUPSInSafeMode()) continue;
    t1 = GetStandbyTimer(1) * 60;                 // minutes -> seconds
    if (t1 <= 0) continue;
    if (!DiskListIdleEnough(list, t1)) { ...; continue; }   // stage 1
    ...
    t2 = GetStandbyTimer(2) * 60;
    if (!DiskListIdleEnough(list, t1 + t2)) continue;       // stage 2
    HibernateAction(3);                            // actually put the group to sleep
}
```

`list` is built from `SYNODiskPortEnum(1)` + `SYNODiskPortEnum(2)` (internal SATA + external
SATA) + `SYNODiskPortEnum(7)` (NVMe). So the standby threshold is **standby-timer-minutes × 60
seconds**, gated through a two-stage timer, which is why hibernation is slow to trigger and why
faking `syno_idle_time` for 90 s does *not* reproduce it (see [§7](#7-empirical-notes)).

### `DiskListIdleEnough(list, threshold)` (`disk_list_idle_get.c`)

This is the whole gate. Decompiled:

```c
int DiskListIdleEnough(list, threshold) {
    for (i = 0; i < list->count; i++) {
        dev = list[i];                                   // e.g. "sata1"
        snprintf(p, "/sys/block/%s", dev);
        if (access(p, F_OK) != 0) continue;              // absent -> ignore
        fp = fopen("%s/device/syno_idle_time", p);
        if (fp) {
            idle = strtol(fgets(fp));
            if (idle < threshold) return 0;              // ANY disk active-recently => not idle
        }
        seen = 1;
    }
    return seen ? 1 : 0;                                 // all present disks idle enough
}
```

It treats every disk in `list` the same. **It does not distinguish SSDs from HDDs.** That's the
root of both problems below.

---

## 2. Why NVMe keeps the HDDs awake, and the NVMe patch

`SYNODiskPortEnum(portType, &list)` enumerates disks by *port type*. Type **7 == NVMe**. Because
`scemd` adds NVMe to the gating list, any NVMe I/O (e.g. Docker on an NVMe volume) keeps
`DiskListIdleEnough` returning 0. The fix removes NVMe from the two lists:

- **`scemd`**: change the port-type argument of the NVMe enumeration from `7` to `0x0B` (an
  unused type). One byte: `mov edi,7` (`BF 07 00 00 00`) → `mov edi,0x0B`.
- **`synostgd-disk`**: insert a 2-byte relative `jmp` (`EB 13`) that skips the entire
  `SYNODiskPortEnum(7,…)` block in `disk_monitor.c`.

These are equal-length, in-place edits located by byte pattern (`SCEMD_TARGET` /
`SYNOSTORAGED_TARGET` in `hiber_fixer.py`). The DSM 7.2 patterns still match 7.3.2 exactly.

---

## 3. Why an SSD in a HDD bay keeps them awake, and the SSD-slot cave patch

An SSD placed in a SATA/HDD bay (used as a data volume) enumerates as the **same port type** as
the HDDs (internal SATA). So it lands in the gating list, and the NVMe trick can't help, because
dropping the SATA port type would drop the HDDs too. The SSD's frequent I/O keeps its
`syno_idle_time` low, so `DiskListIdleEnough` returns 0 and `scemd` never re-issues standby to the
HDDs.

Two facts make a clean fix possible:

- `syno_idle_time` is **per-disk**: SSD-only I/O does not reset the HDD counters (measured on
  hardware, see [§7](#7-empirical-notes)). So if the gate ignored the SSD, the HDDs would sleep
  on their own schedule.
- `libsynoscemd` already imports **`SYNODiskIsSSD("/dev/<dev>")`**, which just reads
  `/sys/block/<dev>/queue/rotational` and returns 1 for a non-rotational disk. The sibling
  function `IsInternalDiskSelfTesting()` in the same file already uses it to skip SSDs.

So the fix is: make `DiskListIdleEnough` skip SSDs, the same way `IsInternalDiskSelfTesting`
does. There is no room to add a call inline, so we redirect one instruction into a **code cave**
(the zero-filled tail of the library's own executable segment) and run the check there.

### The hook

Right after the `access("/sys/block/<dev>") == 0` test, the loop executes
`mov r13,[rsp+0x18]` (5 bytes, `4C 8B 6C 24 18`). We overwrite exactly those 5 bytes with a
`jmp CAVE` (`E9 <rel32>`, also 5 bytes). The instruction after it is untouched, and it's only
ever reached by fall-through, so the replacement is clean.

### The cave (52 bytes)

```asm
CAVE:
    mov  r13,[rsp+0x18]        ; re-do the displaced instruction (r13 = the loop's 4 KiB scratch buf)
    mov  rdi, r13
    mov  esi, 0x1000
    lea  rdx, [rip+"/dev/%s"]
    mov  rcx, r12             ; r12 = the bare disk name, e.g. "sata1"
    xor  eax, eax
    call snprintf             ; build "/dev/sata1" into the scratch buffer
    mov  rdi, r13
    call SYNODiskIsSSD        ; reads /sys/block/sata1/queue/rotational
    cmp  eax, 1
    je   <skip>               ; SSD  -> jump to the loop's "disk done, don't block" path
    jmp  <cont>               ; else -> continue the normal syno_idle_time check
```

`<skip>` is the `xor r13d,r13d` that marks "this disk is fine, keep going"; `<cont>` is the
instruction right after the hook. `SYNODiskIsSSD` needs a `/dev/<name>` argument (it does
`sscanf(arg,"/dev/%s",…)` internally), so we build one with `snprintf` from the bare name the
loop already has in `r12`, into the scratch buffer the loop is about to reuse anyway.

Worked example (DSM 7.3.2 DS920+ build):

| thing | value |
|---|---|
| hook | `0xb9e3`, `4C 8B 6C 24 18` → `E9 D8 D7 00 00` |
| cave | `0x191c0` (16-aligned start of exec-segment tail slack; ~3.6 KiB free) |
| `<cont>` / `<skip>` | `0xb9e8` / `0xba62` |
| `"/dev/%s"` | `0x1a9bc` |
| `snprintf@plt` / `SYNODiskIsSSD@plt` | `0x8e50` / `0x86d0` |
| cave bytes | `4c8b6c24184c89efbe00100000488d15e81700004c89e131c0e872fcfeff4c89efe8eaf4feff83f8010f847328ffffe9f427ffff` |

None of these are hard-coded: `build_ssd_cave_patch()` locates the hook/skip by byte pattern and
resolves the cave address, the two PLT stubs and the string straight from the ELF, then assembles
the cave. If anything can't be resolved it returns an error and refuses to patch.

### eSATA SSDs

The cave lives inside `DiskListIdleEnough`, and that function walks the whole list `scemd` builds
from `SYNODiskPortEnum(1)` (internal SATA) **and `(2)`, which is external/eSATA**. It decides per
disk by reading the `rotational` flag, not by port type, so an SSD on an eSATA port gets skipped
exactly like one in an internal bay. So the fix should cover eSATA SSDs too, though I haven't tested
one (no eSATA device on hand).

One eSATA-specific blocker the cave does *not* touch: `polling_hibernation_timer` forces the gate to
"not idle" whenever an eSATA disk carries a read-write HFS+ filesystem (`HasESATAWithRWHFSPlus()`),
regardless of whether that disk is an SSD. It never even reaches `DiskListIdleEnough` in that case.
Format such a drive ext4 or btrfs and it's a non-issue.

---

## 4. Safety of the cave patch

The patch is designed so the worst realistic outcome is "no effect", never a crash or a wrongly
hibernated HDD:

- **Fail-safe logic.** Only `SYNODiskIsSSD()==1` skips a disk. A HDD returns 0; any error returns
  something other than 1. In both cases the `je` is not taken and control falls through to the
  *original* code path, identical to not patching. A real busy HDD still blocks hibernation
  (verified).
- **ABI-clean.** The registers the cave clobbers (`rax/rcx/rdx/rsi/rdi/r8-r11`) are all dead at
  the hook (the continuation reloads them). `r12/r13/rbp/rbx/r14/r15` are callee-saved and
  preserved across both calls. `rsp` is untouched, so the SysV 16-byte call alignment the
  surrounding code already relies on is preserved. The scratch buffer is the loop's own 4 KiB
  stack buffer.
- **Crash-safe apply order.** The cave body is written and read-back-verified *first*; the 5-byte
  jump is written *last*. A failed or partial write therefore never leaves a live jump into an
  unwritten cave. Reverting does the reverse (restore the hook first, then zero the cave).
- **Verified end to end.** A throwaway `ctypes` harness (`research/dsm-7.3.2/nas_ctypes_test.py`)
  builds the exact disk list `scemd` builds and calls `DiskListIdleEnough` directly, unpatched vs.
  patched, with the idle counters forced:

  | scenario | unpatched | patched |
  |---|---|---|
  | SSD busy, HDDs idle | `0` (blocked) | **`1`** (HDDs may sleep) |
  | SSD idle, a real **HDD** busy | n/a | `0` (still blocked) |
  | all idle | n/a | `1` |

  i.e. the patch skips the SSD, and *only* the SSD. Applying it to the live `scemd` process left
  its PID unchanged (no crash).

---

## 5. How each patch is applied at runtime

All three patches are applied to **process memory**, never to the on-disk binaries, so they are
reverted by a reboot and re-applied by the boot task:

1. Locate the target process (`scemd`, `synostgd-disk`) and, for the cave, the load base of
   `libsynoscemd.so.1` inside `scemd` via `/proc/<pid>/maps`.
2. Compute the sites (pattern replacement for NVMe; the computed cave + hook for the SSD-slot fix).
3. Read the current bytes: if they already equal the patched bytes, it's a no-op; if they don't
   equal the expected originals, abort loudly (don't patch blindly).
4. `PTRACE_ATTACH` (stops the process, so there's no torn instruction fetch), write each site via
   `/proc/<pid>/mem` (which the kernel permits into read-only code pages while ptrace-stopped),
   **read each back to verify**, then `PTRACE_DETACH`.

The SSD-slot patch is opt-in (`fixes.ssd_slot_in_memory_patch`, default `false`) since it's only
relevant when you run an SSD in a SATA/HDD bay next to HDDs you want to sleep.

---

## 6. Re-deriving the patches after a DSM update

If DSM recompiles these binaries and a pattern stops matching, `--run` logs a loud error and
`--diagnose` dumps what it can. To regenerate:

1. Pull the live binaries and `libsynoscemd.so.1` off the NAS (they may be root-only; copy via
   `base64` over SSH, verify with `sha256sum`).
2. Re-decompile with Ghidra headless. The reusable scripts live in the local `revtools/` toolbox:
   `DumpFuncs.java` (decompile a function by name or file-offset, listing its calls) and
   `AnalyzeHibernation.java` (survey every `SYNODiskPortEnum` call site + hibernation strings).
   `elftool.py` in `research/` gives quick capstone-based disassembly, string/xref scans and PLT
   resolution.
3. Confirm the anchors: the `SYNODiskPortEnum(7,…)` call sites for the NVMe patch; and for the
   cave, the `syno_idle_time` read inside `DiskListIdleEnough` (the hook is the `mov r13,[rsp+…]`
   right after the `access()` test) and the "this disk is idle enough" tail (the skip target).
4. Update the byte patterns in `hiber_fixer.py`. The cave itself is fully recomputed from the ELF,
   so usually only `SSD_CAVE_HOOK_PATTERN` / `SSD_CAVE_SKIP_PATTERN` need re-checking.

---

## 7. Empirical notes

- **`syno_idle_time` is per-disk, not a group counter.** With the HDDs idle at 22 s, a single
  8 MB SSD-only write (confirmed by `/proc/diskstats` to hit only the SSD) left the HDD counters
  at 22. They only reset when a real `md0` (`/`) write touched all three RAID1 members. This is
  what makes skipping the SSD in the gate sufficient.
- **The counter is writable** (`echo N > …/syno_idle_time`) and free-runs at ~1/s from whatever
  you write. Handy for the function-level test; not a reliable fix on its own (the kernel resets
  it on the next real I/O).
- **Why the naive behavioural test is misleading.** Because the standby threshold is
  `minutes × 60` gated through a two-stage timer with ~30 s poll sleeps, forcing `syno_idle_time`
  high for 90 s does not make `scemd` spin the disks down in either the patched or unpatched case.
  The correct validation is the direct `DiskListIdleEnough` call in [§4](#4-safety-of-the-cave-patch).
- **The structural floor.** Even with every fix applied, the HDDs still take the occasional wake
  from the unavoidable `md0` (`/`) system-partition write trickle, which DSM mirrors onto the HDDs
  by design. That's a handful of wakes per hour, independent of NVMe or the SSD.
