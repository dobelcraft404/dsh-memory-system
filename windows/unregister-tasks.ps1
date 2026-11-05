# =============================================================================
# unregister-tasks.ps1
#
# 作用：删除本系统注册的两个 Windows 任务计划。
# 不会删除记忆/归档数据。
# =============================================================================

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$names = @('DSH-Memory-Consolidate', 'DSH-Memory-SessionFlush')

foreach ($name in $names) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Host "[tasks] 不存在，跳过: $name"
        continue
    }
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Write-Host "[tasks] 已删除: $name"
}

Write-Host '[tasks] 完成。数据目录未改动。'
