# DSM binaries (not committed)

The actual binaries are **Synology proprietary and intentionally git-ignored**. Please don't
redistribute them. This folder only tracks `SHA256SUMS.txt` so a future pull can re-fetch and
verify the exact build this research was done against (DSM 7.3.2-86009 Update 3, DS920+ / x86-64).

## Re-fetch (from the NAS, over SSH)

DSM's SFTP is chrooted, so copy via base64 over a plain SSH command and verify:

```bash
for f in /usr/syno/bin/scemd /usr/syno/sbin/synostoraged \
         /usr/lib/libsynoscemd.so.1 /usr/lib/libhwcontrol.so.1; do
    ssh you@nas "base64 $(readlink -f "$f" 2>/dev/null || echo "$f")" | base64 -d > "$(basename "$f")"
done
sha256sum -c SHA256SUMS.txt
```

`scemd` and `synostoraged` (`synostgd-disk`) hold the NVMe patch sites; `libsynoscemd.so.1` holds
`DiskListIdleEnough` (the SSD-slot cave); `libhwcontrol.so.1` holds `SYNODiskIsSSD`.
