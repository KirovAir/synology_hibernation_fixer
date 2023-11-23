function check_and_add_device_to_raid() {
    local RAID_ARRAY=$1
    local DEVICE_TO_ADD=$2

    mdadm --detail "$RAID_ARRAY" | grep -q "removed" && {
        echo "Device $DEVICE_TO_ADD is removed. Adding it back to $RAID_ARRAY..."
        mdadm --add "$RAID_ARRAY" "$DEVICE_TO_ADD"
        mdadm --detail "$RAID_ARRAY"
    } || echo "Device $DEVICE_TO_ADD is not removed from $RAID_ARRAY. No action needed."
}

check_and_add_device_to_raid "/dev/md0" "/dev/sdc1"
check_and_add_device_to_raid "/dev/md1" "/dev/sdc2"