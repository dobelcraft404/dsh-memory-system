$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$log = 'E:\ai-talk\_perm\grant-elevated.log'
function Log($m) { $m | Tee-Object -FilePath $log -Append }

Log "start $(Get-Date -Format o)"
Log ("admin=" + ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole('Administrators'))

& node 'E:\ai-talk\_perm\grant.mjs' *>&1 | ForEach-Object { Log $_ }

Log '--- icacls 检查 ---'
& icacls 'D:\dsh-memory-system' *>&1 | Select-String -Pattern 'Mandatory|S-1-16' | ForEach-Object { Log $_.Line.Trim() }
Log "done $(Get-Date -Format o)"
