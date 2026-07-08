# Research toolkit

The reverse-engineering scaffolding behind the patches in `hiber_fixer.py`. Kept in-repo so the
work can be re-run and extended after a DSM update. The write-up of *what* was found lives in
[`../docs/hibernation-internals.md`](../docs/hibernation-internals.md); this is the *how*.

The proprietary DSM binaries are **not** committed (see `dsm-7.3.2/bins/README.md` for how to
re-fetch and verify them).

## Layout

```
research/
  dsm-7.3.2/
    elftool.py          stdlib+capstone ELF helper: strings, xrefs, PLT resolution, disasm
    proto_resolver.py   self-contained (stdlib-only) resolver -> the exact logic ported into
                        hiber_fixer.build_ssd_cave_patch(); verifies it against golden bytes
    build_ssd_cave.py   builds + capstone-validates the SSD-slot cave (hand-derivation of record)
    nas_ctypes_test.py  the definitive test: dlopen libsynoscemd, call DiskListIdleEnough directly,
                        unpatched vs. self-patched, with forced idle counters (run on the NAS)
    nas_behavior_test.py before/after via scemd's real spindown (kept as a cautionary example -- it
                        is inconclusive because scemd's threshold is minutes*60 through a 2-stage timer)
    bins/               proprietary binaries (git-ignored) + SHA256SUMS.txt
  revtools/             Ghidra headless toolkit (see revtools/README.md)
```

## Typical workflow

1. Pull the binaries onto the machine and drop them in `dsm-7.3.2/bins/` (verify `SHA256SUMS.txt`).
2. Quick look with capstone: `cd dsm-7.3.2 && python elftool.py bins/libsynoscemd.so.1`, then use
   `Elf(...)` interactively (`find_string`, `xrefs_to_addr`, `calls_to_symbol`, `print_range`).
3. Clean decompiled C with Ghidra: `research/revtools/` (import once, then decompile any function
   by name or file-offset).
4. Derive/validate a patch: `python proto_resolver.py` reproduces the cave from the ELF and checks
   it against the golden bytes; `build_ssd_cave.py` shows the hand-assembly with capstone verify.
5. Prove behaviour on the NAS: push `hiber_fixer.py` to `/tmp/hiber_fixer_test.py`, then run
   `nas_ctypes_test.py` as root (it self-patches a throwaway process, so a mistake can't hurt DSM).

## Requirements

- Python 3.8+ with `capstone` (`pip install capstone`); `elftool.py` also wants `pyelftools`.
  The stdlib-only `proto_resolver.py` and the NAS test scripts need neither.
- Ghidra + JDK for the decompiler step, see `revtools/README.md`.
