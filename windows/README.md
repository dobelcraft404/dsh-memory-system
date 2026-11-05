# Windows 任务计划（systemd timer 的等价物）

本机 Windows 11 没有可用的 systemd 用户实例（WSL 的 `systemctl --user` 返回 `offline`），所以用任务计划实现同样的四个触发：

| 任务 | 触发 | 对应 systemd timer / 说明 |
| --- | --- | --- |
| `DSH-Memory-Consolidate` | 每天 03:00；`StartWhenAvailable` 错过补跑；随机延迟 0~30 分钟 | `memory-consolidate.timer`；脚本内 6 小时幂等 |
| `DSH-Memory-SessionFlush` | 登录 5 分钟后；之后每小时检查 | `memory-session-flush.timer`；同样受 6 小时幂等约束 |
| `DSH-Memory-Produce` | 每 3 小时 | 新增任务：读取 DSH 会话 → flash 提炼 → `short-term/`；按会话 `max_seq` 去重，**没有新消息时一个模型请求都不发** |
| `DSH-Memory-Backup` | 每天 03:40；随机延迟 0~10 分钟 | 新增任务：`robocopy /E /XO` 增量备份到 `F:\dsh-memory-backup`，从不删除目标文件 |

## 注册

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\dsh-memory-system\windows\register-tasks.ps1
```

可选参数：

```powershell
# 数据目录换到 F 盘
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\register-tasks.ps1 -Root 'F:\dsh-memory-system'

# 指定 Python
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\register-tasks.ps1 -Python 'C:\Python311\python.exe'

# 换备份目标
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\register-tasks.ps1 -BackupDestination 'E:\dsh-memory-backup'
```

不需要管理员权限，不需要密码。任务以当前用户身份运行，默认“只在用户登录时运行”；笔记本合盖/注销后不会跑，登录后由 `StartWhenAvailable` 补跑。

## 短期记忆生产者

`produce_short_term.py` 是唯一会主动读会话记录的组件：

1. 用 `tools/read_session.js` 解压 `~/.dsh/sessions/**/session.v4.jsonl.zstd`
   （DSH 写的是**多帧** zstd，Node 22+ 的 `zlib.zstdDecompressSync` 只解第一帧，
   所以按 magic 切帧逐段解压）；
2. 只取用户/助手的 `text` 块，跳过 reasoning、工具调用与结果；
3. 调用 `state/llm.json` 里配置的 `deepseek-flash`，**一次调用**同时产出：
   - 0~6 条原子命题 → 写入 `short-term/<日期>_<时间>_<会话>_<序号>.md`，frontmatter 与
     `consolidate_memory.py` 的解析器对齐（`id/title/created_at/updated_at/source/tags/importance`）；
   - 情绪事件（若有）→ `emotion-state.md`，强度封顶 0.9，衰减仍由 `consolidate_memory.py` 负责；
   - 关系档案重写 → `persona.md`（整体重写、不追加，≤1600 字，推断内容标 `[推测]`）；
   - 接续句 → `state/last_session.json`（下次会话由 `read_emotion_state` 一并返回）。
4. 情绪/档案/接续句复用同一次模型调用，**不额外发请求**；送模型前会先抹掉疑似 Key/Token。

手动测试：

```powershell
$py = "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe"
& $py -X utf8 D:\dsh-memory-system\produce_short_term.py --root D:\dsh-memory-system --dry-run --verbose
& $py -X utf8 D:\dsh-memory-system\produce_short_term.py --root D:\dsh-memory-system --limit 1 --verbose
```

`state/llm.json` 含 API Key，已用 `icacls` 去掉继承、只授予当前用户读写；该文件
**不会**进入 F 盘备份（`backup-memory.ps1` 里 `/XF llm.json`）。

## 增量备份

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\dsh-memory-system\windows\backup-memory.ps1
```

- 目标：`F:\dsh-memory-backup\memory-system` 与 `F:\dsh-memory-backup\dsh`（profiles + AGENTS.md）；
- 只增量（`/XO`），**从不**用 `/MIR`，源端误删也不会连带删除备份；
- 排除 `.locks`、`__pycache__`、`tests\tmp-consolidate` 和 `llm.json`；
- 日志：`logs/backup.log`；robocopy 退出码 0~7 视为成功。

## 记忆衰减（代谢）

`consolidate_memory.py` 每次运行会顺带做一次纯代码衰减，零 Token：

- 周期 30 天，每周期置信度 × 0.8；
- “最近使用时间”取 `updated_at` 与 `state/memory_access.json` 的较晚者；`read_memory_entry` 每次真实读取都会登记，常被用的条目老得慢；
- 长期置信度 < 0.35 → 降级到中期索引（文件不动，frontmatter 的 `type`/`status` 改为 `mid-term`）；
- 中期置信度 < 0.20 → 退出索引，`status: archived`，文件保留可找回；
- 测试：`tests/build_decay_fixture.py` 造夹具，`tests/check_decay_access.py` 验证访问登记。

## 验证

```powershell
Get-ScheduledTask -TaskName 'DSH-Memory-*' | Format-Table TaskName, State
Get-ScheduledTaskInfo -TaskName 'DSH-Memory-Produce'
Start-ScheduledTask -TaskName 'DSH-Memory-Produce'
Get-Content D:\dsh-memory-system\logs\task-stdout.log -Tail 50
Get-Content D:\dsh-memory-system\logs\consolidate.log -Tail 50
Get-Content D:\dsh-memory-system\logs\producer.log -Tail 50
Get-Content D:\dsh-memory-system\logs\backup.log -Tail 30
# 验证自我状态（情绪 + 关系档案 + 接续句）与 6 个工具
& "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe" -X utf8 D:\dsh-memory-system\tests\check_self_state.py D:\dsh-memory-system
# 验证记忆衰减：造夹具 → 干跑看计划 → 实跑 → 再跑一次应为 0 变化
& "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe" -X utf8 D:\dsh-memory-system\tests\build_decay_fixture.py D:\dsh-memory-system\tests\tmp-decay
& "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe" -X utf8 D:\dsh-memory-system\consolidate_memory.py --root D:\dsh-memory-system\tests\tmp-decay --force --dry-run --no-llm --verbose
```

## 删除

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\dsh-memory-system\windows\unregister-tasks.ps1
```

只删任务，不动数据。

## 与 6 小时幂等的关系

Consolidate 与 SessionFlush 可能在同一时间段触发（例如 03:00 每日任务和每小时补跑撞车）。`consolidate_memory.py` 开头会检查 `state/last_run.json`：距上次成功不足 6 小时就直接退出，因此不会重复调用模型、不会重复写记忆。

Produce 不复用这个 6 小时闸门，而是按“每个会话的 `max_seq` + 已写入命题哈希”去重：没有新消息的会话在解压后比对 `seq` 就跳过，**不会发起任何模型请求**；日志最后一行会打印“模型调用 N 次”，空跑时 N=0，可直接当作用电量证明。3 小时的节奏是为了避免把同一段对话切成多批、产出大量近义条目。

## 已知限制

- 任务计划不会在系统休眠时唤醒机器；`StartWhenAvailable` 只保证“下次有机会时补跑”。
- 如果公司策略禁止用户级任务计划，需要改用管理员注册或 Windows 服务；本脚本不自动提权。
- 日志只追加不轮转；长期运行请自行清理 `logs\*.log`，或后续加轮转脚本。
- 备份只覆盖记忆系统与 profile 配置，不包含 `~/.dsh/sessions`（体积大且含隐私）。
