#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Synology DSM HDD hibernation fixer
==================================

Makes the hard drives of an x86 Synology NAS actually spin down (hibernate) by
removing the things that keep waking them up. Tested target: DSM 7.0 - 7.3.

What it does (all applied by ``--run``):

  1. NVMe hibernation fix  -- patches the *running* ``scemd`` and ``synostgd-disk``
     processes in memory so that ongoing NVMe I/O no longer blocks HDD
     hibernation (the classic "Docker on an NVMe volume keeps my HDDs awake"
     problem). Nothing on disk is modified, so this must run once per boot.
  2. synocrond task tuning -- slows down or deletes the many built-in background
     jobs (disk health, BTRFS maintenance, data collection, ...) that wake disks.
     Which job does what is declared in an external JSON config file.
  3. noatime           -- remounts ``/`` noatime and (optionally) sets data
     volumes to noatime, to stop access-time writes from waking disks.
  4. synocached        -- lowers the redis/synocached idle timeout 3600 -> 900s.

How it persists
---------------
``--install`` copies this script to a data volume (default /volume1/hiber_fixer),
writes a config file next to it, and creates a **boot-up Task Scheduler task that
simply runs this script** (``python3 .../hiber_fixer.py --run``). No compressed
copy of the script is embedded in the task. Both the task (kept in DSM's DB) and
the script (on a user volume) survive DSM upgrades.

Usage
-----
    sudo python3 hiber_fixer.py --install [--install-dir DIR]
    sudo python3 hiber_fixer.py --run          # apply everything now (used by the task)
    sudo python3 hiber_fixer.py --status        # show current state
    sudo python3 hiber_fixer.py --configure     # interactively edit the config
    sudo python3 hiber_fixer.py --diagnose      # dump patch-site info (for new DSM builds)
    sudo python3 hiber_fixer.py --uninstall [--purge]
"""

from __future__ import annotations

import argparse
import ctypes
import fnmatch
import json
import logging
import os
import platform
import re
import shutil
import signal
import struct
import subprocess
import sys
import time
from ctypes import CDLL, POINTER, Structure, c_int, c_size_t, c_ssize_t, c_uint64, c_ulong, c_void_p, cast, create_string_buffer
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #

TASK_NAME = "HDD Hibernation Fixer task"          # kept identical to the old script so --install replaces it
DEFAULT_INSTALL_DIR = "/volume1/hiber_fixer"
CONFIG_BASENAME = "hiber_fixer.config.json"
LOG_PATH = "/var/log/hibernation_fixer.log"
BACKUP_DIR = "/var/synobackup"

PYTHON = "/usr/bin/python3"

SCEMD_PATH = "/usr/syno/bin/scemd"
SYNOSTORAGED_PATH = "/usr/syno/sbin/synostoraged"
SYNOCROND_CONFIG_PATH = "/usr/syno/etc/synocrond.config"
SPACE_TABLE_PATH = "/var/lib/space/space_table"
VOLUME_CONF_PATH = "/usr/syno/etc/volume.conf"
SYNOINFO_CONF_PATH = "/etc/synoinfo.conf"
SYNOCACHED_DIR = "/usr/syno/etc/synocached"
VERSION_PATH = "/etc.defaults/VERSION"

SYNOGETKEYVALUE = "/usr/syno/bin/synogetkeyvalue"
SYNOSETKEYVALUE = "/usr/syno/bin/synosetkeyvalue"
ESYNOSCHEDULER_CANDIDATES = ["/usr/syno/sbin/esynoscheduler", "/usr/syno/bin/esynoscheduler"]

SYNOCROND_TASK_DIRS = [
    "/usr/syno/share/synocron.d/",
    "/usr/syno/etc/synocron.d/",
    "/usr/local/etc/synocron.d/",
]

VALID_ACTIONS = ("unchanged", "hourly", "daily", "weekly", "monthly", "delete")
PERIOD_ACTIONS = ("hourly", "daily", "weekly", "monthly")

BOOT_WAIT_TIMEOUT = 180           # seconds to wait for the system to finish booting

log = logging.getLogger("hiber_fixer")


# --------------------------------------------------------------------------- #
# Recommended default action for each known synocrond task.
# Unknown tasks (e.g. added by a future DSM/package) default to "unchanged".
# --------------------------------------------------------------------------- #

DEFAULT_TASK_ACTIONS: Dict[str, str] = {
    "builtin-synodbud-synodbud": "delete",
    "builtin-dyn-synodbud-default": "delete",
    "builtin-dyn-autopkgupgrade-default": "delete",
    "builtin-libhwcontrol-disk_daily_routine": "weekly",
    "builtin-libhwcontrol-disk_monthly_routine": "monthly",
    "builtin-libhwcontrol-disk_weekly_routine": "weekly",
    "builtin-libhwcontrol-syno_disk_health_record": "weekly",
    "builtin-libsynostorage-syno_disk_health_record": "weekly",
    "builtin-synobtrfssnap-synobtrfssnap": "monthly",
    "builtin-synobtrfssnap-synostgreclaim": "monthly",
    "builtin-synocrond_btrfs_free_space_analyze-default": "monthly",
    "builtin-synodatacollect-udc": "delete",
    "builtin-synodatacollect-udc-disk": "delete",
    "builtin-synorenewdefaultcert-renew_default_certificate": "monthly",
    "builtin-synorenewdefaultcert-default": "monthly",
    "builtin-synosharesnaptree_reconstruct-default": "weekly",
    "builtin-synosharing-default": "monthly",
    "builtin-synolegalnotifier-synolegalnotifier": "monthly",
    "builtin-synolegalnotifier-default": "monthly",
    "builtin-syno_ew_weekly_check-extended_warranty_check": "monthly",
    "builtin-syno_ew_weekly_check-default": "monthly",
    "builtin-syno_ntp_status_check-check_ntp_status": "monthly",
    "builtin-syno_ntp_status_check-default": "monthly",
    "builtin-libsynostorage-syno_disk_db_update": "monthly",
    "builtin-libsynostorage-syno_btrfs_metadata_check": "monthly",
    "builtin-libsynostorage-syno_disk_mail_send": "weekly",
    "pkg-ReplicationService-synobtrfsreplicacore-clean": "monthly",
    "builtin-Docker-docker_check_image_upgradable_job": "weekly",
    "pkg-Docker-docker_check_image_upgradable_job": "weekly",
    "pkg-Docker-default": "weekly",
    "builtin-ContainerManager-docker_check_image_upgradable_job": "weekly",
    "pkg-ContainerManager-docker_check_image_upgradable_job": "weekly",
    "builtin-configautobackup-configautobackup": "unchanged",
    "builtin-dyn-configautobackup-default": "unchanged",
    "builtin-myds-job": "weekly",
    "builtin-dyn-myds-job": "weekly",
    "builtin-autopkgupgrade-autopkgupgrade": "weekly",
    "builtin-synoupgrade_routine-default": "unchanged",
    "builtin-dyn-syno-letsencrypt-syno-letsencrypt - renew": "unchanged",
    "builtin-Spreadsheet-auto_clean_weekly": "monthly",
    "builtin-Spreadsheet-auto_office_clean_temp_daily": "weekly",
    "builtin-SynologyDrive-caculate-db-usage": "weekly",
    "builtin-SynologyDrive-cleanup-db": "weekly",
    "builtin-SynologyPhotos-SynologyPhotosDatabaseToolVacuum": "weekly",
    "builtin-CodecPack-CodecPackCheckAndUpdate": "monthly",
    "builtin-SynologyApplicationService-auto_vacuum_daily": "weekly",
    "builtin-DownloadStation-DownloadStationUpdateJob": "monthly",
    "builtin-DownloadStation-DownloadStationMonitorTransmissionJob": "weekly",
    "pkg-SynologyApplicationService-auto_vacuum_daily": "weekly",
    "pkg-SMBService-smb_stats_update_job": "weekly",
    "pkg-SynoAnalytics-synoanalytics": "delete",
    "pkg-WebStation-webstaion_job": "weekly",
}

DEFAULT_FIXES = {
    "nvme_in_memory_patch": True,
    "remount_root_noatime": True,
    "synocached_timeout_900": True,
    "set_volumes_noatime": False,   # requires a reboot; opt-in only
}

# Short descriptions shown by --status / --configure.
TASK_DESCRIPTIONS: Dict[str, str] = {
    "builtin-synodbud-synodbud": "updates misc DBs (abuser-blocklist, geoip, ca-certs, securityscan)",
    "builtin-dyn-synodbud-default": "updates misc DBs (abuser-blocklist, geoip, ca-certs, securityscan)",
    "builtin-dyn-autopkgupgrade-default": "update checker for installed packages",
    "builtin-libhwcontrol-disk_daily_routine": "disk SMART info collector",
    "builtin-libhwcontrol-disk_monthly_routine": "HDD performance-stats monitor",
    "builtin-libhwcontrol-disk_weekly_routine": "checks SMART/hotspare status for disks",
    "builtin-libhwcontrol-syno_disk_health_record": "parses disk_overview.xml (remaining life, errors, ...)",
    "builtin-libsynostorage-syno_disk_health_record": "parses disk_overview.xml (remaining life, errors, ...)",
    "builtin-synobtrfssnap-synobtrfssnap": "cleans up deleted BTRFS subvolumes",
    "builtin-synobtrfssnap-synostgreclaim": "checks number of deleted BTRFS volumes to reclaim",
    "builtin-synocrond_btrfs_free_space_analyze-default": "calculates BTRFS fragmentation per volume",
    "builtin-synodatacollect-udc": "user data collection",
    "builtin-synodatacollect-udc-disk": "user data collection (disk)",
    "builtin-synorenewdefaultcert-renew_default_certificate": "manages cryptographic certificates",
    "builtin-synorenewdefaultcert-default": "manages cryptographic certificates",
    "builtin-synosharesnaptree_reconstruct-default": "reconstructs BTRFS snapshot tree",
    "builtin-synosharing-default": "cleans up sharing.db SQLite tables",
    "builtin-synolegalnotifier-synolegalnotifier": "downloads user agreements from Synology",
    "builtin-synolegalnotifier-default": "downloads user agreements from Synology",
    "builtin-syno_ew_weekly_check-extended_warranty_check": "queries Synology for extended-warranty info",
    "builtin-syno_ew_weekly_check-default": "queries Synology for extended-warranty info",
    "builtin-syno_ntp_status_check-check_ntp_status": "runs NTP time sync",
    "builtin-syno_ntp_status_check-default": "runs NTP time sync",
    "builtin-libsynostorage-syno_disk_db_update": "downloads/extracts disk compatibility DB",
    "builtin-libsynostorage-syno_btrfs_metadata_check": "checks BTRFS metadata usage, emails alerts",
    "builtin-libsynostorage-syno_disk_mail_send": "sends disk-related notification e-mails",
    "pkg-ReplicationService-synobtrfsreplicacore-clean": "cleans up received BTRFS backup snapshots",
    "builtin-Docker-docker_check_image_upgradable_job": "Docker upgradable-image checker",
    "pkg-Docker-docker_check_image_upgradable_job": "Docker upgradable-image checker",
    "pkg-ContainerManager-docker_check_image_upgradable_job": "Container Manager upgradable-image checker",
    "builtin-synoupgrade_routine-default": "DSM upgrade routine",
    "pkg-SMBService-smb_stats_update_job": "updates SMB usage statistics",
    "pkg-SynoAnalytics-synoanalytics": "Synology analytics / data collection",
    "pkg-WebStation-webstaion_job": "Web Station cron job",
    "builtin-dyn-syno-letsencrypt-syno-letsencrypt - renew": "renews Let's Encrypt certificates",
}


def describe_task(name: str) -> str:
    if name in TASK_DESCRIPTIONS:
        return TASK_DESCRIPTIONS[name]
    if name.startswith("pkg-"):
        return "package-installed synocrond task"
    return ""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def run_cmd(args: List[str], check: bool = False) -> subprocess.CompletedProcess:
    """Run a command, capturing output as text. Never raises unless check=True."""
    log.debug("exec: %s", " ".join(args))
    return subprocess.run(args, capture_output=True, universal_newlines=True, check=check)


def which_first(candidates: List[str]) -> Optional[str]:
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def backup_file(path: str) -> None:
    """Back up a file into BACKUP_DIR before we modify it (best effort).

    Backups are namespaced by full path (so same-named files in different dirs don't
    collide) and the first/pristine copy is kept across re-runs."""
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        if not os.path.exists(path):
            return
        dest_name = path.replace(os.sep, "/").strip("/").replace("/", "_")
        dest = os.path.join(BACKUP_DIR, dest_name)
        if not os.path.exists(dest):
            shutil.copy2(path, dest)
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


def notify_user(title: str, message: str) -> None:
    """Make a failure loud in the log. (No DSM popup: the notification templates
    are fragile and firing the wrong one is worse than none. Users check via
    --status / --diagnose.)"""
    log.error("!!! %s: %s", title, message)


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
#   * scemd / polling_hibernation_timer.c -- the HDD-hibernation polling timer builds
#     a disk list via SYNODiskPortEnum(portType, &list) for port types 1 and 2 (internal
#     SATA) plus 7 (NVMe), then DiskListIdleEnough(list) decides whether the HDDs may sleep.
#   * synostgd-disk / disk_monitor.c -- a forked monitor loop enumerates port types
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


# ---- the actual patch definitions ---------------------------------------- #

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
        p_type = struct.unpack_from("<I", data, off)[0]
        if p_type == 1:  # PT_LOAD
            p_offset, p_vaddr = struct.unpack_from("<QQ", data, off + 8)[0:2]
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


# ---- low-level process memory access ------------------------------------- #

PTRACE_PEEKDATA = 2
PTRACE_POKEDATA = 5
PTRACE_ATTACH = 16
PTRACE_DETACH = 17


class iovec(Structure):
    _fields_ = [("iov_base", c_void_p), ("iov_len", c_size_t)]


class _Libc:
    def __init__(self) -> None:
        self.libc = CDLL("libc.so.6", use_errno=True)
        self.libc.process_vm_readv.argtypes = [c_uint64, POINTER(iovec), c_ulong, POINTER(iovec), c_ulong, c_ulong]
        self.libc.process_vm_readv.restype = c_ssize_t
        self.libc.ptrace.argtypes = [c_uint64, c_uint64, c_void_p, c_void_p]
        self.libc.ptrace.restype = c_uint64

    def read_mem(self, pid: int, addr: int, length: int) -> Optional[bytes]:
        buf = create_string_buffer(length)
        local = iovec(cast(buf, c_void_p), length)
        remote = iovec(c_void_p(addr), length)
        ret = self.libc.process_vm_readv(pid, ctypes.byref(local), 1, ctypes.byref(remote), 1, 0)
        if ret != length:
            log.error("process_vm_readv(pid=%d, addr=%#x, len=%d) -> %d (%s)",
                      pid, addr, length, ret, os.strerror(ctypes.get_errno()))
            return None
        return buf.raw

    def write_mem(self, pid: int, writes: List[Tuple[int, bytes]]) -> bool:
        """Write via ptrace POKEDATA (process_vm_writev cannot write RO code pages)."""
        if self.libc.ptrace(PTRACE_ATTACH, pid, None, None) != 0:
            log.error("ptrace ATTACH failed for pid %d", pid)
            return False
        try:
            _, status = os.waitpid(pid, 0)
            if not (os.WIFSTOPPED(status) and os.WSTOPSIG(status) == signal.SIGSTOP):
                log.error("unexpected stop status %#x for pid %d", status, pid)
                return False

            ok = True
            for addr, payload in writes:
                for off in range(0, len(payload), 8):
                    word_addr = addr + off
                    chunk = payload[off:off + 8]
                    if len(chunk) < 8:  # need the surrounding bytes to preserve the rest of the word
                        ctypes.set_errno(0)
                        old = self.libc.ptrace(PTRACE_PEEKDATA, pid, c_void_p(word_addr), None)
                        eno = ctypes.get_errno()
                        if eno != 0:
                            log.error("ptrace PEEKDATA failed at %#x (pid %d): %s", word_addr, pid, os.strerror(eno))
                            ok = False
                            break
                        chunk = chunk + struct.pack("<Q", old)[len(chunk):]
                    val = struct.unpack("<Q", chunk)[0]
                    if self.libc.ptrace(PTRACE_POKEDATA, pid, c_void_p(word_addr), c_void_p(val)) != 0:
                        log.error("ptrace POKEDATA failed at %#x (pid %d)", word_addr, pid)
                        ok = False
                        break
                if not ok:
                    break
            return ok
        finally:
            self.libc.ptrace(PTRACE_DETACH, pid, None, None)


def get_pid_by_name(name: str) -> Optional[int]:
    try:
        return int(subprocess.check_output(["pidof", name]).split()[0])
    except Exception:
        return None


def get_module_base(pid: int, module_name: str) -> Optional[int]:
    """Load bias of the module: the vaddr at which file offset 0 is mapped."""
    line_re = re.compile(r"^([\da-f]+)-([\da-f]+)\s+\S+\s+([\da-f]+)\s+\S+\s+\d+\s+(.*)$")
    try:
        with open(f"/proc/{pid}/maps") as f:
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


def apply_target(libc: _Libc, target: PatchTarget) -> PatchOutcome:
    out = PatchOutcome(target=target.process_name)

    pid = get_pid_by_name(target.process_name)
    if not pid:
        out.error = f"process '{target.process_name}' not running"
        log.error(out.error)
        return out

    module = os.path.basename(target.binary_path)
    base = get_module_base(pid, module)
    if base is None:
        out.error = f"could not find module base of {module} in pid {pid}"
        log.error(out.error)
        return out

    try:
        segs = parse_elf_load_segments(open(target.binary_path, "rb").read())
    except Exception as e:
        out.error = f"cannot parse ELF {target.binary_path}: {e}"
        log.error(out.error)
        return out

    for variant in target.variants:
        changes = compute_changelist(target.binary_path, variant)
        if not changes:
            continue
        out.matched_variant = variant.name

        # Resolve each change's file offset to a runtime address.
        reads: List[Tuple[int, int]] = []
        for ch in changes:
            vaddr = file_offset_to_vaddr(segs, ch.file_offset)
            if vaddr is None:
                out.error = f"file offset {ch.file_offset:#x} not in any PT_LOAD segment"
                log.error(out.error)
                return out
            reads.append((base + vaddr, len(ch.orig)))

        current = [libc.read_mem(pid, addr, ln) for addr, ln in reads]
        if any(c is None for c in current):
            out.error = "failed reading target process memory"
            return out

        if all(current[i] == changes[i].new for i in range(len(changes))):
            out.already_patched = True
            log.info("%s: already patched in memory (%s)", target.process_name, variant.name)
            return out

        if not all(current[i] == changes[i].orig for i in range(len(changes))):
            out.error = "memory content does not match expected original bytes"
            log.error("%s: %s", target.process_name, out.error)
            return out

        writes = [(reads[i][0], changes[i].new) for i in range(len(changes))]
        if libc.write_mem(pid, writes):
            out.applied = True
            log.info("%s: applied in-memory patch (%s)", target.process_name, variant.name)
        else:
            out.error = "ptrace write failed"
        return out

    out.error = "no known patch pattern matched the current binary"
    log.error("%s: %s (%s) -- DSM may have changed this binary; run --diagnose",
              target.process_name, out.error, target.binary_path)
    return out


def do_in_memory_fixes() -> bool:
    try:
        libc = _Libc()
    except Exception as e:
        log.error("failed to initialise libc bindings: %s", e)
        return False

    all_ok = True
    unmatched = []
    for target in PATCH_TARGETS:
        outcome = apply_target(libc, target)
        if outcome.error and not outcome.already_patched:
            all_ok = False
            if outcome.matched_variant is None:
                unmatched.append(target.binary_path)

    if unmatched:
        notify_user("HDD Hibernation Fixer",
                    "The NVMe hibernation patch no longer matches these DSM binaries: "
                    + ", ".join(unmatched) + ". Run 'hiber_fixer.py --diagnose'.")
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

    fname = os.path.basename(path)
    if "." in fname:
        fname = fname.split(".")[0]

    # Package tasks (under /usr/local/etc/synocron.d) are named pkg-<file>-<name> at
    # runtime; built-in ones (share/ and etc/) use the builtin- prefix.
    norm = path.replace(os.sep, "/")
    prefix = "pkg-" if "/usr/local/etc/synocron.d/" in norm else "builtin-"

    tasks = []
    for entry in entries:
        if "name" in entry:
            name = prefix + fname + "-" + entry["name"]
        else:
            name = prefix + fname + "-default"
        tasks.append(SynocrondTask(name, entry))
    return tasks


def task_period(body: dict) -> str:
    period = body.get("period", "?")
    if period == "crontab" and "crontab" in body:
        period += f" ({body['crontab']})"
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


def discover_tasks() -> Dict[str, Tuple[str, str]]:
    """Return {task_name: (current_period, description)} across task files and the live config."""
    result: Dict[str, Tuple[str, str]] = {}
    for path in enumerate_task_files():
        try:
            for t in load_task_file(path):
                result[t.name] = (task_period(t.body), describe_task(t.name))
        except Exception as e:
            log.warning("skipping task file %s: %s", path, e)

    cfg = load_synocrond_config()
    if cfg:
        for job_name, job in cfg.get("jobs", {}).items():
            name = clean_job_name(job_name)
            result[name] = (task_period(job.get("config", {})), describe_task(name))
    return result


def _handle_dyn_task_deletion(name: str) -> None:
    """Extra work required to keep certain 'dynamic' tasks from coming back."""
    if name == "builtin-dyn-autopkgupgrade-default":
        for key in ("pkg_autoupdate_important", "enable_pkg_autoupdate_all", "upgrade_pkg_dsm_notification"):
            if syno_get_key_value(SYNOINFO_CONF_PATH, key) != "no":
                syno_set_key_value(SYNOINFO_CONF_PATH, key, "no")
    elif name in ("builtin-synodbud-synodbud", "builtin-dyn-synodbud-default"):
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
# 3) noatime + 4) synocached fixes
# --------------------------------------------------------------------------- #

def remount_root_noatime() -> None:
    try:
        out = subprocess.check_output(["mount"], universal_newlines=True)
        for line in out.splitlines():
            if " / " in line and "md0" in line and "noatime" in line:
                return  # already noatime
    except Exception:
        pass
    rc = subprocess.call(["mount", "-o", "noatime,remount", "/"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if rc:
        log.error("remounting / noatime failed; expect HDD wakeups from atime updates")
    else:
        log.info("remounted / as noatime")


def apply_synocached_fix() -> None:
    for fname in ("synocached.conf", "synocached.default.conf"):
        path = os.path.join(SYNOCACHED_DIR, fname)
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                data = f.read()
            new_data = data.replace("timeout 3600", "timeout 900")
            if new_data != data:
                backup_file(path)
                with open(path, "w") as f:
                    f.write(new_data)
                log.info("lowered synocached idle timeout in %s", path)
        except OSError as e:
            log.error("cannot update %s: %s", path, e)


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
    import configparser
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
    import configparser
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
    discovered = discover_tasks()
    for name in sorted(set(discovered) | set(DEFAULT_TASK_ACTIONS)):
        tasks[name] = DEFAULT_TASK_ACTIONS.get(name, "unchanged")
    return {
        "_comment": "Actions: unchanged|hourly|daily|weekly|monthly|delete. Edit, then re-run --run.",
        "fixes": dict(DEFAULT_FIXES),
        "synocrond_tasks": tasks,
    }


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

    return {"_comment": cfg.get("_comment", default_config()["_comment"]),
            "fixes": fixes, "synocrond_tasks": tasks}


def save_config(path: str, cfg: dict) -> None:
    with open(path, "w") as f:
        json.dump(cfg, f, indent=4, sort_keys=False)


# --------------------------------------------------------------------------- #
# Task Scheduler integration
# --------------------------------------------------------------------------- #

def esynoscheduler() -> Optional[str]:
    return which_first(ESYNOSCHEDULER_CANDIDATES)


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
    args = [tool, "--create", f"task_name={TASK_NAME}", "event=bootup", "enable=true",
            "operation_type=script", f"operation={operation}",
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
        out = subprocess.check_output([tool, "--delete", f"task_name={TASK_NAME}"],
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


def wait_for_system(timeout: int = BOOT_WAIT_TIMEOUT) -> bool:
    """Wait until systemd reports the boot has finished (running/degraded)."""
    deadline = time.time() + timeout
    while not system_running():
        if time.time() >= deadline:
            log.warning("system did not finish booting within %ds; continuing anyway", timeout)
            return False
        time.sleep(2)
    return True


def wait_for_daemons(timeout: int = BOOT_WAIT_TIMEOUT) -> bool:
    """Wait until the in-memory patch targets (scemd, synostgd-disk) are running."""
    deadline = time.time() + timeout
    while True:
        missing = [t.process_name for t in PATCH_TARGETS if not get_pid_by_name(t.process_name)]
        if not missing:
            return True
        if time.time() >= deadline:
            log.error("patch target daemon(s) not running within %ds: %s", timeout, ", ".join(missing))
            return False
        time.sleep(2)


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
    if fixes.get("nvme_in_memory_patch", True):
        if wait:
            wait_for_daemons()   # gate only the in-memory patch on the daemons being up
        ok = do_in_memory_fixes() and ok
    apply_config_to_task_files(actions)
    ok = apply_config_to_synocrond_config(actions) and ok
    if fixes.get("synocached_timeout_900", True):
        apply_synocached_fix()

    relatime = find_relatime_volumes()
    if relatime:
        if fixes.get("set_volumes_noatime", False):
            set_volumes_noatime()
        else:
            log.warning("volumes still using relatime (set fixes.set_volumes_noatime=true to fix): %s",
                        ", ".join(n for _u, n in relatime))

    log.info("run: done (%s)", "ok" if ok else "with errors")
    return 0 if ok else 1


def cmd_install(script_path: str, install_dir: str) -> int:
    install_dir = os.path.abspath(install_dir)
    os.makedirs(install_dir, exist_ok=True)
    installed_script = os.path.join(install_dir, "hiber_fixer.py")

    src = os.path.abspath(script_path)
    if src != installed_script:
        shutil.copy2(src, installed_script)
        print(f"Installed script to {installed_script}")
    else:
        print(f"Running from install location {installed_script}")

    config_path = os.path.join(install_dir, CONFIG_BASENAME)
    if not os.path.exists(config_path):
        save_config(config_path, default_config())
        print(f"Wrote default config to {config_path}")
        print("  Review/edit it to choose what to do with each synocrond task, then re-run --run if needed.")
    else:
        # merge in any new tasks
        save_config(config_path, load_config(config_path))
        print(f"Kept existing config {config_path}")

    operation = f"{PYTHON} {installed_script} --run"
    delete_boot_task()
    if not create_boot_task(operation):
        print("ERROR: failed to create the boot-up Task Scheduler task")
        return 1
    print(f'Created boot-up task "{TASK_NAME}" -> {operation}')

    print("\nApplying fixes now...")
    rc = cmd_run(config_path, wait=False)
    print("\nInstallation complete. The fixes will re-apply automatically on every boot.")
    print(f"You can delete the copy you ran --install from; the active copy lives in {install_dir}.")
    return rc


def cmd_uninstall(script_path: str, purge: bool) -> int:
    if delete_boot_task():
        print(f'Removed the "{TASK_NAME}" boot task.')
    else:
        print(f'Could not remove the "{TASK_NAME}" boot task (maybe already gone).')
    print("The in-memory NVMe patch is not persistent; reboot to fully revert it.")
    if purge:
        install_dir = os.path.dirname(os.path.abspath(script_path))
        print(f"--purge: leaving {install_dir} in place; delete it manually if you want.")
        print(f"Backups of modified config files are in {BACKUP_DIR}.")
    return 0


def cmd_status(config_path: str) -> int:
    print(f"== HDD Hibernation Fixer status ==\n")

    state = scheduler_task_state(TASK_NAME)
    if state is None:
        print(f'Boot task "{TASK_NAME}": NOT INSTALLED')
    else:
        print(f'Boot task "{TASK_NAME}": {"ENABLED" if state else "DISABLED (re-enable it or re-run --install)"}')

    print("\nIn-memory NVMe patch (current process state):")
    try:
        libc = _Libc()
    except Exception as e:
        libc = None
        print(f"  (cannot read process memory: {e})")
    if libc:
        for target in PATCH_TARGETS:
            _status_report_target(libc, target)

    print("\nnoatime:")
    try:
        mounts = subprocess.check_output(["mount"], universal_newlines=True)
        root_noatime = any(" / " in l and "md0" in l and "noatime" in l for l in mounts.splitlines())
        print(f"  root (/) noatime: {'yes' if root_noatime else 'NO'}")
    except Exception:
        print("  root (/) noatime: unknown")
    relatime = find_relatime_volumes()
    print(f"  volumes on relatime: {', '.join(n for _u, n in relatime) if relatime else 'none'}")

    print("\nsynocached idle timeout:")
    conf = os.path.join(SYNOCACHED_DIR, "synocached.conf")
    if os.path.exists(conf):
        try:
            val = "?"
            with open(conf) as f:
                for line in f:
                    if line.startswith("timeout"):
                        val = line.split()[1]
            print(f"  {conf}: timeout {val}")
        except Exception:
            print(f"  {conf}: unreadable")

    if os.path.exists(config_path):
        cfg = load_config(config_path)
        acts = cfg["synocrond_tasks"]
        changed = {k: v for k, v in acts.items() if v != "unchanged"}
        print(f"\nConfig: {config_path}")
        print(f"  fixes: {cfg['fixes']}")
        print(f"  synocrond tasks: {len(acts)} known, {len(changed)} set to change")
    else:
        print(f"\nConfig: not found at {config_path}")
    return 0


def _status_report_target(libc: _Libc, target: PatchTarget) -> None:
    pid = get_pid_by_name(target.process_name)
    if not pid:
        print(f"  {target.process_name}: not running")
        return
    base = get_module_base(pid, os.path.basename(target.binary_path))
    if base is None:
        print(f"  {target.process_name}: module base not found")
        return
    try:
        segs = parse_elf_load_segments(open(target.binary_path, "rb").read())
    except Exception as e:
        print(f"  {target.process_name}: cannot parse binary ({e})")
        return
    for variant in target.variants:
        changes = compute_changelist(target.binary_path, variant)
        if not changes:
            continue
        states = []
        for ch in changes:
            vaddr = file_offset_to_vaddr(segs, ch.file_offset)
            cur = libc.read_mem(pid, base + vaddr, len(ch.orig)) if vaddr is not None else None
            if cur == ch.new:
                states.append("patched")
            elif cur == ch.orig:
                states.append("original")
            else:
                states.append("unknown")
        verdict = "PATCHED" if all(s == "patched" for s in states) else \
                  "not patched" if all(s == "original" for s in states) else "partial/unknown"
        print(f"  {target.process_name}: {verdict} (matched {variant.name})")
        return
    print(f"  {target.process_name}: NO PATTERN MATCH -- binary changed; run --diagnose")


def cmd_diagnose() -> int:
    """Report pattern matches against the on-disk binaries and dump candidate sites
    if nothing matches (useful to regenerate patterns after a DSM update)."""
    print("== diagnose ==")
    for target in PATCH_TARGETS:
        print(f"\n{target.binary_path} (process {target.process_name}):")
        if not os.path.exists(target.binary_path):
            print("  binary not found")
            continue
        data = open(target.binary_path, "rb").read()
        matched = False
        for variant in target.variants:
            rx = _compile_search(variant.search)
            hits = list(rx.finditer(data))
            print(f"  variant '{variant.name}': {len(hits)} match(es)"
                  + (f" at {hits[0].start():#x}" if hits else ""))
            matched = matched or bool(hits)
        if not matched:
            print("  no variant matched -- scanning for candidate 'mov edi,7; call' sites:")
            for off in [m.start() for m in re.finditer(re.escape(b"\xBF\x07\x00\x00\x00\xE8"), data)]:
                ctx = data[max(0, off - 24):off + 16]
                if b"\xBF\x01" in ctx or b"\xBF\x03" in ctx:
                    lo = max(0, off - 24)
                    print(f"    off {off:#x}: ...{data[lo:off + 32].hex()}...")
            print("  Send this output to regenerate the patterns for your DSM build.")
    return 0


def cmd_configure(config_path: str) -> int:
    """Interactive editor for the synocrond task actions."""
    cfg = load_config(config_path) if os.path.exists(config_path) else default_config()
    discovered = discover_tasks()
    actions = cfg["synocrond_tasks"]

    names = sorted(set(discovered) | set(actions))
    print("Choose an action per task. Enter = keep current default.")
    print("Options: (u)nchanged (h)ourly (d)aily (w)eekly (m)onthly (x)delete\n")
    letter = {"u": "unchanged", "h": "hourly", "d": "daily", "w": "weekly", "m": "monthly", "x": "delete"}
    try:
        for i, name in enumerate(names, 1):
            cur_period, descr = discovered.get(name, ("(not present)", describe_task(name)))
            default = actions.get(name, "unchanged")
            print(f"[{i}/{len(names)}] {name}")
            if descr:
                print(f"     {descr}")
            print(f"     current interval: {cur_period}   default action: {default}")
            while True:
                ch = input("     action [u/h/d/w/m/x]: ").strip().lower()
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

    cfg["synocrond_tasks"] = actions
    save_config(config_path, cfg)
    print(f"Saved {config_path}")
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


def preflight(require_root: bool = True) -> Optional[str]:
    if platform.machine() != "x86_64":
        return "Only x86_64-based NAS models are supported."
    major = syno_get_key_value(VERSION_PATH, "majorversion")
    if major and major != "7":
        return f"This script targets DSM 7 (found major version {major})."
    if require_root and hasattr(os, "geteuid") and os.geteuid() != 0:
        return "Please run with root privileges (sudo)."
    return None


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Synology DSM HDD hibernation fixer")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--install", action="store_true", help="install the boot task and apply fixes")
    g.add_argument("--uninstall", action="store_true", help="remove the boot task")
    g.add_argument("--run", action="store_true", help="apply all fixes now")
    g.add_argument("--status", action="store_true", help="show current state")
    g.add_argument("--diagnose", action="store_true", help="dump patch-site info")
    g.add_argument("--configure", action="store_true", help="interactively edit the config")
    parser.add_argument("--install-dir", default=DEFAULT_INSTALL_DIR, help=f"install location (default {DEFAULT_INSTALL_DIR})")
    parser.add_argument("--config", default=None, help="path to config file (default: next to the script)")
    parser.add_argument("--purge", action="store_true", help="with --uninstall: also report leftover files/backups")
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose console output")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)

    need_root = not (args.status or args.diagnose)
    err = preflight(require_root=need_root)
    if err:
        print(f"ERROR: {err}")
        return 1

    script_path = os.path.abspath(sys.argv[0])
    config_path = args.config or config_path_for(script_path)

    if args.install:
        return cmd_install(script_path, args.install_dir)
    if args.uninstall:
        return cmd_uninstall(script_path, purge=args.purge)
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
