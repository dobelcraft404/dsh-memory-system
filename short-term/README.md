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