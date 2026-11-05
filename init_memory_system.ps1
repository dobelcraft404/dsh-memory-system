# =============================================================================
# init_memory_system.ps1
#
# 作用：Windows 下初始化 DSH 记忆与归档系统。
#   - 数据目录默认 D:\dsh-memory-system（可用 -MemoryRoot 覆盖）
#   - 建立兼容链接 C:\Users\<你>\.dsh\memory-system -> 数据目录
#     （C 盘只放 junction，实际数据在 D/F 盘）
#   - 只创建缺失文件，绝不覆盖已有记忆/归档
#
# 用法（PowerShell 7）：
#   pwsh -NoProfile -File D:\dsh-memory-system\init_memory_system.ps1
#   pwsh -NoProfile -File .\init_memory_system.ps1 -MemoryRoot 'F:\dsh-memory-system'
# =============================================================================

[CmdletBinding()]
param(
    [string]$MemoryRoot = 'D:\dsh-memory-system',
    [string]$DshHome = (Join-Path $env:USERPROFILE '.dsh')
)

$ErrorActionPreference = 'Stop'
$ScriptDir = $PSScriptRoot
$LinkPath = Join-Path $DshHome 'memory-system'

function Write-Info([string]$Message) { Write-Host "[init] $Message" }
function Write-Warn([string]$Message) { Write-Warning "[init] $Message" }

function Write-Utf8NoBom {
    param([string]$Path, [string]$Text)
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    [System.IO.File]::WriteAllText($Path, $Text, [System.Text.UTF8Encoding]::new($false))
}

function Write-IfMissing {
    param([string]$Path, [string]$Text)
    if (Test-Path -LiteralPath $Path) {
        Write-Info "保留现有文件: $Path"
        return
    }
    Write-Utf8NoBom -Path $Path -Text $Text
    Write-Info "创建: $Path"
}

function Install-Template {
    param([string]$Name)
    $source = Join-Path $ScriptDir $Name
    $target = Join-Path $MemoryRoot $Name
    if (Test-Path -LiteralPath $target) {
        Write-Info "保留现有文件: $target"
        return
    }
    if (-not (Test-Path -LiteralPath $source)) {
        Write-Warn "缺少模板 $Name（应在 $ScriptDir 或目标目录中），跳过"
        return
    }
    $sourceFull = [System.IO.Path]::GetFullPath($source)
    $targetFull = [System.IO.Path]::GetFullPath($target)
    if ($sourceFull -ne $targetFull) {
        Copy-Item -LiteralPath $source -Destination $target
        Write-Info "安装: $target"
    }
}

# ---------------------------------------------------------------------------
# 1. 数据目录与 junction
# ---------------------------------------------------------------------------
if (-not (Test-Path -LiteralPath $MemoryRoot)) {
    New-Item -ItemType Directory -Path $MemoryRoot -Force | Out-Null
    Write-Info "创建数据目录: $MemoryRoot"
} else {
    Write-Info "数据目录已存在: $MemoryRoot"
}

if (-not (Test-Path -LiteralPath $DshHome)) {
    New-Item -ItemType Directory -Path $DshHome -Force | Out-Null
}

$rootFull = [System.IO.Path]::GetFullPath($MemoryRoot).TrimEnd('\')
$linkFull = [System.IO.Path]::GetFullPath($LinkPath).TrimEnd('\')

if ($rootFull -ne $linkFull) {
    if (Test-Path -LiteralPath $LinkPath) {
        $item = Get-Item -LiteralPath $LinkPath -Force
        if ($item.LinkType -eq 'Junction') {
            $target = @($item.Target)[0]
            if ($target -and ([System.IO.Path]::GetFullPath($target).TrimEnd('\') -eq $rootFull)) {
                Write-Info "junction 已就绪: $LinkPath -> $rootFull"
            } else {
                Write-Warn "junction 指向其他位置: $LinkPath -> $target（未自动修改，请手动确认）"
            }
        } else {
            Write-Warn "路径已存在但不是 junction: $LinkPath（未自动删除，请先确认其中数据）"
        }
    } else {
        New-Item -ItemType Junction -Path $LinkPath -Target $rootFull | Out-Null
        Write-Info "创建 junction: $LinkPath -> $rootFull"
    }
} else {
    Write-Info "数据目录与兼容路径相同，跳过 junction"
}

# ---------------------------------------------------------------------------
# 2. 目录结构
# ---------------------------------------------------------------------------
$directories = @(
    (Join-Path $MemoryRoot 'long-term\entries'),
    (Join-Path $MemoryRoot 'mid-term\entries'),
    (Join-Path $MemoryRoot 'short-term'),
    (Join-Path $MemoryRoot 'archive\inbox'),
    (Join-Path $MemoryRoot 'state'),
    (Join-Path $MemoryRoot 'logs'),
    (Join-Path $MemoryRoot 'systemd'),
    (Join-Path $MemoryRoot 'windows')
)
foreach ($dir in $directories) {
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
}
Write-Info "目录已就绪（$($directories.Count) 个）"

# ---------------------------------------------------------------------------
# 3. 模板与初始文件（不覆盖）
# ---------------------------------------------------------------------------
Install-Template 'MEMORY-POLICY.md'
Install-Template 'EMOTION-STYLES.md'

$now = (Get-Date).ToString('o')
$emotionState = @"
---
emotion: "平静"
intensity: 0.50
updated_at: "$now"
ttl_hours: 24
reason: "初始状态"
---
# 当前情绪

平静，强度 0.50。情绪会按 TTL 周期自然衰减，由后台脚本计算，不消耗模型 Token。

> 本文件由 ``consolidate_memory.py`` 维护；主模型只读。手动调整时保持 frontmatter 字段名不变。
"@

$longTermIndex = @'
# 长期记忆索引

> 由 `consolidate_memory.py` 自动维护。主模型只读；检索请用 `get_memory_index`。

| id | title | type | tags | summary | confidence | updated_at | path |
| --- | --- | --- | --- | --- | --- | --- | --- |
'@

$midTermIndex = @'
# 中期记忆索引

> 由 `consolidate_memory.py` 自动维护。主模型只读；检索请用 `get_memory_index`。

| id | title | type | tags | summary | confidence | updated_at | path |
| --- | --- | --- | --- | --- | --- | --- | --- |
'@

$archiveIndex = @'
# 归档索引

> 由 `mcp__memory__archive_write` 自动维护。检索请用 `archive_search`，不要手工遍历目录。

| id | title | domain | subdomain | tags | summary | path | created_at | status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
'@

$suggestions = @'
# 待确认记忆与归档建议

> 后台整理脚本自动追加。确认后由用户/主模型决定是否保留；主模型不直接修改记忆层。
'@

$shortTermReadme = @'
# 短期记忆目录

`consolidate_memory.py` 只消费本目录中 `updated_at` 晚于上次整理时间戳的文件。
当前 DSH 会话日志是 `session.v4.jsonl.zstd`，不能直接当短期记忆使用；需要一个生产者把
“值得沉淀的对话片段”写成下面的格式（放到本目录任意子目录均可，建议 `YYYY-MM-DD/`）：

```markdown
---
id: "st-20261105-001"
created_at: "2026-11-05T10:00:00+08:00"
updated_at: "2026-11-05T10:05:00+08:00"
source: "dsh-session"
session_id: "session-xxxx"
tags: ["termux", "编译"]
---

用户/助手对话摘要或原始片段，建议一段一个文件，保留上下文。
```

字段约定：

- `updated_at` 必填，ISO 8601；脚本据此做增量。
- `id` 建议唯一；缺失时脚本按文件名生成。
- `source` 可选，用于追溯。
- 文件不要超过 256 KiB；长对话先摘要再写入。
'@

Write-IfMissing (Join-Path $MemoryRoot 'emotion-state.md') $emotionState
Write-IfMissing (Join-Path $MemoryRoot 'long-term\MEMORY-INDEX.md') $longTermIndex
Write-IfMissing (Join-Path $MemoryRoot 'mid-term\MEMORY-INDEX.md') $midTermIndex
Write-IfMissing (Join-Path $MemoryRoot 'archive\INDEX.md') $archiveIndex
Write-IfMissing (Join-Path $MemoryRoot 'SUGGESTIONS.md') $suggestions
Write-IfMissing (Join-Path $MemoryRoot 'short-term\README.md') $shortTermReadme

Write-Info '初始化完成。'
Write-Info '提示：主模型只读记忆层；归档写入请通过 MCP 工具 mcp__memory__archive_write。'
Write-Info '提示：注册 Windows 定时任务请运行 windows\register-tasks.ps1。'
