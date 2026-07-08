# Import + analyze both binaries into a PERSISTENT project (once).
# After this, use decompile.ps1 with -process -noanalysis for fast iteration.
$env:JAVA_HOME = "C:\Users\Jesse\AppData\Local\revtools\jdk\jdk-21.0.11+10"
$ah = "C:\Users\Jesse\AppData\Local\revtools\ghidra\ghidra_12.1.2_PUBLIC\support\analyzeHeadless.bat"
$bins = "C:\Users\Jesse\dev\__tmp\synology_hibernation_fixer\research\dsm-7.3.2\bins"
$proj = "C:\Users\Jesse\AppData\Local\revtools\persist"
New-Item -ItemType Directory -Force $proj | Out-Null
Write-Output "########## IMPORT+ANALYZE scemd ##########"
& $ah $proj hib -import "$bins\scemd" -analysisTimeoutPerFile 900 2>&1
Write-Output "########## IMPORT+ANALYZE synostoraged ##########"
& $ah $proj hib -import "$bins\synostoraged" -analysisTimeoutPerFile 900 2>&1
Write-Output "########## IMPORT DONE ##########"
