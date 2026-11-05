# =============================================================================
# backup-memory.ps1
#
# 作用：把记忆系统与 DSH 配置增量备份到 F 盘（robocopy /E /XO）。
#
# 设计意图：
#   - 只增量复制（/XO：源比目标新才覆盖），从不用 /MIR，避免源端误删把备份也删掉；
#   - 排除 .locks、__pycache__、测试临时目录和 state\llm.json（含 API Key，不进备份）；
#   - 退出码 0~7 都算成功（robocopy 约定：8 及以上才是失败）；
#   - 所有输出追加到 logs\backup.log。
#
# 用法：
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File backup-memory.ps1
#   ... -Destination 'F:\dsh-memory-backup' -DryRun
# =============================================================================

[CmdletBinding()]
param(
    [string]$Root = 'D:\dsh-memory-system',
    [string]$Destination = 'F:\dsh-memory-backup',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $Root)) {
    throw "源目录不存在：$Root"
}
if (-not (Test-Path -LiteralPath (Split-Path -Parent $Destination))) {
    throw "备份目标所在盘符不存在：$Destination"
}

$logDir = Join-Path $Root 'logs'
if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}
$backupLog = Join-Path $logDir 'backup.log'

$memoryTarget = Join-Path $Destination 'memory-system'
$dshTarget = Join-Path $Destination 'dsh'

function Write-Log([string]$Message) {
    "[{0}] {1}" -f (Get-Date -Format o), $Message |
        Out-File -LiteralPath $backupLog -Append -Encoding utf8
}

function Invoke-Robocopy {
    param(
        [string]$Source,
        [string]$Target,
        [string]$Label,
        [string[]]$Extra = @()
    )
    if (-not (Test-Path -LiteralPath $Source)) {
        Write-Log "SKIP $Label：源不存在 $Source"
        return 0
    }
    $arguments = @(
        "`"$Source`"", "`"$Target`"", '/E', '/XO', '/R:1', '/W:1', '/NP', '/NFL', '/NDL',
        "/LOG+:`"$backupLog`""
    ) + $Extra
    if ($DryRun) { $arguments += '/L' }
    Write-Log ("ROBOCOPY {0} {1} -> {2}" -f $Label, $Source, $Target)
    & robocopy.exe @arguments | Out-Null
    $code = $LASTEXITCODE
    if ($code -ge 8) {
        Write-Log ("ERROR {0} robocopy exit={1}" -f $Label, $code)
    } else {
        Write-Log ("OK {0} robocopy exit={1}" -f $Label, $code)
    }
    return $code
}

Write-Log ("BACKUP START dryRun={0} root={1} dest={2}" -f [bool]$DryRun, $Root, $Destination)

# 记忆系统：排除锁文件、字节码缓存、测试临时目录；llm.json 含密钥，不备份。
$excludeDirs = @(
    (Join-Path $Root '.locks'),
    (Join-Path $Root '__pycache__'),
    (Join-Path $Root 'tests\tmp-consolidate')
)
$excludeFiles = @('llm.json')
$memoryArgs = @()
foreach ($dir in $excludeDirs) { $memoryArgs += '/XD'; $memoryArgs += "`"$dir`"" }
foreach ($file in $excludeFiles) { $memoryArgs += '/XF'; $memoryArgs += "`"$file`"" }

$code1 = Invoke-Robocopy -Source $Root -Target $memoryTarget -Label 'memory-system' -Extra $memoryArgs

# DSH 配置：只备份 profiles（插件与 cordis 配置）和 AGENTS.md；sessions/storages 体积大且
# 含对话隐私，用户明确要求“不丢历史会话”时另行备份，本脚本默认不碰。
$dshProfiles = Join-Path $env:USERPROFILE '.dsh\profiles'
$code2 = Invoke-Robocopy -Source $dshProfiles -Target (Join-Path $dshTarget 'profiles') -Label 'dsh-profiles'

$agents = Join-Path $env:USERPROFILE '.dsh\AGENTS.md'
if (Test-Path -LiteralPath $agents) {
    $agentsTarget = Join-Path $dshTarget 'AGENTS.md'
    New-Item -ItemType Directory -Path $dshTarget -Force | Out-Null
    if (-not $DryRun) {
        Copy-Item -LiteralPath $agents -Destination $agentsTarget -Force
    }
    Write-Log "OK AGENTS.md -> $agentsTarget"
} else {
    Write-Log 'SKIP AGENTS.md：不存在'
}

$failed = @($code1, $code2) | Where-Object { $_ -ge 8 }
if ($failed.Count -gt 0) {
    Write-Log 'BACKUP END failed=1'
    exit 8
}
Write-Log 'BACKUP END failed=0'
exit 0
