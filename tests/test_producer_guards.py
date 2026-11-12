#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生产者守卫测试：schema 违约不推进游标、失败隔离与恢复。

用法::

    python -X utf8 tests/test_producer_guards.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import produce_short_term as pst  # noqa: E402

NODE = shutil.which("node")
assert NODE, "需要 node"


def make_session(path: Path) -> Path:
    """用 Node 写一个两帧的合法会话文件。"""

    script = r"""
const fs = require('fs'), zlib = require('zlib');
const out = process.argv[process.argv.length - 1];
const now = Date.now();
const line1 = JSON.stringify({type:'user/message',seq:1,time:now-2000,data:{source:{kind:'user'},content:[{type:'text',text:'我在修记忆系统的生产者，游标老是被提前推进'}]}})+'\n';
const line2 = JSON.stringify({type:'assistant/message',seq:2,time:now-1000,data:{message:{content:[{type:'text',text:'那检查一下 build_transcript 返回的是不是真正消费到的 seq'}]}}})+'\n';
fs.writeFileSync(out, Buffer.concat([zlib.zstdCompressSync(Buffer.from(line1)), zlib.zstdCompressSync(Buffer.from(line2))]));
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([NODE, "-e", script, str(path)], check=True)
    return path


BAD_REPLY = json.dumps(
    {
        "memories": [{"title": "缺字段的记忆"}],
        "emotion_event": None,
        "persona_update": None,
        "session_note": "聊了生产者",
    },
    ensure_ascii=False,
)

GOOD_REPLY = json.dumps(
    {
        "memories": [
            {
                "title": "生产者游标必须按实际消费的 seq 推进",
                "statement": "build_transcript_batch 返回真正送入模型的最后一条 seq，游标只能推进到该 seq。",
                "tags": ["producer", "cursor"],
                "importance": 0.8,
            }
        ],
        "emotion_event": None,
        "persona_update": None,
        "session_note": "聊了生产者",
    },
    ensure_ascii=False,
)


def main() -> int:
    # 注意：不要用 tempfile（%TEMP% 在 DSH 沙箱下可能不可写），改用仓库内 .tmp。
    base = ROOT / ".tmp" / f"pst-guard-{os.getpid()}"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    try:
        root = base / "root"
        for name in ("state", "short-term", "logs"):
            (root / name).mkdir(parents=True, exist_ok=True)
        (root / ".locks").mkdir(exist_ok=True)
        tools = root / "tools"
        tools.mkdir()
        shutil.copy2(ROOT / "tools" / "read_session.js", tools / "read_session.js")

        sessions = base / "sessions"
        session_file = make_session(sessions / "ws" / "session-demo" / "session.v4.jsonl.zstd")
        key = "ws/session-demo/session.v4.jsonl.zstd"

        replies = [BAD_REPLY]

        def fake_llm(config, prompt):  # noqa: ARG001
            return replies[0]

        pst.load_llm_config = lambda root_path: {  # noqa: ARG005
            "model": "fake",
            "base_url": "http://127.0.0.1:9",
            "api_key": "fake",
        }
        pst.call_llm = fake_llm

        # 1) schema 违约：全部候选被丢弃，游标不能推进。
        code = pst.process(root, sessions, NODE, tools / "read_session.js", None, False, False)
        state = json.loads((root / "state" / "producer_state.json").read_text(encoding="utf-8"))
        assert code == 1, f"schema 违约应以失败退出，实际 {code}"
        assert key not in state["sessions"], f"schema 违约时游标被推进了：{state['sessions'].get(key)}"
        assert state["failures"].get(key, {}).get("count") == 1, state.get("failures")
        assert not list((root / "short-term").glob("producer-*.md")), "不该写入任何记忆"
        print("ok  1/3 schema 违约不推进游标，并登记失败")

        # 2) 恢复正常：写入记忆、推进游标、清空失败计数。
        replies[0] = GOOD_REPLY
        code = pst.process(root, sessions, NODE, tools / "read_session.js", None, False, False)
        state = json.loads((root / "state" / "producer_state.json").read_text(encoding="utf-8"))
        assert code == 0, f"正常回复应成功，实际 {code}"
        assert state["sessions"].get(key, {}).get("max_seq") == 2, state["sessions"].get(key)
        assert key not in state["failures"], "成功后退回失败计数"
        assert list((root / "short-term").glob("producer-*.md")), "应写入一条短期记忆"
        print("ok  2/3 恢复正常后写记忆、推进游标、清空失败")

        # 3) 连续失败达到上限后隔离，文件变化自动解除。
        (root / "state" / "producer_state.json").unlink()
        replies[0] = BAD_REPLY
        for _ in range(pst.MAX_SESSION_FAILURES):
            pst.process(root, sessions, NODE, tools / "read_session.js", None, False, False)
        state = json.loads((root / "state" / "producer_state.json").read_text(encoding="utf-8"))
        assert key in state["quarantine"], f"连续失败未隔离：{state.get('failures')}"
        # 会话文件没变：隔离保持，不再重试。
        code = pst.process(root, sessions, NODE, tools / "read_session.js", None, False, False)
        assert code == 0, f"隔离后的空跑应成功，实际 {code}"
        # 会话文件变化：隔离自动解除并重试（仍失败则重新计数）。
        make_session(session_file)
        pst.process(root, sessions, NODE, tools / "read_session.js", None, False, False)
        state = json.loads((root / "state" / "producer_state.json").read_text(encoding="utf-8"))
        assert key not in state["quarantine"], "会话文件变化后隔离应自动解除"
        print("ok  3/3 隔离与自动解除都正常")
        return 0
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
