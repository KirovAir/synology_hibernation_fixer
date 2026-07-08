# Synology HDD hibernation fixer

This script fixes the handful of things that stop a Synology NAS from hibernating its hard drives.

It's especially useful if you run Docker on an NVMe drive or an SSD. By default DSM has a flaw where
that activity keeps the hard drives spinning, even when nothing is actually touching them.

It's a cleaned-up fork of [AlexFromChaos's original](https://github.com/AlexFromChaos/synology_hibernation_fixer),
which worked out why this happens and how to patch it (he wrote it up
[here](https://www.reddit.com/r/synology/comments/10cpbqd/making_disk_hibernation_work_on_synology_dsm_7/)
and [here](https://www.reddit.com/r/synology/comments/129lzjg/fixing_hdd_hibernation_when_you_have_docker_on/)).
This version installs itself as a boot task that reapplies the fixes on each boot, and adds one for
an SSD sitting in a SATA drive bay.

Developed and tested on a DS920+ (DSM 7.3.2). It targets x86_64 models on DSM 7.0 through 7.3, and
runs on ARM too, just without the process patches (see the table).

## What it does

Each run applies these fixes:

- Stops NVMe activity from keeping the HDDs awake.
- Stops an SSD used as a data volume in a drive bay from keeping the HDDs awake (off by default).
  Should also cover an SSD on eSATA, though that's untested.
- Slows down or removes the DSM scheduled jobs that wake the disks (disk health, BTRFS maintenance,
  cert renewal, telemetry, update checks), driven by a config file you control.
- Mounts the system partition, and optionally your volumes, `noatime` so reads stop causing writes.
- Turns off DSM's hibernation debug logger, which writes to the system partition and keeps the
  disks up on its own.

The first two only ever change memory in the running processes, never the files on disk, so a
reboot clears them and the boot task puts them back.

## Which fixes run where

| Fix | x86_64 | ARM |
|---|:---:|:---:|
| Stop NVMe activity from keeping the HDDs awake | Yes | No |
| Stop an SSD in a drive bay from keeping the HDDs awake | Yes | No |
| Everything else (scheduled jobs, noatime, debug logger) | Yes | Yes |

The No's are the two in-memory patches. They are x86_64 machine code, so ARM skips them with a note
in the log and runs everything else. Most ARM models have no NVMe slot anyway, so an SSD in a SATA
bay is really the only thing an ARM user would miss.

## Install

Put the script somewhere that survives a reboot. A git clone on a data volume works well, say
`/volume1/homes/you/dev/synology_hibernation_fixer`. SSH in, go to that folder, and run it as root:

```bash
sudo python3 hiber_fixer.py --install
```

That points a boot task at the script where it sits (nothing gets copied), writes a config file
next to it, and applies everything straight away. To update later, `git pull` in that folder and run
`sudo python3 hiber_fixer.py --run`.

## Commands

```bash
sudo python3 hiber_fixer.py --run         # apply everything now (what the boot task runs)
sudo python3 hiber_fixer.py --status      # what's on, what's patched, config summary
sudo python3 hiber_fixer.py --configure   # toggle the fixes and choose what each scheduled job does
sudo python3 hiber_fixer.py --diagnose    # patch details, handy after a DSM update
sudo python3 hiber_fixer.py --uninstall   # remove the boot task and undo the config changes
```

## Settings

`hiber_fixer.config.json` sits next to the script. The `fixes` block turns each fix on or off:

```json
{
    "fixes": {
        "nvme_in_memory_patch": true,
        "ssd_slot_in_memory_patch": false,
        "remount_root_noatime": true,
        "set_volumes_noatime": true,
        "disable_hibernation_debug": true
    }
}
```

Set `ssd_slot_in_memory_patch` to `true` if you run an SSD in a SATA bay next to HDDs you want to
sleep, then run `--run`.

The `synocrond_tasks` block lists DSM's scheduled jobs with an action for each: `unchanged`,
`hourly`, `daily`, `weekly`, `monthly`, or `delete`. It ships with sensible defaults, and jobs from
future DSM or package updates get added as `unchanged` so you can review them.

Rather than editing this file by hand, `--configure` walks you through everything interactively:
first each fix (on/off), then each scheduled job. After changing anything, run `--run` to apply it.

## If it stops working after a DSM update

DSM updates sometimes disable root-level boot tasks, and now and then they rebuild the files this
patches. Start with `--status`. A disabled boot task can be re-enabled in Control Panel > Task
Scheduler, or just rerun `--install`. If `--status` shows a patch as not matching, `--diagnose`
dumps what it found so the byte patterns can be regenerated.

## How it works

DSM lets the HDDs sleep once every internal disk has been idle for a while. NVMe drives and
SSDs-in-bays land in that check even though they never sleep themselves, so their normal activity
keeps the hard drives up. The fixes leave them out of the check.

The NVMe fix is a tiny edit to the running process. The SSD case takes more work, because DSM can't
tell an SSD in a SATA bay apart from a hard drive by slot, so the patch adds its own check that
skips SSDs. Both are written so that anything unexpected falls through to DSM's normal behaviour,
which means the worst case is a fix that does nothing.

For the full write-up (the disassembly, the exact bytes, the safety argument, and how to redo it
after a DSM update) see [docs/hibernation-internals.md](docs/hibernation-internals.md). The research
scripts and Ghidra setup live in [research/](research/).

## Roadmap

- The SSD-in-a-bay fix could handle other cases DSM gets wrong (mixed pools, cache SSDs, expansion
  units) the same way, all without editing a file on disk.
- A real Synology package (`.spk`) with a small UI: see which fixes are live, change settings
  without SSH, watch hibernation stats. The CLI already exposes everything a UI would need.

## Uninstall

```bash
sudo python3 hiber_fixer.py --uninstall
sudo reboot
```

`--uninstall` removes the boot task and reverts the DSM settings it changed (volume noatime, the
scheduled-job changes, the debug logger). The reboot clears the in-memory patches, which only ever
live in RAM, and brings back the original mount settings. Delete the folder afterwards if you are
done with it.

## Credits and thanks

Huge thanks to **[AlexFromChaos](https://github.com/AlexFromChaos)**. His original
[synology_hibernation_fixer](https://github.com/AlexFromChaos/synology_hibernation_fixer) did the
hard part: figuring out that `scemd` and `synostgd-disk` are what keep the HDDs awake, that NVMe
gets pulled into the hibernation group through `SYNODiskPortEnum`, and how to patch the running
processes to fix it. None of this exists without that work. His repo has been quiet since 2023, so
this is a cleaned-up rewrite that carries it forward and adds a few things (the SSD-in-a-bay fix,
run-in-place install, a real config, and a full write-up of the internals). The two posts linked up
top are his, and both are worth reading.
