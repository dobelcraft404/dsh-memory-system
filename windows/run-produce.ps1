# =============================================================================
# run-produce.ps1
#
# 作用：Windows 任务计划入口。找到健康的 Python，调用 produce_short_term.py，
#       把新增会话消息提炼成 short-term/*.md。
#
# 与 run-consolidate.ps1 的区别：本脚本不做 6 小时幂等（生产者在脚本内部按
# “每个会话的 max_seq”去重），没有新消息时一个模型请求都不发；计划任务每 3 小时跑一次。
# =============================================================================

[CmdletBinding()]
param(
    [string]$Root = 'D:\dsh-memory-system',
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'

function Test-PythonHealth {
    # 2026-11-05 事故后 Python311 无法 import encodings 等模块；只判“存在”会误选。
    param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $false }
    try {
        $probe = & $Path -c "import encodings, json, logging, re; print('ok')" 2>$null
        return ($LASTEXITCODE -eq 0 -and (($probe -join '') -match 'ok'))
    } catch {
        return $false
    }
}

function Resolve-Python {
    param([string]$Explicit)
    $candidates = @()
    if ($Explicit) { $candidates += $Explicit }
    $candidates += @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe')
    )
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
    foreach ($candidate in $candidates) {
        if (Test-PythonHealth -Path $candidate) { return $candidate }
    }
    return $null
}

$logDir = Join-Path $Root 'logs'
if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}
$taskLog = Join-Path $logDir 'task-stdout.log'

$pythonPath = Resolve-Python -Explicit $Python
if (-not $pythonPath) {
    "[{0}] ERROR: produce 找不到 python.exe（可用 -Python 指定）" -f (Get-Date -Format o) |
        Out-File -LiteralPath $taskLog -Append -Encoding utf8
    exit 127
}

$scriptPath = Join-Path $Root 'produce_short_term.py'
if (-not (Test-Path -LiteralPath $scriptPath)) {
    "[{0}] ERROR: 缺少 {1}" -f (Get-Date -Format o), $scriptPath |
        Out-File -LiteralPath $taskLog -Append -Encoding utf8
    exit 2
}

"[{0}] PRODUCE START python={1}" -f (Get-Date -Format o), $pythonPath |
    Out-File -LiteralPath $taskLog -Append -Encoding utf8

try {
    $previousConsoleEncoding = [Console]::OutputEncoding
    [Console]::OutputEncoding = [Text.Encoding]::UTF8
    $OutputEncoding = [Text.Encoding]::UTF8
    $env:PYTHONIOENCODING = 'utf-8'
    & $pythonPath -X utf8 $scriptPath --root $Root 2>&1 |
        Out-File -LiteralPath $taskLog -Append -Encoding utf8
    $code = $LASTEXITCODE
    [Console]::OutputEncoding = $previousConsoleEncoding
    "[{0}] PRODUCE END exit={1}" -f (Get-Date -Format o), $code |
        Out-File -LiteralPath $taskLog -Append -Encoding utf8
    exit $code
} catch {
    "[{0}] PRODUCE EXCEPTION: {1}" -f (Get-Date -Format o), $_.Exception.Message |
        Out-File -LiteralPath $taskLog -Append -Encoding utf8
    exit 1
}
