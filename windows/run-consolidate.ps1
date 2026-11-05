# =============================================================================
# run-consolidate.ps1
#
# 作用：Windows 任务计划的统一入口。找到 Python，调用 consolidate_memory.py，
#       把 stdout/stderr 追加到 logs\task-stdout.log，返回脚本退出码。
#
# 设计意图：
#   - 任务计划只依赖 PowerShell，不依赖 PATH 里的 python；
#   - Python 自己的日志写 logs\consolidate.log，这里再兜一层未捕获输出；
#   - 脚本内部有 6 小时幂等检查，重复触发不会重复整理；
#   - 启动前从 state\llm.json 注入 OpenAI 兼容 LLM 配置（本机没有 Ollama）。
# =============================================================================

[CmdletBinding()]
param(
    [string]$Root = 'D:\dsh-memory-system',
    [ValidateSet('daily', 'session-flush', 'manual')]
    [string]$Reason = 'manual',
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'

function Test-PythonHealth {
    # 只检查“存在”不够：2026-11-05 的事故后 Python311 的 Lib 里
    # encodings/json/logging/re 被删，解释器能打印 --version 却无法 import。
    # 这里真的 import 一次，坏解释器直接跳过。
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
    "[{0}] ERROR: 找不到 python.exe（可用 -Python 指定）" -f (Get-Date -Format o) |
        Out-File -LiteralPath $taskLog -Append -Encoding utf8
    exit 127
}

# 从 state\llm.json 注入 OpenAI 兼容 LLM 配置（deepseek-flash）。
# 为什么：本机没有 Ollama；consolidate_memory.py 优先读 MEMORY_LLM_* 环境变量。
$llmConfig = Join-Path $Root 'state\llm.json'
if (Test-Path -LiteralPath $llmConfig) {
    try {
        $cfg = Get-Content -LiteralPath $llmConfig -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($cfg.base_url -and $cfg.api_key -and $cfg.model) {
            $env:MEMORY_LLM_PROVIDER = 'openai'
            $env:MEMORY_LLM_BASE_URL = [string]$cfg.base_url
            $env:MEMORY_LLM_API_KEY = [string]$cfg.api_key
            $env:MEMORY_LLM_MODEL = [string]$cfg.model
        } else {
            "[{0}] WARN: llm.json 字段不完整，跳过 LLM 注入" -f (Get-Date -Format o) |
                Out-File -LiteralPath $taskLog -Append -Encoding utf8
        }
    } catch {
        "[{0}] WARN: llm.json 解析失败：{1}" -f (Get-Date -Format o), $_.Exception.Message |
            Out-File -LiteralPath $taskLog -Append -Encoding utf8
    }
}

$scriptPath = Join-Path $Root 'consolidate_memory.py'
if (-not (Test-Path -LiteralPath $scriptPath)) {
    "[{0}] ERROR: 缺少 {1}" -f (Get-Date -Format o), $scriptPath |
        Out-File -LiteralPath $taskLog -Append -Encoding utf8
    exit 2
}

"[{0}] START reason={1} python={2}" -f (Get-Date -Format o), $Reason, $pythonPath |
    Out-File -LiteralPath $taskLog -Append -Encoding utf8

try {
    # 统一用 UTF-8 追加；PS 5.1 的 *>> 默认写 UTF-16LE，
    # 且原生程序输出按控制台代码页解码，会与 UTF-8 日志混成乱码。
    $previousConsoleEncoding = [Console]::OutputEncoding
    [Console]::OutputEncoding = [Text.Encoding]::UTF8
    $OutputEncoding = [Text.Encoding]::UTF8
    $env:PYTHONIOENCODING = 'utf-8'
    & $pythonPath -X utf8 $scriptPath --root $Root --reason $Reason 2>&1 |
        Out-File -LiteralPath $taskLog -Append -Encoding utf8
    $code = $LASTEXITCODE
    [Console]::OutputEncoding = $previousConsoleEncoding
    "[{0}] END reason={1} exit={2}" -f (Get-Date -Format o), $Reason, $code |
        Out-File -LiteralPath $taskLog -Append -Encoding utf8
    exit $code
} catch {
    "[{0}] EXCEPTION: {1}" -f (Get-Date -Format o), $_.Exception.Message |
        Out-File -LiteralPath $taskLog -Append -Encoding utf8
    exit 1
}
