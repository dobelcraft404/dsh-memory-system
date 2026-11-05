#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构造记忆衰减测试夹具：覆盖刷新、降级、归档、存活四种情况。

用法::

    python -X utf8 tests/build_decay_fixture.py D:\\dsh-memory-system\\tests\\tmp-decay
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def entry_text(
    entry_id: str,
    title: str,
    summary: str,
    confidence: float,
    updated_at: datetime,
    status: str = "active",
) -> str:
    return (
        "---\n"
        f'id: "{entry_id}"\n'
        f'title: "{title}"\n'
        'domain: "computer-science"\n'
        'subdomain: "operating-systems"\n'
        f'type: "{"mid-term" if status == "mid-term" else "long-term"}"\n'
        'tags: ["test"]\n'
        f'summary: "{summary}"\n'
        f'created_at: "{iso(updated_at)}"\n'
        f'updated_at: "{iso(updated_at)}"\n'
        f'status: "{status}"\n'
        f"confidence: {confidence}\n"
        "---\n\n"
        f"# {title}\n\n{summary}\n"
    )


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else r"D:\dsh-memory-system\tests\tmp-decay")
    now = datetime(2026, 11, 5, 13, 10, 0, tzinfo=timezone(timedelta(hours=8)))

    for relative in ("long-term/entries", "mid-term/entries", "short-term", "archive/inbox", "state"):
        (root / relative).mkdir(parents=True, exist_ok=True)

    cases = [
        ("decay-refresh-0001", "刷新案例", "三天周期后仍高于降级线", 0.90, 100, "active"),
        ("decay-demote-0002", "降级案例", "六个周期后跌到降级线以下", 0.90, 200, "active"),
        ("decay-keep-0003", "存活案例", "十天前更新，未满一个周期", 0.90, 10, "active"),
    ]
    long_rows = []
    for entry_id, title, summary, confidence, days_ago, status in cases:
        updated = now - timedelta(days=days_ago)
        (root / "long-term" / "entries" / f"{entry_id}.md").write_text(
            entry_text(entry_id, title, summary, confidence, updated, status),
            encoding="utf-8",
        )
        long_rows.append(
            f"| {entry_id} | {title} | long-term | test | {summary} | "
            f"{confidence:.2f} | {iso(updated)} | long-term/entries/{entry_id}.md |"
        )

    mid_id = "decay-forget-0004"
    mid_updated = now - timedelta(days=100)
    (root / "long-term" / "entries" / f"{mid_id}.md").write_text(
        entry_text(mid_id, "归档案例", "三个周期后低于中期保留线", 0.30, mid_updated, "mid-term"),
        encoding="utf-8",
    )

    long_header = (
        "# 长期记忆索引\n\n> 测试夹具。\n\n"
        "| id | title | type | tags | summary | confidence | updated_at | path |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
    )
    mid_header = (
        "# 中期记忆索引\n\n> 测试夹具。\n\n"
        "| id | title | type | tags | summary | confidence | updated_at | path |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
    )
    (root / "long-term" / "MEMORY-INDEX.md").write_text(
        long_header + "\n".join(long_rows) + "\n", encoding="utf-8"
    )
    (root / "mid-term" / "MEMORY-INDEX.md").write_text(
        mid_header
        + f"| {mid_id} | 归档案例 | mid-term | test | 三个周期后低于中期保留线 | "
        + f"0.30 | {iso(mid_updated)} | long-term/entries/{mid_id}.md |\n",
        encoding="utf-8",
    )

    access = {
        "version": 1,
        "entries": {
            "decay-refresh-0001": {"count": 2, "last_accessed_at": iso(now - timedelta(days=120))},
            "decay-keep-0003": {"count": 1, "last_accessed_at": iso(now - timedelta(days=5))},
            mid_id: {"count": 1, "last_accessed_at": iso(now - timedelta(days=95))},
        },
    }
    (root / "state" / "memory_access.json").write_text(
        json.dumps(access, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"fixture ready: {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
