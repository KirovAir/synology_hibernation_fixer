$env:JAVA_HOME = "C:\Users\Jesse\AppData\Local\revtools\jdk\jdk-21.0.11+10"
$ah = "C:\Users\Jesse\AppData\Local\revtools\ghidra\ghidra_12.1.2_PUBLIC\support\analyzeHeadless.bat"
$scripts = "C:\Users\Jesse\AppData\Local\revtools\scripts"
$bins = "C:\Users\Jesse\dev\__tmp\synology_hibernation_fixer\research\dsm-7.3.2\bins"
$proj = "C:\Users\Jesse\AppData\Local\revtools\proj"
New-Item -ItemType Directory -Force $proj | Out-Null
Write-Output "########## SCEMD ##########"
& $ah $proj scemd_proj -import "$bins\scemd" -scriptPath $scripts -postScript DecompilePatchSites.java 0x20103 -deleteProject -analysisTimeoutPerFile 600 2>&1
Write-Output "########## SYNOSTORAGED ##########"
& $ah $proj storaged_proj -import "$bins\synostoraged" -scriptPath $scripts -postScript DecompilePatchSites.java 0xf375 -deleteProject -analysisTimeoutPerFile 600 2>&1
Write-Output "########## GHIDRA DONE ##########"
