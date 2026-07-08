# revtools: Ghidra headless toolkit

Scripts to decompile the DSM binaries with Ghidra from the command line. The Ghidra + JDK installs
themselves are large and **not** in the repo; these are the post-scripts and runner wrappers.

## Setup (what the `.ps1` paths assume)

- **JDK 21** and **Ghidra 12.1.2 PUBLIC**, installed under `%LOCALAPPDATA%\revtools\` (see
  `paths.txt` for the exact `java.exe` / `analyzeHeadless.bat` locations and `ghidra_url.txt` for
  the Ghidra download). Adjust the paths at the top of each `.ps1` if you install elsewhere.
- The binaries in `../dsm-7.3.2/bins/`.

## Ghidra post-scripts (`*.java`)

- **`DumpFuncs.java`**: the workhorse. Args are `name:Symbol` or `off:0xNNNN` (a file-offset ==
  vaddr for these PIE binaries); for each it prints the signature, the in-order list of calls, and
  the decompiled C. Used for `DiskListIdleEnough`, `polling_hibernation_timer`, `disk_monitor`, etc.
- **`AnalyzeHibernation.java`**: open-ended survey: every `SYNODiskPortEnum` call site with its
  decompiled caller, plus hibernation/idle/spindown/nvme strings and who references them.
- **`DecompilePatchSites.java`**: decompiles the function containing each patch offset and names
  the call target at the site (used to confirm the NVMe patch sites).

## Runners (`*.ps1`)

- **`import_analyze.ps1`**: import + analyze both NVMe binaries into a persistent project once.
- **`import_one.ps1 <binary>`**: import + analyze one more binary (e.g. `libsynoscemd.so.1`).
- **`decompile.ps1 <bin> off:0x.. name:..`**: fast decompile against the analyzed project
  (`-process -noanalysis`), no re-analysis. This is the one to iterate with.
- **`run_ghidra.ps1` / `run_survey.ps1`**: older one-shot runners (import+analyze+script+delete)
  kept for reference.

Import once with `import_analyze.ps1` (+ `import_one.ps1` for the library), then iterate with
`decompile.ps1`. Ghidra addresses = image base `0x100000` + file offset, so a file offset `0xNNNN`
is Ghidra address `0x10NNNN`.
