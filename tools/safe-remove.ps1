<#
.SYNOPSIS
    安全递归删除封装：任何 Remove-Item -Recurse -Force 之前必须先过这一关。

.DESCRIPTION
    针对 2026-11-05 事故（脚本里 `$home` 赋值失败后 Remove-Item 删掉整个 C:\Users\<user>）。
    校验项（任一不满足直接拒绝，退出码 2）：
      1. 路径必须是绝对路径，不得含通配符 / '..' / 环境变量残留，不得以 '\' 结尾。
      2. 目标不得是：盘符根、C:\Windows、C:\Program Files(  (x86))、C:\ProgramData、C:\Users。
      3. 目标不得等于用户目录根（$env:USERPROFILE）。
      4. 目标不得是用户目录的直接子项（C:\Users\<你>\AppData、.dsh、Documents ... 一律拒绝）。
      5. 必须显式传 -AllowRoot，且目标必须严格位于 AllowRoot 之下、与 AllowRoot 至少差 1 层。
      6. AllowRoot 本身不能是上面任何受保护路径。
    默认只预览；加 -Force 才真正删除。删除前会校验一次目标仍然存在且仍是目录/文件本身。

.PARAMETER Path
    要删除的目录或文件（绝对路径）。

.PARAMETER AllowRoot
    你认定这次清理的合法根目录，例如 D:\my-project。目标必须在其下。

.PARAMETER Force
    真正执行删除。不加时只打印将被删除的内容。

.EXAMPLE
    # 预览
    powershell -NoProfile -File D:\dsh-memory-system\tools\safe-remove.ps1 -Path D:\my-project\tests\tmp-x -AllowRoot D:\my-project
    # 真删
    powershell -NoProfile -File D:\dsh-memory-system\tools\safe-remove.ps1 -Path D:\my-project\tests\tmp-x -AllowRoot D:\my-project -Force
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$AllowRoot,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

function Fail([string]$Why) {
    Write-Host "[safe-remove] 拒绝: $Why" -ForegroundColor Red
    Write-Host "[safe-remove] 目标: $Path" -ForegroundColor Red
    exit 2
}

# ---------- 规范化 ----------
if ([string]::IsNullOrWhiteSpace($Path)) { Fail 'Path 为空' }
if ($Path -match '[*?]') { Fail '路径含通配符' }
if ($Path -match '(^|\\)\.\.(\\|$)') { Fail '路径含 ..' }
if (-not [System.IO.Path]::IsPathRooted($Path)) { Fail '不是绝对路径' }

$target = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
$root   = [System.IO.Path]::GetFullPath($AllowRoot).TrimEnd('\')
if ($target.Length -le 3) { Fail '目标等于盘符根' }
if ($target -match '\$|%') { Fail '路径含未展开的变量/百分号' }

$profileDir = [System.IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd('\')
$protected = @(
    'C:\Windows',
    'C:\Program Files',
    'C:\Program Files (x86)',
    'C:\ProgramData'
)

function Is-Same([string]$a, [string]$b) { $a.TrimEnd('\') -ieq $b.TrimEnd('\') }
function Is-Under([string]$child, [string]$parent) {
    return $child.TrimEnd('\').ToLowerInvariant().StartsWith($parent.TrimEnd('\').ToLowerInvariant() + '\')
}

# ---------- 规则 ----------
if (Is-Same $target 'C:\Users') { Fail '目标是 C:\Users 根' }
if (Is-Same $target $profileDir) { Fail '目标是用户目录根（$env:USERPROFILE）——正是 2026-11-05 事故的形态' }
if (Is-Under $profileDir $target) { Fail '目标是用户目录的祖先' }

foreach ($p in $protected) {
    if (Is-Same $target $p) { Fail "目标是受保护的系统目录: $p" }
    if (Is-Under $target $p) { Fail "目标位于受保护的系统目录内: $p" }
    if (Is-Same $root $p)  { Fail "AllowRoot 是受保护的系统目录: $p" }
    if (Is-Under $root $p) { Fail "AllowRoot 位于受保护的系统目录内: $p" }
}

# 用户目录的直接子项（C:\Users\<name>\AppData 等）：一律拒绝
if (Is-Under $target $profileDir) {
    $rel = $target.Substring($profileDir.Length).Trim('\')
    $depth = ($rel -split '\\').Count
    if ($depth -le 1) { Fail '目标是用户目录的直接子项（如 AppData / .dsh / Documents），事故级别操作' }
}

if ([string]::IsNullOrWhiteSpace($root)) { Fail 'AllowRoot 为空' }
if (Is-Same $target $root) { Fail '目标不能等于 AllowRoot 本身' }
if (-not (Is-Under $target $root)) { Fail "目标不在 AllowRoot 之下 (AllowRoot=$root)" }

$targetDepth = ($target -split '\\').Count
$rootDepth   = ($root -split '\\').Count
if ($targetDepth -le $rootDepth) { Fail '目标层级不深于 AllowRoot' }

# ---------- 执行 ----------
if (-not (Test-Path -LiteralPath $target)) {
    Write-Host "[safe-remove] 目标不存在，无需删除: $target" -ForegroundColor Yellow
    exit 0
}

$item = Get-Item -LiteralPath $target -Force
if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
    Write-Host "[safe-remove] 目标是重解析点（ junction/symlink ），只删链接本身: $target" -ForegroundColor Yellow
    if ($Force) { (Get-Item -LiteralPath $target -Force).Delete() }
    exit 0
}

$stat = Get-ChildItem -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue
$fileCount = ($stat | Where-Object { -not $_.PSIsContainer }).Count
$dirCount  = ($stat | Where-Object { $_.PSIsContainer }).Count
$bytes     = ($stat | Where-Object { -not $_.PSIsContainer } | Measure-Object -Property Length -Sum).Sum
if (-not $bytes) { $bytes = 0 }

Write-Host "[safe-remove] 目标   : $target" -ForegroundColor Cyan
Write-Host "[safe-remove] AllowRoot: $root" -ForegroundColor Cyan
Write-Host ("[safe-remove] 规模   : {0} 个文件, {1} 个子目录, {2:N1} MB" -f $fileCount, $dirCount, ($bytes / 1MB)) -ForegroundColor Cyan
$stat | Select-Object -First 15 | ForEach-Object { "  样例: $($_.FullName)" }

if (-not $Force) {
    Write-Host "[safe-remove] 预览模式，未删除。确认无误后加 -Force 真删。" -ForegroundColor Yellow
    exit 0
}

# 真删前再校验一次：目标解析后未变、且不是受保护路径
$recheck = [System.IO.Path]::GetFullPath($target).TrimEnd('\')
if (-not (Is-Same $recheck $target)) { Fail '目标在检查后被改变' }

Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction Stop
if (Test-Path -LiteralPath $target) {
    Write-Host "[safe-remove] 删除未完成（仍有残留，可能被占用）: $target" -ForegroundColor Red
    exit 3
}
Write-Host "[safe-remove] 已删除: $target" -ForegroundColor Green
exit 0
