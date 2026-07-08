$env:JAVA_HOME = "C:\Users\Jesse\AppData\Local\revtools\jdk\jdk-21.0.11+10"
$ah = "C:\Users\Jesse\AppData\Local\revtools\ghidra\ghidra_12.1.2_PUBLIC\support\analyzeHeadless.bat"
$scripts = "C:\Users\Jesse\AppData\Local\revtools\scripts"
$bins = "C:\Users\Jesse\dev\__tmp\synology_hibernation_fixer\research\dsm-7.3.2\bins"
$proj = "C:\Users\Jesse\AppData\Local\revtools\proj2"
New-Item -ItemType Directory -Force $proj | Out-Null
& $ah $proj scemd -import "$bins\scemd" -scriptPath $scripts -postScript AnalyzeHibernation.java -deleteProject -analysisTimeoutPerFile 600 2>&1 | Out-File -Encoding utf8 "C:\Users\Jesse\dev\__tmp\synology_hibernation_fixer\research\dsm-7.3.2\ghidra_survey\scemd_survey.txt"
& $ah $proj storaged -import "$bins\synostoraged" -scriptPath $scripts -postScript AnalyzeHibernation.java -deleteProject -analysisTimeoutPerFile 600 2>&1 | Out-File -Encoding utf8 "C:\Users\Jesse\dev\__tmp\synology_hibernation_fixer\research\dsm-7.3.2\ghidra_survey\synostoraged_survey.txt"
Write-Output "GHIDRA SURVEY DONE"
