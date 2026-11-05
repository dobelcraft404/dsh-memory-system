#!/usr/bin/env bash
# =============================================================================
# init_memory_system.sh
#
# 作用：初始化 DSH 记忆与归档系统的目录和初始文件。
# 适用：Linux / macOS / WSL（Windows 请用 init_memory_system.ps1）。
#
# 设计原则：
#   1. 幂等：已存在的文件绝不覆盖，避免误删记忆/归档。
#   2. 只创建缺失项；策略与情绪模板从脚本同目录复制。
#   3. 默认根目录 ~/.dsh/memory-system；可用 MEMORY_ROOT 覆盖。
#
# 用法：
#   bash init_memory_system.sh
#   MEMORY_ROOT=/data/dsh-memory-system bash init_memory_system.sh
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
MEMORY_ROOT="${MEMORY_ROOT:-$DSH_HOME/memory-system}"

# 规范化路径，便于判断“源文件是否就在目标目录里”。
MEMORY_ROOT="$(cd "$MEMORY_ROOT" 2>/dev/null && pwd || { mkdir -p "$MEMORY_ROOT"; cd "$MEMORY_ROOT" && pwd; })"

log() { printf '[init] %s\n' "$*"; }
warn() { printf '[init][warn] %s\n' "$*" >&2; }

log "memory root: $MEMORY_ROOT"

# ---------------------------------------------------------------------------
# 1. 目录结构
# ---------------------------------------------------------------------------
directories=(
  "$MEMORY_ROOT/long-term/entries"
  "$MEMORY_ROOT/mid-term/entries"
  "$MEMORY_ROOT/short-term"
  "$MEMORY_ROOT/archive/inbox"
  "$MEMORY_ROOT/state"
  "$MEMORY_ROOT/logs"
  "$MEMORY_ROOT/systemd"
  "$MEMORY_ROOT/windows"
)

for dir in "${directories[@]}"; do
  mkdir -p "$dir"
done
log "目录已就绪（${#directories[@]} 个）"

# ---------------------------------------------------------------------------
# 2. 固定模板文件：仅当目标不存在时写入
# ---------------------------------------------------------------------------
write_if_missing() {
  local target="$1"
  shift
  if [ -e "$target" ]; then
    log "保留现有文件: $target"
    return 0
  fi
  "$@" > "$target"
  log "创建: $target"
}

# 策略与情绪模板：优先从脚本同目录复制；源和目标相同则跳过。
install_template() {
  local name="$1"
  local source="$SCRIPT_DIR/$name"
  local target="$MEMORY_ROOT/$name"

  if [ -e "$target" ]; then
    log "保留现有文件: $target"
    return 0
  fi
  if [ -f "$source" ] && [ "$(cd "$(dirname "$source")" && pwd)/$(basename "$source")" != "$target" ]; then
    cp "$source" "$target"
    log "安装: $target"
  else
    warn "缺少模板 $name（应在 $SCRIPT_DIR 或目标目录中），跳过"
  fi
}

install_template "MEMORY-POLICY.md"
install_template "EMOTION-STYLES.md"

# 当前情绪：默认中性；不覆盖已有状态。
# updated_at 必须是初始化时刻，不能用 1970：否则首次整理会把 0.5 直接衰减到 0。
INIT_NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
write_if_missing "$MEMORY_ROOT/emotion-state.md" cat <<EOF
---
emotion: "平静"
intensity: 0.50
updated_at: "$INIT_NOW"
ttl_hours: 24
reason: "初始状态"
---
# 当前情绪

平静，强度 0.50。情绪会按 TTL 周期自然衰减，由后台脚本计算，不消耗模型 Token。

> 本文件由 \`consolidate_memory.py\` 维护；主模型只读。手动调整时保持 frontmatter 字段名不变。
EOF

write_if_missing "$MEMORY_ROOT/long-term/MEMORY-INDEX.md" cat <<'EOF'
# 长期记忆索引

> 由 `consolidate_memory.py` 自动维护。主模型只读；检索请用 `get_memory_index`。

| id | title | type | tags | summary | confidence | updated_at | path |
| --- | --- | --- | --- | --- | --- | --- | --- |
EOF

write_if_missing "$MEMORY_ROOT/mid-term/MEMORY-INDEX.md" cat <<'EOF'
# 中期记忆索引

> 由 `consolidate_memory.py` 自动维护。主模型只读；检索请用 `get_memory_index`。

| id | title | type | tags | summary | confidence | updated_at | path |
| --- | --- | --- | --- | --- | --- | --- | --- |
EOF

write_if_missing "$MEMORY_ROOT/archive/INDEX.md" cat <<'EOF'
# 归档索引

> 由 `mcp__memory__archive_write` 自动维护。检索请用 `archive_search`，不要手工遍历目录。

| id | title | domain | subdomain | tags | summary | path | created_at | status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
EOF

write_if_missing "$MEMORY_ROOT/SUGGESTIONS.md" cat <<'EOF'
# 待确认记忆与归档建议

> 后台整理脚本自动追加。确认后由用户/主模型决定是否保留；主模型不直接修改记忆层。
EOF

# ---------------------------------------------------------------------------
# 3. 短期记忆格式说明（不是数据，只是给生产者看的契约）
# ---------------------------------------------------------------------------
write_if_missing "$MEMORY_ROOT/short-term/README.md" cat <<'EOF'
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
EOF

# ---------------------------------------------------------------------------
# 4. 结果汇总
# ---------------------------------------------------------------------------
log "初始化完成。"
log "提示：主模型只读记忆层；归档写入请通过 MCP 工具 mcp__memory__archive_write。"
log "提示：systemd timer 需要 systemd 用户实例；无 systemd 的 Windows 请用 windows/ 下的任务计划脚本。"
