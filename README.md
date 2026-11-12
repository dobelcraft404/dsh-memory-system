# DSH 本地记忆与归档系统

> 给 [DeepSeek Harness](https://github.com/deepseek-ai) 这类本地 Agent 配套的**记忆 / 归档 / 情绪 / 日记**四层方案。数据全部落在本机，不依赖任何在线服务；整理任务由 Windows 计划任务或 systemd timer 定时跑，人不用管。
>
> **本仓库的代码与文档由 AI 生成**，人类只做需求设定与验收。

---

## 它解决什么问题

长期对话的 Agent 有两个硬伤：**上下文有上限**，**明天的它不记得今天的你**。

这套方案把「记得」拆成四层，各管一段：

| 层 | 位置 | 作用 | 谁来写 |
| --- | --- | --- | --- |
| 短期记忆 | `short-term/` | 每个会话结束后由外部生产者落成一份 Markdown | `produce_short_term.py` |
| 长期 / 中期记忆 | `long-term/`、`mid-term/` | 跨会话可检索的结论，带置信度与衰减 | `consolidate_memory.py` |
| 归档 | `archive/` | 大段卷宗：事故复盘、方案设计、可复用命令 | 主模型经 MCP 工具写入 |
| 日记 | `diary/` | 私密、加密、不给人看的自述 | `tools/diary.py`（DPAPI 加密） |

另外两个横向能力：**情绪状态**（有来源、会衰减，不是随机数）和**关系档案**（沉淀称呼、偏好、雷点）。

## 架构

```
会话 → produce_short_term.py → short-term/*.md
                                  │
                     consolidate_memory.py（每 6 小时，幂等）
                                  ├→ long-term/entries + MEMORY-INDEX.md
                                  ├→ mid-term/entries（置信度不足时降级）
                                  ├→ SUGGESTIONS.md（低于阈值，待人确认；只留最近 7 天）
                                  ├→ emotion-state.md（TTL 衰减）
                                  └→ persona.md（关系档案）
```

MCP 服务端 `memory_system.py` 只读多、写少：主模型**不许**直接改记忆层，只能读索引、读条目、往归档里写新文档。

## MCP 工具

| 工具 | 作用 |
| --- | --- |
| `get_memory_index` | 读长期 / 中期记忆索引 |
| `read_memory_entry` | 按 id 读一条记忆全文（会刷新「最近使用时间」） |
| `read_emotion_state` | 读情绪、关系档案、上次会话接续 |
| `archive_search` | 关键词检索归档索引 |
| `archive_read` | 读归档全文 |
| `archive_write` | 写归档：自动建目录、生成摘要与元数据、更新索引 |
| `session_search` | 在历史会话**原文**里按关键词检索（见下节） |

## 会话原文检索（session_search）

记忆层只沉淀结论、归档层只放可复用笔记，两者都不保留对话原文。想回答「我上次是不是说过这句话 / 你当时怎么答的」，
用 `session_search` 直接翻 DSH 的会话文件（`~/.dsh/sessions/**/session.v4.jsonl.zstd`，多帧 zstd，由 `tools/read_session.js` 切帧解压）：

- `query` 必填，空格/逗号分词，**全部命中**才算（AND）；`role` 可选 `any` / `user` / `assistant`，
  `session`（会话名子串）、`days`（只要最近 N 天改动过的会话）、`limit`（1~50）可选；
- 返回 `session` / `session_id` / `seq` / `time` / `role` / `score` / `snippet`，按时间倒序；
- 解析结果缓存到 `state/session-cache/`，按 `mtime_ns + size` 失效，第二次查询基本不再解压（返回里带 `cache_hits`）；
- 插件注入的 user 消息（`source.kind` 不是 `user`，例如 `dsh-time-context`、`dsh-emotion-context`）**不进索引**，
  避免把注入文本当成用户发言；
- 会话根默认 `~/.dsh/sessions`，可用环境变量 `DSH_SESSIONS_ROOT` 覆盖；
- 不依赖 MCP 也能用：`python -X utf8 memory_system.py --search "关键词" --role user --limit 10`。

## 生产者的健壮性（游标 / 失败隔离）

`produce_short_term.py` 的原则是**宁可少提炼，也不能把没提炼的内容跨过去**：

- 游标只推进到「真正送进模型并成功提交」的最后一条 seq；
- 应答解析失败（`memories` 缺失或不是数组）直接抛错，不当成「本会话没有记忆」消费游标；
- 模型返回候选但**全部校验失败**时，记 ERROR、登记失败、不推进游标；
- 单会话 backlog 超过 40 条只取最早一批；写入额度不足一个会话时不截断、候选进 `state["pending"]`，下次 `drain_pending` 补写；
- 同一会话连续失败 3 次自动隔离（`state["quarantine"]`），不再每轮重试；会话文件 `mtime/size` 变化后自动解除，或手动 `--retry-quarantined`。

## 记忆衰减（用进废退）

纯代码执行，零 token：

- 每 30 天一个周期，置信度 ×0.8；
- 长期低于 `0.35` → 降到 mid-term；中期低于 `0.20` → 退出索引（文件保留，`status: archived`）；
- 每次真实读取会刷新最近使用时间，常被用的条目老得慢；
- **只改索引和 frontmatter，不删文件。**

## 日记：加密且留后门

- 整条 JSON（`created_at` / `title` / `body`）用 **Windows DPAPI 当前用户作用域**加密，落盘为 `diary/YYYY-MM/YYYY-MM-DD_HHMMSS.md.dpapi`；
- 文件名只带时间，不泄露标题；
- 不用自己写密钥管理：DPAPI 天然只能由**同一个 Windows 账号**解开——这既是加密，也是「出事后主人还能救」的后门；
- 换机、换账号解不开，用 `export` 子命令在有权限的机器上导出明文备份。

```powershell
python tools/diary.py write --title "标题" --text "正文"
python tools/diary.py list
python tools/diary.py read 标题或时间前缀
python tools/diary.py export --out D:\backup\diary-md
python tools/diary.py doctor        # DPAPI 自检 + 逐条可读性检查
```

## 安全设计

- **删除一律走白名单脚本**：`tools/safe-remove.ps1` 拒绝用户目录根、盘符根、系统目录，以及不在 `-AllowRoot` 之下的路径，默认只预览、加 `-Force` 才真删。
- **定时任务不弹窗**：Windows 侧用 `wscript.exe` 跑 `windows/run-hidden.vbs`，无控制台、不抢焦点（`-WindowStyle Hidden` 仍会闪一下并抢焦点，这是踩过的坑）。
- **沙箱只放开指定目录**：若 Agent 跑在 DSH 的 `workspace-write` 沙箱里，可用 `tools/grant-write-acl.mjs` 复用沙箱自己的能力 SID + Low 完整性标签，只给记忆系统目录开写权限，不需要整盘放开。

## 安装

**Windows**

```powershell
powershell -ExecutionPolicy Bypass -File .\init_memory_system.ps1 -Root D:\dsh-memory-system
# 注册定时任务（建议先读 windows\README.md，确认四个任务的动作与权限）
powershell -ExecutionPolicy Bypass -File .\windows\register-tasks.ps1 -Root D:\dsh-memory-system
```

**Linux / macOS / WSL**

```bash
bash ./init_memory_system.sh
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl enable --now memory-consolidate.timer memory-session-flush.timer
```

然后在 DSH 的 `cordis.patch.yml` 里挂载 MCP：

```yaml
- id: memory
  name: "@deepseek-ai/dsh-mcp-client"
  config:
    serverName: memory
    transport: stdio
    command: python
    args: ["D:\\dsh-memory-system\\memory_system.py"]
```

装完把 `MEMORY-POLICY.md` 的内容并入 `~/.dsh/AGENTS.md`（或等价的项目说明），Agent 才知道该怎么用这四层。

## 测试

```bash
python tests/build_decay_fixture.py   # 造一份衰减测试数据
python tests/check_decay_access.py    # 校验衰减与索引
python tests/check_self_state.py      # 校验情绪/关系档案读取
python tests/smoke_mcp.py             # MCP 七个工具冒烟
python tests/test_session_search.py   # 会话检索（造两个多帧 zstd 会话）
python tests/test_producer_guards.py  # 生产者游标守卫与失败隔离
```

## 目录结构

```
dsh-memory-system/
├── MEMORY-POLICY.md          行为规范（给 Agent 看的）
├── memory_system.py          MCP stdio 服务端
├── consolidate_memory.py     后台整理（幂等，每 6 小时）
├── produce_short_term.py     会话 → 短期记忆
├── EMOTION-STYLES.md         情绪风格表
├── emotion-state.md          当前情绪（会被覆盖）
├── SUGGESTIONS.md            待确认建议（只留最近 7 天）
├── init_memory_system.ps1/.sh
├── long-term/ mid-term/ short-term/ archive/ diary/
├── state/ logs/              运行时状态与日志（不入库）
├── systemd/                  Linux 定时器
├── tools/                    safe-remove、read_session、diary、grant-write-acl
├── tests/                    自检脚本
└── windows/                  计划任务注册与静默运行封装
```

## 边界与已知限制

- **单人单机**设计，没有多用户隔离；
- 检索是**关键词 + 索引**，没有向量库，语义召回靠索引摘要写得好不好；
- 日记加密绑定 Windows 账号，跨平台能力为 0；
- Windows 计划任务需要交互式登录态，不覆盖「无人登录」场景；
- 记忆的「正确性」靠后台整理模型的判断，会犯糊涂，所以保留了 `SUGGESTIONS.md` 待确认区。

## 许可

MIT，见 [LICENSE](LICENSE)。Copyright (c) 2026 dobelcraft404。

## 更新日志

- **2026-11-12**：新增 `session_search`（会话原文检索 + `state/session-cache` 缓存，工具数 6 → 7）；`produce_short_term.py` 加游标守卫与失败隔离（`state["quarantine"]`、`--retry-quarantined`）；补 `tests/test_session_search.py`、`tests/test_producer_guards.py`。
- **2026-11-08**：归档层改为默认主动检索；`SUGGESTIONS.md` 只保留最近 7 天；新增 DPAPI 加密日记；Windows 定时任务改为 `wscript` 静默启动（消除窗口闪烁与焦点抢占）；新增沙箱单目录授权脚本。
- **2026-11-06**：首个公开版本，四层结构 + MCP 工具 + 衰减机制。
