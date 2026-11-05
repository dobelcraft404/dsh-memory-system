# systemd 定时任务（Linux / WSL）

适用于有 systemd 用户实例的 Linux。Windows 本机请用 `windows\register-tasks.ps1`。

## 安装

```bash
mkdir -p ~/.config/systemd/user
cp ~/.dsh/memory-system/systemd/*.service ~/.config/systemd/user/
cp ~/.dsh/memory-system/systemd/*.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now memory-consolidate.timer
systemctl --user enable --now memory-session-flush.timer
```

如果希望注销后仍运行（笔记本关机/休眠场景很重要）：

```bash
loginctl enable-linger "$USER"
```

## 检查

```bash
systemctl --user list-timers --all | grep memory
systemctl --user status memory-consolidate.timer
journalctl --user -u memory-consolidate.service -n 50 --no-pager
tail -n 50 ~/.dsh/memory-system/logs/consolidate.log
```

## 两个 timer 的分工

| Timer | 触发 | 作用 |
| --- | --- | --- |
| `memory-consolidate.timer` | 每天 03:00，`Persistent=true`，随机延迟 0~30 分钟 | 正常每日整理；关机错过则开机补跑 |
| `memory-session-flush.timer` | 开机 5 分钟后 + 之后每小时 | 会话结束/开机补跑；脚本内部 6 小时幂等，不会和每日任务重复干活 |

`Persistent=true` 只记录“错过了一次”，真正的补跑仍受脚本开头 6 小时幂等检查约束：如果 6 小时内已经成功整理过，补跑会直接跳过。

## 注意

- `%h` 是运行 systemd 用户的 home；若 DSH 数据目录不在 `~/.dsh`，改 service 里的 `--root` 和路径。
- 本机 WSL 的 `systemctl --user` 当前返回 `offline`，所以这套 unit 只作为 Linux 部署件交付；Windows 用任务计划。
- `RandomizedDelaySec=1800` 是为了避免开机瞬间和会话补跑撞车；不是每次随机 30 分钟才开始，systemd 会在 03:00 后的 0~30 分钟内随机触发。
