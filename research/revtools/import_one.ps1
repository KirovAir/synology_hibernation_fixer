# Import + analyze ONE binary into the persistent project.
# Usage: import_one.ps1 <path-to-binary>
param([Parameter(Mandatory=$true)][string]$bin)
$env:JAVA_HOME = "C:\Users\Jesse\AppData\Local\revtools\jdk\jdk-21.0.11+10"
$ah = "C:\Users\Jesse\AppData\Local\revtools\ghidra\ghidra_12.1.2_PUBLIC\support\analyzeHeadless.bat"
$proj = "C:\Users\Jesse\AppData\Local\revtools\persist"
New-Item -ItemType Directory -Force $proj | Out-Null
& $ah $proj hib -import "$bin" -analysisTimeoutPerFile 900 2>&1
Write-Output "IMPORT DONE: $bin"
