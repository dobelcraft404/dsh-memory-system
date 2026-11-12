#!/usr/bin/env python3
"""MCP 冒烟测试：启动 memory_system.py，走完整 JSON-RPC 握手并调用全部工具。

用法：
    python -X utf8 tests/smoke_mcp.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "memory_system.py"


class McpClient:
    def __init__(self, root: Path) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(SERVER), "--root", str(root)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.next_id = 0

    def _send(self, payload: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _read(self) -> dict:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if not line:
            stderr = self.proc.stderr.read() if self.proc.stderr else ""
            raise RuntimeError(f"服务器无响应。stderr:\n{stderr}")
        return json.loads(line)

    def request(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        payload = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)
        while True:
            message = self._read()
            if message.get("id") == self.next_id:
                return message

    def notify(self, method: str, params: dict | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)

    def tool(self, name: str, arguments: dict) -> tuple[dict, bool]:
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        assert "result" in response, response
        result = response["result"]
        text = result["content"][0]["text"]
        return json.loads(text), bool(result.get("isError"))

    def close(self) -> None:
        try:
            self.notify("exit")
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  ok  {message}")


def main() -> int:
    # 用仓库内 .tmp：某些环境（%TEMP% 不可写、杀软/沙箱）下 tempfile 会失败。
    temp_root = ROOT / ".tmp" / f"smoke-{os.getpid()}"
    shutil.rmtree(temp_root, ignore_errors=True)
    temp_root.mkdir(parents=True, exist_ok=True)
    client = McpClient(temp_root)
    try:
        init = client.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "smoke", "version": "0.0.1"},
            },
        )
        check(init["result"]["serverInfo"]["name"] == "memory-system", "initialize 返回 serverInfo")
        client.notify("notifications/initialized")

        tools = client.request("tools/list")["result"]["tools"]
        names = [tool["name"] for tool in tools]
        check(names == sorted(names), "tools/list 按名称字母序")
        check(
            set(names) == {
                "archive_read",
                "archive_search",
                "archive_write",
                "get_memory_index",
                "read_emotion_state",
                "read_memory_entry",
                "session_search",
            },
            "七个工具齐全",
        )

        emotion, is_error = client.tool("read_emotion_state", {})
        check(not is_error and emotion["ok"], "read_emotion_state 成功")
        check(emotion["emotion"] == "平静", "默认情绪为平静")
        check(0 <= emotion["intensity"] <= 1, "intensity 在 0~1")

        written, is_error = client.tool(
            "archive_write",
            {
                "title": "TCP 重传排查笔记",
                "domain": "computer-science",
                "subdomain": "networking",
                "content": "## 背景\n\n测试归档。\n\n## 结论\n\n用 `ss -ti` 看重传。",
                "tags": "tcp, 重传, 测试",
            },
        )
        check(not is_error and written["ok"], "archive_write 成功")
        check(written["domain"] == "computer-science", "domain 保持")
        check(written["subdomain"] == "networking", "subdomain 保持")
        check(written["path"].startswith("archive/computer-science/networking/"), "目录层级正确")
        target = temp_root / written["path"]
        check(target.is_file(), "归档文件已落盘")
        check("id:" in target.read_text(encoding="utf-8"), "文件含 frontmatter")

        index_text = (temp_root / "archive" / "INDEX.md").read_text(encoding="utf-8")
        check("TCP 重传排查笔记" in index_text, "INDEX.md 已追加记录")

        hits, is_error = client.tool("archive_search", {"query": "重传"})
        check(not is_error and hits["ok"], "archive_search 成功")
        check(hits["count"] >= 1, "archive_search 命中")
        matched = next(item for item in hits["results"] if item["title"] == "TCP 重传排查笔记")
        check("path" in matched and "summary" in matched, "索引条目含 path/summary")

        readback, is_error = client.tool("archive_read", {"file_path": matched["path"]})
        check(not is_error and readback["ok"], "archive_read 成功")
        check("ss -ti" in readback["content"], "archive_read 返回正文")

        outside, is_error = client.tool("archive_read", {"file_path": "../secret.md"})
        check(is_error and outside["error"]["code"] == "path_outside_archive", "拒绝目录穿越")

        missing, is_error = client.tool("archive_read", {"file_path": "archive/nope.md"})
        check(is_error and missing["error"]["code"] == "file_not_found", "缺失文件返回 file_not_found")

        lt_index, is_error = client.tool("get_memory_index", {"memory_type": "long-term"})
        check(not is_error and lt_index["ok"], "get_memory_index 成功")
        check(lt_index["count"] == 0, "长期索引初始为空")

        bad_type, is_error = client.tool("get_memory_index", {"memory_type": "bogus"})
        check(is_error and bad_type["error"]["code"] == "invalid_argument", "非法 memory_type 被拒绝")

        missing_entry, is_error = client.tool("read_memory_entry", {"entry_id": "does-not-exist"})
        check(is_error and missing_entry["error"]["code"] == "not_found", "缺失记忆条目返回 not_found")

        empty_search, is_error = client.tool("archive_search", {"query": "量子引力不存在"})
        check(not is_error and empty_search["count"] == 0, "无命中返回空列表")

        print("\n全部冒烟测试通过。")
        return 0
    finally:
        client.close()
        shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
