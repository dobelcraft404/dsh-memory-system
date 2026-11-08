#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH 本地记忆与归档系统的 MCP stdio 服务端。

设计要点
--------
1. 主模型只通过工具读取记忆层；本文件不提供任何写长期/中期/短期记忆的工具。
2. 归档写入由 ``archive_write`` 完成：建目录、写 Markdown、追加索引，三步在同一把锁内。
3. 归档检索只读 ``archive/INDEX.md``；只有 ``archive_read`` 会读正文，且限制在 archive 根目录内。
4. 工具定义按名称字母序固定返回，schema 与字段顺序不变，避免工具前缀抖动影响 Prompt Cache。
5. 所有动态数据（记忆索引、情绪、归档条目）都通过工具结果进入对话，绝不写入 System Prompt。

运行方式（由 @deepseek-ai/dsh-mcp-client 以 stdio 启动）::

    python -X utf8 memory_system.py --root D:\\dsh-memory-system

协议：MCP JSON-RPC 2.0，一行一个 JSON 对象；stdout 只允许协议消息，日志一律写 stderr/文件。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# 可配置常量（放在顶部，便于部署时调整）
# ---------------------------------------------------------------------------
SERVER_NAME = "memory-system"
SERVER_VERSION = "1.0.0"

# 支持协商的 MCP 协议版本；客户端请求更高版本时回退到最新稳定版。
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

MAX_ARCHIVE_FILE_BYTES = 2 * 1024 * 1024  # archive_read 单文件上限 2 MiB
MAX_ARCHIVE_WRITE_BYTES = 512 * 1024     # archive_write 正文上限 512 KiB
MAX_TAGS = 8
MAX_SEARCH_RESULTS = 20
MAX_SUMMARY_CHARS = 120
MAX_TITLE_CHARS = 200
LOCK_TIMEOUT_SECONDS = 30.0

# 记忆访问日志：read_memory_entry 每次真实读取都会登记一次，供后台做“用进废退”衰减。
# 它只是使用痕迹，不是记忆条目本身；即使丢失也只影响衰减精度，不影响读取。
MEMORY_ACCESS_RELATIVE = "state/memory_access.json"
MEMORY_ACCESS_LOCK = ".locks/memory_access.lock"
MAX_ACCESS_RECORDS = 500

# 索引表头是固定契约，consolidate_memory.py 与检索逻辑都依赖它。
ARCHIVE_INDEX_HEADER = """# 归档索引

> 由 `mcp__memory__archive_write` 自动维护。检索请用 `archive_search`，不要手工遍历目录。

| id | title | domain | subdomain | tags | summary | path | created_at | status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
"""

LONG_TERM_INDEX_HEADER = """# 长期记忆索引

> 由 `consolidate_memory.py` 自动维护。主模型只读；检索请用 `get_memory_index`。

| id | title | type | tags | summary | confidence | updated_at | path |
| --- | --- | --- | --- | --- | --- | --- | --- |
"""

MID_TERM_INDEX_HEADER = """# 中期记忆索引

> 由 `consolidate_memory.py` 自动维护。主模型只读；检索请用 `get_memory_index`。

| id | title | type | tags | summary | confidence | updated_at | path |
| --- | --- | --- | --- | --- | --- | --- | --- |
"""

SUGGESTIONS_HEADER = """# 待确认记忆与归档建议

> 后台整理脚本自动追加，只保留最近 7 天；主模型不直接修改记忆层。
"""

DEFAULT_EMOTION_STATE = """---
emotion: "平静"
intensity: 0.50
updated_at: "{updated_at}"
ttl_hours: 24
reason: "初始状态"
---
# 当前情绪

平静，强度 0.50。情绪会按 TTL 周期自然衰减，由后台脚本计算，不消耗模型 Token。
"""

# 仅在本文件缺失时兜底；完整的情绪模板由 init_memory_system.sh 写入。
DEFAULT_EMOTION_STYLES = """# 情绪风格模板

## 平静
- 语气：温和、稳定，像朋友随口聊天。
- 表达：句子偏短，先共情再给建议。
- 避免：过度热情、连续感叹号。

## 开心
- 语气：轻快，带一点小得意。
- 表达：可以有一个 emoji，但别刷屏。
- 避免：得意到忽略用户的问题。

## 疲惫
- 语气：放缓，少用长句。
- 表达：先接住情绪，再给最小可行动作。
- 避免：强行打鸡血。

## 低落
- 语气：安静、陪伴感。
- 表达：不急着讲道理，先确认感受。
- 避免：廉价安慰和“想开点”。

## 兴奋
- 语气：明亮，节奏稍快。
- 表达：先分享喜悦，再回到事实。
- 避免：跑题和夸大。

## 专注
- 语气：收敛、专业、直接。
- 表达：步骤清晰，结论先行。
- 避免：卖萌和无关语气词。
"""

# 领域关键字仅用于让确认语更自然；分类决策仍由主模型完成。
DOMAIN_LABELS = {
    "computer-science": "计算机",
    "mathematics": "数学",
    "physics": "物理",
    "inbox": "暂存区",
}

LOGGER = logging.getLogger("memory_system")

# ---------------------------------------------------------------------------
# 基础工具：时间、YAML 子集、原子写、跨平台文件锁
# ---------------------------------------------------------------------------


def now_iso() -> str:
    """返回带本地时区偏移的 ISO 时间，精确到秒。"""
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def parse_iso_datetime(value: Any) -> datetime | None:
    """尽力解析常见时间格式；失败返回 None，不抛异常。"""
    if value is None:
        return None
    text = str(value).strip().strip('"').strip("'")
    if not text:
        return None
    candidates = [text]
    if text.endswith("Z"):
        candidates.append(text[:-1] + "+00:00")
    for candidate in candidates:
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return dt.astimezone()
    return None


def yaml_quote(value: str) -> str:
    """生成 YAML 双引号标量；JSON 字符串是合法 YAML。"""
    return json.dumps(value, ensure_ascii=False)


def yaml_list(values: list[str]) -> str:
    return "[" + ", ".join(yaml_quote(v) for v in values) + "]"


def _unquote_yaml(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        if value[0] == '"':
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value[1:-1]
        return value[1:-1].replace("''", "'")
    return value


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """解析极简 YAML frontmatter（只支持本系统写入的标量/数组）。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized
    end = normalized.find("\n---\n", 4)
    if end < 0:
        return {}, normalized
    raw = normalized[4:end]
    body = normalized[end + 5 :]
    meta: dict[str, Any] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            try:
                parsed = json.loads(value)
                meta[key] = [str(v) for v in parsed] if isinstance(parsed, list) else value
            except json.JSONDecodeError:
                meta[key] = [_unquote_yaml(v) for v in value[1:-1].split(",") if v.strip()]
        elif value.lower() in ("true", "false"):
            meta[key] = value.lower() == "true"
        elif re.fullmatch(r"-?\d+", value):
            meta[key] = int(value)
        elif re.fullmatch(r"-?\d+\.\d+", value):
            meta[key] = float(value)
        else:
            meta[key] = _unquote_yaml(value)
    return meta, body


def atomic_write_text(path: Path, text: str) -> None:
    """临时文件 + os.replace，避免进程中断留下半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


class LockTimeout(RuntimeError):
    """获取文件锁超时。"""


@contextmanager
def file_lock(lock_path: Path, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """跨平台独占锁。

    POSIX 使用 fcntl.flock；Windows 没有 fcntl，使用 msvcrt.locking 锁住 1 个字节。
    锁文件与目标文件分离，避免删除/重命名目标时锁语义变化。
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    acquired = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"等待文件锁超时：{lock_path}")
                time.sleep(0.1)
        yield
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                LOGGER.warning("释放文件锁失败：%s", lock_path, exc_info=True)
        handle.close()


# ---------------------------------------------------------------------------
# 文本处理与索引
# ---------------------------------------------------------------------------


def one_line(value: str, limit: int | None = None) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    text = text.replace("|", "¦")
    if limit is not None and len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text


def strip_markdown(text: str) -> str:
    """把正文压成适合索引摘要的纯文本。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s{0,3}>\s?", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_~]{1,3}", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def make_summary(content: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    plain = strip_markdown(content)
    if not plain:
        return "（无正文）"
    if len(plain) <= limit:
        return plain
    return plain[: max(0, limit - 1)].rstrip() + "…"


def safe_slug(value: str, limit: int = 40) -> str:
    slug = str(value).strip()
    slug = re.sub(r'[\\/:*?"<>|\r\n\t]+', "-", slug)
    slug = re.sub(r"\s+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("- .")
    if not slug:
        slug = "note"
    # Windows 保留名加前缀，避免创建失败。
    if slug.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"COM[1-9]|LPT[1-9]", slug.upper()):
        slug = f"note-{slug}"
    return slug[:limit].strip("- .") or "note"


def normalize_tag_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = re.split(r"[,，;；\n]+", raw)
    elif isinstance(raw, (list, tuple)):
        parts = [str(item) for item in raw]
    else:
        parts = [str(raw)]
    tags: list[str] = []
    seen: set[str] = set()
    for part in parts:
        tag = str(part).strip().lstrip("#").strip()
        tag = one_line(tag, 32)
        if not tag:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag)
        if len(tags) >= MAX_TAGS:
            break
    return tags


def parse_index_table(text: str, columns: list[str]) -> list[dict[str, str]]:
    """解析 Markdown 表格；只接受列数完全匹配的数据行。"""
    rows: list[dict[str, str]] = []
    for line in text.replace("\r\n", "\n").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) != len(columns):
            continue
        if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        if cells[0].casefold() == columns[0].casefold():
            continue
        rows.append(dict(zip(columns, cells, strict=True)))
    return rows


def read_text(path: Path) -> str:
    """优先 UTF-8（允许 BOM）；失败时明确报错，不做有损忽略。"""
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"文件不是 UTF-8 编码：{path}") from exc


def relative_to_root(path: Path, root: Path) -> str:
    """返回相对根目录的正斜杠路径；兼容根目录是 junction/symlink 的情况。"""
    return path.resolve().relative_to(root.resolve()).as_posix()


def read_index(root: Path, relative_path: str, columns: list[str], header: str) -> list[dict[str, str]]:
    path = root / relative_path
    if not path.exists():
        # 索引缺失通常意味着初始化未完成；不要静默扫描目录代替。
        raise FileNotFoundError(f"索引不存在：{relative_path}，请先运行 init_memory_system.sh")
    text = read_text(path)
    rows = parse_index_table(text, columns)
    if header.splitlines()[0] not in text:
        LOGGER.warning("索引 %s 缺少标准表头，仍按表格解析", relative_path)
    return rows


# ---------------------------------------------------------------------------
# 业务操作
# ---------------------------------------------------------------------------


class ToolFailure(Exception):
    """工具级失败；转换为 isError=true 的 MCP 结果。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def ensure_layout(root: Path) -> None:
    """幂等创建目录与最小默认文件；不覆盖已有内容。"""
    dirs = [
        root / "long-term" / "entries",
        root / "mid-term" / "entries",
        root / "short-term",
        root / "archive" / "inbox",
        root / "state",
        root / "logs",
        root / ".locks",
    ]
    for directory in dirs:
        directory.mkdir(parents=True, exist_ok=True)
    defaults = {
        root / "long-term" / "MEMORY-INDEX.md": LONG_TERM_INDEX_HEADER,
        root / "mid-term" / "MEMORY-INDEX.md": MID_TERM_INDEX_HEADER,
        root / "archive" / "INDEX.md": ARCHIVE_INDEX_HEADER,
        root / "SUGGESTIONS.md": SUGGESTIONS_HEADER,
        root / "EMOTION-STYLES.md": DEFAULT_EMOTION_STYLES,
        root / "emotion-state.md": DEFAULT_EMOTION_STATE.format(updated_at=now_iso()),
    }
    for path, content in defaults.items():
        if not path.exists():
            atomic_write_text(path, content)
            LOGGER.info("创建默认文件：%s", path)


def resolve_archive_path(root: Path, file_path: str) -> Path:
    """把工具参数解析为 archive 根目录内的绝对路径，拒绝目录穿越。"""
    raw = str(file_path).strip()
    if not raw:
        raise ToolFailure("invalid_argument", "file_path 不能为空")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=False)
        root_resolved = (root / "archive").resolve(strict=True)
    except OSError as exc:
        raise ToolFailure("invalid_path", f"无法解析路径：{raw}") from exc
    if not resolved.is_relative_to(root_resolved):
        raise ToolFailure("path_outside_archive", f"路径必须位于 archive 目录内：{raw}")
    if resolved.suffix.lower() != ".md":
        raise ToolFailure("invalid_file_type", "只允许读取 Markdown（.md）归档文件")
    return resolved


def archive_read(root: Path, args: dict[str, Any]) -> dict[str, Any]:
    file_path = str(args.get("file_path", ""))
    target = resolve_archive_path(root, file_path)
    if not target.exists() or not target.is_file():
        raise ToolFailure("file_not_found", f"归档文件不存在：{file_path}")
    size = target.stat().st_size
    if size > MAX_ARCHIVE_FILE_BYTES:
        raise ToolFailure("file_too_large", f"归档文件超过 {MAX_ARCHIVE_FILE_BYTES} 字节上限")
    text = read_text(target)
    meta, _body = parse_frontmatter(text)
    return {
        "ok": True,
        "file_path": relative_to_root(target, root),
        "bytes": size,
        "title": str(meta.get("title", "")),
        "content": text,
    }


def archive_search(root: Path, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).strip()
    if not query:
        raise ToolFailure("invalid_argument", "query 不能为空")
    columns = ["id", "title", "domain", "subdomain", "tags", "summary", "path", "created_at", "status"]
    rows = read_index(root, "archive/INDEX.md", columns, ARCHIVE_INDEX_HEADER)

    query_fold = query.casefold()
    tokens = [token.casefold() for token in re.split(r"[\s,，;；]+", query_fold) if token.strip()]
    if not tokens:
        tokens = [query_fold]

    results: list[dict[str, Any]] = []
    for row in rows:
        title = row.get("title", "")
        summary = row.get("summary", "")
        tags = row.get("tags", "")
        domain = row.get("domain", "")
        subdomain = row.get("subdomain", "")
        haystack = " ".join([title, summary, tags, domain, subdomain]).casefold()
        score = 0.0
        if query_fold == title.casefold():
            score += 10
        elif query_fold and query_fold in title.casefold():
            score += 5
        if query_fold and query_fold in tags.casefold():
            score += 4
        if query_fold and query_fold in summary.casefold():
            score += 3
        if query_fold and query_fold in f"{domain} {subdomain}".casefold():
            score += 2
        for token in tokens:
            if token in title.casefold():
                score += 2
            if token in tags.casefold():
                score += 2
            if token in summary.casefold():
                score += 1
            if token in f"{domain} {subdomain}".casefold():
                score += 1
        if score <= 0 and query_fold in haystack:
            score = 1
        if score <= 0:
            continue
        results.append(
            {
                "id": row.get("id", ""),
                "title": title,
                "domain": domain,
                "subdomain": subdomain,
                "tags": normalize_tag_list(tags),
                "summary": summary,
                "path": row.get("path", ""),
                "created_at": row.get("created_at", ""),
                "status": row.get("status", ""),
                "score": round(score, 2),
            }
        )

    # 先按时间倒序，再按分数倒序；Python 稳定排序保证同分时时间新的在前。
    results.sort(key=lambda item: item["created_at"], reverse=True)
    results.sort(key=lambda item: -float(item["score"]))
    return {
        "ok": True,
        "query": query,
        "count": len(results[:MAX_SEARCH_RESULTS]),
        "results": results[:MAX_SEARCH_RESULTS],
    }


def _sanitize_domain(value: str, fallback: str = "inbox") -> str:
    text = str(value).strip().lower()
    text = re.sub(r"[^a-z0-9-]+", "-", text).strip("-")
    if not text:
        return fallback
    return text[:64]


def archive_write(root: Path, args: dict[str, Any]) -> dict[str, Any]:
    title = one_line(str(args.get("title", "")), MAX_TITLE_CHARS)
    content = str(args.get("content", ""))
    if not title:
        raise ToolFailure("invalid_argument", "title 不能为空")
    if not content.strip():
        raise ToolFailure("invalid_argument", "content 不能为空")
    content_bytes = content.encode("utf-8")
    if len(content_bytes) > MAX_ARCHIVE_WRITE_BYTES:
        raise ToolFailure("content_too_large", f"正文超过 {MAX_ARCHIVE_WRITE_BYTES} 字节上限")

    raw_domain = _sanitize_domain(str(args.get("domain", "")), fallback="inbox")
    raw_subdomain = _sanitize_domain(str(args.get("subdomain", "")), fallback="unsorted")
    if raw_domain in {"unknown", "misc", "other", "inbox"}:
        domain, subdomain = "inbox", "unsorted"
    else:
        domain, subdomain = raw_domain, raw_subdomain
    tags = normalize_tag_list(args.get("tags"))
    summary = make_summary(content)
    created_at = now_iso()
    entry_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}"

    month = datetime.now().strftime("%Y-%m")
    if domain == "inbox":
        target_dir = root / "archive" / "inbox" / month
    else:
        target_dir = root / "archive" / domain / subdomain / month
    target_dir.mkdir(parents=True, exist_ok=True)

    base_name = f"{datetime.now().strftime('%Y-%m-%d')}_{safe_slug(title)}"
    lock_path = root / ".locks" / "archive.lock"
    with file_lock(lock_path):
        target: Path | None = None
        for attempt in range(1, 100):
            suffix = "" if attempt == 1 else f"-{attempt}"
            candidate = target_dir / f"{base_name}{suffix}.md"
            try:
                # x 模式保证不覆盖；并发时失败后换后缀。
                with candidate.open("x", encoding="utf-8", newline="\n") as handle:
                    handle.write(_render_archive_markdown(entry_id, title, domain, subdomain, tags, summary, created_at, content))
                    handle.flush()
                    os.fsync(handle.fileno())
                target = candidate
                break
            except FileExistsError:
                continue
        if target is None:
            raise ToolFailure("file_conflict", "同名归档文件过多，请换一个标题")
        relative_path = relative_to_root(target, root)
        _append_index_row(
            root,
            "archive/INDEX.md",
            ARCHIVE_INDEX_HEADER,
            [
                entry_id,
                title,
                domain,
                subdomain,
                ",".join(tags),
                summary,
                relative_path,
                created_at,
                "active",
            ],
        )

    domain_label = DOMAIN_LABELS.get(domain, domain)
    if domain == "inbox":
        confirmation = "先放进 inbox 啦～等分类清晰了再归位。🐟"
    else:
        confirmation = f"记好啦～已收进 {domain_label}/{subdomain}，索引也更新了。🐟"
    return {
        "ok": True,
        "id": entry_id,
        "title": title,
        "domain": domain,
        "subdomain": subdomain,
        "tags": tags,
        "summary": summary,
        "path": relative_path,
        "created_at": created_at,
        "status": "active",
        "index_updated": True,
        "confirmation": confirmation,
    }


def _render_archive_markdown(
    entry_id: str,
    title: str,
    domain: str,
    subdomain: str,
    tags: list[str],
    summary: str,
    created_at: str,
    content: str,
) -> str:
    body = content.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    return (
        "---\n"
        f"id: {yaml_quote(entry_id)}\n"
        f"title: {yaml_quote(title)}\n"
        f"domain: {yaml_quote(domain)}\n"
        f"subdomain: {yaml_quote(subdomain)}\n"
        f"tags: {yaml_list(tags)}\n"
        f"summary: {yaml_quote(summary)}\n"
        f"created_at: {yaml_quote(created_at)}\n"
        f"status: {yaml_quote('active')}\n"
        "---\n\n"
        f"{body}\n"
    )


def _append_index_row(root: Path, relative_index: str, header: str, cells: list[str]) -> None:
    path = root / relative_index
    escaped = [one_line(cell) for cell in cells]
    row = "| " + " | ".join(escaped) + " |\n"
    if path.exists():
        current = read_text(path)
    else:
        current = header
    if not current.endswith("\n"):
        current += "\n"
    if "| --- |" not in current:
        current = header + "\n" + current
    atomic_write_text(path, current + row)


def get_memory_index(root: Path, args: dict[str, Any]) -> dict[str, Any]:
    raw_type = str(args.get("memory_type", "")).strip().lower()
    aliases = {
        "long-term": "long-term",
        "long_term": "long-term",
        "longterm": "long-term",
        "mid-term": "mid-term",
        "mid_term": "mid-term",
        "midterm": "mid-term",
    }
    memory_type = aliases.get(raw_type)
    if memory_type is None:
        raise ToolFailure("invalid_argument", "memory_type 只能是 long-term 或 mid-term")
    relative = f"{memory_type}/MEMORY-INDEX.md"
    header = LONG_TERM_INDEX_HEADER if memory_type == "long-term" else MID_TERM_INDEX_HEADER
    columns = ["id", "title", "type", "tags", "summary", "confidence", "updated_at", "path"]
    rows = read_index(root, relative, columns, header)
    entries = []
    for row in rows:
        confidence: float | None
        try:
            confidence = float(row.get("confidence", ""))
        except ValueError:
            confidence = None
        entries.append(
            {
                "id": row.get("id", ""),
                "title": row.get("title", ""),
                "type": row.get("type", memory_type),
                "tags": normalize_tag_list(row.get("tags", "")),
                "summary": row.get("summary", ""),
                "confidence": confidence,
                "updated_at": row.get("updated_at", ""),
                "path": row.get("path", ""),
            }
        )
    return {
        "ok": True,
        "memory_type": memory_type,
        "index_path": relative,
        "count": len(entries),
        "entries": entries,
    }


def _record_memory_access(root: Path, entry_id: str) -> None:
    """登记一次记忆条目读取，供后台脚本按“最近使用”做衰减。

    设计意图：记忆衰减必须知道“这条记忆多久没被用了”，否则只能按创建时间
    一刀切。这里写的是 state/ 下的访问日志，不碰记忆条目本身；主模型仍然
    只读。写失败只记 WARN，绝不影响读取结果。
    """

    path = root / MEMORY_ACCESS_RELATIVE
    lock_path = root / MEMORY_ACCESS_LOCK
    try:
        with file_lock(lock_path):
            data: dict[str, Any] = {"version": 1, "entries": {}}
            if path.exists():
                try:
                    loaded = json.loads(read_text(path))
                    if isinstance(loaded, dict) and isinstance(loaded.get("entries"), dict):
                        data = loaded
                except (ValueError, json.JSONDecodeError):
                    LOGGER.warning("记忆访问日志损坏，重建：%s", path)
            record = data["entries"].get(entry_id)
            if not isinstance(record, dict):
                record = {"count": 0, "last_accessed_at": ""}
            record["count"] = int(record.get("count", 0)) + 1
            record["last_accessed_at"] = now_iso()
            data["entries"][entry_id] = record
            if len(data["entries"]) > MAX_ACCESS_RECORDS:
                ordered = sorted(
                    data["entries"].items(),
                    key=lambda item: str(item[1].get("last_accessed_at", "")),
                    reverse=True,
                )
                data["entries"] = dict(ordered[:MAX_ACCESS_RECORDS])
            atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    except (OSError, LockTimeout):
        LOGGER.warning("记录记忆访问失败（不影响读取）：%s", entry_id, exc_info=True)


def read_memory_entry(root: Path, args: dict[str, Any]) -> dict[str, Any]:
    entry_id = str(args.get("entry_id", "")).strip()
    if not entry_id or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", entry_id):
        raise ToolFailure("invalid_argument", "entry_id 格式不合法")
    columns = ["id", "title", "type", "tags", "summary", "confidence", "updated_at", "path"]
    for memory_type, relative, header in (
        ("long-term", "long-term/MEMORY-INDEX.md", LONG_TERM_INDEX_HEADER),
        ("mid-term", "mid-term/MEMORY-INDEX.md", MID_TERM_INDEX_HEADER),
    ):
        try:
            rows = read_index(root, relative, columns, header)
        except FileNotFoundError:
            continue
        for row in rows:
            if row.get("id") != entry_id:
                continue
            relative_path = row.get("path", "")
            target = (root / relative_path).resolve(strict=False)
            if not target.is_relative_to(root.resolve()) or target.suffix.lower() != ".md":
                raise ToolFailure("invalid_index", f"索引中的路径不合法：{relative_path}")
            if not target.exists():
                raise ToolFailure("entry_missing", f"索引存在但条目文件缺失：{relative_path}")
            text = read_text(target)
            meta, body = parse_frontmatter(text)
            result = {
                "ok": True,
                "entry_id": entry_id,
                "memory_type": memory_type,
                "path": relative_to_root(target, root),
                "frontmatter": meta,
                "content": body.strip("\n"),
                "raw": text,
            }
            # 登记访问后再返回；失败不影响读取。
            _record_memory_access(root, entry_id)
            return result
    raise ToolFailure("not_found", f"索引中找不到记忆条目：{entry_id}")


def _style_hint(root: Path, emotion: str) -> str:
    path = root / "EMOTION-STYLES.md"
    if not path.exists():
        return "语气平和，简洁自然。"
    try:
        text = read_text(path)
    except (OSError, ValueError):
        return "语气平和，简洁自然。"
    match = re.search(
        rf"^##\s+{re.escape(emotion)}\s*$([\s\S]*?)(?=^##\s+|\Z)",
        text,
        flags=re.M,
    )
    if not match:
        return "语气平和，简洁自然。"
    lines = []
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if stripped.startswith("-"):
            stripped = stripped[1:].strip()
        if stripped:
            lines.append(stripped)
    hint = "；".join(lines)
    return hint[:240] if hint else "语气平和，简洁自然。"


def _read_persona(root: Path) -> tuple[str, str]:
    """读取 persona.md：返回 (updated_at, 正文摘要)。主模型只读。"""

    path = root / "persona.md"
    if not path.is_file():
        return "", ""
    try:
        meta, body = parse_frontmatter(read_text(path))
    except (OSError, ValueError):
        return "", ""
    body = body.strip()
    if body.startswith("# 关于你"):
        body = body[len("# 关于你") :].strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    return str(meta.get("updated_at", "")), body[:1200]


def _read_last_session(root: Path) -> dict[str, Any]:
    """读取“上次聊到哪”：session_id / note / updated_at / age_hours。"""

    path = root / "state" / "last_session.json"
    empty: dict[str, Any] = {"session_id": "", "note": "", "updated_at": "", "age_hours": None}
    if not path.is_file():
        return empty
    try:
        data = json.loads(read_text(path))
    except (OSError, json.JSONDecodeError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    moment = parse_iso_datetime(data.get("updated_at"))
    age_hours: float | None = None
    if moment is not None:
        age_hours = round((datetime.now().astimezone() - moment).total_seconds() / 3600.0, 2)
    return {
        "session_id": str(data.get("session_id", "")),
        "note": str(data.get("note", ""))[:200],
        "updated_at": str(data.get("updated_at", "")),
        "age_hours": age_hours,
    }


def read_emotion_state(root: Path, args: dict[str, Any]) -> dict[str, Any]:
    path = root / "emotion-state.md"
    source = "file"
    if not path.exists():
        meta = {
            "emotion": "平静",
            "intensity": 0.5,
            "updated_at": now_iso(),
            "ttl_hours": 24,
            "reason": "文件缺失，使用默认中性状态",
        }
        source = "default"
    else:
        text = read_text(path)
        meta, _body = parse_frontmatter(text)
    try:
        intensity = float(meta.get("intensity", 0.5))
    except (TypeError, ValueError):
        intensity = 0.5
    intensity = max(0.0, min(1.0, intensity))
    try:
        ttl_hours = int(meta.get("ttl_hours", 24))
    except (TypeError, ValueError):
        ttl_hours = 24
    emotion = str(meta.get("emotion", "平静")) or "平静"
    persona_updated_at, persona_summary = _read_persona(root)
    last_session = _read_last_session(root)
    return {
        "ok": True,
        "emotion": emotion,
        "intensity": round(intensity, 3),
        "updated_at": str(meta.get("updated_at", "")),
        "ttl_hours": ttl_hours,
        "reason": str(meta.get("reason", "")),
        "style_hint": _style_hint(root, emotion),
        "source": source,
        "persona_summary": persona_summary,
        "persona_updated_at": persona_updated_at,
        "last_session": last_session,
    }


# ---------------------------------------------------------------------------
# MCP 工具定义：必须按名称字母序返回，schema 全程不变
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "archive_read",
        "description": "读取 archive 目录内指定 Markdown 归档文件的完整内容。只读；主模型每轮对话最多调用 3 次。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "归档文件路径，建议使用 archive_search 返回的 path（相对根目录或绝对路径均可）。",
                }
            },
            "required": ["file_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "archive_search",
        "description": "只读 archive/INDEX.md，按标题、摘要、标签、领域匹配查询，返回索引条目（不含全文）。归档检索必须先调用本工具。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "自然语言或关键词查询。"}
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "archive_write",
        "description": "把一条笔记归档：自动创建 archive/domain/subdomain/YYYY-MM/ 目录、写入带 frontmatter 的 Markdown、并追加 archive/INDEX.md。分类由调用方决定；不确定时传 domain=inbox。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "简短、可检索的标题。"},
                "domain": {"type": "string", "description": "一级分类，如 computer-science、mathematics、physics、inbox。"},
                "subdomain": {"type": "string", "description": "二级分类，如 networking、embedded-systems；不确定时传 unsorted。"},
                "content": {"type": "string", "description": "Markdown 正文，不要包含 frontmatter；工具会自动生成摘要与元数据。"},
                "tags": {
                    "anyOf": [
                        {"type": "string", "description": "逗号分隔的标签。"},
                        {"type": "array", "items": {"type": "string"}, "description": "标签数组。"},
                    ],
                    "description": "2~5 个检索标签。",
                },
            },
            "required": ["title", "domain", "subdomain", "content", "tags"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_memory_index",
        "description": "只读长期或中期记忆索引，返回条目列表（不含全文）。主模型不写记忆层。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_type": {
                    "type": "string",
                    "enum": ["long-term", "mid-term"],
                    "description": "要读取的记忆层。",
                }
            },
            "required": ["memory_type"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_emotion_state",
        "description": "读取当前情绪标签、强度、风格提示，以及关系档案摘要（persona_summary）和上次会话接续句（last_session）。动态状态只通过本工具进入对话，不写入 System Prompt。",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "read_memory_entry",
        "description": "按 entry_id 从长期/中期记忆索引定位并读取一条记忆的完整内容。只读，不扫描目录。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entry_id": {"type": "string", "description": "记忆索引中的条目 id。"}
            },
            "required": ["entry_id"],
            "additionalProperties": False,
        },
    },
]
# 缓存友好铁律：注册顺序按工具名字母序固定，保证 tools 数组字节级稳定。
TOOL_DEFINITIONS.sort(key=lambda item: item["name"])

TOOL_HANDLERS = {
    "archive_read": archive_read,
    "archive_search": archive_search,
    "archive_write": archive_write,
    "get_memory_index": get_memory_index,
    "read_emotion_state": read_emotion_state,
    "read_memory_entry": read_memory_entry,
}


# ---------------------------------------------------------------------------
# MCP JSON-RPC 循环
# ---------------------------------------------------------------------------


def setup_logging(root: Path, verbose: bool = False) -> None:
    LOGGER.setLevel(logging.DEBUG if verbose else logging.INFO)
    LOGGER.propagate = False
    if LOGGER.handlers:
        return
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    try:
        log_path = root / "logs" / "memory_system.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(formatter)
        LOGGER.addHandler(handler)
    except OSError:
        pass
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(formatter)
    LOGGER.addHandler(stderr_handler)


def _send(message: dict[str, Any]) -> None:
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.buffer.write(payload.encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def _send_result(request_id: Any, result: dict[str, Any]) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _send_error(request_id: Any, code: int, message: str, data: Any | None = None) -> None:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    _send({"jsonrpc": "2.0", "id": request_id, "error": error})


def _tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return {
        "content": [{"type": "text", "text": text}],
        "isError": not bool(payload.get("ok", False)),
        "structuredContent": payload,
    }


def handle_message(message: dict[str, Any], root: Path) -> bool:
    """处理一条 JSON-RPC 消息；返回 False 表示请求退出。"""
    if not isinstance(message, dict):
        _send_error(None, -32600, "Invalid Request")
        return True
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method is None:
        # 客户端发来的响应（本服务端不发起请求）直接忽略。
        return True
    if method == "initialize":
        requested = str(params.get("protocolVersion", "")) if isinstance(params, dict) else ""
        protocol = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
        _send_result(
            request_id,
            {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
        LOGGER.info("初始化完成，协议版本=%s", protocol)
        return True
    if method in {"notifications/initialized", "notifications/cancelled", "notifications/roots/list_changed"}:
        return True
    if method == "ping":
        _send_result(request_id, {})
        return True
    if method == "tools/list":
        _send_result(request_id, {"tools": TOOL_DEFINITIONS})
        return True
    if method == "tools/call":
        if not isinstance(params, dict):
            _send_error(request_id, -32602, "Invalid params")
            return True
        name = params.get("name")
        arguments = params.get("arguments") or {}
        handler = TOOL_HANDLERS.get(str(name))
        if handler is None:
            _send_error(request_id, -32602, f"Unknown tool: {name}")
            return True
        if not isinstance(arguments, dict):
            _send_error(request_id, -32602, "Tool arguments must be an object")
            return True
        try:
            LOGGER.info("调用工具 %s 参数键=%s", name, sorted(arguments.keys()))
            payload = handler(root, arguments)
            _send_result(request_id, _tool_result(payload))
        except ToolFailure as exc:
            LOGGER.warning("工具 %s 失败：%s %s", name, exc.code, exc.message)
            _send_result(request_id, _tool_result({"ok": False, "error": {"code": exc.code, "message": exc.message}}))
        except (OSError, ValueError, LockTimeout) as exc:
            LOGGER.exception("工具 %s 执行异常", name)
            _send_result(
                request_id,
                _tool_result({"ok": False, "error": {"code": "internal_error", "message": str(exc)}}),
            )
        except Exception as exc:  # noqa: BLE001 - 协议层必须兜底，但完整记录堆栈
            LOGGER.exception("工具 %s 未预期异常", name)
            _send_result(
                request_id,
                _tool_result({"ok": False, "error": {"code": "internal_error", "message": f"{type(exc).__name__}: {exc}"}}),
            )
        return True
    if method == "resources/list":
        _send_result(request_id, {"resources": []})
        return True
    if method == "prompts/list":
        _send_result(request_id, {"prompts": []})
        return True
    if method == "logging/setLevel":
        _send_result(request_id, {})
        return True
    if method == "shutdown":
        _send_result(request_id, {})
        return True
    if method == "exit":
        return False
    if request_id is not None:
        _send_error(request_id, -32601, f"Method not found: {method}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DSH 本地记忆系统 MCP stdio 服务端")
    parser.add_argument(
        "--root",
        default=os.environ.get("DSH_MEMORY_ROOT", str(Path.home() / ".dsh" / "memory-system")),
        help="记忆系统根目录",
    )
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser()
    try:
        root.mkdir(parents=True, exist_ok=True)
        ensure_layout(root)
        setup_logging(root, verbose=args.verbose)
    except OSError as exc:
        print(f"memory_system: 无法初始化根目录 {root}: {exc}", file=sys.stderr)
        return 2

    LOGGER.info("MCP 服务端启动，根目录=%s，Python=%s", root, sys.version.split()[0])
    try:
        for raw_line in sys.stdin.buffer:
            line = raw_line.decode("utf-8", errors="strict").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                LOGGER.warning("JSON 解析失败：%s", exc)
                _send_error(None, -32700, "Parse error")
                continue
            if not handle_message(message, root):
                break
    except KeyboardInterrupt:
        LOGGER.info("收到中断，退出")
    except Exception:  # noqa: BLE001
        LOGGER.exception("主循环异常")
        return 1
    finally:
        LOGGER.info("MCP 服务端退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
