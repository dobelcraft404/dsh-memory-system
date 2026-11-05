#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小 stdio 客户端：验证 read_emotion_state 的新字段与工具列表稳定。"""

import json
import subprocess
import sys
from pathlib import Path

PYTHON = sys.executable
SERVER = Path(__file__).resolve().parent.parent / "memory_system.py"


def main() -> int:
    root = sys.argv[1]
    proc = subprocess.Popen(
        [PYTHON, "-X", "utf8", str(SERVER), "--root", root],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout

    def rpc(payload: dict) -> dict:
        proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        proc.stdin.flush()
        line = proc.stdout.readline().decode("utf-8").strip()
        return json.loads(line)

    rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "check_self_state", "version": "0"},
            },
        }
    )
    proc.stdin.write(
        (json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode("utf-8")
    )
    proc.stdin.flush()

    tools = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    names = [tool["name"] for tool in tools]
    print("tools:", names)
    assert names == sorted(names), "工具未按字母序"
    assert len(names) == 6, f"工具数量异常：{len(names)}"

    result = rpc(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "read_emotion_state", "arguments": {}},
        }
    )["result"]
    text = result["content"][0]["text"]
    data = json.loads(text)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    expected = ["ok", "emotion", "intensity", "updated_at", "ttl_hours", "reason",
                "style_hint", "source", "persona_summary", "persona_updated_at", "last_session"]
    assert list(data.keys()) == expected, f"字段顺序异常：{list(data.keys())}"
    proc.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
