// Function to perform disk port enumeration based on the disk type
undefined8 SYNODiskPortEnum(uint diskType, long *diskList) {
    int index;
    ulong result;
    ulong funcResult;
    char *blockSysPrefix;
    char *synoBootVar;
    long blockPathList;
    uint localDiskType;
    undefined sysBlockPath [16];

    blockPathList = 0;
    localDiskType = 0;
    sysBlockPath = (undefined  [16])0x0;

    // Allocate a list for block paths
    blockPathList = SLIBCSzListAlloc(0x400);

    // Define prefixes and variables based on the disk type
    if (diskType == 0) {
        // Handle case when diskType is 0
        __syslog_chk(3, 2, "%s:%d device name list can not be NULL", "external/external_disk_port_enum.c", 0x17e);
    } else {
        // Define sysBlockPath prefix and synoBootVar based on diskType
        synoBootVar = "synoboot";
        blockSysPrefix = "/sys/block/%s";

        if (diskType == 5) {
            // Handle case when diskType is 5
            __snprintf_chk(sysBlockPath, 0x80, 2, 0x80, blockSysPrefix, synoBootVar);
            SLIBCSzListPush(&blockPathList, sysBlockPath);
        } else if (diskType == 6) {
            // Handle case when diskType is 6
            synoBootVar = "isd";
            blockSysPrefix = "/sys/block/%s*";
            goto addBlockPath;
        } else {
            // Handle case when diskType is not 5 or 6
            synoBootVar = "sd";
            if (diskType < 7) {
                synoBootVar = "isd";
                blockSysPrefix = "/sys/block/%s*";
                goto addBlockPath;
            } else if (diskType == 7 || diskType == 0xb) {
                synoBootVar = "sysd";
                blockSysPrefix = "/sys/block/%s*";
                goto addBlockPath;
            } else {
                // Log an error for unsupported disk types
                __syslog_chk(3, 2, "%s:%d disk port type doesn\'t exist", "external/external_disk_port_enum.c", 0x210, &DAT_001d4959);
                goto endFunc;
            }
        }

        // Set localDiskType and call Func2 for disk enumeration
        localDiskType = diskType;
        funcResult = Func2(blockPathList, (int *)&localDiskType, 1, diskList);
        result = funcResult & 0xffffffff;

        // Check if Func2 was successful
        if ((int)funcResult == 0) {
            // Sort the disk list and end the function
            SLIBCSzListSortItems(*diskList, 1);
            goto endFunc2;
        } else {
            // Log an error if Func2 fails
            __syslog_chk(3, 2, "%s:%d SYNODiskPathGlobAndPortCheck fail!", "external/external_disk_port_enum.c", 0x219);
        }
    }

endFunc:
    result = 0xffffffff;

endFunc2:
    // Free the allocated blockPathList
    SLIBCSzListFree(blockPathList);

    // Check for stack smashing
    if (local_30 != *(long *)(in_FS_OFFSET + 0x28)) {
        __stack_chk_fail();
    }

    return result;
}

// Helper function for disk port enumeration
undefined8 Func2(long blockPathList, int *localDiskType, int paramvar1, long *diskList) {
    int globRes;
    char *blockPathListValue;
    ulong index;
    char **ppcVar5;
    long blockPathListSize;
    undefined globResult [16];

    globResult = (undefined [16])0x0;

    // Check for invalid parameters
    if (paramvar1 < 1 && (localDiskType != (int *)0x0) || (localDiskType == (int *)0x0 && (byte)paramvar1 != 0) ||
        (diskList == (long *)0x0 || (*diskList == 0) || blockPathList == 0)) {
        result = 0xffffffff;
        __syslog_chk(3, 2, "%s:%d Bad parameter", "external/external_disk_port_enum.c", 0x2a);
        ppcVar5 = (char **)globResult._8_8_;
    } else {
        // Check if the blockPathList size is less than 1
        if (*(int *)(blockPathList + 4) < 1) {
            result = 0;
            goto endFunc;
        }

        index = 0;
        do {
            blockPathListValue = (char *)SLIBCSzListGet(blockPathList, index);
            globResult = (undefined [16])0x0;
            globRes = glob64(blockPathListValue, 8, (__errfunc *)0x0, (glob64_t *)globResult);
            ppcVar5 = (char **)globResult._8_8_;

            // Check if glob64 encountered an error
            if (globRes != 0) {
                if (globRes == 2) {
                    result = 0x3a;
                    pcVar2 = "%s:%d read error :%s";
                    goto globErr;
                } else {
                    if (globRes == 1) {
                        result = 0x3c;
                        pcVar2 = "%s:%d out of memory to alloc glob function when looking for:%s";
                        goto globErr;
                    }
                    if (globRes == 3) goto endLoop;
                }

                result = 0xffffffff;
                ppcVar5 = (char **)globResult._8_8_;
                goto LAB_00169b36;
            }

            if (globResult._0_8_ != 0) {
                uVar4 = 0;
                do {
                    blockPathListValue = strrchr(ppcVar5[uVar4], 0x2f);
                    if (blockPathListValue != (char *)0x0) {
                        uVar1 = SYNODiskPortCheck(blockPathListValue + 1);

                        // Check conditions based on diskType and localDiskType
                        if (paramvar1 < 1 || localDiskType == (int *)0x0) {
                            if ((int)uVar1 != 10) goto LAB_00169ab5;
                        } else if ((int)uVar1 == *localDiskType) {
                            LAB_00169ab5:
                            SLIBCSzListPush(diskList, blockPathListValue + 1);
                        }
                        ppcVar5 = (char **)globResult._8_8_;
                    }
                    uVar4 = uVar4 + 1;
                } while (uVar4 < (ulong)globResult._0_8_);
            }

        endLoop:
            // Free the allocated globResult
            if (ppcVar5 != (char **)0x0) {
                globfree64((glob64_t *)globResult);
            }
            index = index + 1;
        } while (index < *(int *)(blockPathList + 4));

        result = 0;
        ppcVar5 = (char **)globResult._8_8_;
    }

LAB_00169b36:
    // Free the allocated globResult
    if (ppcVar5 != (char **)0x0) {
        globfree64((glob64_t *)globResult);
    }

endFunc:
    // Check for stack smashing
    if (local_40 != *(long *)(in_FS_OFFSET + 0x28)) {
        __stack_chk_fail();
    }

    return result;
}
