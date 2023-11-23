
void FUN_0010f1b0(char **param_1)

{
  char *__s;
  int iVar1;
  int iVar2;
  size_t __n;
  time_t tVar3;
  undefined8 uVar4;
  long lVar5;
  long lVar6;
  long in_FS_OFFSET;
  long local_38;
  long local_30;
  
  local_30 = *(long *)(in_FS_OFFSET + 0x28);
  iVar1 = SLIBCProcFork();
  if (iVar1 != 0) {
    if (local_30 == *(long *)(in_FS_OFFSET + 0x28)) {
      return;
    }
                    /* WARNING: Subroutine does not return */
    __stack_chk_fail();
  }
  __s = *param_1;
  local_38 = 0;
  __n = strlen(__s);
  strncpy(__s,"synostgd-disk",__n);
  iVar1 = prctl(0xf,"synostgd-disk");
  if (iVar1 < 0) goto LAB_0010f2b0;
  while( true ) {
    signal(1,FUN_0010e220);
    signal(0xf,FUN_0010e220);
    signal(10,FUN_0010e240);
    local_38 = SLIBCSzListAlloc();
    if (local_38 != 0) break;
    __syslog_chk(3,2,"%s:%d Fail to allocate list","disk_monitor.c",0x1cf);
    _Exit(0);
LAB_0010f2b0:
    __syslog_chk(3,2,"%s:%d Failed to rename process","disk_monitor.c",0x1c6);
  }
  lVar6 = 0;
  lVar5 = 0;
  tVar3 = 0;
  do {
    FUN_0010e260();
    if (DAT_0011c800 == '\0') {
      tVar3 = time((time_t *)0x0);
      if (0x3b < tVar3 - lVar5) goto LAB_0010f314;
    }
    else {
LAB_0010f314:
      lVar5 = tVar3;
      iVar1 = SYNODiskPortEnum(1,&local_38);
      tVar3 = lVar5;
      if (iVar1 < 0) {
        __syslog_chk(3,2,"%s:%d Fail to enum internal disks","disk_monitor.c",0x19e);
      }
      else {
        iVar1 = SYNODiskPortEnum(3,&local_38);
        if (iVar1 < 0) {
          __syslog_chk(3,2,"%s:%d Fail to enum eunit disks","disk_monitor.c",0x1a3);
        }
        else {
          iVar1 = SYNODiskPortEnum(0xb);
          if (-1 < iVar1) {
            if ((local_38 != 0) && (iVar1 = *(int *)(local_38 + 4) + -1, -1 < iVar1)) {
              do {
                uVar4 = SLIBCSzListGet();
                iVar2 = IsDiskSystemAndSatadom(uVar4);
                if (iVar2 != 2) {
                  SLIBCSzListRemove(local_38);
                }
                iVar1 = iVar1 + -1;
              } while (iVar1 != -1);
            }
            if ((DAT_0011c800 == '\0') && (lVar5 - lVar6 < 0xe10)) {
              FUN_0010e960(local_38,0);
            }
            else {
              FUN_0010e960(local_38,1);
              lVar6 = lVar5;
            }
            SYNOStorageCheckNUpdateDiskSBCache();
            DAT_0011c800 = '\0';
            goto LAB_0010f3dc;
          }
          __syslog_chk(3,2,"%s:%d Fail to enum system disks","disk_monitor.c",0x1ad);
        }
      }
      __syslog_chk(3,2,"%s:%d Fail to enum disks","disk_monitor.c",0x1dc);
    }
LAB_0010f3dc:
    SLIBCSzListRemoveAll(local_38);
    if (DAT_0011c804 != 0) {
                    /* WARNING: Subroutine does not return */
      _exit(DAT_0011c804);
    }
    sleep(5);
  } while( true );
}

