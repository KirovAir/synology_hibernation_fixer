#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Synology DSM HDD hibernation fixer (x86 NAS, DSM 7.0 - 7.3).

Makes the hard drives spin down (hibernate) by removing what keeps waking them.
`--run` applies these fixes: (1) an in-memory patch of the running scemd / synostgd-disk
processes so ongoing NVMe I/O no longer blocks HDD hibernation (reapplied each boot;
the on-disk binary is never touched), (2) synocrond task tuning driven by an external
JSON config, (3) noatime for / and data volumes, (4) turning off DSM's HDD-hibernation debug
logger (it writes to the HDD system partition and keeps the disks awake), (5) OPTIONAL (off by
default): an in-memory code-cave patch of
libsynoscemd's DiskListIdleEnough() so that an SSD living in a SATA/HDD bay no longer blocks
the HDD hibernation group. Enable it with fixes.ssd_slot_in_memory_patch=true if you run an
SSD in an internal drive bay alongside HDDs you want to sleep.

`--install` creates a boot-up Task Scheduler task that runs this script from wherever it
currently lives (no copying, no embedded blob) and writes a config file next to it. Keep the
script somewhere that survives a reboot. A git clone on a data volume is ideal. `--uninstall`
removes the boot task and undoes the config changes; reboot afterwards to drop the in-memory
patches (they only live in RAM) and apply the restored mount settings.

    sudo python3 hiber_fixer.py --install
    sudo python3 hiber_fixer.py --run | --status | --diagnose | --configure
    sudo python3 hiber_fixer.py --uninstall
"""

from __future__ import annotations

import argparse
import configparser
import ctypes
import fnmatch
import json
import logging
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #

TASK_NAME = "HDD Hibernation Fixer task"          # kept identical to the old script so --install replaces it
CONFIG_BASENAME = "hiber_fixer.config.json"
CONFIG_COMMENT = "Actions: unchanged|hourly|daily|weekly|monthly|delete. Edit, then re-run --run."
LOG_PATH = "/var/log/hibernation_fixer.log"
BACKUP_DIR = "/var/synobackup"
BACKUP_MANIFEST = "/var/synobackup/hiber_fixer_manifest.json"

PYTHON = "/usr/bin/python3"

SCEMD_PATH = "/usr/syno/bin/scemd"
SYNOSTORAGED_PATH = "/usr/syno/sbin/synostoraged"
LIBSYNOSCEMD_PATH = "/usr/lib/libsynoscemd.so.1"   # loaded inside the scemd process
SYNOCROND_CONFIG_PATH = "/usr/syno/etc/synocrond.config"
SPACE_TABLE_PATH = "/var/lib/space/space_table"
VOLUME_CONF_PATH = "/usr/syno/etc/volume.conf"
SYNOINFO_CONF_PATH = "/etc/synoinfo.conf"
VERSION_PATH = "/etc.defaults/VERSION"
SYNO_HIBERNATION_LOG_LEVEL = "/proc/sys/kernel/syno_hibernation_log_level"
HIBERNATION_DEBUG_SERVICE = "hibernationdebug.service"

SYNOGETKEYVALUE = "/usr/syno/bin/synogetkeyvalue"
SYNOSETKEYVALUE = "/usr/syno/bin/synosetkeyvalue"
ESYNOSCHEDULER_CANDIDATES = ["/usr/syno/sbin/esynoscheduler", "/usr/syno/bin/esynoscheduler"]

SYNOCROND_TASK_DIRS = [
    "/usr/syno/share/synocron.d/",
    "/usr/syno/etc/synocron.d/",
    "/usr/local/etc/synocron.d/",
]

PERIOD_ACTIONS = ("hourly", "daily", "weekly", "monthly")
BOOT_WAIT_TIMEOUT = 180           # seconds to wait for the system to finish booting

log = logging.getLogger("hiber_fixer")


# --------------------------------------------------------------------------- #
# Known synocrond tasks: name -> (recommended action, short description).
# Single source of truth; unknown tasks (future DSM/package) default to "unchanged".
# --------------------------------------------------------------------------- #

TASK_DEFAULTS = {   # task name -> (recommended action, short description)
    "builtin-synodbud-synodbud": ("delete", "updates misc DBs (abuser-blocklist, geoip, ca-certs, securityscan)"),
    "builtin-dyn-synodbud-default": ("delete", "updates misc DBs (abuser-blocklist, geoip, ca-certs, securityscan)"),
    "builtin-dyn-autopkgupgrade-default": ("delete", "update checker for installed packages"),
    "builtin-libhwcontrol-disk_daily_routine": ("weekly", "disk SMART info collector"),
    "builtin-libhwcontrol-disk_monthly_routine": ("monthly", "HDD performance-stats monitor"),
    "builtin-libhwcontrol-disk_weekly_routine": ("weekly", "checks SMART/hotspare status for disks"),
    "builtin-libhwcontrol-syno_disk_health_record": ("weekly", "parses disk_overview.xml (remaining life, errors, ...)"),
    "builtin-libsynostorage-syno_disk_health_record": ("weekly", "parses disk_overview.xml (remaining life, errors, ...)"),
    "builtin-synobtrfssnap-synobtrfssnap": ("monthly", "cleans up deleted BTRFS subvolumes"),
    "builtin-synobtrfssnap-synostgreclaim": ("monthly", "checks number of deleted BTRFS volumes to reclaim"),
    "builtin-synocrond_btrfs_free_space_analyze-default": ("monthly", "calculates BTRFS fragmentation per volume"),
    "builtin-synodatacollect-udc": ("delete", "user data collection"),
    "builtin-synodatacollect-udc-disk": ("delete", "user data collection (disk)"),
    "builtin-synorenewdefaultcert-renew_default_certificate": ("monthly", "manages cryptographic certificates"),
    "builtin-synorenewdefaultcert-default": ("monthly", "manages cryptographic certificates"),
    "builtin-synosharesnaptree_reconstruct-default": ("weekly", "reconstructs BTRFS snapshot tree"),
    "builtin-synosharing-default": ("monthly", "cleans up sharing.db SQLite tables"),
    "builtin-synolegalnotifier-synolegalnotifier": ("monthly", "downloads user agreements from Synology"),
    "builtin-synolegalnotifier-default": ("monthly", "downloads user agreements from Synology"),
    "builtin-syno_ew_weekly_check-extended_warranty_check": ("monthly", "queries Synology for extended-warranty info"),
    "builtin-syno_ew_weekly_check-default": ("monthly", "queries Synology for extended-warranty info"),
    "builtin-syno_ntp_status_check-check_ntp_status": ("monthly", "runs NTP time sync"),
    "builtin-syno_ntp_status_check-default": ("monthly", "runs NTP time sync"),
    "builtin-libsynostorage-syno_disk_db_update": ("monthly", "downloads/extracts disk compatibility DB"),
    "builtin-libsynostorage-syno_btrfs_metadata_check": ("monthly", "checks BTRFS metadata usage, emails alerts"),
    "builtin-libsynostorage-syno_disk_mail_send": ("weekly", "sends disk-related notification e-mails"),
    "pkg-ReplicationService-synobtrfsreplicacore-clean": ("monthly", "cleans up received BTRFS backup snapshots"),
    "builtin-Docker-docker_check_image_upgradable_job": ("weekly", "Docker upgradable-image checker"),
    "pkg-Docker-docker_check_image_upgradable_job": ("weekly", "Docker upgradable-image checker"),
    "pkg-Docker-default": ("weekly", ""),
    "builtin-ContainerManager-docker_check_image_upgradable_job": ("weekly", ""),
    "pkg-ContainerManager-docker_check_image_upgradable_job": ("weekly", "Container Manager upgradable-image checker"),
    "builtin-configautobackup-configautobackup": ("unchanged", ""),
    "builtin-dyn-configautobackup-default": ("unchanged", ""),
    "builtin-myds-job": ("weekly", ""),
    "builtin-dyn-myds-job": ("weekly", ""),
    "builtin-autopkgupgrade-autopkgupgrade": ("weekly", ""),
    "builtin-synoupgrade_routine-default": ("unchanged", "DSM upgrade routine"),
    "builtin-dyn-syno-letsencrypt-syno-letsencrypt - renew": ("unchanged", "renews Let's Encrypt certificates"),
    "builtin-Spreadsheet-auto_clean_weekly": ("monthly", ""),
    "builtin-Spreadsheet-auto_office_clean_temp_daily": ("weekly", ""),
    "builtin-SynologyDrive-caculate-db-usage": ("weekly", ""),
    "builtin-SynologyDrive-cleanup-db": ("weekly", ""),
    "builtin-SynologyPhotos-SynologyPhotosDatabaseToolVacuum": ("weekly", ""),
    "builtin-CodecPack-CodecPackCheckAndUpdate": ("monthly", ""),
    "builtin-SynologyApplicationService-auto_vacuum_daily": ("weekly", ""),
    "builtin-DownloadStation-DownloadStationUpdateJob": ("monthly", ""),
    "builtin-DownloadStation-DownloadStationMonitorTransmissionJob": ("weekly", ""),
    "pkg-SynologyApplicationService-auto_vacuum_daily": ("weekly", ""),
    "pkg-SMBService-smb_stats_update_job": ("weekly", "updates SMB usage statistics"),
    "pkg-SynoAnalytics-synoanalytics": ("delete", "Synology analytics / data collection"),
    "pkg-WebStation-webstaion_job": ("weekly", "Web Station cron job"),
}

DEFAULT_TASK_ACTIONS: Dict[str, str] = {name: action for name, (action, _desc) in TASK_DEFAULTS.items()}

DEFAULT_FIXES = {
    "nvme_in_memory_patch": True,
    "remount_root_noatime": True,
    "set_volumes_noatime": True,        # also set data volumes noatime (applies on next reboot)
    "disable_hibernation_debug": True,  # stop DSM's wakeup logger writing to the HDD system partition
    # Off by default: only needed if you run an SSD in an internal SATA/HDD bay next to HDDs
    # you want to hibernate. Adds an in-memory code-cave patch to libsynoscemd (see below).
    "ssd_slot_in_memory_patch": False,
}

# One-line descriptions for --configure. Order here is the order shown.
FIX_DESCRIPTIONS = {
    "nvme_in_memory_patch": "Stop NVMe activity from keeping the HDDs awake (x86 only)",
    "ssd_slot_in_memory_patch": "Stop an SSD in a SATA/eSATA bay from keeping the HDDs awake (x86 only)",
    "remount_root_noatime": "Mount / with noatime so reads don't cause writes",
    "set_volumes_noatime": "Set your data volumes to noatime too (applies on next reboot)",
    "disable_hibernation_debug": "Turn off DSM's hibernation debug logger",
}


def describe_task(name: str) -> str:
    entry = TASK_DEFAULTS.get(name)
    if entry and entry[1]:
        return entry[1]
    if name.startswith("pkg-"):
        return "package-installed synocrond task"
    return ""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def run_cmd(args: List[str]) -> subprocess.CompletedProcess:
    log.debug("exec: %s", " ".join(args))
    return subprocess.run(args, capture_output=True, universal_newlines=True)


def _load_manifest() -> dict:
    """Manifest recording what the tool changed, so --uninstall can undo it."""
    try:
        with open(BACKUP_MANIFEST) as f:
            m = json.load(f)
    except Exception:
        m = {}
    m.setdefault("files", {})            # original_path -> backup filename in BACKUP_DIR
    m.setdefault("synoinfo", {})         # synoinfo.conf key -> original value
    m.setdefault("services_masked", [])  # systemd units we masked
    return m


def _save_manifest(m: dict) -> None:
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        with open(BACKUP_MANIFEST, "w") as f:
            json.dump(m, f, indent=4)
    except Exception as e:
        log.warning("could not write backup manifest: %s", e)


def backup_file(path: str) -> None:
    """Back up a file into BACKUP_DIR before we modify it (best effort) and record it in
    the manifest for --uninstall. Namespaced by full path; the first (pristine) copy is
    kept across re-runs."""
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        if not os.path.exists(path):
            return
        dest_name = path.replace(os.sep, "/").strip("/").replace("/", "_")
        if not os.path.exists(os.path.join(BACKUP_DIR, dest_name)):
            shutil.copy2(path, os.path.join(BACKUP_DIR, dest_name))
        m = _load_manifest()
        if path not in m["files"]:
            m["files"][path] = dest_name
            _save_manifest(m)
    except Exception as e:
        log.warning("could not back up %s: %s", path, e)


def syno_get_key_value(conf_file: str, key: str) -> Optional[str]:
    try:
        out = subprocess.check_output([SYNOGETKEYVALUE, conf_file, key], universal_newlines=True)
        return out.strip()
    except Exception:
        return None


def syno_set_key_value(conf_file: str, key: str, value: str) -> bool:
    try:
        return subprocess.call([SYNOSETKEYVALUE, conf_file, key, value]) == 0
    except Exception:
        log.warning("failed to set %s=%s in %s", key, value, conf_file)
        return False


# --------------------------------------------------------------------------- #
# 1) In-memory binary patching (the NVMe hibernation fix)
# --------------------------------------------------------------------------- #
#
# Each patch target is a running process plus a small set of "variants" (one per
# DSM code layout). A variant is expressed as an ordered list of tokens so the
# regex is built safely (literals are re.escape()d, avoiding the classic footgun
# where a literal 0x24 byte becomes the regex metacharacter "$"):
#
#   search  token: bytes            -> literal bytes
#                   ("any", n)       -> n wildcard bytes, captured in order
#   replace token: bytes            -> literal bytes
#                   ("cap", k)       -> reinsert the k-th captured group (1-based)
#
# Confirmed by Ghidra decompilation of the DSM 7.3.2 binaries:
#   * scemd / polling_hibernation_timer.c: the HDD-hibernation polling timer builds
#     a disk list via SYNODiskPortEnum(portType, &list) for port types 1 and 2 (internal
#     SATA) plus 7 (NVMe), then DiskListIdleEnough(list) decides whether the HDDs may sleep.
#   * synostgd-disk / disk_monitor.c: a forked monitor loop enumerates port types
#     1, 3, 7 (NVMe) and 11 and watches each disk for activity.
# Port type 7 == NVMe, so including it lets NVMe I/O keep the HDDs awake. The fix removes
# NVMe from these lists:
#   * scemd:        SYNODiskPortEnum(7,..) -> SYNODiskPortEnum(0x0B,..)   (byte 07 -> 0B)
#   * synostoraged: insert a 2-byte jmp (EB 13) that skips the SYNODiskPortEnum(7,..) block
# The byte sequences are unchanged from DSM 7.2 and verified to still match 7.3.2 exactly.

Segment = object  # bytes | ("any", n) | ("cap", k)


@dataclass
class PatchVariant:
    name: str
    search: List[Segment]
    replace: List[Segment]


@dataclass
class PatchTarget:
    process_name: str
    binary_path: str
    variants: List[PatchVariant]


def _compile_search(segments: List[Segment]) -> "re.Pattern":
    rx = bytearray()
    for s in segments:
        if isinstance(s, (bytes, bytearray)):
            rx += re.escape(bytes(s))
        else:
            kind, n = s
            assert kind == "any"
            rx += b"(" + (b"." * n) + b")"
    return re.compile(bytes(rx), re.DOTALL)


def _build_replacement(segments: List[Segment], groups: Tuple[bytes, ...]) -> bytes:
    out = bytearray()
    for s in segments:
        if isinstance(s, (bytes, bytearray)):
            out += bytes(s)
        else:
            kind, k = s
            assert kind == "cap"
            out += groups[k - 1]
    return bytes(out)


SCEMD_TARGET = PatchTarget("scemd", SCEMD_PATH, [
    PatchVariant(
        "scemd DSM 7.2-7.3 (rbp)",
        search=[b"\x48\x89\xEE\xBF\x01\x00\x00\x00\x48\x89\x04\x24\xE8", ("any", 4),
                b"\x48\x89\xEE\xBF\x02\x00\x00\x00\x89\xC3\xE8", ("any", 4),
                b"\x48\x89\xEE\xBF\x07\x00\x00\x00\xE8", ("any", 4),
                b"\x85\xDB"],
        replace=[b"\x48\x89\xEE\xBF\x01\x00\x00\x00\x48\x89\x04\x24\xE8", ("cap", 1),
                 b"\x48\x89\xEE\xBF\x02\x00\x00\x00\x89\xC3\xE8", ("cap", 2),
                 b"\x48\x89\xEE\xBF\x0B\x00\x00\x00\xE8", ("cap", 3),
                 b"\x85\xDB"],
    ),
    PatchVariant(
        "scemd DSM 7.0-7.1 (rbx)",
        search=[b"\x48\x89\xDE\xBF\x01\x00\x00\x00\x48\x89\x04\x24\xE8", ("any", 4),
                b"\x48\x89\xDE\xBF\x02\x00\x00\x00\x89\xC5\xE8", ("any", 4),
                b"\x48\x89\xDE\xBF\x07\x00\x00\x00\xE8", ("any", 4),
                b"\x85\xED"],
        replace=[b"\x48\x89\xDE\xBF\x01\x00\x00\x00\x48\x89\x04\x24\xE8", ("cap", 1),
                 b"\x48\x89\xDE\xBF\x02\x00\x00\x00\x89\xC5\xE8", ("cap", 2),
                 b"\x48\x89\xDE\xBF\x0B\x00\x00\x00\xE8", ("cap", 3),
                 b"\x85\xED"],
    ),
])

SYNOSTORAGED_TARGET = PatchTarget("synostgd-disk", SYNOSTORAGED_PATH, [
    PatchVariant(
        "synostoraged DSM 7.2-7.3 (rbx)",
        search=[b"\x48\x89\xDE\xBF\x03\x00\x00\x00\xE8", ("any", 4),
                b"\x85\xC0\x0F\x88", ("any", 4),
                b"\x48\x89\xDE\xBF\x07\x00\x00\x00\xE8", ("any", 4),
                b"\x85\xC0\x0F\x88", ("any", 4),
                b"\x48\x89\xDE\xBF\x0B\x00\x00\x00\xE8"],
        replace=[b"\x48\x89\xDE\xBF\x03\x00\x00\x00\xE8", ("cap", 1),
                 b"\x85\xC0\x0F\x88", ("cap", 2),
                 b"\xEB\x13\xDE\xBF\x07\x00\x00\x00\xE8", ("cap", 3),   # 48 89 -> EB 13 (jmp over type-7 block)
                 b"\x85\xC0\x0F\x88", ("cap", 4),
                 b"\x48\x89\xDE\xBF\x0B\x00\x00\x00\xE8"],
    ),
    PatchVariant(
        "synostoraged DSM 7.0-7.1 (r13)",
        search=[b"\x4C\x89\xEE\xBF\x03\x00\x00\x00\xE8", ("any", 4),
                b"\x85\xC0\x0F\x88", ("any", 4),
                b"\x4C\x89\xEE\xBF\x07\x00\x00\x00\xE8", ("any", 4),
                b"\x85\xC0\x0F\x88", ("any", 4),
                b"\x4C\x89\xEE\xBF\x0B\x00\x00\x00\xE8"],
        replace=[b"\x4C\x89\xEE\xBF\x03\x00\x00\x00\xE8", ("cap", 1),
                 b"\x85\xC0\x0F\x88", ("cap", 2),
                 b"\xEB\x13\xEE\xBF\x07\x00\x00\x00\xE8", ("cap", 3),   # 4C 89 -> EB 13
                 b"\x85\xC0\x0F\x88", ("cap", 4),
                 b"\x4C\x89\xEE\xBF\x0B\x00\x00\x00\xE8"],
    ),
])

PATCH_TARGETS = [SCEMD_TARGET, SYNOSTORAGED_TARGET]


# --------------------------------------------------------------------------- #
# 1b) In-memory code-cave patch: skip an SSD-in-a-HDD-bay in the hibernation gate
# --------------------------------------------------------------------------- #
#
# THE PROBLEM
#   On an x86 DSM box the internal drive bays form ONE hibernation group: scemd only spins
#   the HDDs down once *every* internal disk has been idle past the standby timer. That
#   decision comes from a single function, DiskListIdleEnough(list, threshold), exported by
#   libsynoscemd.so.1 (scemd's private helper library). Ghidra-decompiled, it is:
#
#       for each disk in list:                       # list = all internal SATA + NVMe
#           idle = read("/sys/block/<dev>/device/syno_idle_time")
#           if (idle < threshold): return 0          # ANY disk active-recently => "not idle"
#       return 1
#
#   An SSD sitting in a SATA bay (used as a data volume) enumerates as the *same port type*
#   as the HDDs, so it ends up in `list`. syno_idle_time is per-disk (verified on hardware
#   that SSD-only I/O never resets the HDD counters), so the SSD does not physically wake the
#   HDDs. But its low idle time makes DiskListIdleEnough() return 0, so scemd never re-issues
#   standby and the HDDs keep spinning. The NVMe fix above cannot help: it drops a whole port
#   type, and the SSD shares the SATA port type with the HDDs we want to keep gating on.
#
# THE FIX
#   Make DiskListIdleEnough() skip SSDs, exactly the way the sibling IsInternalDiskSelfTesting()
#   in the same library already does:
#       if (SYNODiskIsSSD("/dev/<dev>") == 1) continue;   # treat the SSD as always-idle
#   SYNODiskIsSSD() (also already imported by libsynoscemd) simply reads
#   /sys/block/<dev>/queue/rotational and returns 1 when the first byte is '0' (SSD).
#
#   There is no room to add a call inline, so we redirect one instruction into a code cave
#   (the zero-filled tail of the library's own executable segment) and run the check there:
#
#     hook: the 5-byte `mov r13,[rsp+0x18]` right after the `access("/sys/block/<dev>")==0`
#           test is replaced by a 5-byte `jmp CAVE`.
#     CAVE (52 bytes):
#           mov  r13,[rsp+0x18]         ; re-do the displaced instruction (r13 = scratch buffer)
#           mov  rdi,r13
#           mov  esi,0x1000
#           lea  rdx,[rip+"/dev/%s"]
#           mov  rcx,r12               ; r12 = bare disk name, e.g. "sata1"
#           xor  eax,eax
#           call snprintf              ; build "/dev/sata1" into the scratch buffer
#           mov  rdi,r13
#           call SYNODiskIsSSD         ; reads /sys/block/sata1/queue/rotational
#           cmp  eax,1
#           je   <skip>                ; SSD -> jump to the "disk done, don't block" path
#           jmp  <cont>                ; else -> continue the normal syno_idle_time check
#
# WHY IT'S SAFE (never crashes, never spins down a busy HDD)
#   * Fail-safe by construction: only SYNODiskIsSSD()==1 skips a disk. A HDD returns 0 and an
#     error returns something != 1, so `je` is not taken and we fall through to the ORIGINAL
#     behavior, so the worst case is exactly "no patch".
#   * Register/ABI clean: the only registers we clobber (rax/rcx/rdx/rsi/rdi/r8-r11) are dead
#     at the hook; r12/r13/rbp/rbx/r14/r15 are callee-saved and preserved across both calls;
#     rsp is untouched, so call alignment is preserved. The scratch buffer reuses the
#     function's own 4 KiB stack buffer.
#   * Crash-safe apply order: the cave is written and verified FIRST, then the 5-byte jump is
#     written last, so a failed/partial write never leaves a live jump into empty space.
#   * Every write is read back; a no-match aborts loudly instead of patching blindly.
#
# Everything below is computed from the actual library at apply time (hook/skip located by
# byte pattern; the cave address, the snprintf / SYNODiskIsSSD PLT stubs and the "/dev/%s"
# string resolved straight from the ELF), so it adapts to a rebuilt library instead of
# hard-coding offsets, and it refuses to touch anything it cannot resolve.

# The two anchors we locate by byte pattern. Wildcards ("any", n) cover the rip-relative
# displacements of the `jne`/`lea` instructions, which differ between builds.
SSD_CAVE_HOOK_PATTERN = [                       # ...==0 test; the mov is the hook site
    b"\x85\xC0\x0F\x85", ("any", 4),            # test eax,eax ; jne <access failed>
    b"\x4C\x8B\x6C\x24\x18\xBA\x02\x00\x00\x00\x49\x89\xE9\x4C\x8D\x05", ("any", 4),
    b"\xB9\x00\x10\x00\x00\xBE\x00\x10\x00\x00\x4C\x89\xEF",
]                                               # hook = mov r13,[rsp+0x18] at match.start()+8
SSD_CAVE_SKIP_PATTERN = [                        # the "this disk is idle enough / done" path
    b"\x39\x44\x24\x14\x0F\x8F", ("any", 4),     # cmp [rsp+0x14],eax ; jg <not idle>
    b"\x45\x31\xED\x83\xC3\x01\x41\x39\x5E\x04",  # xor r13d,r13d ; add ebx,1 ; cmp [r14+4],ebx
]                                                # skip = xor r13d,r13d at match.start()+10
SSD_CAVE_HOOK_INSN = b"\x4C\x8B\x6C\x24\x18"     # mov r13,[rsp+0x18]  (the 5 bytes we replace)
SSD_CAVE_SKIP_INSN = b"\x45\x31\xED"             # xor r13d,r13d
DEV_FMT_STRING = "/dev/%s"


# ---- minimal ELF reader (program headers, dynamic table, PLT, rodata) ----- #
# Self-contained (stdlib struct only) so it works on the NAS with no extra modules.

def _elf_phdrs(data: bytes):
    """Return [(p_type, p_flags, p_offset, p_vaddr, p_filesz, p_memsz), ...]."""
    if data[:4] != b"\x7fELF" or data[4] != 2:
        raise ValueError("not an ELF64 file")
    e_phoff = struct.unpack_from("<Q", data, 32)[0]
    e_phentsize = struct.unpack_from("<H", data, 54)[0]
    e_phnum = struct.unpack_from("<H", data, 56)[0]
    phdrs = []
    for i in range(e_phnum):
        o = e_phoff + i * e_phentsize
        p_type, p_flags = struct.unpack_from("<II", data, o)
        p_offset, p_vaddr = struct.unpack_from("<QQ", data, o + 8)
        p_filesz, p_memsz = struct.unpack_from("<QQ", data, o + 32)
        phdrs.append((p_type, p_flags, p_offset, p_vaddr, p_filesz, p_memsz))
    return phdrs


def _elf_va_to_foff(phdrs, va: int) -> Optional[int]:
    for t, _fl, off, vaddr, filesz, _memsz in phdrs:
        if t == 1 and vaddr <= va < vaddr + filesz:      # PT_LOAD
            return off + (va - vaddr)
    return None


def _elf_foff_to_va(phdrs, foff: int) -> Optional[int]:
    for t, _fl, off, vaddr, filesz, _memsz in phdrs:
        if t == 1 and off <= foff < off + filesz:
            return vaddr + (foff - off)
    return None


def _elf_exec_seg(phdrs):
    """(offset, vaddr, filesz, memsz) of the executable PT_LOAD, or None."""
    for t, fl, off, vaddr, filesz, memsz in phdrs:
        if t == 1 and (fl & 1):                          # PT_LOAD + PF_X
            return off, vaddr, filesz, memsz
    return None


def _elf_dyn_tags(phdrs, data) -> Dict[int, int]:
    """First value seen for each DT_* tag in PT_DYNAMIC."""
    tags: Dict[int, int] = {}
    for t, _fl, off, _va, _fsz, _msz in phdrs:
        if t == 2:                                       # PT_DYNAMIC
            p = off
            while True:
                tag, val = struct.unpack_from("<qQ", data, p)
                p += 16
                if tag == 0:                             # DT_NULL
                    break
                tags.setdefault(tag, val)
            break
    return tags


def _elf_plt_stub(phdrs, data, symname: str) -> Optional[int]:
    """Virtual address of the .plt(.sec) stub the code calls for an imported symbol.

    Resolve the symbol's GOT slot from .rela.plt (DT_JMPREL/DT_PLTRELSZ), then find the
    `jmp [rip+disp]` in the executable segment that points at that slot; the stub start is
    that instruction aligned down to 16. For CET binaries there are two stubs per symbol
    (.plt lazy + .plt.sec); the one the compiler *calls* begins with endbr64, so prefer it.
    """
    DT_PLTRELSZ, DT_STRTAB, DT_SYMTAB, DT_SYMENT, DT_JMPREL = 2, 5, 6, 11, 23
    tg = _elf_dyn_tags(phdrs, data)
    symtab, strtab = tg.get(DT_SYMTAB), tg.get(DT_STRTAB)
    syment, jmprel, pltsz = tg.get(DT_SYMENT, 24), tg.get(DT_JMPREL), tg.get(DT_PLTRELSZ)
    if not all((symtab, strtab, jmprel, pltsz)):
        return None

    def sym_name(idx: int) -> str:
        st_name = struct.unpack_from("<I", data, _elf_va_to_foff(phdrs, symtab) + idx * syment)[0]
        base = _elf_va_to_foff(phdrs, strtab) + st_name
        return data[base:data.index(b"\x00", base)].decode("latin1")

    got_slot = None
    jf = _elf_va_to_foff(phdrs, jmprel)
    for off in range(jf, jf + pltsz, 24):                # Elf64_Rela: r_offset, r_info, r_addend
        r_off, r_info = struct.unpack_from("<QQ", data, off)
        if sym_name(r_info >> 32) == symname:
            got_slot = r_off
            break
    if got_slot is None:
        return None

    eo, eva, efsz, _emsz = _elf_exec_seg(phdrs)
    code = data[eo:eo + efsz]
    candidates = []
    i = 0
    while True:
        j = code.find(b"\xFF\x25", i)                    # jmp [rip+disp32]
        if j < 0:
            break
        disp = struct.unpack_from("<i", code, j + 2)[0]
        if eva + j + 6 + disp == got_slot:
            candidates.append((eva + j) // 16 * 16)
        if j >= 1 and code[j - 1] == 0xF2:               # bnd jmp [rip+disp32]
            if eva + (j - 1) + 7 + disp == got_slot:
                candidates.append((eva + j - 1) // 16 * 16)
        i = j + 2
    for c in candidates:                                 # prefer the endbr64 (.plt.sec) stub
        fo = _elf_va_to_foff(phdrs, c)
        if fo is not None and data[fo:fo + 4] == b"\xF3\x0F\x1E\xFA":
            return c
    return candidates[0] if candidates else None


def _elf_find_string(phdrs, data, text: str) -> Optional[int]:
    """Virtual address of a NUL-terminated string in a non-executable PT_LOAD (rodata)."""
    needle = text.encode() + b"\x00"
    for t, fl, off, vaddr, filesz, _memsz in phdrs:
        if t == 1 and not (fl & 1):
            idx = data.find(needle, off, off + filesz)
            if idx >= 0:
                return vaddr + (idx - off)
    return None


@dataclass
class SSDCavePlan:
    hook_vaddr: int          # where the 5-byte jmp goes
    hook_orig: bytes         # mov r13,[rsp+0x18]
    hook_new: bytes          # jmp CAVE
    cave_vaddr: int          # start of the cave in the exec-segment tail slack
    cave_orig: bytes         # what should be there before patching (zeros)
    cave_new: bytes          # the 52-byte cave body
    # resolved addresses, kept for --diagnose visibility
    skip_vaddr: int
    cont_vaddr: int
    devfmt_vaddr: int
    snprintf_plt: int
    isssd_plt: int


def build_ssd_cave_patch(data: bytes) -> "SSDCavePlan | str":
    """Compute the SSD-skip cave for this libsynoscemd image, or return an error string."""
    phdrs = _elf_phdrs(data)

    hooks = list(_compile_search(SSD_CAVE_HOOK_PATTERN).finditer(data))
    skips = list(_compile_search(SSD_CAVE_SKIP_PATTERN).finditer(data))
    if len(hooks) != 1:
        return "DiskListIdleEnough hook pattern matched %d times (expected 1)" % len(hooks)
    if len(skips) != 1:
        return "idle-loop skip pattern matched %d times (expected 1)" % len(skips)

    hook_foff = hooks[0].start() + 8
    skip_foff = skips[0].start() + 10
    if data[hook_foff:hook_foff + 5] != SSD_CAVE_HOOK_INSN:
        return "hook instruction mismatch (got %s)" % data[hook_foff:hook_foff + 5].hex()
    if data[skip_foff:skip_foff + 3] != SSD_CAVE_SKIP_INSN:
        return "skip instruction mismatch (got %s)" % data[skip_foff:skip_foff + 3].hex()

    hook_vaddr = _elf_foff_to_va(phdrs, hook_foff)
    skip_vaddr = _elf_foff_to_va(phdrs, skip_foff)
    if hook_vaddr is None or skip_vaddr is None:
        return "hook/skip not inside a PT_LOAD segment"
    cont_vaddr = hook_vaddr + 5                           # instruction right after the hook

    exec_seg = _elf_exec_seg(phdrs)
    if not exec_seg:
        return "no executable PT_LOAD segment"
    _eo, eva, efsz, emsz = exec_seg
    cave_vaddr = (eva + efsz + 15) & ~15                  # 16-aligned start of the tail slack
    page_end = (eva + emsz + 0xFFF) & ~0xFFF              # slack runs to the end of the last page

    devfmt = _elf_find_string(phdrs, data, DEV_FMT_STRING)
    snprintf_plt = _elf_plt_stub(phdrs, data, "snprintf")
    isssd_plt = _elf_plt_stub(phdrs, data, "SYNODiskIsSSD")
    if devfmt is None:
        return "could not find the \"/dev/%s\" format string"
    if snprintf_plt is None or isssd_plt is None:
        return "could not resolve snprintf / SYNODiskIsSSD PLT stubs"

    # Assemble the cave. op() appends the opcode bytes first, then (when a target is given)
    # a rip-relative rel32 measured from the END of the instruction, so Python evaluation
    # order can never silently shift a displacement.
    code = bytearray()

    def op(opcodes: bytes, target: Optional[int] = None) -> None:
        code.extend(opcodes)
        if target is not None:
            code.extend(struct.pack("<i", target - (cave_vaddr + len(code) + 4)))

    op(b"\x4C\x8B\x6C\x24\x18")           # mov r13,[rsp+0x18]   (displaced instruction)
    op(b"\x4C\x89\xEF")                   # mov rdi,r13
    op(b"\xBE\x00\x10\x00\x00")           # mov esi,0x1000
    op(b"\x48\x8D\x15", devfmt)           # lea rdx,[rip+"/dev/%s"]
    op(b"\x4C\x89\xE1")                   # mov rcx,r12          (bare disk name)
    op(b"\x31\xC0")                       # xor eax,eax
    op(b"\xE8", snprintf_plt)             # call snprintf
    op(b"\x4C\x89\xEF")                   # mov rdi,r13
    op(b"\xE8", isssd_plt)                # call SYNODiskIsSSD
    op(b"\x83\xF8\x01")                   # cmp eax,1
    op(b"\x0F\x84", skip_vaddr)           # je  <skip>          (SSD: don't block)
    op(b"\xE9", cont_vaddr)               # jmp <cont>          (else: normal idle check)
    cave_new = bytes(code)

    if cave_vaddr + len(cave_new) > page_end:
        return "cave (%d bytes) does not fit the executable tail slack" % len(cave_new)

    hook_new = b"\xE9" + struct.pack("<i", cave_vaddr - (hook_vaddr + 5))
    return SSDCavePlan(
        hook_vaddr=hook_vaddr, hook_orig=SSD_CAVE_HOOK_INSN, hook_new=hook_new,
        cave_vaddr=cave_vaddr, cave_orig=b"\x00" * len(cave_new), cave_new=cave_new,
        skip_vaddr=skip_vaddr, cont_vaddr=cont_vaddr, devfmt_vaddr=devfmt,
        snprintf_plt=snprintf_plt, isssd_plt=isssd_plt,
    )


@dataclass
class CaveTarget:
    """A process whose *loaded library* we patch with a computed code cave (not a pattern)."""
    process_name: str        # process the library lives in (scemd)
    binary_path: str         # the library on disk (== the mapped file)
    name: str                # human label for status/diagnose


SSD_CAVE_TARGET = CaveTarget("scemd", LIBSYNOSCEMD_PATH, "libsynoscemd SSD-slot skip")


# ---- ELF file-offset -> virtual-address mapping -------------------------- #

@dataclass
class ElfSegment:
    offset: int
    vaddr: int
    filesz: int


def parse_elf_load_segments(data: bytes) -> List[ElfSegment]:
    if data[:4] != b"\x7fELF" or data[4] != 2:
        raise ValueError("not an ELF64 file")
    e_phoff = struct.unpack_from("<Q", data, 32)[0]
    e_phentsize = struct.unpack_from("<H", data, 54)[0]
    e_phnum = struct.unpack_from("<H", data, 56)[0]
    segs: List[ElfSegment] = []
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        if struct.unpack_from("<I", data, off)[0] == 1:  # PT_LOAD
            p_offset, p_vaddr = struct.unpack_from("<QQ", data, off + 8)
            p_filesz = struct.unpack_from("<Q", data, off + 32)[0]
            segs.append(ElfSegment(p_offset, p_vaddr, p_filesz))
    return segs


def file_offset_to_vaddr(segs: List[ElfSegment], foff: int) -> Optional[int]:
    for s in segs:
        if s.offset <= foff < s.offset + s.filesz:
            return s.vaddr + (foff - s.offset)
    return None


# ---- compute the byte-level changelist for a binary ---------------------- #

@dataclass
class Change:
    file_offset: int
    orig: bytes
    new: bytes


def compute_changelist(binary_path: str, variant: PatchVariant) -> Optional[List[Change]]:
    """Return the (file_offset, orig, new) changes for one matching variant, or
    None if this variant's pattern does not occur exactly once."""
    try:
        with open(binary_path, "rb") as f:
            data = f.read()
    except OSError as e:
        log.error("cannot read %s: %s", binary_path, e)
        return None

    rx = _compile_search(variant.search)
    matches = list(rx.finditer(data))
    if len(matches) != 1:
        if len(matches) > 1:
            log.error("variant '%s' matched %d times in %s (expected 1)", variant.name, len(matches), binary_path)
        return None

    m = matches[0]
    new_block = _build_replacement(variant.replace, m.groups())
    old_block = data[m.start():m.end()]
    if len(new_block) != len(old_block):
        log.error("variant '%s': replacement changed length (%d -> %d)", variant.name, len(old_block), len(new_block))
        return None

    changes: List[Change] = []
    i = 0
    n = len(old_block)
    while i < n:
        if old_block[i] != new_block[i]:
            j = i
            while j < n and old_block[j] != new_block[j]:
                j += 1
            changes.append(Change(m.start() + i, old_block[i:j], new_block[i:j]))
            i = j
        else:
            i += 1
    return changes


# ---- process memory access via /proc/<pid>/mem --------------------------- #
#
# Reads use /proc/pid/mem directly (root can read a running process). Writes to
# read-only code pages are done through /proc/pid/mem too, which the kernel allows
# while the target is ptrace-stopped (the same FOLL_FORCE path debuggers use) --
# so we only need libc ptrace for ATTACH/DETACH, not the old PEEK/POKE word loop.

PTRACE_ATTACH, PTRACE_DETACH = 16, 17


class Ptrace:
    def __init__(self) -> None:
        self.libc = ctypes.CDLL("libc.so.6", use_errno=True)
        self.libc.ptrace.argtypes = [ctypes.c_uint64, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_void_p]
        self.libc.ptrace.restype = ctypes.c_uint64

    def attach(self, pid: int) -> bool:
        if self.libc.ptrace(PTRACE_ATTACH, pid, None, None) != 0:
            log.error("ptrace ATTACH failed for pid %d: %s", pid, os.strerror(ctypes.get_errno()))
            return False
        _, status = os.waitpid(pid, 0)
        if not os.WIFSTOPPED(status):
            log.error("pid %d did not stop after ATTACH (status %#x)", pid, status)
            self.detach(pid)
            return False
        return True

    def detach(self, pid: int) -> None:
        self.libc.ptrace(PTRACE_DETACH, pid, None, None)


def read_mem(pid: int, addr: int, length: int) -> Optional[bytes]:
    try:
        fd = os.open("/proc/%d/mem" % pid, os.O_RDONLY)
        try:
            return os.pread(fd, length, addr)
        finally:
            os.close(fd)
    except OSError as e:
        log.error("read /proc/%d/mem @ %#x failed: %s", pid, addr, e)
        return None


def write_mem(pid: int, writes: List[Tuple[int, bytes]]) -> bool:
    """Write (addr, bytes) pairs and read each back to verify. Caller must have the
    target ptrace-stopped so writes to read-only code pages are permitted."""
    try:
        fd = os.open("/proc/%d/mem" % pid, os.O_RDWR)
    except OSError as e:
        log.error("open /proc/%d/mem (rw) failed: %s", pid, e)
        return False
    try:
        for addr, data in writes:
            if os.pwrite(fd, data, addr) != len(data):
                log.error("pwrite to %#x (pid %d) was short", addr, pid)
                return False
            if os.pread(fd, len(data), addr) != data:
                log.error("read-back mismatch at %#x (pid %d)", addr, pid)
                return False
        return True
    except OSError as e:
        log.error("write /proc/%d/mem failed: %s", pid, e)
        return False
    finally:
        os.close(fd)


def get_pid_by_name(name: str) -> Optional[int]:
    try:
        return int(subprocess.check_output(["pidof", name]).split()[0])
    except Exception:
        return None


def get_module_base(pid: int, module_name: str) -> Optional[int]:
    """Load bias of the module: the vaddr at which file offset 0 is mapped."""
    line_re = re.compile(r"^([\da-f]+)-([\da-f]+)\s+\S+\s+([\da-f]+)\s+\S+\s+\d+\s+(.*)$")
    try:
        with open("/proc/%d/maps" % pid) as f:
            for line in f:
                m = line_re.match(line.rstrip("\n"))
                if not m:
                    continue
                start, _end, offset, path = m.groups()
                if os.path.basename(path.strip()) == module_name and int(offset, 16) == 0:
                    return int(start, 16)
    except OSError as e:
        log.error("cannot read /proc/%d/maps: %s", pid, e)
    return None


@dataclass
class PatchOutcome:
    target: str
    matched_variant: Optional[str] = None
    applied: bool = False
    already_patched: bool = False
    error: Optional[str] = None


def resolve_cave_sites(target: CaveTarget):
    """Resolve the SSD-skip code cave for a CaveTarget, in the same (pid, name, sites, error)
    shape as resolve_sites. sites are ordered cave-first so the crash-safe write order (cave
    body before the 5-byte jump) falls out of the sequential write in write_mem()."""
    pid = get_pid_by_name(target.process_name)
    if not pid:
        return None, None, None, "process '%s' not running" % target.process_name
    module = os.path.basename(target.binary_path)
    base = get_module_base(pid, module)
    if base is None:
        return pid, None, None, "could not find %s mapped in pid %d" % (module, pid)
    try:
        data = open(target.binary_path, "rb").read()
    except OSError as e:
        return pid, None, None, "cannot read %s: %s" % (target.binary_path, e)
    plan = build_ssd_cave_patch(data)
    if isinstance(plan, str):
        return pid, None, None, plan
    sites = [
        (base + plan.cave_vaddr, plan.cave_orig, plan.cave_new),   # write + verify first
        (base + plan.hook_vaddr, plan.hook_orig, plan.hook_new),   # flip the hop last
    ]
    return pid, target.name, sites, None


def resolve_sites(target):
    """Return (pid, variant_name, sites, error) where sites = [(runtime_addr, orig, new)].
    Shared by apply_target (writes) and cmd_status (read-only report)."""
    if isinstance(target, CaveTarget):
        return resolve_cave_sites(target)
    pid = get_pid_by_name(target.process_name)
    if not pid:
        return None, None, None, "process '%s' not running" % target.process_name
    module = os.path.basename(target.binary_path)
    base = get_module_base(pid, module)
    if base is None:
        return pid, None, None, "could not find module base of %s in pid %d" % (module, pid)
    try:
        segs = parse_elf_load_segments(open(target.binary_path, "rb").read())
    except Exception as e:
        return pid, None, None, "cannot parse ELF %s: %s" % (target.binary_path, e)
    for variant in target.variants:
        changes = compute_changelist(target.binary_path, variant)
        if not changes:
            continue
        sites = []
        for ch in changes:
            vaddr = file_offset_to_vaddr(segs, ch.file_offset)
            if vaddr is None:
                return pid, variant.name, None, "file offset %#x not in any PT_LOAD segment" % ch.file_offset
            sites.append((base + vaddr, ch.orig, ch.new))
        return pid, variant.name, sites, None
    return pid, None, None, "no known patch pattern matched the current binary"


def apply_target(ptrace: Ptrace, target: PatchTarget) -> PatchOutcome:
    out = PatchOutcome(target=target.process_name)
    pid, variant, sites, error = resolve_sites(target)
    out.matched_variant = variant
    if error:
        out.error = error
        log.error("%s: %s", target.process_name, error)
        return out

    current = [read_mem(pid, addr, len(orig)) for addr, orig, _new in sites]
    if any(c is None for c in current):
        out.error = "failed reading target process memory"
        return out
    if all(current[i] == sites[i][2] for i in range(len(sites))):
        out.already_patched = True
        log.info("%s: already patched in memory (%s)", target.process_name, variant)
        return out
    if not all(current[i] == sites[i][1] for i in range(len(sites))):
        out.error = "memory content does not match expected original bytes"
        log.error("%s: %s", target.process_name, out.error)
        return out

    if not ptrace.attach(pid):
        out.error = "ptrace attach failed"
        return out
    try:
        if write_mem(pid, [(addr, new) for addr, _orig, new in sites]):
            out.applied = True
            log.info("%s: applied in-memory patch (%s)", target.process_name, variant)
        else:
            out.error = "memory write failed"
    finally:
        ptrace.detach(pid)
    return out


def do_in_memory_fixes(targets) -> bool:
    try:
        ptrace = Ptrace()
    except Exception as e:
        log.error("failed to initialise ptrace/libc bindings: %s", e)
        return False

    all_ok = True
    unmatched = []
    for target in targets:
        outcome = apply_target(ptrace, target)
        if outcome.error and not outcome.already_patched:
            all_ok = False
            if outcome.matched_variant is None:
                unmatched.append(target.binary_path)

    if unmatched:
        # Loud so a future DSM that changes these binaries is visible, not a silent no-op.
        log.error("!!! hibernation patch no longer matches %s -- run 'hiber_fixer.py --diagnose'",
                  ", ".join(unmatched))
    return all_ok


# --------------------------------------------------------------------------- #
# 2) synocrond task tuning
# --------------------------------------------------------------------------- #

@dataclass
class SynocrondTask:
    name: str
    body: dict


def _find_conf_files(directory: str) -> List[str]:
    result = []
    if not os.path.isdir(directory):
        return result
    for root, _dirs, files in os.walk(directory):
        for name in files:
            if fnmatch.fnmatch(name, "*.conf"):
                result.append(os.path.join(root, name))
    return result


def enumerate_task_files() -> List[str]:
    paths: List[str] = []
    for d in SYNOCROND_TASK_DIRS:
        paths += _find_conf_files(d)
    return paths


def load_task_file(path: str) -> List[SynocrondTask]:
    """Parse one synocron.d .conf file into a list of tasks (files hold a dict or a list)."""
    with open(path) as f:
        obj = json.load(f)
    entries = obj if isinstance(obj, list) else [obj]

    fname = os.path.basename(path).split(".")[0]
    # Package tasks (under /usr/local/etc/synocron.d) are named pkg-<file>-<name> at
    # runtime; built-in ones (share/ and etc/) use the builtin- prefix.
    prefix = "pkg-" if "/usr/local/etc/synocron.d/" in path.replace(os.sep, "/") else "builtin-"

    tasks = []
    for entry in entries:
        name = prefix + fname + "-" + (entry["name"] if "name" in entry else "default")
        tasks.append(SynocrondTask(name, entry))
    return tasks


def task_period(body: dict) -> str:
    period = body.get("period", "?")
    if period == "crontab" and "crontab" in body:
        period += " (%s)" % body["crontab"]
    return period


def clean_job_name(job_name: str) -> str:
    # DSM 7.2+ prefixes runtime job keys with "synocrond-job-".
    prefix = "synocrond-job-"
    return job_name[len(prefix):] if job_name.startswith(prefix) else job_name


def load_synocrond_config() -> Optional[dict]:
    try:
        with open(SYNOCROND_CONFIG_PATH) as f:
            return json.load(f)
    except Exception as e:
        log.error("cannot load %s: %s", SYNOCROND_CONFIG_PATH, e)
        return None


def discover_tasks() -> Dict[str, str]:
    """Return {task_name: current_period} across task files and the live synocrond.config."""
    result: Dict[str, str] = {}
    for path in enumerate_task_files():
        try:
            for t in load_task_file(path):
                result[t.name] = task_period(t.body)
        except Exception as e:
            log.warning("skipping task file %s: %s", path, e)

    cfg = load_synocrond_config()
    if cfg:
        for job_name, job in cfg.get("jobs", {}).items():
            result[clean_job_name(job_name)] = task_period(job.get("config", {}))
    return result


def _handle_dyn_task_deletion(name: str) -> None:
    """Extra work required to keep certain 'dynamic' tasks from coming back.
    Original state is recorded in the manifest so --uninstall can undo it."""
    if name == "builtin-dyn-autopkgupgrade-default":
        m = _load_manifest()
        dirty = False
        for key in ("pkg_autoupdate_important", "enable_pkg_autoupdate_all", "upgrade_pkg_dsm_notification"):
            cur = syno_get_key_value(SYNOINFO_CONF_PATH, key)
            if cur != "no":
                m["synoinfo"].setdefault(key, cur if cur is not None else "")
                syno_set_key_value(SYNOINFO_CONF_PATH, key, "no")
                dirty = True
        if dirty:
            _save_manifest(m)
    elif name in ("builtin-synodbud-synodbud", "builtin-dyn-synodbud-default"):
        m = _load_manifest()
        if "synodbud_autoupdate.service" not in m["services_masked"]:
            m["services_masked"].append("synodbud_autoupdate.service")
            _save_manifest(m)
        run_cmd(["systemctl", "mask", "synodbud_autoupdate.service"])
        run_cmd(["systemctl", "stop", "synodbud_autoupdate.service"])
        run_cmd(["synodbud", "-p"])


def apply_config_to_task_files(actions: Dict[str, str]) -> None:
    for path in enumerate_task_files():
        try:
            tasks = load_task_file(path)
        except Exception as e:
            log.warning("skipping task file %s: %s", path, e)
            continue

        changed = False
        remaining: List[SynocrondTask] = []
        for t in tasks:
            action = actions.get(t.name, "unchanged")
            if action == "delete":
                changed = True
                continue  # drop this task; everything else is preserved as-is
            if action in PERIOD_ACTIONS and t.body.get("period") != action:
                t.body["period"] = action
                changed = True
            remaining.append(t)

        if not changed:
            continue

        backup_file(path)
        try:
            if not remaining:
                log.info("removing task file %s (all its tasks deleted)", path)
                os.unlink(path)
                continue
            payload = remaining[0].body if len(remaining) == 1 else [t.body for t in remaining]
            with open(path, "w") as f:
                json.dump(payload, f, indent=4)
            log.info("updated task file %s", path)
        except OSError as e:
            log.error("cannot write %s: %s", path, e)


def apply_config_to_synocrond_config(actions: Dict[str, str]) -> bool:
    cfg = load_synocrond_config()
    if not cfg:
        return False
    jobs = cfg.get("jobs", {})

    changed = False
    for job_name in list(jobs.keys()):
        name = clean_job_name(job_name)
        action = actions.get(name, "unchanged")
        cur_period = jobs[job_name].get("config", {}).get("period")
        if action == "unchanged":
            continue
        if action == "delete":
            _handle_dyn_task_deletion(name)
            del jobs[job_name]
            changed = True
        elif action in PERIOD_ACTIONS and cur_period != action:
            jobs[job_name]["config"]["period"] = action
            changed = True

    if not changed:
        return True

    log.info("updating %s", SYNOCROND_CONFIG_PATH)
    if subprocess.call(["systemctl", "stop", "synocrond"]) != 0:
        log.error("failed to stop synocrond")
        return False

    ok = True
    try:
        backup_file(SYNOCROND_CONFIG_PATH)
        with open(SYNOCROND_CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=4)
        # Drop the runtime cache so synocrond regenerates it from the new config.
        for p in ("/run/synocrond", "/run/synocrond.st.config", "/run/synocrond.config"):
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p)
                elif os.path.exists(p):
                    os.unlink(p)
            except OSError:
                pass
    except Exception as e:
        log.error("failed to update synocrond.config: %s", e)
        ok = False
    finally:
        # Always bring synocrond back up, even if the write failed.
        if subprocess.call(["systemctl", "start", "synocrond"]) != 0:
            log.error("failed to restart synocrond")
            ok = False
    return ok


# --------------------------------------------------------------------------- #
# 3) noatime
# --------------------------------------------------------------------------- #

def root_is_noatime() -> bool:
    try:
        out = subprocess.check_output(["mount"], universal_newlines=True)
        return any(" / " in l and "md0" in l and "noatime" in l for l in out.splitlines())
    except Exception:
        return False


def remount_root_noatime() -> None:
    if root_is_noatime():
        return
    rc = subprocess.call(["mount", "-o", "noatime,remount", "/"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if rc:
        log.error("remounting / noatime failed; expect HDD wakeups from atime updates")
    else:
        log.info("remounted / as noatime")


def apply_hibernation_debug_off() -> None:
    """Turn off DSM's HDD-hibernation debug logger. It writes /var/log/hibernationFull.log to
    md0 (the system partition, which is RAID1-mirrored onto the HDDs), so it keeps the disks
    awake on its own. The synoinfo flag is persistent (recorded in the manifest for --uninstall);
    the kernel log level and the service reset on reboot, so we re-apply those on every run."""
    cur = syno_get_key_value(SYNOINFO_CONF_PATH, "enable_hibernation_debug")
    if cur == "yes":
        m = _load_manifest()
        m["synoinfo"].setdefault("enable_hibernation_debug", cur)
        _save_manifest(m)
        syno_set_key_value(SYNOINFO_CONF_PATH, "enable_hibernation_debug", "no")
        log.info("disabled enable_hibernation_debug (its wakeup log to md0 keeps the HDDs awake)")

    try:
        if os.path.exists(SYNO_HIBERNATION_LOG_LEVEL):
            with open(SYNO_HIBERNATION_LOG_LEVEL, "w") as f:
                f.write("0")
    except OSError as e:
        log.warning("could not set %s: %s", SYNO_HIBERNATION_LOG_LEVEL, e)

    subprocess.call(["systemctl", "stop", HIBERNATION_DEBUG_SERVICE],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _load_volume_names() -> Dict[str, str]:
    names: Dict[str, str] = {}
    try:
        with open(SPACE_TABLE_PATH) as f:
            spaces = json.load(f)
        for space in spaces:
            for vol in space.get("volumes", []):
                names[vol["fs_uuid"]] = vol["id"]
    except Exception:
        pass
    return names


def find_relatime_volumes() -> List[Tuple[str, str]]:
    """Return [(uuid, display_name)] for volumes whose atime_opt != noatime."""
    names = _load_volume_names()
    result: List[Tuple[str, str]] = []
    try:
        conf = configparser.ConfigParser(interpolation=None)
        conf.read(VOLUME_CONF_PATH)
        for uuid in conf.sections():
            if conf[uuid].get("atime_opt", "") != "noatime":
                result.append((uuid, names.get(uuid, uuid)))
    except Exception as e:
        log.error("failed to parse %s: %s", VOLUME_CONF_PATH, e)
    return result


def set_volumes_noatime() -> bool:
    bad = find_relatime_volumes()
    if not bad:
        return True
    try:
        conf = configparser.ConfigParser(interpolation=None)
        conf.read(VOLUME_CONF_PATH)
        for uuid, _name in bad:
            conf[uuid]["atime_opt"] = "noatime"
        backup_file(VOLUME_CONF_PATH)
        with open(VOLUME_CONF_PATH, "w") as f:
            conf.write(f, space_around_delimiters=False)
        log.info("set noatime for volumes: %s", ", ".join(n for _u, n in bad))
        log.warning("reboot required to apply the new volume atime settings")
        return True
    except Exception as e:
        log.error("failed to update %s: %s", VOLUME_CONF_PATH, e)
        return False


# --------------------------------------------------------------------------- #
# Config file
# --------------------------------------------------------------------------- #

def config_path_for(script_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(script_path)), CONFIG_BASENAME)


def default_config() -> dict:
    tasks = {}
    discovered = set(discover_tasks())
    for name in sorted(discovered | set(DEFAULT_TASK_ACTIONS)):
        tasks[name] = DEFAULT_TASK_ACTIONS.get(name, "unchanged")
    return {"_comment": CONFIG_COMMENT, "fixes": dict(DEFAULT_FIXES), "synocrond_tasks": tasks}


def load_config(path: str) -> dict:
    """Load config, filling in defaults and merging in any newly-discovered tasks."""
    cfg = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                cfg = json.load(f)
        except Exception as e:
            log.error("cannot parse config %s: %s -- using defaults", path, e)
    fixes = dict(DEFAULT_FIXES)
    fixes.update(cfg.get("fixes", {}))
    tasks = dict(cfg.get("synocrond_tasks", {}))

    added = 0
    for name in discover_tasks():
        if name not in tasks:
            tasks[name] = DEFAULT_TASK_ACTIONS.get(name, "unchanged")
            added += 1
    if added and os.path.exists(path):
        log.info("config: %d newly-discovered task(s) added with default actions", added)

    return {"_comment": cfg.get("_comment", CONFIG_COMMENT), "fixes": fixes, "synocrond_tasks": tasks}


def save_config(path: str, cfg: dict) -> None:
    with open(path, "w") as f:
        json.dump(cfg, f, indent=4, sort_keys=False)


# --------------------------------------------------------------------------- #
# Task Scheduler integration
# --------------------------------------------------------------------------- #

def esynoscheduler() -> Optional[str]:
    for c in ESYNOSCHEDULER_CANDIDATES:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def scheduler_list_raw() -> str:
    tool = esynoscheduler()
    if not tool:
        return ""
    try:
        return subprocess.check_output([tool, "--list"], stderr=subprocess.DEVNULL).decode("utf-8", "replace")
    except Exception:
        return ""


def scheduler_tasks() -> List[dict]:
    """Parse `esynoscheduler --list` output (log noise + a `jOut=[...]` JSON array)."""
    raw = scheduler_list_raw()
    j = raw.find("jOut=")
    start = raw.find("[", j) if j != -1 else raw.find("[")
    if start == -1:
        return []
    try:
        val, _ = json.JSONDecoder().raw_decode(raw[start:])
        return val if isinstance(val, list) else []
    except Exception:
        return []


def scheduler_task_state(task_name: str) -> Optional[bool]:
    """Return the enabled state of the boot task with this name, or None if absent."""
    for t in scheduler_tasks():
        if t.get("task_name") == task_name:
            return bool(t.get("enable", False))
    return None


def create_boot_task(operation: str) -> bool:
    tool = esynoscheduler()
    if not tool:
        log.error("esynoscheduler not found")
        return False
    args = [tool, "--create", "task_name=%s" % TASK_NAME, "event=bootup", "enable=true",
            "operation_type=script", "operation=%s" % operation,
            "description=HDD hibernation fixer (runs hiber_fixer.py at boot)",
            r'owner={"0":"root"}']
    try:
        out = subprocess.check_output(args, stderr=subprocess.STDOUT).decode("utf-8", "replace")
        return "save ok" in out
    except Exception as e:
        log.error("failed to create Task Scheduler task: %s", e)
        return False


def delete_boot_task() -> bool:
    tool = esynoscheduler()
    if not tool:
        return False
    try:
        out = subprocess.check_output([tool, "--delete", "task_name=%s" % TASK_NAME],
                                      stderr=subprocess.STDOUT).decode("utf-8", "replace")
        return "delete task ok" in out
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Boot readiness
# --------------------------------------------------------------------------- #

def system_running() -> bool:
    try:
        out = subprocess.run(["systemctl", "is-system-running"], capture_output=True,
                             universal_newlines=True).stdout.strip()
        return out in ("running", "degraded")
    except Exception:
        return True  # don't block if systemd query fails


def wait_until(predicate, timeout: int, on_timeout=None) -> bool:
    deadline = time.time() + timeout
    while not predicate():
        if time.time() >= deadline:
            if on_timeout:
                on_timeout()
            return False
        time.sleep(2)
    return True


def wait_for_system(timeout: int = BOOT_WAIT_TIMEOUT) -> bool:
    """Wait until systemd reports the boot has finished (running/degraded)."""
    return wait_until(system_running, timeout,
                      lambda: log.warning("system did not finish booting within %ds; continuing anyway", timeout))


def wait_for_daemons(targets, timeout: int = BOOT_WAIT_TIMEOUT) -> bool:
    """Wait until the in-memory patch targets' processes are running."""
    procs = sorted({t.process_name for t in targets})
    def missing():
        return [p for p in procs if not get_pid_by_name(p)]
    return wait_until(lambda: not missing(), timeout,
                      lambda: log.error("patch target daemon(s) not running within %ds: %s", timeout, ", ".join(missing())))


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_run(config_path: str, wait: bool = True) -> int:
    log.info("run: applying hibernation fixes")
    cfg = load_config(config_path)
    fixes = cfg["fixes"]
    actions = cfg["synocrond_tasks"]

    if wait:
        wait_for_system()

    ok = True
    if fixes.get("remount_root_noatime", True):
        remount_root_noatime()
    mem_targets = list(PATCH_TARGETS) if fixes.get("nvme_in_memory_patch", True) else []
    if fixes.get("ssd_slot_in_memory_patch", False):
        mem_targets.append(SSD_CAVE_TARGET)
    if mem_targets and not is_x86():
        log.warning("in-memory patches skipped: unsupported architecture %s (they are x86-64 only). "
                    "The other fixes still apply.", platform.machine())
    elif mem_targets:
        if wait:
            wait_for_daemons(mem_targets)   # gate only the in-memory patch on the daemons being up
        ok = do_in_memory_fixes(mem_targets) and ok
    apply_config_to_task_files(actions)
    ok = apply_config_to_synocrond_config(actions) and ok
    if fixes.get("disable_hibernation_debug", True):
        apply_hibernation_debug_off()

    relatime = find_relatime_volumes()
    if relatime:
        if fixes.get("set_volumes_noatime", False):
            set_volumes_noatime()
        else:
            log.warning("volumes still using relatime (set fixes.set_volumes_noatime=true to fix): %s",
                        ", ".join(n for _u, n in relatime))

    log.info("run: done (%s)", "ok" if ok else "with errors")
    return 0 if ok else 1


def cmd_install(script_path: str) -> int:
    """Register the boot task to run this script from wherever it currently lives.
    Nothing is copied: keep the script somewhere that survives a reboot (a git clone on a
    data volume is ideal). The config file is written next to the script."""
    script_path = os.path.abspath(script_path)
    script_dir = os.path.dirname(script_path)
    config_path = config_path_for(script_path)

    if not os.path.exists(config_path):
        save_config(config_path, default_config())
        print("Wrote default config to %s" % config_path)
        print("  Review/edit it to choose what to do with each synocrond task, then re-run --run if needed.")
    else:
        save_config(config_path, load_config(config_path))   # merge in newly discovered tasks
        print("Kept existing config %s" % config_path)

    if not script_path.startswith("/volume"):
        print("WARNING: %s is not on a data volume (/volumeN)." % script_path)
        print("         The boot task runs the script by path, so keep it where it survives a reboot.")

    operation = "%s %s --run" % (PYTHON, script_path)
    delete_boot_task()
    if not create_boot_task(operation):
        print("ERROR: failed to create the boot-up Task Scheduler task")
        return 1
    print('Created boot-up task "%s" -> %s' % (TASK_NAME, operation))

    print("\nApplying fixes now...")
    rc = cmd_run(config_path, wait=False)
    print("\nInstallation complete. The fixes will re-apply on every boot, running this script")
    print("in place from %s." % script_dir)
    return rc


def cmd_uninstall() -> int:
    """Full teardown: remove the boot task and undo the config changes. The in-memory patches
    live only in RAM, so a reboot clears them (and applies the restored mount settings)."""
    if delete_boot_task():
        print('Removed the "%s" boot task.' % TASK_NAME)
    else:
        print('Boot task "%s" not found (already removed?).' % TASK_NAME)

    print("Undoing config changes...")
    restore_config_changes()

    print("\nUninstalled. Reboot to finish: that drops the in-memory patches (they only live in")
    print("RAM) and applies the restored noatime/mount settings. You can delete this folder too.")
    return 0


def restore_config_changes() -> None:
    """Restore every file the tool backed up (synocrond config/tasks, volume.conf) to its
    pristine pre-change content, and undo the recorded synoinfo/service side effects."""
    m = _load_manifest()
    files = m.get("files", {})
    if not files and not m.get("synoinfo") and not m.get("services_masked"):
        print("No config changes to undo (nothing recorded in %s)." % BACKUP_MANIFEST)
        return

    restored, failed = [], []
    for path, dest_name in files.items():
        src = os.path.join(BACKUP_DIR, dest_name)
        if not os.path.exists(src):
            failed.append("%s (backup %s missing)" % (path, dest_name))
            continue
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            shutil.copy2(src, path)
            restored.append(path)
        except OSError as e:
            failed.append("%s (%s)" % (path, e))

    for key, val in m.get("synoinfo", {}).items():
        syno_set_key_value(SYNOINFO_CONF_PATH, key, val)
    for svc in m.get("services_masked", []):
        run_cmd(["systemctl", "unmask", svc])

    # Refresh synocrond from the restored config.
    for p in ("/run/synocrond", "/run/synocrond.st.config", "/run/synocrond.config"):
        try:
            if os.path.isdir(p):
                shutil.rmtree(p)
            elif os.path.exists(p):
                os.unlink(p)
        except OSError:
            pass
    subprocess.call(["systemctl", "restart", "synocrond"])

    print("Restored %d config file(s) from %s." % (len(restored), BACKUP_DIR))
    if m.get("synoinfo"):
        print("Reverted synoinfo keys: %s" % ", ".join(sorted(m["synoinfo"])))
    if m.get("services_masked"):
        print("Unmasked services: %s" % ", ".join(m["services_masked"]))
    if failed:
        print("Could NOT restore:")
        for x in failed:
            print("  %s" % x)


def _status_report_target(target) -> None:
    label = target.name if isinstance(target, CaveTarget) else target.process_name
    pid, variant, sites, error = resolve_sites(target)
    if sites is None:
        print("  %s: %s" % (label, error))
        return
    states = []
    for addr, orig, new in sites:
        cur = read_mem(pid, addr, len(orig))
        states.append("patched" if cur == new else "original" if cur == orig else "unknown")
    verdict = "PATCHED" if all(s == "patched" for s in states) else \
              "not patched" if all(s == "original" for s in states) else "partial/unknown"
    print("  %s: %s (matched %s)" % (label, verdict, variant))


def cmd_status(config_path: str) -> int:
    print("== HDD Hibernation Fixer status ==\n")

    state = scheduler_task_state(TASK_NAME)
    if state is None:
        print('Boot task "%s": NOT INSTALLED' % TASK_NAME)
    else:
        print('Boot task "%s": %s' % (TASK_NAME, "ENABLED" if state else "DISABLED (re-enable it or re-run --install)"))

    cfg_fixes = load_config(config_path)["fixes"] if os.path.exists(config_path) else dict(DEFAULT_FIXES)

    if not is_x86():
        print("\nIn-memory patches: skipped -- unsupported architecture %s (x86-64 only)." % platform.machine())
    else:
        print("\nIn-memory NVMe patch (current process state):")
        for target in PATCH_TARGETS:
            _status_report_target(target)

        ssd_on = cfg_fixes.get("ssd_slot_in_memory_patch", False)
        print("\nIn-memory SSD-slot patch (%s):" % ("enabled" if ssd_on else "disabled -- set fixes.ssd_slot_in_memory_patch=true"))
        _status_report_target(SSD_CAVE_TARGET)

    print("\nnoatime:")
    print("  root (/) noatime: %s" % ("yes" if root_is_noatime() else "NO"))
    relatime = find_relatime_volumes()
    print("  volumes on relatime: %s" % (", ".join(n for _u, n in relatime) if relatime else "none"))

    if os.path.exists(config_path):
        cfg = load_config(config_path)
        acts = cfg["synocrond_tasks"]
        changed = {k: v for k, v in acts.items() if v != "unchanged"}
        print("\nConfig: %s" % config_path)
        print("  fixes: %s" % cfg["fixes"])
        print("  synocrond tasks: %d known, %d set to change" % (len(acts), len(changed)))
    else:
        print("\nConfig: not found at %s" % config_path)
    return 0


def cmd_diagnose() -> int:
    """Report pattern matches against the on-disk binaries and dump candidate sites
    if nothing matches (useful to regenerate patterns after a DSM update)."""
    print("== diagnose ==")
    if not is_x86():
        print("architecture %s is not x86-64; the byte patterns below are x86-64 only "
              "and will not match." % platform.machine())
    for target in PATCH_TARGETS:
        print("\n%s (process %s):" % (target.binary_path, target.process_name))
        if not os.path.exists(target.binary_path):
            print("  binary not found")
            continue
        data = open(target.binary_path, "rb").read()
        matched = False
        for variant in target.variants:
            hits = list(_compile_search(variant.search).finditer(data))
            print("  variant '%s': %d match(es)%s"
                  % (variant.name, len(hits), (" at %#x" % hits[0].start()) if hits else ""))
            matched = matched or bool(hits)
        if not matched:
            print("  no variant matched -- scanning for candidate 'mov edi,7; call' sites:")
            for m in re.finditer(re.escape(b"\xBF\x07\x00\x00\x00\xE8"), data):
                off = m.start()
                ctx = data[max(0, off - 24):off + 16]
                if b"\xBF\x01" in ctx or b"\xBF\x03" in ctx:
                    print("    off %#x: ...%s..." % (off, data[max(0, off - 24):off + 32].hex()))
            print("  Send this output to regenerate the patterns for your DSM build.")

    # SSD-slot code cave (computed, not a pattern replacement)
    print("\n%s (%s):" % (SSD_CAVE_TARGET.binary_path, SSD_CAVE_TARGET.name))
    if not os.path.exists(SSD_CAVE_TARGET.binary_path):
        print("  library not found")
    else:
        plan = build_ssd_cave_patch(open(SSD_CAVE_TARGET.binary_path, "rb").read())
        if isinstance(plan, str):
            print("  cannot build cave: %s" % plan)
        else:
            print("  hook @ %#x  %s -> %s" % (plan.hook_vaddr, plan.hook_orig.hex(), plan.hook_new.hex()))
            print("  cave @ %#x  (%d bytes)  %s" % (plan.cave_vaddr, len(plan.cave_new), plan.cave_new.hex()))
            print("  resolved: skip=%#x cont=%#x \"/dev/%%s\"=%#x snprintf@plt=%#x SYNODiskIsSSD@plt=%#x"
                  % (plan.skip_vaddr, plan.cont_vaddr, plan.devfmt_vaddr, plan.snprintf_plt, plan.isssd_plt))
    return 0


def cmd_configure(config_path: str) -> int:
    """Interactive editor for the fixes (on/off) and the synocrond task actions."""
    cfg = load_config(config_path) if os.path.exists(config_path) else default_config()
    fixes = cfg["fixes"]
    discovered = discover_tasks()
    actions = cfg["synocrond_tasks"]
    letter = {"u": "unchanged", "h": "hourly", "d": "daily", "w": "weekly", "m": "monthly", "x": "delete"}
    try:
        print("== Fixes ==   (y = on, n = off, Enter = keep current)\n")
        for key in FIX_DESCRIPTIONS:
            cur = bool(fixes.get(key, DEFAULT_FIXES.get(key, False)))
            print("%s   [currently %s]" % (key, "on" if cur else "off"))
            print("     %s" % FIX_DESCRIPTIONS[key])
            while True:
                ch = input("     enable? [y/n, Enter=keep]: ").strip().lower()
                if not ch:
                    break
                if ch[0] == "y":
                    fixes[key] = True
                    break
                if ch[0] == "n":
                    fixes[key] = False
                    break
                print("     please answer y or n")
            print()

        names = sorted(set(discovered) | set(actions))
        unknown = [n for n in names if n not in DEFAULT_TASK_ACTIONS]
        print("== Scheduled jobs ==   %d found, %d with no built-in recommendation" % (len(names), len(unknown)))
        print("  1) use the recommended defaults for all (no prompts)")
        print("  2) review only the %d with no recommendation" % len(unknown))
        print("  3) review every job one by one")
        mode = ""
        while mode not in ("1", "2", "3"):
            mode = input("choice [1/2/3, Enter=1]: ").strip() or "1"
            if mode not in ("1", "2", "3"):
                print("  please enter 1, 2 or 3")
        print()

        if mode == "1":
            for name in names:
                actions[name] = DEFAULT_TASK_ACTIONS.get(name, "unchanged")
        else:
            review = unknown if mode == "2" else names
            print("Enter = keep current. Options: (u)nchanged (h)ourly (d)aily (w)eekly (m)onthly (x)delete\n")
            for i, name in enumerate(review, 1):
                cur_period = discovered.get(name, "(not present)")
                descr = describe_task(name)
                action = actions.get(name, DEFAULT_TASK_ACTIONS.get(name, "unchanged"))
                print("[%d/%d] %s" % (i, len(review), name))
                if descr:
                    print("     %s" % descr)
                print("     current interval: %s   action: %s" % (cur_period, action))
                while True:
                    ch = input("     action [u/h/d/w/m/x, Enter=keep]: ").strip().lower()
                    if not ch:
                        break
                    if ch[0] in letter:
                        actions[name] = letter[ch[0]]
                        break
                    print("     invalid, try again")
                print()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled; nothing saved.")
        return 1

    save_config(config_path, cfg)
    print("Saved %s" % config_path)
    return 0


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def setup_logging(verbose: bool) -> None:
    log.setLevel(logging.DEBUG)
    try:
        fh = logging.FileHandler(LOG_PATH)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s\t%(message)s"))
        log.addHandler(fh)
    except Exception:
        pass
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    log.addHandler(sh)


def is_x86() -> bool:
    # The in-memory byte patches are x86-64 machine code. Everything else (noatime, synocrond
    # tuning, the hibernation debug logger) is architecture-independent and runs anywhere.
    return platform.machine() == "x86_64"


def preflight(require_root: bool = True) -> Optional[str]:
    major = syno_get_key_value(VERSION_PATH, "majorversion")
    if major and major != "7":
        return "This script targets DSM 7 (found major version %s)." % major
    if require_root and hasattr(os, "geteuid") and os.geteuid() != 0:
        return "Please run with root privileges (sudo)."
    return None


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Synology DSM HDD hibernation fixer")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--install", action="store_true", help="register the boot task (runs this script in place) and apply the fixes")
    g.add_argument("--uninstall", action="store_true", help="remove the boot task and undo the config changes")
    g.add_argument("--run", action="store_true", help="apply all fixes now")
    g.add_argument("--status", action="store_true", help="show current state")
    g.add_argument("--diagnose", action="store_true", help="dump patch-site info")
    g.add_argument("--configure", action="store_true", help="interactively edit the config")
    parser.add_argument("--config", default=None, help="path to config file (default: next to the script)")
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose console output")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)

    need_root = not (args.status or args.diagnose)
    err = preflight(require_root=need_root)
    if err:
        print("ERROR: %s" % err)
        return 1

    script_path = os.path.abspath(sys.argv[0])
    config_path = args.config or config_path_for(script_path)

    if args.install:
        return cmd_install(script_path)
    if args.uninstall:
        return cmd_uninstall()
    if args.run:
        return cmd_run(config_path)
    if args.status:
        return cmd_status(config_path)
    if args.diagnose:
        return cmd_diagnose()
    if args.configure:
        return cmd_configure(config_path)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
