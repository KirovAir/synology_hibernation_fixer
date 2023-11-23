
undefined4 DiskIdleCheck(undefined4 param_1)

{
  int iVar1;
  undefined4 uVar2;
  long in_FS_OFFSET;
  long local_28;
  long local_20;
  
  local_20 = *(long *)(in_FS_OFFSET + 0x28);
  local_28 = 0;
  local_28 = SLIBCSzListAlloc(0x400);
  iVar1 = SYNODiskPortEnum(1,&local_28);
  SYNODiskPortEnum(2,&local_28);
  SYNODiskPortEnum(7,&local_28);
  if (iVar1 < 0) {
    uVar2 = 0;
    __syslog_chk(3,2,"%s:%d internal sata port enum failed","polling_hibernation_timer.c",0x27);
    SLIBCSzListFree(local_28);
  }
  else {
    iVar1 = HasESATAWithRWHFSPlus();
    uVar2 = 0;
    if (iVar1 == 0) {
      uVar2 = DiskListIdleEnough(local_28,param_1);
    }
    if (local_28 != 0) {
      SLIBCSzListFree();
    }
  }
  if (local_20 == *(long *)(in_FS_OFFSET + 0x28)) {
    return uVar2;
  }
                    /* WARNING: Subroutine does not return */
  __stack_chk_fail();
}

