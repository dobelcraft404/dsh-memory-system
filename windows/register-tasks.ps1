# =============================================================================
# register-tasks.ps1
#
# 作用：在 Windows 任务计划中注册四个任务，作为 systemd timer 的等价物：
#   DSH-Memory-Consolidate   每天 03:00（错过则开机/登录后补跑，随机延迟 0~30 分钟）
#   DSH-Memory-SessionFlush  开机 5 分钟后 + 之后每小时检查一次
#   DSH-Memory-Produce       每 3 小时把新增会话消息提炼成 short-term（脚本内部去重，
#                            无新消息时不调用模型）
#   DSH-Memory-Backup        每天 03:40 增量备份到 F 盘（整理完成后）
#
# 特点：
#   - 不需要管理员权限（当前用户任务）；
#   - 不需要密码；
#   - 任务动作经 wscript.exe + run-hidden.vbs 静默启动（无控制台窗口，不抢焦点）；
#   - 任务动作调用 windows\run-*.ps1 / backup-memory.ps1，Python 路径自动探测；
#   - consolidate 脚本内部 6 小时幂等，重复触发会跳过。
#
# 用法：
#   pwsh -NoProfile -File D:\dsh-memory-system\windows\register-tasks.ps1
#   pwsh -NoProfile -File .\register-tasks.ps1 -Root 'F:\dsh-memory-system'
# =============================================================================

[CmdletBinding()]
param(
    [string]$Root = 'D:\dsh-memory-system',
    [string]$Python = '',
    [string]$BackupDestination = 'F:\dsh-memory-backup'
)

$ErrorActionPreference = 'Stop'

function Write-Info([string]$Message) { Write-Host "[tasks] $Message" }
function Write-Warn([string]$Message) { Write-Warning "[tasks] $Message" }

$runnerConsolidate = Join-Path $Root 'windows\run-consolidate.ps1'
$runnerProduce = Join-Path $Root 'windows\run-produce.ps1'
$scriptBackup = Join-Path $Root 'windows\backup-memory.ps1'
foreach ($required in @($runnerConsolidate, $runnerProduce, $scriptBackup)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "找不到任务入口：$required"
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $Root 'consolidate_memory.py'))) {
    throw "找不到 consolidate_memory.py（先完成初始化）"
}
if (-not (Test-Path -LiteralPath (Join-Path $Root 'produce_short_term.py'))) {
    throw "找不到 produce_short_term.py（先完成初始化）"
}

# 用当前 PowerShell 的可执行文件，避免任务计划里 PATH 不同。
$pwshPath = (Get-Process -Id $PID).Path
if (-not $pwshPath -or -not (Test-Path -LiteralPath $pwshPath)) {
    $pwshPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
}

# 2026-11-08：静默启动器。任务计划直接跑 powershell.exe 时，即使加了
# -WindowStyle Hidden 也会先弹出一个控制台窗口再隐藏，全屏游戏里会闪一下并造成失焦。
# 改为 wscript.exe + run-hidden.vbs（WshShell.Run style=0），全程没有控制台窗口。
$hiddenLauncher = Join-Path $Root 'windows\run-hidden.vbs'
$wscriptPath = Join-Path $env:SystemRoot 'System32\wscript.exe'
foreach ($silentRequired in @($hiddenLauncher, $wscriptPath)) {
    if (-not (Test-Path -LiteralPath $silentRequired)) {
        throw "静默启动器缺失：$silentRequired"
    }
}

function New-Action {
    param([string]$ScriptPath, [string[]]$ScriptArgs = @())
    $argument = @(
        '-NoProfile',
        '-NonInteractive',
        '-WindowStyle', 'Hidden',
        '-ExecutionPolicy', 'Bypass',
        '-File', "`"$ScriptPath`""
    ) + $ScriptArgs
    if ($Python) {
        $argument += @('-Python', "`"$Python`"")
    }
    $vbsArgs = @('//B', '//Nologo', "`"$hiddenLauncher`"", "`"$Root`"", "`"$pwshPath`"") + $argument
    return New-ScheduledTaskAction `
        -Execute $wscriptPath `
        -Argument ($vbsArgs -join ' ') `
        -WorkingDirectory $Root
}

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -Hidden `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

# ---------------------------------------------------------------------------
# 每日整理：03:00，随机延迟 0~30 分钟，错过补跑
# ---------------------------------------------------------------------------
$dailyTrigger = New-ScheduledTaskTrigger -Daily -At '03:00'
try {
    # CIM 属性是 ISO 8601 duration；失败时退化为无随机延迟，不阻塞注册。
    $dailyTrigger.RandomDelay = 'PT30M'
} catch {
    Write-Warn "无法设置每日任务随机延迟：$($_.Exception.Message)"
}

$dailyAction = New-Action -ScriptPath $runnerConsolidate -ScriptArgs @(
    '-Root', "`"$Root`"", '-Reason', 'daily'
)
Register-ScheduledTask `
    -TaskName 'DSH-Memory-Consolidate' `
    -Action $dailyAction `
    -Trigger $dailyTrigger `
    -Settings $settings `
    -Description 'DSH 记忆整理：每天 03:00，错过补跑，随机延迟 0~30 分钟' `
    -Force | Out-Null
Write-Info '已注册 DSH-Memory-Consolidate'

# ---------------------------------------------------------------------------
# 会话补跑：登录 5 分钟后 + 每小时检查
# 说明：AtStartup 触发需要管理员权限；普通用户改用 AtLogOn，
#       对“非 24 小时开机”的场景等价，且无需提权。
# ---------------------------------------------------------------------------
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$bootTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
try {
    $bootTrigger.Delay = 'PT5M'
} catch {
    Write-Warn "无法设置登录延迟：$($_.Exception.Message)"
}

$hourlyTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(5) `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$flushAction = New-Action -ScriptPath $runnerConsolidate -ScriptArgs @(
    '-Root', "`"$Root`"", '-Reason', 'session-flush'
)
Register-ScheduledTask `
    -TaskName 'DSH-Memory-SessionFlush' `
    -Action $flushAction `
    -Trigger @($bootTrigger, $hourlyTrigger) `
    -Settings $settings `
    -Description 'DSH 记忆补跑：登录 5 分钟后 + 每小时检查；脚本内 6 小时幂等' `
    -Force | Out-Null
Write-Info '已注册 DSH-Memory-SessionFlush'

# ---------------------------------------------------------------------------
# 短期记忆生产：每 3 小时。
# 生产者在脚本内部按“每个会话的 max_seq + 命题哈希”去重，空跑只解压最近变更的
# 会话，开销可控；没有任何新消息时直接跳过，不会调用模型。
# 3 小时节奏是为了减少碎片：30 分钟一次会把同一段对话切成多批，产生大量近义条目。
# ---------------------------------------------------------------------------
$produceTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At ((Get-Date).AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Hours 3) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$produceAction = New-Action -ScriptPath $runnerProduce -ScriptArgs @('-Root', "`"$Root`"")
Register-ScheduledTask `
    -TaskName 'DSH-Memory-Produce' `
    -Action $produceAction `
    -Trigger $produceTrigger `
    -Settings $settings `
    -Description 'DSH 短期记忆生产：每 3 小时扫描新增会话，无新消息不调模型' `
    -Force | Out-Null
Write-Info '已注册 DSH-Memory-Produce'

# ---------------------------------------------------------------------------
# 增量备份：03:40（整理之后），随机延迟 0~10 分钟
# ---------------------------------------------------------------------------
$backupTrigger = New-ScheduledTaskTrigger -Daily -At '03:40'
try {
    $backupTrigger.RandomDelay = 'PT10M'
} catch {
    Write-Warn "无法设置备份随机延迟：$($_.Exception.Message)"
}

$backupAction = New-Action -ScriptPath $scriptBackup -ScriptArgs @(
    '-Root', "`"$Root`"", '-Destination', "`"$BackupDestination`""
)
Register-ScheduledTask `
    -TaskName 'DSH-Memory-Backup' `
    -Action $backupAction `
    -Trigger $backupTrigger `
    -Settings $settings `
    -Description "DSH 记忆增量备份：每天 03:40 -> $BackupDestination（robocopy /E /XO，不删除）" `
    -Force | Out-Null
Write-Info '已注册 DSH-Memory-Backup'

Write-Info '完成。检查：Get-ScheduledTask -TaskName "DSH-Memory-*"'
Write-Info '立即测试：Start-ScheduledTask -TaskName "DSH-Memory-Produce"'
Write-Info '查看日志：Get-Content "$Root\logs\task-stdout.log" -Tail 50'
