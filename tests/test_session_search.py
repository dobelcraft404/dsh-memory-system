#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话检索（session_search）测试：造两个多帧 zstd 会话，走 MCP 调工具。

用法::

    python -X utf8 tests/test_session_search.py

注意：不要用 tempfile（部分沙箱下 %TEMP% 不可写），统一用仓库内 .tmp。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from smoke_mcp import McpClient  # noqa: E402

NODE = shutil.which("node")


def write_session(path: Path, lines: list[dict]) -> None:
    """把若干 JSON 行各压一帧写进同一个 zstd 文件（模拟真实多帧会话）。"""
    assert NODE, "需要 node 才能生成 zstd 会话文件"
    path.parent.mkdir(parents=True, exist_ok=True)
    script = r"""
const fs = require('fs'), zlib = require('zlib');
const out = process.argv[process.argv.length - 2];
const payload = JSON.parse(process.argv[process.argv.length - 1]);
const frames = payload.map((line) => zlib.zstdCompressSync(Buffer.from(JSON.stringify(line) + '\n')));
fs.writeFileSync(out, Buffer.concat(frames));
"""
    subprocess.run([NODE, "-e", script, str(path), json.dumps(lines, ensure_ascii=False)], check=True)


def user_line(seq: int, text: str, when: int) -> dict:
    return {
        "type": "user/message",
        "seq": seq,
        "time": when,
        "data": {"source": {"kind": "user"}, "content": [{"type": "text", "text": text}]},
    }


def assistant_line(seq: int, text: str, when: int) -> dict:
    return {
        "type": "assistant/message",
        "seq": seq,
        "time": when,
        "data": {"message": {"content": [{"type": "text", "text": text}]}},
    }


def injected_line(seq: int, text: str, when: int) -> dict:
    """time-context 之类的注入消息：不是真人发言，role 过滤时不该算 user。"""
    return {
        "type": "user/message",
        "seq": seq,
        "time": when,
        "data": {"source": {"kind": "time-context"}, "content": [{"type": "text", "text": text}]},
    }


def main() -> int:
    base = ROOT / ".tmp" / f"session-search-{os.getpid()}"
    shutil.rmtree(base, ignore_errors=True)
    sessions = base / "sessions"
    root = base / "root"
    (root / "state").mkdir(parents=True, exist_ok=True)

    now = 1762950000000  # 固定毫秒时间戳，避免依赖当前时间
    write_session(
        sessions / "ws-a" / "session-alpha" / "session.v4.jsonl.zstd",
        [
            injected_line(1, "当前时间：2026-11-12 21:00:00", now - 9000),
            user_line(2, "我想给记忆系统加一个会话检索入口，现在只能靠临时脚本解 zstd", now - 8000),
            assistant_line(3, "那就加个 session_search 做会话检索，走 tools/read_session.js 解帧", now - 7000),
            user_line(4, "那缓存放哪？每次都全量解压太慢", now - 6000),
            assistant_line(5, "放 state/session-cache，按 mtime_ns+size 失效", now - 5000),
        ],
    )
    write_session(
        sessions / "ws-b" / "session-beta" / "session.v4.jsonl.zstd",
        [
            user_line(1, "神舟那个 BIOS 老麻烦了，能不能你帮我刷", now - 4000),
            assistant_line(2, "隔着聊天刷 BIOS 会把机器刷砖，这活我不接", now - 3000),
        ],
    )

    os.environ["DSH_SESSIONS_ROOT"] = str(sessions)
    client = McpClient(root)

    def call(name: str, arguments: dict) -> dict:
        payload, is_error = client.tool(name, arguments)
        assert not is_error, payload
        assert payload.get("ok") is True, payload
        return payload

    try:
        client.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "session-search-check", "version": "1.0"},
            },
        )
        client.notify("notifications/initialized")

        names = [tool["name"] for tool in client.request("tools/list")["result"]["tools"]]
        assert "session_search" in names, names
        assert names == sorted(names), f"tools/list 必须字母序：{names}"
        print(f"[ok] 工具已注册，共 {len(names)} 个")

        hit = call("session_search", {"query": "会话检索"})
        assert hit["count"] == 2, hit
        assert hit["tokens"] == ["会话检索"], hit
        assert hit["total_hits"] == 2, hit
        assert hit["scanned_sessions"] == 2, hit
        assert hit["matched_sessions"] == 1, hit
        roles = {item["seq"] for item in hit["results"]}
        assert roles == {2, 3}, hit["results"]
        assert all(item["session_id"] == "session-alpha" for item in hit["results"]), hit["results"]
        assert "会话检索" in hit["results"][0]["snippet"], hit["results"][0]
        print("[ok] 多词 AND 命中，session/seq/snippet 正确")

        # 插件注入的 user 消息（source.kind 不是 user）根本不进索引。
        for role in ("user", "any"):
            injected = call("session_search", {"query": "当前时间", "role": role})
            assert injected["count"] == 0, injected
        print("[ok] 注入消息不进索引")

        # role 过滤：同一关键词分别在 user / assistant 侧命中。
        by_user = call("session_search", {"query": "BIOS", "role": "user"})
        assert [item["seq"] for item in by_user["results"]] == [1], by_user
        by_assistant = call("session_search", {"query": "BIOS", "role": "assistant"})
        assert [item["seq"] for item in by_assistant["results"]] == [2], by_assistant
        print("[ok] role 过滤正确")

        # 会话名过滤 + 空命中。
        beta = call("session_search", {"query": "BIOS", "session": "session-beta"})
        assert beta["count"] == 2, beta
        none = call("session_search", {"query": "量子引力不存在"})
        assert none["count"] == 0 and none["total_hits"] == 0, none
        print("[ok] session 过滤命中，无结果时 count=0")

        # 缓存：第二次查询应复用 state/session-cache，不再全量解压。
        cache_dir = root / "state" / "session-cache"
        cache_files = [p for p in cache_dir.glob("*.json") if not p.name.endswith(".meta.json")]
        assert len(cache_files) == 2, cache_files
        cached = call("session_search", {"query": "会话检索"})
        assert cached["count"] == 2 and cached["cache_hits"] == 2, cached
        print(f"[ok] 缓存复用，state/session-cache 下 {len(cache_files)} 个文件")

        # 参数校验：缺 query / 空 query / 非法 role 报错；limit 越界则收敛。
        for bad in ({}, {"query": ""}, {"query": "x", "role": "bot"}):
            payload, is_error = client.tool("session_search", bad)
            assert is_error and payload["error"]["code"] == "invalid_argument", (bad, payload)
        clamped, is_error = client.tool("session_search", {"query": "BIOS", "limit": 0})
        assert not is_error and clamped["count"] == 2, clamped
        huge, is_error = client.tool("session_search", {"query": "BIOS", "limit": 1000})
        assert not is_error and huge["count"] == 2, huge
        print("[ok] 非法参数被拒绝，limit 越界收敛")
    finally:
        client.close()
        shutil.rmtree(base, ignore_errors=True)

    print("all session_search checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
