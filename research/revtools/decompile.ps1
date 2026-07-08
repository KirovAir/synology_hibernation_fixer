# Fast decompile against the persistent analyzed project (no re-analysis).
# Usage: decompile.ps1 <scemd|synostoraged> off:0x20104 name:Foo ...
param([Parameter(Mandatory=$true)][string]$bin,
      [Parameter(ValueFromRemainingArguments=$true)][string[]]$targets)
$env:JAVA_HOME = "C:\Users\Jesse\AppData\Local\revtools\jdk\jdk-21.0.11+10"
$ah = "C:\Users\Jesse\AppData\Local\revtools\ghidra\ghidra_12.1.2_PUBLIC\support\analyzeHeadless.bat"
$scripts = "C:\Users\Jesse\AppData\Local\revtools\scripts"
$proj = "C:\Users\Jesse\AppData\Local\revtools\persist"
& $ah $proj hib -process $bin -noanalysis -scriptPath $scripts -postScript DumpFuncs.java @targets 2>&1
