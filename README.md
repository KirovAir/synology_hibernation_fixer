# Synology DSM HDD hibernation fixer

Makes the hard drives of an x86 Synology NAS actually spin down (hibernate) by removing the
things that keep waking them up. It is especially useful when you run Docker containers on an
NVMe volume — by default DSM won't let HDDs hibernate while there is any NVMe activity.

This is a cleaned-up rewrite of [AlexFromChaos/synology_hibernation_fixer](https://github.com/AlexFromChaos/synology_hibernation_fixer)
(upstream, unmaintained since 2023). The fixes are based on the writeups
[here](https://www.reddit.com/r/synology/comments/10cpbqd/making_disk_hibernation_work_on_synology_dsm_7/)
and [here](https://www.reddit.com/r/synology/comments/129lzjg/fixing_hdd_hibernation_when_you_have_docker_on/).

Supported: **x86_64 NAS models on DSM 7.0 – 7.3** (verified against DSM 7.3.2).

## What it fixes

Applied every time `--run` executes:

1. **NVMe hibernation patch** — patches the *running* `scemd` and `synostgd-disk` processes in
   memory so ongoing NVMe I/O no longer blocks HDD hibernation. Only process memory is changed,
   never the on-disk binary, so this re-applies on each boot.
2. **synocrond task tuning** — slows down or deletes the many built-in background jobs (disk
   health, BTRFS maintenance, cert renewal, data collection, …) that wake disks. What to do with
   each job is declared in a plain JSON config file — no more baking choices into the source.
3. **noatime** — remounts `/` `noatime`, and (opt-in) sets data volumes to `noatime`.
4. **synocached** — lowers the redis/synocached idle timeout from 3600 s to 900 s.

## How it persists (what changed vs. the original)

The original embedded the *entire script* as an `xz`+`base64` blob inside the Task Scheduler
task. **This version does not.** Instead:

- `--install` copies the script to a data volume (default `/volume1/hiber_fixer/`) and writes a
  config file next to it (`hiber_fixer.config.json`).
- It creates a boot-up Task Scheduler task that simply runs
  `python3 /volume1/hiber_fixer/hiber_fixer.py --run`.

Both the task (kept in DSM's database) and the script (on a user volume) survive DSM upgrades.

> **Note:** DSM updates sometimes *disable* root-privileged boot tasks. If your disks stop
> hibernating after an update, run `--status`; if the task shows `DISABLED`, re-enable it in
> **Control Panel → Task Scheduler**, or just re-run `--install`.

## Usage

SSH into your NAS and switch to root (`sudo -i`), then:

```bash
# install (copies to /volume1/hiber_fixer, creates the boot task, applies fixes now)
python3 hiber_fixer.py --install
# or choose a different install location:
python3 hiber_fixer.py --install --install-dir /volume2/hiber_fixer

python3 hiber_fixer.py --run         # apply everything now (what the boot task runs)
python3 hiber_fixer.py --status      # show current state (task, patch, noatime, config)
python3 hiber_fixer.py --configure   # interactively edit which jobs to slow down / delete
python3 hiber_fixer.py --diagnose    # dump patch-site info (use if a DSM update breaks the patch)
python3 hiber_fixer.py --uninstall   # remove the boot task
```

After `--install` you can delete the copy you ran it from; the active copy lives in the install
directory. Logs go to `/var/log/hibernation_fixer.log`; backups of any modified config file go to
`/var/synobackup`.

## The config file

`hiber_fixer.config.json` (created next to the script on install):

```json
{
    "fixes": {
        "nvme_in_memory_patch": true,
        "remount_root_noatime": true,
        "synocached_timeout_900": true,
        "set_volumes_noatime": true
    },
    "synocrond_tasks": {
        "builtin-synodatacollect-udc": "delete",
        "builtin-libhwcontrol-disk_weekly_routine": "weekly",
        "...": "..."
    }
}
```

Each task action is one of `unchanged | hourly | daily | weekly | monthly | delete`. Sensible
defaults are filled in; edit and re-run `--run`. New tasks introduced by future DSM/package
updates are added automatically (as `unchanged`) so you can review them. `set_volumes_noatime`
(default `true`) also sets your data volumes to `noatime`, which takes effect on the next reboot;
set it to `false` to leave volume mount options untouched.

## How the NVMe patch works (for the curious)

Decompilation (Ghidra) shows both binaries call `SYNODiskPortEnum(portType, &list)` to build a
list of disk ports, then decide hibernation from it:

- `scemd` (`polling_hibernation_timer.c`) enumerates port types **1, 2** (internal SATA) and
  **7** (NVMe), then `DiskListIdleEnough(list)` decides whether the HDDs may sleep.
- `synostgd-disk` (`disk_monitor.c`) loops enumerating port types **1, 3, 7, 11** and watches
  each disk for activity.

**Port type 7 is NVMe**, so including it lets NVMe I/O keep the HDDs awake. The patch removes NVMe
from those lists — in `scemd` by changing the argument (`7` → `0x0B`), in `synostoraged` by
inserting a 2-byte `jmp` that skips the type-7 enumeration. The byte patterns are identical across
DSM 7.2 and 7.3.2; if a future DSM recompiles these binaries and the patterns stop matching,
`--run` logs a loud error and `--diagnose` dumps the candidate sites so new patterns can be
generated (re-decompile with Ghidra to confirm the `SYNODiskPortEnum(7,…)` site).

## Uninstalling

Delete the **"HDD Hibernation Fixer task"** in **Task Scheduler**, or run
`python3 hiber_fixer.py --uninstall`. Reboot to fully revert the in-memory patch.
