# DSH 本地记忆与归档系统（DSH Memory & Archive）

> **AI 生成声明（必读）**
> 本仓库的代码、脚本与文档由 AI 助手（DeepSeek Harness 中的「小肥鱼」）生成，经用户人工审阅并在 Windows 11 + Python 3.13 上实际部署验证。
> 记忆条目、情绪状态、关系档案等内容同样由 AI 后台脚本自动生成。
> 因此：内容可能包含错误、过时信息或不适用于你环境的假设；请自行审阅后再使用。本项目按「现状」提供，不附带任何担保。

一个面向 [DeepSeek Harness](https://github.com/deepseek-ai) 的本地记忆与归档系统，目标是让 AI 助手**有记忆、有情绪、有延续感**，同时把算力消耗控制在最低。

## 它解决什么问题

普通对话式 AI 每次会话都从零开始。本项目用两层结构补上这件事：

- **记忆层**（长期 / 中期 / 短期）：由后台定时任务增量整理，主模型**只读**。
- **归档层**（笔记库）：主模型在对话中自主分类、写入、按索引检索。

核心约束：

- 记忆整理完全剥离主对话，只处理新增内容（增量），且 6 小时幂等。
- 归档检索只走索引，禁止遍历目录。
- 情绪衰减、记忆衰减都是纯代码计算，零 Token。
- System Prompt 与工具定义全程静态，动态内容只通过工具结果注入 —— 对 Prompt Caching 友好。
- 工具按名称字母序注册，返回字段顺序固定。

## 架构

```
short-term/ ──(produce_short_term.py，调模型)──> 短期记忆
     │
     └─(consolidate_memory.py，6h 幂等)─> 规则裁决 ─> long-term/ / SUGGESTIONS.md
                                              │
                                      记忆衰减（30 天 ×0.8）
                                              │
                                      mid-term/ ─> 退出索引（文件保留）

archive/ <──(主模型 archive_write，自主分类)── 归档笔记 + INDEX.md
```

## 快速开始

### Windows

```powershell
# 1. 初始化目录与空索引（不覆盖已有文件）
powershell -ExecutionPolicy Bypass -File .\init_memory_system.ps1 -MemoryRoot 'D:\dsh-memory-system'

# 2. 配置模型（可选，用于短期记忆提炼）
#    在 state\llm.json 写入 provider / base_url / model / api_key，
#    并建议收紧 ACL：icacls ... /inheritance:r

# 3. 注册计划任务（整理 / 补跑 / 生产 / 备份）
powershell -ExecutionPolicy Bypass -File .\windows\register-tasks.ps1
```

### Linux / macOS / WSL

```bash
bash ./init_memory_system.sh
# systemd 用户定时器：systemd/ 目录下四个文件
systemctl --user enable --now memory-consolidate.timer memory-session-flush.timer
```

### 接入 DSH（MCP）

在 profile 的 `cordis.patch.yml` 中用 `insert` 增加 stdio MCP 服务，指向 `memory_system.py`：

```yaml
- insert:
    - id: mcp-memory
      name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: memory
        transport: stdio
        command: "C:\\path\\to\\python.exe"
        args: ["-X", "utf8", "D:\\dsh-memory-system\\memory_system.py", "--root", "D:\\dsh-memory-system"]
        toolCallTimeoutMs: 120000
```

重启 DSH 后，工具会以 `mcp__memory__*` 的形式出现。

## 六个工具

| 工具 | 作用 |
| --- | --- |
| `archive_write` | 归档写入：建 `archive/<domain>/<subdomain>/YYYY-MM/`、写 frontmatter、追加索引 |
| `archive_search` | 只读 `archive/INDEX.md`，按摘要/标签匹配 |
| `archive_read` | 读取指定归档全文（单轮最多 3 次由策略约束） |
| `get_memory_index` | 读取长期/中期记忆索引 |
| `read_memory_entry` | 按 id 读记忆全文，并登记访问（供衰减退化用） |
| `read_emotion_state` | 一次读取情绪、风格提示、关系档案、上次会话接续句 |

## 记忆衰减（用进废退）

- 每 30 天一个周期，置信度 × 0.8；`read_memory_entry` 的真实读取会刷新“最近使用时间”。
- 长期跌破 0.35 → 降级进中期索引；中期跌破 0.20 → 退出索引（frontmatter 标记 archived）。
- **不删文件**，只动索引和 frontmatter，误判可恢复。

## 情绪、关系档案与会话接续

- 生产者一次模型调用同时产出：短期记忆、情绪事件（强度封顶 0.9）、关系档案重写、会话接续句。
- 情绪衰减由整理脚本按 TTL 执行：`新强度 = 旧强度 × 0.7^周期数`，低于 0.15 归零。
- 关系档案 `persona.md` 与接续句 `state/last_session.json` 由后台维护，主模型只读。

## 目录结构

```
MEMORY-POLICY.md      主模型行为规范（含人格设定、铁律、流程）
memory_system.py      MCP stdio 服务端（6 个工具）
consolidate_memory.py 后台增量整理（规则裁决 + 衰减）
produce_short_term.py 会话 → 短期记忆生产者
init_memory_system.*  初始化脚本（Windows / POSIX）
tools/
  read_session.js     解多帧 zstd 的 DSH 会话记录
  safe-remove.ps1     递归删除安全封装
long-term/ mid-term/ short-term/ state/ logs/
archive/INDEX.md + inbox/
systemd/ windows/ tests/
```

## 隐私与敏感信息

本仓库**刻意不包含**任何个人记忆内容：

- 不包含 `state/llm.json`（含 API Key）、`logs/`、`state/producer_state.json`、`persona.md`、`last_session.json`；
- 不包含 `long-term/entries/`、`mid-term/entries/`、`short-term/*.md`、`archive/` 下的实际笔记；
- 索引文件只保留表头，供你初始化后自行生成。

`.gitignore` 已默认排除这些路径，避免误提交。

## 提示词中的非工程内容

`MEMORY-POLICY.md` 是给 AI 的行为规范，不全是项目工程说明。里面有明确属于「非工程」的部分：

- **人设设定**：「小肥鱼」人格——闲聊与任务模式的语气切换、emoji 使用上限、禁止主仆式表达等。
- **系统安全操作红线**（零号条款）：删除/移动/覆盖前先做路径解析校验、禁止用 `$home`/`$profile` 等自动变量拼接路径、破坏性脚本必须 `-ErrorActionPreference='Stop'`、递归删除一律走 `tools/safe-remove.ps1` 的白名单封装。
- **工作流纪律**：归档检索只走索引、记忆层只读、System Prompt 缓存友好约束等。

这几类内容会影响 AI 助手的行为，但**不属于代码逻辑**，可按需整段替换；如果你把它接到别的助手上，换成自己的设定即可，不影响脚本运行。

## 已知限制

- Windows 上用任务计划替代 systemd；文件锁在 Windows 用 `msvcrt`、POSIX 用 `fcntl`。
- `MAX_TITLE_CHARS=40` 会把过长的中文标题截断。
- 记忆衰减阈值（0.8 / 30 天 / 0.35 / 0.20）是经验值，建议按真实数据再调。

## 许可

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

本项目以 **MIT License** 发布，见 [LICENSE](LICENSE)。

- **可以商用**：复制、修改、合并、发布、再许可、销售都允许，不额外收费。
- **唯一义务**：保留版权声明和许可证全文（在副本或实质部分中）。
- **无担保**：按「现状」提供，作者不对使用结果承担责任。
