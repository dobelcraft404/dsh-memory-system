#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证：read_memory_entry 会登记访问日志，且工具列表仍按字母序固定。

用法（先跑 tests/build_decay_fixture.py）::

    python -X utf8 tests/check_decay_access.py tests/tmp-decay
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_mcp import McpClient  # noqa: E402

EXPECTED_TOOLS = [
    "archive_read",
    "archive_search",
    "archive_write",
    "get_memory_index",
    "read_emotion_state",
    "read_memory_entry",
]


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else Path(__file__).resolve().parent / "tmp-decay")
    client = McpClient(root)
    try:
        init = client.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "decay-access-check", "version": "1.0"},
            },
        )
        assert "result" in init, init
        client.notify("notifications/initialized")

        listing = client.request("tools/list", {})
        names = [tool["name"] for tool in listing["result"]["tools"]]
        assert names == EXPECTED_TOOLS, f"工具列表不是字母序或数量不对：{names}"
        print(f"[ok] tools/list 字母序 {len(names)} 个：{', '.join(names)}")

        payload, is_error = client.tool("read_memory_entry", {"entry_id": "decay-keep-0003"})
        assert not is_error, payload
        assert payload["ok"] is True, payload

        access_path = root / "state" / "memory_access.json"
        data = json.loads(access_path.read_text(encoding="utf-8"))
        record = data["entries"]["decay-keep-0003"]
        assert record["count"] >= 2, record
        assert record["last_accessed_at"], record
        print(f"[ok] 访问日志已登记：count={record['count']} at={record['last_accessed_at']}")

        missing, is_error = client.tool("read_memory_entry", {"entry_id": "decay-archived-9999"})
        assert is_error, missing
        assert missing.get("error", {}).get("code") == "not_found", missing
        print("[ok] 不存在条目仍返回 not_found")
    finally:
        client.close()
    print("all decay access checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
