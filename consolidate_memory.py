#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH 本地记忆系统的后台整理脚本。

职责
----
1. 扫描 ``short-term/`` 中新增或变化的 Markdown，提炼候选命题；
2. 与 ``long-term/MEMORY-INDEX.md`` 中列出的长期记忆做去重、冲突和更新裁决；
3. 对满足条件的命题自动晋升，其余写入 ``SUGGESTIONS.md``；
4. 按 TTL 衰减 ``emotion-state.md`` 中的情绪强度；
5. 扫描 ``archive/inbox/`` 给出归档分类建议（不移动文件）；
6. 成功后把运行状态写入 ``state/last_run.json``，保证重复运行幂等。

只使用 Python 3.10+ 标准库。日志默认写入 ``logs/consolidate.log``。
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import logging
import math
import os
import re
import secrets
import sys
import time
import traceback
import unicodedata
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# 集中可配置常量（部署时优先改这里，而不是散落在业务代码中）
# ---------------------------------------------------------------------------
CONSOLIDATION_MIN_INTERVAL_HOURS = 6
CONFIDENCE_THRESHOLD = 0.75
EMOTION_DECAY_FACTOR = 0.7
EMOTION_ZERO_THRESHOLD = 0.15
EMOTION_DEFAULT_TTL_HOURS = 24
MAX_SHORT_TERM_FILE_BYTES = 262144
MAX_INBOX_FILES = 30
LLM_TIMEOUT_SECONDS = 60
MAX_CANDIDATES_PER_RUN = 50
MAX_PROPOSITIONS_PER_FILE = 2  # 每份短期记忆最多晋升的命题数，防止一句话被拆成十几条
AUTO_PROMOTE = True

# 记忆代谢（衰减）。与情绪衰减同一思路：纯代码计算，零 Token。
# 一条记忆每经过一个 TTL 周期，置信度乘以 MEMORY_DECAY_FACTOR；被读取过的条目
# 会刷新“最近使用时间”，从而活得久一些（用进废退）。
MEMORY_DECAY_ENABLED = True
MEMORY_DECAY_FACTOR = 0.8          # 每周期保留 80%
MEMORY_DECAY_TTL_DAYS = 30         # 30 天一个周期
LONG_TERM_DEMOTE_BELOW = 0.35      # 长期置信度低于此值 → 降级到中期
MID_TERM_FORGET_BELOW = 0.20       # 中期置信度低于此值 → 退出索引（文件保留，状态归档）
MEMORY_ACCESS_RELATIVE = "state/memory_access.json"

# 以下为内部常量，一般无需调整
SCRIPT_VERSION = "1.0.0"
LONG_TERM_TTL_HOURS = 720
LOCK_TIMEOUT_SECONDS = 30.0
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
MAX_TITLE_CHARS = 40
MAX_SUMMARY_CHARS = 120
MAX_TAGS = 5
MAX_LLM_INPUT_CHARS = 12000
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
STATE_PROCESSED_LIMIT = 500

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

> 后台整理脚本自动追加。确认后由用户/主模型决定是否保留；主模型不直接修改记忆层。
"""

INDEX_COLUMNS = [
    "id",
    "title",
    "type",
    "tags",
    "summary",
    "confidence",
    "updated_at",
    "path",
]

# 规则提炼使用的提示词
UNCERTAINTY_WORDS = (
    "可能",
    "也许",
    "大概",
    "不确定",
    "猜测",
    "待确认",
    "我觉得",
    "似乎",
    "或许",
    "尚未确认",
)
CERTAINTY_WORDS = (
    "记住",
    "确定",
    "事实",
    "配置",
    "版本",
    "路径",
    "命令",
    "必须",
    "不要",
)
NEGATION_WORDS = ("不要", "不", "禁止", "不能", "没有", "非")

# 领域关键词。元组顺序用于同分时的稳定裁决；关键词保留原始大小写，写入 tags 时更好看。
DOMAIN_KEYWORDS: list[tuple[str, str, tuple[str, ...]]] = [
    (
        "computer-science",
        "embedded-systems",
        (
            "嵌入式",
            "单片机",
            "MCU",
            "STM32",
            "ESP32",
            "RTOS",
            "FreeRTOS",
            "GPIO",
            "寄存器",
            "中断",
            "固件",
            "firmware",
            "Arduino",
            "树莓派",
            "Raspberry Pi",
            "I2C",
            "SPI",
            "UART",
            "CAN 总线",
        ),
    ),
    (
        "computer-science",
        "networking",
        (
            "TCP/IP",
            "TCP",
            "UDP",
            "HTTP",
            "HTTPS",
            "DNS",
            "网络",
            "路由",
            "交换机",
            "防火墙",
            "socket",
            "TLS",
            "SSL",
            "IPv4",
            "IPv6",
            "端口",
            "协议",
            "带宽",
            "局域网",
            "以太网",
            "Wi-Fi",
            "WiFi",
            "VPN",
            "CDN",
        ),
    ),
    (
        "computer-science",
        "operating-systems",
        (
            "操作系统",
            "Linux",
            "Windows",
            "内核",
            "进程",
            "线程",
            "内存管理",
            "文件系统",
            "systemd",
            "shell",
            "bash",
            "调度",
            "驱动",
            "虚拟内存",
            "容器",
            "Docker",
            "POSIX",
            "Ubuntu",
            "Debian",
            "CentOS",
        ),
    ),
    (
        "mathematics",
        "mathematics",
        (
            "数学",
            "定理",
            "矩阵",
            "积分",
            "导数",
            "概率",
            "方程",
            "向量",
            "几何",
            "代数",
            "统计学",
            "函数",
            "微积分",
            "离散数学",
        ),
    ),
    (
        "physics",
        "physics",
        (
            "物理",
            "量子",
            "力学",
            "电磁",
            "光子",
            "能量",
            "质量",
            "加速度",
            "电压",
            "电流",
            "电阻",
            "电路",
            "热力学",
            "光学",
        ),
    ),
]

LOGGER = logging.getLogger("consolidate_memory")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass
class Candidate:
    """一条待裁决的候选命题。"""

    text: str
    confidence: float
    tags: list[str] = field(default_factory=list)
    requires_confirmation: bool = False
    source: str = ""
    origin: str = "rule"


@dataclass
class MemoryEntry:
    """长期记忆的比对视图；只保留裁决所需字段。"""

    id: str
    path: str
    text: str
    norm: str
    entities: set[str]
    numbers: set[str]
    has_negation: bool


@dataclass
class PendingSuggestion:
    """准备写入 SUGGESTIONS.md 的待确认记忆。"""

    id: str
    confidence: float
    reason: str
    source: str
    text: str


@dataclass
class InboxSuggestion:
    """准备写入 SUGGESTIONS.md 的归档建议。"""

    path: str
    target: str
    evidence: str


@dataclass
class EmotionResult:
    """情绪衰减结果。changed 表示是否（或 dry-run 下将会）修改文件。"""

    text: str
    changed: bool = False


@dataclass
class DecayResult:
    """记忆代谢结果：刷新/降级/归档的条目数。"""

    text: str
    changed: bool = False
    refreshed: int = 0
    demoted: int = 0
    forgotten: int = 0


@dataclass
class RunStats:
    """一次运行的统计值，最终会写入 state/last_run.json。"""

    scanned_files: int = 0
    processed_files: int = 0
    candidates: int = 0
    promoted: int = 0
    suggestions: int = 0
    conflicts: int = 0
    duplicates: int = 0
    updates: int = 0
    inbox_suggestions: int = 0
    emotion_changes: int = 0
    memory_refreshed: int = 0
    memory_demoted: int = 0
    memory_forgotten: int = 0
    duration_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned_files": self.scanned_files,
            "processed_files": self.processed_files,
            "candidates": self.candidates,
            "promoted": self.promoted,
            "suggestions": self.suggestions,
            "conflicts": self.conflicts,
            "duplicates": self.duplicates,
            "updates": self.updates,
            "inbox_suggestions": self.inbox_suggestions,
            "emotion_changes": self.emotion_changes,
            "memory_refreshed": self.memory_refreshed,
            "memory_demoted": self.memory_demoted,
            "memory_forgotten": self.memory_forgotten,
            "duration_seconds": self.duration_seconds,
        }


class RunSkipped(Exception):
    """非错误的跳过（幂等窗口、已有实例等），退出码为 0。"""


class LockTimeout(RuntimeError):
    """等待文件锁超时。"""


class LLMError(RuntimeError):
    """LLM 调用或响应解析失败。"""


class LLMUnavailable(LLMError):
    """没有可用的 LLM 配置。"""


# ---------------------------------------------------------------------------
# 时间、文本与 YAML 子集
# ---------------------------------------------------------------------------
def local_now() -> datetime:
    """当前本地时间（带时区，精确到秒）。"""

    return datetime.now().astimezone().replace(microsecond=0)


def isoformat_seconds(value: datetime) -> str:
    """输出带时区偏移的 ISO 时间，精确到秒。"""

    if value.tzinfo is None:
        value = value.astimezone()
    return value.replace(microsecond=0).isoformat()


def parse_datetime(value: Any) -> datetime | None:
    """解析常见时间格式；无法解析返回 None，不抛异常。"""

    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.astimezone()
    text = str(value).strip().strip('"').strip("'")
    if not text:
        return None
    candidates = [text]
    if text.endswith("Z") or text.endswith("z"):
        candidates.append(text[:-1] + "+00:00")
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.astimezone()
    return None


def read_text(path: Path) -> str:
    """按 UTF-8 读取（容忍 BOM）；解码失败时明确报错，不做有损忽略。"""

    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"文件不是 UTF-8 编码：{path}") from exc


def _unquote_yaml(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        if value[0] == '"':
            try:
                return str(json.loads(value))
            except (json.JSONDecodeError, TypeError):
                return value[1:-1]
        return value[1:-1].replace("''", "'")
    return value


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """解析本系统使用的极简 YAML frontmatter。

    只支持单行标量、布尔值、数字和数组；不实现完整 YAML，避免引入第三方库。
    """

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized
    match = re.match(r"^---\n(.*?)\n---(?:\n|$)", normalized, flags=re.S)
    if match is None:
        return {}, normalized
    raw = match.group(1)
    body = normalized[match.end() :]
    meta: dict[str, Any] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value.startswith("[") and value.endswith("]"):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = [_unquote_yaml(part) for part in value[1:-1].split(",") if part.strip()]
            if isinstance(parsed, list):
                meta[key] = [str(item) for item in parsed]
            else:
                meta[key] = value
        elif value.lower() in ("true", "false"):
            meta[key] = value.lower() == "true"
        elif re.fullmatch(r"-?\d+", value):
            meta[key] = int(value)
        elif re.fullmatch(r"-?(?:\d+\.\d*|\.\d+)", value):
            meta[key] = float(value)
        else:
            meta[key] = _unquote_yaml(value)
    return meta, body


def render_frontmatter(meta: dict[str, Any]) -> str:
    """把 frontmatter 字典渲染为 YAML 子集；保持传入字典的键顺序。"""

    lines = ["---"]
    for key, value in meta.items():
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (int, float)):
            rendered = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, (list, tuple)):
            rendered = json.dumps([str(item) for item in value], ensure_ascii=False)
        elif value is None:
            rendered = "null"
        else:
            rendered = json.dumps(str(value), ensure_ascii=False)
        lines.append(f"{key}: {rendered}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def one_line(value: Any) -> str:
    """压缩为单行，并转义会破坏 Markdown 表格的竖线。"""

    text = re.sub(r"\s+", " ", str(value)).strip()
    return text.replace("|", "¦")


def clean_proposition(value: str) -> str:
    """清理候选命题：去掉包裹的反引号、列表符号和多余空白。"""

    text = str(value).strip()
    text = re.sub(r"^\s{0,3}(?:#{1,6}|[-*+]|>)\s+", "", text)
    text = text.strip().strip("`").strip()
    return re.sub(r"\s+", " ", text)


def normalize_text(value: str) -> str:
    """归一化：全角转半角、小写、去空白与标点。"""

    folded = unicodedata.normalize("NFKC", str(value)).casefold()
    return "".join(
        char
        for char in folded
        if not char.isspace()
        and not unicodedata.category(char).startswith(("P", "Z", "C"))
    )


def extract_entities(value: str) -> tuple[set[str], set[str]]:
    """提取实体集合与数字集合。

    - 英文/数字 token 保留 ``. / _ - +``；
    - 连续中文生成 2~4 字滑动片段；
    - 数字单独成集合，用于冲突判断。
    """

    folded = unicodedata.normalize("NFKC", str(value)).casefold()
    entities: set[str] = set()
    for match in re.finditer(r"[a-z0-9][a-z0-9._/+\-]*", folded):
        token = match.group(0).strip("._/+-")
        if token:
            entities.add(token)
    numbers = set(re.findall(r"\d+(?:\.\d+)?", folded))
    for run in re.findall(r"[\u4e00-\u9fff]+", folded):
        for length in (2, 3, 4):
            for index in range(0, len(run) - length + 1):
                entities.add(run[index : index + length])
    return entities, numbers


def has_negation(value: str) -> bool:
    """是否出现否定词；按需求做字面包含判断。"""

    return any(word in value for word in NEGATION_WORDS)


def jaccard(left: set[str], right: set[str]) -> float:
    """集合 Jaccard 相似度。"""

    if not left and not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def text_similarity(left: str, right: str) -> float:
    """归一化文本的 SequenceMatcher 相似度。"""

    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def core_proposition(value: str) -> str:
    """去掉否定词、确定性副词和数字，只留命题主干。

    用于识别“必须用 3.11”与“不要用 3.12”这类措辞不同、
    但主干相同的冲突。数字与否定词单独比较，避免被主干抹平。
    """

    folded = normalize_text(value)
    for word in sorted(set(NEGATION_WORDS + CERTAINTY_WORDS), key=len, reverse=True):
        folded = folded.replace(normalize_text(word), "")
    return re.sub(r"\d+(?:\.\d+)?", "", folded)


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
        rows.append(dict(zip(columns, cells)))
    return rows


def normalize_tags(raw: Any, limit: int = MAX_TAGS) -> list[str]:
    """把 frontmatter 中的 tags 归一化为去重后的字符串列表。"""

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
        tag = one_line(part).strip().lstrip("#").strip()
        if not tag or len(tag) > 32:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag)
        if len(tags) >= limit:
            break
    return tags


def domain_keyword_count(haystack: str, keyword: str) -> int:
    """统计关键词出现次数；英文关键词要求单词边界，避免 ``ip`` 命中无关词。"""

    folded_keyword = keyword.casefold()
    if re.fullmatch(r"[a-z0-9 ._+\-/]+", folded_keyword):
        pattern = rf"(?<![a-z0-9]){re.escape(folded_keyword)}(?![a-z0-9])"
        return len(re.findall(pattern, haystack))
    return haystack.count(folded_keyword)


def infer_domain(title: str, tags: list[str], body: str) -> tuple[str, str, str | None]:
    """按关键词映射推断 domain/subdomain，并返回得分最高的关键词。"""

    title_text = unicodedata.normalize("NFKC", title).casefold()
    tag_text = unicodedata.normalize("NFKC", " ".join(tags)).casefold()
    body_text = unicodedata.normalize("NFKC", body[:4000]).casefold()

    best: tuple[int, int, str, str, str | None] | None = None
    for order, (domain, subdomain, keywords) in enumerate(DOMAIN_KEYWORDS):
        score = 0
        best_keyword: str | None = None
        best_keyword_score = 0
        for keyword in keywords:
            folded_keyword = keyword.casefold()
            title_count = domain_keyword_count(title_text, folded_keyword)
            tag_count = domain_keyword_count(tag_text, folded_keyword)
            body_count = min(domain_keyword_count(body_text, folded_keyword), 3)
            keyword_score = title_count * 3 + tag_count * 2 + body_count
            if keyword_score <= 0:
                continue
            score += keyword_score
            candidate_score = keyword_score * 100 + len(keyword)
            if candidate_score > best_keyword_score:
                best_keyword_score = candidate_score
                best_keyword = keyword
        if score <= 0:
            continue
        candidate = (score, -order, domain, subdomain, best_keyword)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    if best is None:
        return "general", "uncategorized", None
    return best[2], best[3], best[4]


def infer_rule_tags(value: str, limit: int = 3) -> list[str]:
    """规则提炼时用领域关键词生成少量 tags。"""

    folded = unicodedata.normalize("NFKC", value).casefold()
    tags: list[str] = []
    seen: set[str] = set()
    for _domain, _subdomain, keywords in DOMAIN_KEYWORDS:
        for keyword in keywords:
            if domain_keyword_count(folded, keyword) <= 0:
                continue
            key = keyword.casefold()
            if key in seen:
                continue
            seen.add(key)
            tags.append(keyword)
            if len(tags) >= limit:
                return tags
    return tags


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def setup_logging(root: Path, verbose: bool, dry_run: bool) -> None:
    """配置控制台日志；非 dry-run 时追加轮转文件日志。"""

    for handler in list(LOGGER.handlers):
        LOGGER.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # 关闭旧 handler 失败不应影响本次运行
            LOGGER.debug("关闭旧日志 handler 失败", exc_info=True)
    LOGGER.setLevel(logging.DEBUG if verbose else logging.INFO)
    LOGGER.propagate = False

    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    LOGGER.addHandler(console)

    if dry_run:
        # dry-run 明确不创建 logs/ 或任何日志文件。
        return

    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "consolidate.log",
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    LOGGER.addHandler(file_handler)


def log_stats(stats: RunStats, emotion_text: str, duration: float, exit_code: int) -> None:
    """按固定字段顺序输出本次运行统计。"""

    LOGGER.info("扫描文件数=%d", stats.scanned_files)
    LOGGER.info("候选数=%d", stats.candidates)
    LOGGER.info("晋升数=%d", stats.promoted)
    LOGGER.info("建议数=%d", stats.suggestions)
    LOGGER.info("冲突数=%d", stats.conflicts)
    LOGGER.info("情绪变化=%s", emotion_text)
    LOGGER.info(
        "记忆代谢：刷新=%d 降级=%d 归档=%d",
        stats.memory_refreshed,
        stats.memory_demoted,
        stats.memory_forgotten,
    )
    LOGGER.info("inbox 建议数=%d", stats.inbox_suggestions)
    LOGGER.info("耗时=%.3fs", duration)
    LOGGER.info("退出码=%d", exit_code)


# ---------------------------------------------------------------------------
# 跨平台文件锁与原子写
# ---------------------------------------------------------------------------
def _busy_error(exc: OSError) -> bool:
    """判断异常是否表示“锁被别人占用”，而不是磁盘/权限故障。"""

    if isinstance(exc, BlockingIOError):
        return True
    return getattr(exc, "errno", None) in {
        errno.EACCES,
        errno.EAGAIN,
        errno.EDEADLK,
        36,  # Windows: 文件被另一进程锁定
    }


@contextmanager
def file_lock(lock_path: Path, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """跨平台独占锁：POSIX 用 fcntl.flock，Windows 用 msvcrt.locking。

    锁文件与目标文件分离，避免目标文件被 os.replace 后锁语义变化。
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
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"等待文件锁超时：{lock_path}") from exc
                time.sleep(0.05)
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


class ProcessLock:
    """根目录 ``.consolidate.lock`` 的进程级互斥锁。

    ``create=False`` 用于 --dry-run：锁文件不存在时不创建，保证 dry-run 零写入；
    存在时仍然参与互斥检查。
    """

    def __init__(self, path: Path, create: bool = True) -> None:
        self.path = path
        self.create = create
        self.handle: Any = None
        self.acquired = False
        self.no_lock = False

    def acquire(self) -> bool:
        if not self.create and not self.path.exists():
            self.no_lock = True
            self.acquired = True
            return True

        self.handle = self.path.open("a+b")
        try:
            self.handle.seek(0, os.SEEK_END)
            if self.handle.tell() == 0:
                if not self.create:
                    # dry-run 不写锁文件；空文件仍尝试直接加锁，失败则视为被占用。
                    self.handle.seek(0)
                else:
                    self.handle.write(b"\0")
                    self.handle.flush()
                    self.handle.seek(0)
            else:
                self.handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if _busy_error(exc):
                    self.handle.close()
                    self.handle = None
                    return False
                raise
            self.acquired = True
            return True
        except Exception:
            if self.handle is not None:
                self.handle.close()
                self.handle = None
            raise

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            if self.acquired:
                if os.name == "nt":
                    import msvcrt

                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            LOGGER.warning("释放进程锁失败：%s", self.path, exc_info=True)
        finally:
            self.handle.close()
            self.handle = None
            self.acquired = False


def lock_path_for(root: Path, target: Path) -> Path:
    """为一次具体写操作生成稳定的锁文件路径（放在 .locks/ 下）。"""

    try:
        resolved = str(target.resolve(strict=False))
    except OSError:
        resolved = str(target.absolute())
    digest = hashlib.sha256(os.path.normcase(resolved).encode("utf-8")).hexdigest()[:24]
    return root / ".locks" / f"write-{digest}.lock"


def atomic_write_text(path: Path, text: str) -> None:
    """临时文件 + os.replace 原子写入。调用方负责持有写锁。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    try:
        with temp_path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                LOGGER.debug("清理临时文件失败：%s", temp_path, exc_info=True)


def locked_write_text(root: Path, path: Path, text: str) -> None:
    """持锁执行原子写。"""

    with file_lock(lock_path_for(root, path)):
        atomic_write_text(path, text)


@contextmanager
def locked_file(root: Path, path: Path) -> Iterator[None]:
    """在读-改-写场景中持锁。"""

    with file_lock(lock_path_for(root, path)):
        yield


def relative_to_root(path: Path, root: Path) -> str:
    """返回相对根目录的正斜杠路径。"""

    resolved = path.resolve(strict=False)
    root_resolved = root.resolve()
    return resolved.relative_to(root_resolved).as_posix()


# ---------------------------------------------------------------------------
# state / 区间检查 / short-term 增量扫描
# ---------------------------------------------------------------------------
def load_state(root: Path) -> dict[str, Any]:
    """读取 state/last_run.json；不存在返回空状态。"""

    path = root / "state" / "last_run.json"
    if not path.exists():
        return {}
    text = read_text(path)
    try:
        state = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"state/last_run.json 不是合法 JSON：{exc}") from exc
    if not isinstance(state, dict):
        raise RuntimeError("state/last_run.json 顶层必须是 JSON 对象")
    if state.get("version") != 1:
        LOGGER.warning("state/last_run.json 的 version=%r，仍按 version=1 读取", state.get("version"))
    return state


def check_interval(state: dict[str, Any], started_at: datetime, force: bool) -> None:
    """不足 6 小时且未 --force 时抛出 RunSkipped。"""

    if force:
        return
    last_success = parse_datetime(state.get("last_success_at"))
    if last_success is None:
        return
    elapsed_hours = (started_at - last_success).total_seconds() / 3600.0
    if elapsed_hours < CONSOLIDATION_MIN_INTERVAL_HOURS:
        raise RunSkipped(
            "距上次成功运行不足 "
            f"{CONSOLIDATION_MIN_INTERVAL_HOURS} 小时"
            f"（last_success_at={isoformat_seconds(last_success)}，"
            f"已过 {max(elapsed_hours, 0.0):.2f} 小时）"
        )


def sha256_file(path: Path) -> str:
    """流式计算文件 sha256。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_short_term(
    root: Path,
    state: dict[str, Any],
    started_at: datetime,
    stats: RunStats,
) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """扫描 short-term 下所有 ``*.md``，返回 (本次哈希表, 待处理列表)。

    待处理列表元素为 ``(相对路径, 全文, 去掉 frontmatter 的正文)``。
    过滤条件：文件时间晚于 last_success_at，且 sha256 与 processed 中记录不同。
    """

    short_root = root / "short-term"
    if not short_root.exists():
        LOGGER.warning("short-term 目录不存在：%s", short_root)
        return {}, []

    processed_raw = state.get("processed")
    processed: dict[str, str] = {}
    if isinstance(processed_raw, dict):
        for key, value in processed_raw.items():
            if isinstance(key, str) and isinstance(value, str):
                processed[key] = value
    elif processed_raw is not None:
        LOGGER.warning("state.processed 不是对象，已忽略")

    last_success = parse_datetime(state.get("last_success_at"))
    hashes: dict[str, str] = {}
    eligible: list[tuple[str, str, str]] = []
    files = sorted(
        (path for path in short_root.rglob("*.md") if path.is_file() and path.name.lower() != "readme.md"),
        key=lambda item: item.as_posix().casefold(),
    )
    stats.scanned_files = len(files)

    for path in files:
        try:
            relative = relative_to_root(path, root)
        except ValueError:
            LOGGER.warning("跳过根目录之外的符号链接文件：%s", path)
            continue
        try:
            stat = path.stat()
            digest = sha256_file(path)
            hashes[relative] = digest
            if stat.st_size > MAX_SHORT_TERM_FILE_BYTES:
                LOGGER.warning(
                    "跳过大文件（%d > %d 字节）：%s",
                    stat.st_size,
                    MAX_SHORT_TERM_FILE_BYTES,
                    relative,
                )
                continue
            text = read_text(path)
            meta, body = parse_frontmatter(text)
            updated_raw = meta.get("updated_at")
            updated_at = parse_datetime(updated_raw)
            if updated_at is None:
                if updated_raw not in (None, ""):
                    LOGGER.warning("无法解析 updated_at=%r，改用 mtime：%s", updated_raw, relative)
                updated_at = datetime.fromtimestamp(stat.st_mtime).astimezone()
                updated_at = updated_at.replace(microsecond=0)
        except (OSError, ValueError) as exc:
            LOGGER.exception("读取短期记忆失败：%s（%s）", path, exc)
            raise

        if last_success is not None and updated_at <= last_success:
            LOGGER.debug(
                "跳过未更新时间戳的文件：%s（%s <= %s）",
                relative,
                isoformat_seconds(updated_at),
                isoformat_seconds(last_success),
            )
            continue
        if processed.get(relative) == digest:
            LOGGER.debug("跳过内容未变化的文件：%s", relative)
            continue
        eligible.append((relative, text, body))

    stats.processed_files = len(eligible)
    return hashes, eligible


# ---------------------------------------------------------------------------
# 长期记忆读取与裁决
# ---------------------------------------------------------------------------
def extract_body_proposition(body: str, fallback: str = "") -> str:
    """从长期记忆正文中提取命题原文（跳过标题和“来源/说明”段）。"""

    lines: list[str] = []
    for line in body.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        stripped = line.strip()
        if stripped.startswith("## 来源") or stripped.startswith("## 说明"):
            break
        if not lines and stripped.startswith("# "):
            continue
        if stripped:
            lines.append(stripped)
    text = clean_proposition(" ".join(lines))
    if text:
        return text
    return clean_proposition(fallback)


def load_long_term_entries(root: Path) -> list[MemoryEntry]:
    """只按 long-term/MEMORY-INDEX.md 中列出的 path 读取条目，禁止扫描 entries 目录。"""

    index_path = root / "long-term" / "MEMORY-INDEX.md"
    if not index_path.exists():
        LOGGER.warning("长期索引不存在，按空索引处理：%s", index_path)
        return []
    text = read_text(index_path)
    if LONG_TERM_INDEX_HEADER.splitlines()[0] not in text:
        LOGGER.warning("长期索引缺少标准标题，仍尝试解析表格：%s", index_path)
    rows = parse_index_table(text, INDEX_COLUMNS)
    entries: list[MemoryEntry] = []
    root_resolved = root.resolve()
    for row in rows:
        relative = row.get("path", "").strip().replace("\\", "/")
        if not relative:
            LOGGER.warning("长期索引存在空 path 行，已跳过：id=%r", row.get("id"))
            continue
        if not relative.startswith("long-term/entries/"):
            LOGGER.warning("长期索引 path 不在 long-term/entries/ 下，已跳过：%s", relative)
            continue
        target = (root / relative).resolve(strict=False)
        if not target.is_relative_to(root_resolved):
            LOGGER.warning("长期索引 path 越出根目录，已跳过：%s", relative)
            continue
        if not target.is_file():
            LOGGER.warning("长期索引列出的条目不存在：%s", relative)
            continue
        entry_text = read_text(target)
        meta, body = parse_frontmatter(entry_text)
        proposition = extract_body_proposition(
            body,
            fallback=str(meta.get("summary") or meta.get("title") or ""),
        )
        if not proposition:
            LOGGER.warning("长期条目正文为空，仍以标题参与去重：%s", relative)
            proposition = clean_proposition(str(meta.get("title") or ""))
        entities, numbers = extract_entities(proposition)
        entry_id = str(meta.get("id") or row.get("id") or target.stem)
        entries.append(
            MemoryEntry(
                id=entry_id,
                path=relative,
                text=proposition,
                norm=normalize_text(proposition),
                entities=entities,
                numbers=numbers,
                has_negation=has_negation(proposition),
            )
        )
    LOGGER.debug("从索引读取长期记忆 %d 条", len(entries))
    return entries


def judge_candidate(
    candidate: Candidate,
    entries: list[MemoryEntry],
) -> tuple[str, MemoryEntry | None]:
    """裁决候选：duplicate / conflict / update / new（严格按需求顺序）。"""

    norm = normalize_text(candidate.text)
    if not norm:
        return "duplicate", None

    # 1. 归一化后完全相同。
    for entry in entries:
        if entry.norm and entry.norm == norm:
            return "duplicate", entry

    # 2. SequenceMatcher 相似度 >= 0.85 一律视为重复。
    best_similarity = 0.0
    best_similarity_entry: MemoryEntry | None = None
    for entry in entries:
        if not entry.norm:
            continue
        similarity = text_similarity(norm, entry.norm)
        if similarity > best_similarity:
            best_similarity = similarity
            best_similarity_entry = entry
    if best_similarity >= 0.85 and best_similarity_entry is not None:
        return "duplicate", best_similarity_entry

    # 3. 相似度不足 0.85 时，再看实体 Jaccard；否定/数字不同则冲突，否则更新候选。
    candidate_entities, candidate_numbers = extract_entities(candidate.text)
    candidate_negation = has_negation(candidate.text)
    best_jaccard = 0.0
    best_jaccard_entry: MemoryEntry | None = None
    for entry in entries:
        score = jaccard(candidate_entities, entry.entities)
        if score > best_jaccard:
            best_jaccard = score
            best_jaccard_entry = entry
    if best_jaccard >= 0.5 and best_jaccard_entry is not None:
        if (
            candidate_negation != best_jaccard_entry.has_negation
            or candidate_numbers != best_jaccard_entry.numbers
        ):
            return "conflict", best_jaccard_entry
        return "update", best_jaccard_entry
    return "new", None


def make_entry_id(now: datetime, existing_ids: set[str], root: Path) -> str:
    """生成 ``YYYYMMDD-HHMMSS-<6位hex>`` 形式的唯一 id。"""

    prefix = now.strftime("%Y%m%d-%H%M%S")
    for _attempt in range(20):
        candidate = f"{prefix}-{secrets.token_hex(3)}"
        if candidate in existing_ids:
            continue
        if (root / "long-term" / "entries" / f"{candidate}.md").exists():
            continue
        return candidate
    raise RuntimeError("无法生成唯一的长期记忆 id")


def render_long_term_entry(
    entry_id: str,
    candidate: Candidate,
    domain: str,
    subdomain: str,
    title: str,
    summary: str,
    now: datetime,
) -> str:
    """按固定字段顺序渲染长期记忆 Markdown。"""

    timestamp = isoformat_seconds(now)
    tags_json = json.dumps(candidate.tags, ensure_ascii=False)
    sources_json = json.dumps([candidate.source], ensure_ascii=False)
    return (
        "---\n"
        f'id: {json.dumps(entry_id, ensure_ascii=False)}\n'
        f'title: {json.dumps(title, ensure_ascii=False)}\n'
        f'domain: {json.dumps(domain, ensure_ascii=False)}\n'
        f'subdomain: {json.dumps(subdomain, ensure_ascii=False)}\n'
        'type: "long-term"\n'
        f"tags: {tags_json}\n"
        f'summary: {json.dumps(summary, ensure_ascii=False)}\n'
        f'created_at: {json.dumps(timestamp, ensure_ascii=False)}\n'
        f'updated_at: {json.dumps(timestamp, ensure_ascii=False)}\n'
        'status: "active"\n'
        f"confidence: {json.dumps(round(float(candidate.confidence), 4))}\n"
        f"sources: {sources_json}\n"
        f"ttl_hours: {LONG_TERM_TTL_HOURS}\n"
        "---\n\n"
        f"# {title}\n\n"
        f"{candidate.text}\n\n"
        "## 来源\n"
        f"- {candidate.source}\n\n"
        "## 说明\n"
        f"本条由后台整理脚本自动晋升，置信度 {candidate.confidence:.2f}；"
        "如与事实不符，请在 SUGGESTIONS.md 中提出修正。\n"
    )


def append_index_row(root: Path, row: str) -> None:
    """持锁读取-追加长期索引行；索引缺失时用固定表头初始化。"""

    path = root / "long-term" / "MEMORY-INDEX.md"
    with locked_file(root, path):
        if path.exists():
            current = read_text(path)
        else:
            current = LONG_TERM_INDEX_HEADER
        if not current.strip():
            current = LONG_TERM_INDEX_HEADER
        elif "| --- |" not in current:
            current = LONG_TERM_INDEX_HEADER + "\n" + current.lstrip()
        if not current.endswith("\n"):
            current += "\n"
        atomic_write_text(path, current + row + "\n")


def promote_candidate(
    root: Path,
    candidate: Candidate,
    now: datetime,
    existing_ids: set[str],
    dry_run: bool,
    entries: list[MemoryEntry],
) -> str:
    """写入长期条目并追加索引；dry-run 只记录动作。"""

    domain, subdomain, _keyword = infer_domain(candidate.text, candidate.tags, candidate.text)
    entry_id = make_entry_id(now, existing_ids, root)
    title = one_line(candidate.text)[:MAX_TITLE_CHARS]
    summary = one_line(candidate.text)[:MAX_SUMMARY_CHARS]
    relative = f"long-term/entries/{entry_id}.md"
    existing_ids.add(entry_id)
    if dry_run:
        LOGGER.info(
            "[dry-run] 将晋升：%s -> %s（置信度 %.2f，%s/%s）",
            candidate.source,
            relative,
            candidate.confidence,
            domain,
            subdomain,
        )
    else:
        rendered = render_long_term_entry(entry_id, candidate, domain, subdomain, title, summary, now)
        locked_write_text(root, root / relative, rendered)
        row = (
            f"| {entry_id} | {one_line(title)} | long-term | "
            f"{one_line(', '.join(candidate.tags))} | {one_line(summary)} | "
            f"{candidate.confidence:.2f} | {isoformat_seconds(now)} | {relative} |"
        )
        append_index_row(root, row)
        LOGGER.info(
            "晋升：%s -> %s（置信度 %.2f，%s/%s）",
            candidate.source,
            relative,
            candidate.confidence,
            domain,
            subdomain,
        )
    entities, numbers = extract_entities(candidate.text)
    entries.append(
        MemoryEntry(
            id=entry_id,
            path=relative,
            text=candidate.text,
            norm=normalize_text(candidate.text),
            entities=entities,
            numbers=numbers,
            has_negation=has_negation(candidate.text),
        )
    )
    return relative


def suggestion_reason(candidate: Candidate, kind: str, matched: MemoryEntry | None) -> str:
    """生成 SUGGESTIONS.md 中的人类可读原因。"""

    if kind == "conflict":
        matched_id = matched.id if matched else "未知"
        return f"与已有记忆冲突（{matched_id}）"
    if kind == "update":
        matched_id = matched.id if matched else "未知"
        return f"疑似更新已有记忆（{matched_id}）"
    reasons: list[str] = []
    if candidate.confidence < CONFIDENCE_THRESHOLD:
        reasons.append("低于阈值")
    if candidate.requires_confirmation:
        reasons.append("需人工确认")
    if not AUTO_PROMOTE:
        reasons.append("自动晋升已关闭")
    return "；".join(reasons) if reasons else "未自动晋升"


def render_suggestion_line(suggestion: PendingSuggestion) -> str:
    """渲染一条待确认记忆。"""

    return (
        f"- [ ] `{suggestion.id}` | 置信度 {suggestion.confidence:.2f} | "
        f"原因：{one_line(suggestion.reason)} | 来源：{one_line(suggestion.source)} | "
        f"{one_line(suggestion.text)}"
    )


def render_inbox_line(item: InboxSuggestion) -> str:
    """渲染一条归档建议。"""

    return f"- [ ] `{item.path}` → `{item.target}`（依据：{one_line(item.evidence)}）"


# ---------------------------------------------------------------------------
# 候选提炼：LLM 优先，规则兜底
# ---------------------------------------------------------------------------
@dataclass
class LLMSettings:
    """LLM 连接参数。"""

    provider: str
    base_url: str
    api_key: str
    model: str


def _http_json(url: str, payload: dict[str, Any] | None, headers: dict[str, str], timeout: float) -> Any:
    """发送 HTTP 请求并解析 JSON；HTTPError 会附带响应体片段。"""

    data = None
    request_headers = {"Accept": "application/json", **headers}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, data=data, headers=request_headers, method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            detail = ""
        raise LLMError(f"HTTP {exc.code}：{detail or exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LLMError(f"网络请求失败：{exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError(f"LLM 返回的不是合法 JSON：{raw[:300]}") from exc


def discover_ollama_model(base_url: str) -> tuple[str, str] | None:
    """探测 Ollama 并返回 (base_url, 第一个可用模型)；不可用返回 None。"""

    base = (base_url or DEFAULT_OLLAMA_BASE_URL).rstrip("/")
    try:
        data = _http_json(base + "/api/tags", None, {}, timeout=min(3.0, float(LLM_TIMEOUT_SECONDS)))
    except LLMError as exc:
        LOGGER.debug("Ollama 探测失败：%s", exc)
        return None
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return None
    for model in models:
        if not isinstance(model, dict):
            continue
        name = model.get("name") or model.get("model")
        if isinstance(name, str) and name.strip():
            return base, name.strip()
    return None


def build_llm_settings() -> LLMSettings:
    """根据环境变量选择 LLM；OpenAI 兼容优先，Ollama 次之。"""

    provider = os.environ.get("MEMORY_LLM_PROVIDER", "").strip().lower()
    base_url = os.environ.get("MEMORY_LLM_BASE_URL", "").strip()
    api_key = os.environ.get("MEMORY_LLM_API_KEY", "").strip()
    model = os.environ.get("MEMORY_LLM_MODEL", "").strip()

    if provider == "ollama":
        base = base_url or DEFAULT_OLLAMA_BASE_URL
        if not model:
            discovered = discover_ollama_model(base)
            if discovered is None:
                raise LLMUnavailable("MEMORY_LLM_PROVIDER=ollama，但无法发现可用模型")
            base, model = discovered
        return LLMSettings("ollama", base, "", model)

    if provider and provider not in ("openai", "openai-compatible"):
        LOGGER.warning("未知 MEMORY_LLM_PROVIDER=%r，按默认探测逻辑处理", provider)

    if base_url or api_key or model:
        if base_url and api_key and model:
            return LLMSettings("openai", base_url.rstrip("/"), api_key, model)
        LOGGER.warning(
            "OpenAI 兼容 LLM 配置不完整（需要 MEMORY_LLM_BASE_URL/MEMORY_LLM_API_KEY/"
            "MEMORY_LLM_MODEL），尝试探测 Ollama"
        )

    discovered = discover_ollama_model(base_url or DEFAULT_OLLAMA_BASE_URL)
    if discovered is not None:
        base, discovered_model = discovered
        return LLMSettings("ollama", base, "", model or discovered_model)
    raise LLMUnavailable("未配置 OpenAI 兼容 LLM，且未探测到 Ollama")


def _llm_prompt(body: str, source: str) -> str:
    """构造小模型提炼提示词。"""

    clipped = body[:MAX_LLM_INPUT_CHARS]
    return (
        "你是本地记忆整理助手。请从下面的短期记忆 Markdown 中提取适合长期保存的原子命题。\n"
        "只输出一个 JSON 对象，不要 Markdown 代码块，不要解释。格式必须是：\n"
        '{"propositions":[{"text":"命题原文","confidence":0.0,"tags":["标签"],'
        '"requires_confirmation":false}]}\n'
        "要求：\n"
        "1. 每条 text 是独立、可验证的中文/英文命题，长度 3~500 字符；\n"
        "2. confidence 是 0~1 的数字；tags 最多 5 个；\n"
        "3. 不确定、猜测、需要用户确认的内容，requires_confirmation 设为 true；\n"
        "4. 不要复述标题、代码块、纯符号或来源说明；\n"
        "5. 最多 2 条：只保留最值得长期记住的事实，不要把一句话拆成多条，"
        "不要提取清单式细节。\n\n"
        f"短期记忆文件：{source}\n"
        "--- 内容开始 ---\n"
        f"{clipped}\n"
        "--- 内容结束 ---\n"
    )


def call_openai_compatible(settings: LLMSettings, prompt: str) -> str:
    """调用 OpenAI 兼容 /chat/completions，返回 message.content。"""

    payload = {
        "model": settings.model,
        "messages": [
            {"role": "system", "content": "你只输出合法 JSON 对象。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
    }
    data = _http_json(
        settings.base_url + "/chat/completions",
        payload,
        {"Authorization": f"Bearer {settings.api_key}"},
        float(LLM_TIMEOUT_SECONDS),
    )
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError("OpenAI 兼容响应缺少 choices[0].message.content") from exc
    if isinstance(content, list):
        content = "".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content
        )
    if not isinstance(content, str):
        raise LLMError("OpenAI 兼容响应 content 不是字符串")
    return content


def call_ollama(settings: LLMSettings, prompt: str) -> str:
    """调用 Ollama /api/chat，stream=false，format=json。"""

    payload = {
        "model": settings.model,
        "messages": [
            {"role": "system", "content": "你只输出合法 JSON 对象。"},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.1},
    }
    data = _http_json(
        settings.base_url.rstrip("/") + "/api/chat",
        payload,
        {},
        float(LLM_TIMEOUT_SECONDS),
    )
    content: Any
    if isinstance(data, dict) and isinstance(data.get("message"), dict):
        content = data["message"].get("content")
    elif isinstance(data, dict):
        content = data.get("response")
    else:
        content = None
    if not isinstance(content, str) or not content.strip():
        raise LLMError("Ollama 响应缺少 message.content")
    return content


def extract_json_object(raw: str) -> dict[str, Any]:
    """从可能带代码围栏的 LLM 输出中截取 JSON 对象。"""

    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise LLMError("LLM 输出中没有 JSON 对象")
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"LLM JSON 解析失败：{exc}") from exc
    if not isinstance(parsed, dict):
        raise LLMError("LLM 顶层 JSON 必须是对象")
    return parsed


def validate_llm_propositions(payload: dict[str, Any], source: str) -> list[Candidate]:
    """校验 LLM 返回的 propositions；非法条目丢弃，超长文本截断。"""

    raw_propositions = payload.get("propositions")
    if not isinstance(raw_propositions, list):
        raise LLMError("LLM JSON 缺少 propositions 数组")
    candidates: list[Candidate] = []
    for item in raw_propositions:
        if not isinstance(item, dict):
            LOGGER.warning("丢弃非对象候选：%r", item)
            continue
        raw_text = item.get("text")
        if not isinstance(raw_text, str):
            LOGGER.warning("丢弃 text 非字符串的候选：%r", item)
            continue
        text = clean_proposition(raw_text)
        if len(text) < 3:
            LOGGER.warning("丢弃过短候选：%r", text)
            continue
        if len(text) > 500:
            LOGGER.warning("截断超长候选（%d -> 500 字符）", len(text))
            text = text[:500]
        if not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", text):
            LOGGER.warning("丢弃纯符号候选：%r", text)
            continue
        raw_confidence = item.get("confidence", 0.5)
        try:
            confidence = float(raw_confidence)
        except (TypeError, ValueError):
            LOGGER.warning("丢弃 confidence 非数字的候选：%r", item)
            continue
        confidence = max(0.0, min(1.0, confidence))
        tags = normalize_tags(item.get("tags", []), limit=MAX_TAGS)
        requires_confirmation = bool(item.get("requires_confirmation", False))
        candidates.append(
            Candidate(
                text=text,
                confidence=round(confidence, 4),
                tags=tags,
                requires_confirmation=requires_confirmation,
                source=source,
                origin="llm",
            )
        )
    if len(candidates) > MAX_PROPOSITIONS_PER_FILE:
        # 小模型有时把一句话拆成十几条琐碎命题；按置信度保留前 N 条，避免长期记忆膨胀。
        kept = sorted(candidates, key=lambda item: item.confidence, reverse=True)[
            :MAX_PROPOSITIONS_PER_FILE
        ]
        order = {id(item): index for index, item in enumerate(candidates)}
        LOGGER.info(
            "LLM 返回 %d 条命题，按置信度保留前 %d 条：%s",
            len(candidates),
            MAX_PROPOSITIONS_PER_FILE,
            source,
        )
        candidates = sorted(kept, key=lambda item: order[id(item)])
    return candidates


def extract_with_llm(body: str, source: str) -> list[Candidate]:
    """调用当前可用 LLM；OpenAI 失败时再尝试 Ollama。"""

    settings = build_llm_settings()
    prompt = _llm_prompt(body, source)
    try:
        if settings.provider == "ollama":
            raw = call_ollama(settings, prompt)
        else:
            raw = call_openai_compatible(settings, prompt)
    except LLMError as exc:
        LOGGER.warning("LLM（%s）调用失败：%s", settings.provider, exc)
        if settings.provider == "openai":
            discovered = discover_ollama_model(DEFAULT_OLLAMA_BASE_URL)
            if discovered is not None:
                base, model = discovered
                LOGGER.warning("尝试回退到 Ollama 模型：%s", model)
                raw = call_ollama(LLMSettings("ollama", base, "", model), prompt)
            else:
                raise
        else:
            raise
    payload = extract_json_object(raw)
    candidates = validate_llm_propositions(payload, source)
    if not candidates:
        raise LLMError("LLM 没有返回合法候选")
    return candidates


def split_rule_blocks(body: str) -> Iterator[str]:
    """按标题、代码块、空行和列表项切分规则提炼的文本块。"""

    current: list[str] = []
    in_code = False

    def flush() -> Iterator[str]:
        nonlocal current
        if current:
            yield " ".join(current)
            current = []

    for line in body.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            yield from flush()
            in_code = not in_code
            continue
        if in_code:
            continue
        if not stripped:
            yield from flush()
            continue
        if re.match(r"^\s{0,3}#{1,6}\s+", line):
            yield from flush()
            continue
        if stripped.startswith("|"):
            continue
        bullet = re.match(r"^\s*(?:[-*+]|\d+[.、)])\s+(.*)$", line)
        if bullet:
            yield from flush()
            current = [bullet.group(1).strip()]
            continue
        current.append(stripped)
    yield from flush()


def split_rule_sentences(block: str) -> list[str]:
    """按中英文句末标点切句。"""

    parts = re.split(r"(?<=[。！？!?；;])\s*|(?<=[.!?])\s+(?=[A-Z0-9])", block)
    return [part.strip() for part in parts if part.strip()]


def extract_rule_candidates(body: str, source: str) -> list[Candidate]:
    """规则提炼：切段/句、过滤噪声、按词表计算置信度。"""

    candidates: list[Candidate] = []
    seen: set[str] = set()
    for block in split_rule_blocks(body):
        for part in split_rule_sentences(block):
            text = clean_proposition(part)
            text = re.sub(r"^[\s:：,，;；]+|[\s:：,，;；]+$", "", text).strip()
            if len(text) < 6 or len(text) > 500:
                continue
            if not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", text):
                continue
            if re.fullmatch(r"[-_=*#\s]+", text):
                continue
            if re.fullmatch(r".{0,12}[:：]", text):
                continue
            if text.startswith(("http://", "https://")) and len(text) < 40:
                continue
            norm = normalize_text(text)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            confidence = 0.55
            if any(word in text for word in CERTAINTY_WORDS):
                confidence += 0.15
            if 10 <= len(text) <= 200:
                confidence += 0.05
            confidence = round(min(0.9, confidence), 2)
            requires_confirmation = any(word in text for word in UNCERTAINTY_WORDS)
            candidates.append(
                Candidate(
                    text=text,
                    confidence=confidence,
                    tags=infer_rule_tags(text),
                    requires_confirmation=requires_confirmation,
                    source=source,
                    origin="rule",
                )
            )
    return candidates


def extract_candidates_for_text(body: str, source: str, no_llm: bool) -> list[Candidate]:
    """单文件候选提炼：LLM 优先，失败/不可用自动回退规则。"""

    if not no_llm:
        try:
            candidates = extract_with_llm(body, source)
            LOGGER.info("LLM 提炼：%s -> %d 条候选", source, len(candidates))
            return candidates
        except LLMUnavailable as exc:
            LOGGER.warning("LLM 不可用，回退规则提炼：%s", exc)
        except Exception as exc:  # LLM 失败不得导致整体失败
            LOGGER.warning("LLM 提炼失败，回退规则提炼：%s（%s）", source, exc)
    else:
        LOGGER.debug("--no-llm：使用规则提炼 %s", source)
    return extract_rule_candidates(body, source)


# ---------------------------------------------------------------------------
# 情绪衰减
# ---------------------------------------------------------------------------
def process_emotion(root: Path, now: datetime, dry_run: bool) -> EmotionResult:
    """按 TTL 周期衰减情绪强度；只有 periods >= 1 时才写文件。"""

    path = root / "emotion-state.md"
    if not path.exists():
        LOGGER.info("情绪状态文件不存在，跳过情绪衰减")
        return EmotionResult("文件不存在", changed=False)
    text = read_text(path)
    meta, body = parse_frontmatter(text)
    if not meta:
        LOGGER.warning("情绪状态缺少 frontmatter，跳过情绪衰减")
        return EmotionResult("解析失败", changed=False)

    emotion = str(meta.get("emotion", "平静") or "平静")
    try:
        old_intensity = float(meta.get("intensity", 0.0))
    except (TypeError, ValueError):
        LOGGER.warning("情绪 intensity 非数字，跳过情绪衰减：%r", meta.get("intensity"))
        return EmotionResult("解析失败", changed=False)
    old_intensity = max(0.0, min(1.0, old_intensity))
    updated_at = parse_datetime(meta.get("updated_at"))
    if updated_at is None:
        LOGGER.warning("情绪 updated_at 缺失或无法解析，跳过情绪衰减：%r", meta.get("updated_at"))
        return EmotionResult("解析失败", changed=False)
    try:
        ttl_hours = float(meta.get("ttl_hours", EMOTION_DEFAULT_TTL_HOURS))
    except (TypeError, ValueError):
        ttl_hours = float(EMOTION_DEFAULT_TTL_HOURS)
    if ttl_hours <= 0:
        LOGGER.warning("情绪 ttl_hours=%r 非法，改用默认 %d", meta.get("ttl_hours"), EMOTION_DEFAULT_TTL_HOURS)
        ttl_hours = float(EMOTION_DEFAULT_TTL_HOURS)

    elapsed_hours = (now - updated_at).total_seconds() / 3600.0
    periods = max(0, math.floor(elapsed_hours / ttl_hours))
    if periods < 1:
        LOGGER.debug("情绪无需衰减：elapsed=%.2fh ttl=%.2fh periods=%d", elapsed_hours, ttl_hours, periods)
        return EmotionResult(f"{old_intensity:.4f} 无变化（周期=0）", changed=False)

    new_intensity = round(max(0.0, min(1.0, old_intensity * (EMOTION_DECAY_FACTOR**periods))), 4)
    new_emotion = emotion
    reason_added = False
    if new_intensity < EMOTION_ZERO_THRESHOLD:
        new_intensity = 0.0
        new_emotion = "平静"
        reason = str(meta.get("reason", "") or "")
        if "强度已衰减归零" not in reason:
            reason = f"{reason}；强度已衰减归零" if reason else "强度已衰减归零"
            meta["reason"] = reason
            reason_added = True

    meta["emotion"] = new_emotion
    meta["intensity"] = new_intensity
    meta["updated_at"] = isoformat_seconds(now)
    if "ttl_hours" not in meta:
        meta["ttl_hours"] = int(ttl_hours) if ttl_hours.is_integer() else ttl_hours

    marker_start = "<!-- consolidate:emotion:start -->"
    marker_end = "<!-- consolidate:emotion:end -->"
    block = (
        f"{marker_start}\n"
        "## 整理状态\n\n"
        f"- 当前情绪：{new_emotion}\n"
        f"- 当前强度：{new_intensity:.4f}\n"
        f"- 本次周期数：{periods}（TTL {ttl_hours:g} 小时）\n"
        f"- 最近更新：{isoformat_seconds(now)}\n"
        f"{marker_end}"
    )
    pattern = re.compile(
        re.escape(marker_start) + r".*?" + re.escape(marker_end), flags=re.S
    )
    # 正文由脚本系统维护：重写“当前情绪”摘要，避免 frontmatter 已归零、
    # 正文还写着旧情绪（例如“开心”）的不一致。
    summary_line = (
        f"{new_emotion}，强度 {new_intensity:.2f}。"
        f"最近更新：{isoformat_seconds(now)}。"
    )
    new_body = f"# 当前情绪\n\n{summary_line}\n\n{block}"
    if pattern.search(body):
        # 保留旧状态块之外的用户备注（如果有），只替换旧摘要与状态块。
        old_without_block = pattern.sub("", body).strip("\n")
        old_without_block = re.sub(
            r"^#\s*当前情绪\s*", "", old_without_block
        ).strip("\n")
        if old_without_block:
            new_body = (
                f"# 当前情绪\n\n{summary_line}\n\n"
                f"{old_without_block}\n\n{block}"
            )
    new_text = render_frontmatter(meta) + "\n" + new_body.strip("\n") + "\n"

    if dry_run:
        LOGGER.info(
            "[dry-run] 将衰减情绪：%s %.4f -> %.4f（周期=%d，reason_added=%s）",
            new_emotion,
            old_intensity,
            new_intensity,
            periods,
            reason_added,
        )
    else:
        locked_write_text(root, path, new_text)
        LOGGER.info(
            "情绪衰减：%.4f -> %.4f（周期=%d，情绪=%s）",
            old_intensity,
            new_intensity,
            periods,
            new_emotion,
        )
    return EmotionResult(
        f"{old_intensity:.4f}->{new_intensity:.4f}（周期={periods}）",
        changed=True,
    )


# ---------------------------------------------------------------------------
# 记忆代谢（衰减）
# ---------------------------------------------------------------------------
def load_memory_access(root: Path) -> dict[str, datetime]:
    """读取 state/memory_access.json，返回 {entry_id: 最近访问时间}。

    访问日志由 MCP 端的 ``read_memory_entry`` 登记；缺失或损坏时按“从未读过”
    处理，衰减只会更保守（按 updated_at 计算），不会误删内容。
    """

    path = root / MEMORY_ACCESS_RELATIVE
    if not path.exists():
        return {}
    try:
        data = json.loads(read_text(path))
    except (OSError, ValueError):
        LOGGER.warning("记忆访问日志损坏，按空日志处理：%s", path)
        return {}
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return {}
    result: dict[str, datetime] = {}
    for entry_id, record in entries.items():
        if not isinstance(record, dict):
            continue
        accessed = parse_datetime(record.get("last_accessed_at"))
        if accessed is not None:
            result[str(entry_id)] = accessed
    return result


def render_index_row(row: dict[str, str], columns: list[str]) -> str:
    """按固定列顺序渲染一行索引；与 MCP 端解析逻辑保持一致。"""

    cells = [one_line(str(row.get(column, ""))) for column in columns]
    return "| " + " | ".join(cells) + " |"


def write_memory_index(root: Path, relative: str, header: str, rows: list[dict[str, str]]) -> None:
    """整表重写记忆索引。

    衰减需要删除/移动行，单纯的“追加”表达不了，所以这里重写整张表；表头与
    列顺序保持不变，MCP 端仍能按同一契约解析。
    """

    lines = [header.rstrip("\n")]
    lines.extend(render_index_row(row, INDEX_COLUMNS) for row in rows)
    text = "\n".join(lines) + "\n"
    locked_write_text(root, root / relative, text)


def _update_entry_meta(root: Path, relative: str, updates: dict[str, Any], now: datetime) -> bool:
    """只更新条目 frontmatter 的指定字段，正文保持不变。"""

    if not relative:
        return False
    path = root / relative
    if not path.exists():
        LOGGER.warning("衰减目标条目不存在，跳过 frontmatter 更新：%s", relative)
        return False
    try:
        text = read_text(path)
    except (OSError, ValueError):
        LOGGER.warning("读取条目失败，跳过 frontmatter 更新：%s", relative)
        return False
    meta, body = parse_frontmatter(text)
    if not meta:
        LOGGER.warning("条目缺少 frontmatter，跳过更新：%s", relative)
        return False
    meta.update(updates)
    meta["updated_at"] = isoformat_seconds(now)
    new_text = render_frontmatter(meta) + "\n" + body.strip("\n") + "\n"
    locked_write_text(root, path, new_text)
    return True


def _periods_between(last_touch: datetime, now: datetime) -> int:
    """计算从 last_touch 到现在经过的完整衰减周期数。"""

    days = (now - last_touch).total_seconds() / 86400.0
    if days <= 0:
        return 0
    return int(days // MEMORY_DECAY_TTL_DAYS)


def _decay_entry(
    row: dict[str, str],
    tier: str,
    access: dict[str, datetime],
    now: datetime,
) -> tuple[str, dict[str, str], float, int]:
    """对单条记忆做一次衰减裁决。

    返回 (动作, 行, 新置信度, 周期数)；动作 ∈ keep / refresh / demote / forget / skip。
    “最近使用时间”取 updated_at 与访问日志的较晚者，这样常被读取的记忆活得更久。
    """

    entry_id = str(row.get("id", ""))
    try:
        confidence = float(row.get("confidence", ""))
    except ValueError:
        LOGGER.warning("记忆 %r 置信度无法解析，跳过衰减", entry_id)
        return "skip", row, 0.0, 0
    updated_at = parse_datetime(row.get("updated_at"))
    if updated_at is None:
        LOGGER.warning("记忆 %r 的 updated_at 无法解析，跳过衰减", entry_id)
        return "skip", row, 0.0, 0
    last_touch = max(updated_at, access.get(entry_id, updated_at))
    periods = _periods_between(last_touch, now)
    if periods < 1:
        return "keep", row, confidence, 0
    new_confidence = round(max(0.0, confidence * (MEMORY_DECAY_FACTOR**periods)), 4)
    if tier == "long-term" and new_confidence < LONG_TERM_DEMOTE_BELOW:
        return "demote", row, new_confidence, periods
    if tier == "mid-term" and new_confidence < MID_TERM_FORGET_BELOW:
        return "forget", row, new_confidence, periods
    new_row = dict(row)
    new_row["confidence"] = f"{new_confidence:.2f}"
    new_row["updated_at"] = isoformat_seconds(now)
    return "refresh", new_row, new_confidence, periods


def process_memory_decay(root: Path, now: datetime, dry_run: bool) -> DecayResult:
    """记忆代谢主流程：长期用久降中期，中期用久退出索引（文件保留）。

    与情绪衰减一样是纯代码计算，不调用模型、零 Token。设计取舍：只“降级/退出
    索引”，从不删除条目文件——被忘记的记忆仍可在磁盘上找回，可通过归档层另存。
    """

    if not MEMORY_DECAY_ENABLED:
        return DecayResult("已禁用")

    access = load_memory_access(root)
    long_relative = "long-term/MEMORY-INDEX.md"
    mid_relative = "mid-term/MEMORY-INDEX.md"
    long_path = root / long_relative
    mid_path = root / mid_relative
    if not long_path.exists() and not mid_path.exists():
        LOGGER.warning("长期/中期索引均不存在，跳过记忆衰减")
        return DecayResult("索引不存在")

    long_rows = parse_index_table(read_text(long_path), INDEX_COLUMNS) if long_path.exists() else []
    mid_rows = parse_index_table(read_text(mid_path), INDEX_COLUMNS) if mid_path.exists() else []

    kept_long: list[dict[str, str]] = []
    kept_mid: list[dict[str, str]] = []
    demoted: list[tuple[dict[str, str], float, int]] = []
    forgotten: list[tuple[dict[str, str], float, int]] = []
    refreshed_rows: list[tuple[dict[str, str], float]] = []
    long_changed = False
    mid_changed = False

    for row in long_rows:
        action, new_row, new_confidence, periods = _decay_entry(row, "long-term", access, now)
        if action == "demote":
            demoted.append((row, new_confidence, periods))
            long_changed = True
            continue
        kept_long.append(new_row)
        if action == "refresh":
            refreshed_rows.append((new_row, new_confidence))
            long_changed = True

    for row in mid_rows:
        action, new_row, new_confidence, periods = _decay_entry(row, "mid-term", access, now)
        if action == "forget":
            forgotten.append((row, new_confidence, periods))
            mid_changed = True
            continue
        kept_mid.append(new_row)
        if action == "refresh":
            refreshed_rows.append((new_row, new_confidence))
            mid_changed = True

    for row, new_confidence, _periods in demoted:
        moved_row = dict(row)
        moved_row["type"] = "mid-term"
        moved_row["confidence"] = f"{new_confidence:.2f}"
        moved_row["updated_at"] = isoformat_seconds(now)
        kept_mid.append(moved_row)
        mid_changed = True

    refreshed = len(refreshed_rows)
    changed = bool(demoted or forgotten or refreshed)
    summary = f"刷新{refreshed}/降级{len(demoted)}/归档{len(forgotten)}"
    if not changed:
        LOGGER.debug("记忆代谢：无需衰减（%s）", summary)
        return DecayResult(summary)

    if dry_run:
        for row, new_confidence, periods in demoted:
            LOGGER.info(
                "[dry-run] 记忆降级 长期→中期：%s（%.2f→%.2f，周期=%d）",
                row.get("id"),
                float(row.get("confidence", 0) or 0),
                new_confidence,
                periods,
            )
        for row, new_confidence, periods in forgotten:
            LOGGER.info(
                "[dry-run] 记忆归档（退出中期索引）：%s（%.2f→%.2f，周期=%d）",
                row.get("id"),
                float(row.get("confidence", 0) or 0),
                new_confidence,
                periods,
            )
        LOGGER.info("[dry-run] 记忆代谢：%s", summary)
        return DecayResult(summary, changed=True, refreshed=refreshed,
                           demoted=len(demoted), forgotten=len(forgotten))

    for row, new_confidence, periods in demoted:
        _update_entry_meta(
            root,
            row.get("path", ""),
            {
                "type": "mid-term",
                "status": "mid-term",
                "confidence": new_confidence,
                "demoted_at": isoformat_seconds(now),
            },
            now,
        )
    for row, new_confidence, periods in forgotten:
        _update_entry_meta(
            root,
            row.get("path", ""),
            {
                "status": "archived",
                "confidence": new_confidence,
                "archived_at": isoformat_seconds(now),
            },
            now,
        )
    for row, new_confidence in refreshed_rows:
        _update_entry_meta(root, row.get("path", ""), {"confidence": new_confidence}, now)

    if long_changed:
        write_memory_index(root, long_relative, LONG_TERM_INDEX_HEADER, kept_long)
    if mid_changed:
        write_memory_index(root, mid_relative, MID_TERM_INDEX_HEADER, kept_mid)

    LOGGER.info("记忆代谢：%s", summary)
    return DecayResult(summary, changed=True, refreshed=refreshed,
                       demoted=len(demoted), forgotten=len(forgotten))


# ---------------------------------------------------------------------------
# Inbox 归档建议
# ---------------------------------------------------------------------------
def existing_pending_suggestion_keys(suggestions_text: str) -> set[tuple[str, str]]:
    """从已有 SUGGESTIONS.md 中提取（来源，归一化命题）键，避免重复追加。"""

    keys: set[tuple[str, str]] = set()
    pattern = re.compile(
        r"^- \[ \] `[^`]+` \| 置信度 [^|]+ \| 原因：[^|]+ \| "
        r"来源：([^|]+?) \| (.+?)\s*$"
    )
    for line in suggestions_text.splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        source = match.group(1).strip().replace("\\", "/")
        text_norm = normalize_text(match.group(2))
        if source and text_norm:
            keys.add((source, text_norm))
    return keys


def existing_inbox_suggestion_paths(suggestions_text: str) -> set[str]:
    """从已有 SUGGESTIONS.md 中提取已建议过的 inbox 路径，避免重复追加。"""

    return {
        match.group(1).replace("\\", "/")
        for match in re.finditer(r"`(archive/inbox/[^`]+?\.md)`", suggestions_text)
    }


def scan_inbox(root: Path, existing_paths: set[str], dry_run: bool) -> list[InboxSuggestion]:
    """递归扫描 archive/inbox/**/*.md，给出分类建议；不移动/重命名文件。"""

    inbox_root = root / "archive" / "inbox"
    if not inbox_root.exists():
        LOGGER.debug("archive/inbox 不存在，跳过归档建议")
        return []
    suggestions: list[InboxSuggestion] = []
    files = sorted(
        (
            path
            for path in inbox_root.rglob("*.md")
            if path.is_file() and path.name.lower() not in {"index.md", "readme.md"}
        ),
        key=lambda item: item.as_posix().casefold(),
    )
    for path in files:
        try:
            relative = relative_to_root(path, root)
        except ValueError:
            LOGGER.warning("跳过根目录之外的符号链接 inbox 文件：%s", path)
            continue
        if relative in existing_paths:
            LOGGER.debug("inbox 建议已存在，跳过：%s", relative)
            continue
        text = read_text(path)
        meta, body = parse_frontmatter(text)
        title = str(meta.get("title", "") or path.stem)
        tags = normalize_tags(meta.get("tags", []), limit=8)
        domain, subdomain, keyword = infer_domain(title, tags, body)
        if keyword is None:
            # 需求约定：无法确定分类时进 inbox，而不是 general/uncategorized。
            domain, subdomain = "inbox", "unsorted"
        target = f"{domain}/{subdomain}"
        evidence = f"关键词 {keyword}" if keyword else "未匹配关键词，建议暂存 inbox"
        suggestions.append(InboxSuggestion(path=relative, target=target, evidence=evidence))
        if len(suggestions) >= MAX_INBOX_FILES:
            LOGGER.warning("inbox 建议达到上限 %d，其余文件下次再处理", MAX_INBOX_FILES)
            break
        if dry_run:
            LOGGER.info("[dry-run] 将追加归档建议：%s -> %s（%s）", relative, target, evidence)
        else:
            LOGGER.info("归档建议：%s -> %s（%s）", relative, target, evidence)
    return suggestions


# ---------------------------------------------------------------------------
# SUGGESTIONS.md / state 写入
# ---------------------------------------------------------------------------
def append_suggestions(
    root: Path,
    pending: list[PendingSuggestion],
    inbox_items: list[InboxSuggestion],
    now: datetime,
    dry_run: bool,
) -> None:
    """把本次待确认记忆与归档建议追加为同一时间块。"""

    if not pending and not inbox_items:
        return
    if dry_run:
        LOGGER.info(
            "[dry-run] 将追加 SUGGESTIONS.md：待确认 %d 条，归档建议 %d 条",
            len(pending),
            len(inbox_items),
        )
        return
    path = root / "SUGGESTIONS.md"
    block_lines: list[str] = [f"## {isoformat_seconds(now)}"]
    if pending:
        block_lines.extend(["", "### 待确认记忆"])
        block_lines.extend(render_suggestion_line(item) for item in pending)
    if inbox_items:
        block_lines.extend(["", "### 归档建议"])
        block_lines.extend(render_inbox_line(item) for item in inbox_items)
    block = "\n".join(block_lines)

    with locked_file(root, path):
        if path.exists():
            current = read_text(path)
        else:
            current = SUGGESTIONS_HEADER
        if not current.strip():
            current = SUGGESTIONS_HEADER
        elif not current.lstrip().startswith("# 待确认记忆与归档建议"):
            current = SUGGESTIONS_HEADER + "\n" + current.lstrip()
        new_text = current.rstrip() + "\n\n" + block + "\n"
        atomic_write_text(path, new_text)


def save_state(
    root: Path,
    started_at: datetime,
    stats: RunStats,
    scanned_hashes: dict[str, str],
    previous_state: dict[str, Any],
) -> None:
    """成功结束时写 state/last_run.json；processed 最多保留 500 条并按路径排序。"""

    processed: dict[str, str] = {}
    previous_processed = previous_state.get("processed")
    if isinstance(previous_processed, dict):
        for key, value in previous_processed.items():
            if isinstance(key, str) and isinstance(value, str):
                processed[key] = value
    processed.update(scanned_hashes)
    processed = dict(sorted(processed.items()))
    if len(processed) > STATE_PROCESSED_LIMIT:
        processed = dict(list(processed.items())[-STATE_PROCESSED_LIMIT:])

    state = {
        "version": 1,
        "last_success_at": isoformat_seconds(started_at),
        "last_stats": stats.as_dict(),
        "processed": processed,
    }
    path = root / "state" / "last_run.json"
    payload = json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n"
    locked_write_text(root, path, payload)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run_consolidation(
    root: Path,
    args: argparse.Namespace,
    started_at: datetime,
    started_perf: float,
) -> tuple[RunStats, str]:
    """执行一次完整整理；返回值用于最终统计日志。"""

    stats = RunStats()
    state = load_state(root)
    check_interval(state, started_at, force=bool(args.force))

    scanned_hashes, eligible = scan_short_term(root, state, started_at, stats)
    LOGGER.info("短期记忆扫描完成：发现 %d 个文件，待处理 %d 个", stats.scanned_files, len(eligible))

    entries = load_long_term_entries(root)
    existing_ids = {entry.id for entry in entries}
    candidates: list[Candidate] = []
    seen_candidates: set[str] = set()
    for source, _text, body in eligible:
        for candidate in extract_candidates_for_text(body, source, bool(args.no_llm)):
            key = normalize_text(candidate.text)
            if not key or key in seen_candidates:
                continue
            seen_candidates.add(key)
            candidate.source = source
            candidates.append(candidate)
            if len(candidates) >= MAX_CANDIDATES_PER_RUN:
                LOGGER.warning("候选数达到上限 %d，后续候选已截断", MAX_CANDIDATES_PER_RUN)
                break
        if len(candidates) >= MAX_CANDIDATES_PER_RUN:
            break
    stats.candidates = len(candidates)
    LOGGER.debug("候选明细：%s", [(item.source, item.origin, item.confidence, item.text) for item in candidates])

    suggestions_path = root / "SUGGESTIONS.md"
    existing_suggestions_text = read_text(suggestions_path) if suggestions_path.exists() else ""
    existing_inbox_paths = existing_inbox_suggestion_paths(existing_suggestions_text)
    existing_pending_keys = existing_pending_suggestion_keys(existing_suggestions_text)

    pending: list[PendingSuggestion] = []

    def queue_pending(candidate: Candidate, suggestion_id: str, reason: str) -> None:
        """追加待确认建议；同来源、同命题文本已存在时不重复写。"""

        key = (candidate.source, normalize_text(candidate.text))
        if key in existing_pending_keys:
            LOGGER.info("建议已存在，跳过重复追加：%s", candidate.text)
            return
        existing_pending_keys.add(key)
        pending.append(
            PendingSuggestion(
                id=suggestion_id,
                confidence=candidate.confidence,
                reason=reason,
                source=candidate.source,
                text=candidate.text,
            )
        )

    for candidate in candidates:
        kind, matched = judge_candidate(candidate, entries)
        if kind == "duplicate":
            stats.duplicates += 1
            matched_path = matched.path if matched else "（归一化后为空）"
            LOGGER.info("重复跳过：%s（匹配 %s）", candidate.text, matched_path)
            continue

        suggestion_id = make_entry_id(started_at, existing_ids, root)
        existing_ids.add(suggestion_id)
        if kind == "conflict":
            stats.conflicts += 1
            reason = suggestion_reason(candidate, kind, matched)
            queue_pending(candidate, suggestion_id, reason)
            LOGGER.info("冲突：%s（%s）", candidate.text, reason)
            continue
        if kind == "update":
            stats.updates += 1
            reason = suggestion_reason(candidate, kind, matched)
            queue_pending(candidate, suggestion_id, reason)
            LOGGER.info("更新候选：%s（%s）", candidate.text, reason)
            continue

        can_promote = (
            AUTO_PROMOTE
            and candidate.confidence >= CONFIDENCE_THRESHOLD
            and not candidate.requires_confirmation
        )
        if can_promote:
            promote_candidate(
                root,
                candidate,
                started_at,
                existing_ids,
                bool(args.dry_run),
                entries,
            )
            stats.promoted += 1
        else:
            reason = suggestion_reason(candidate, kind, matched)
            pending_before = len(pending)
            queue_pending(candidate, suggestion_id, reason)
            if len(pending) > pending_before:
                LOGGER.info("写入待确认建议：%s（%s）", candidate.text, reason)

    stats.suggestions = len(pending)
    emotion_result = process_emotion(root, started_at, bool(args.dry_run))
    stats.emotion_changes = 1 if emotion_result.changed else 0

    decay_result = process_memory_decay(root, started_at, bool(args.dry_run))
    stats.memory_refreshed = decay_result.refreshed
    stats.memory_demoted = decay_result.demoted
    stats.memory_forgotten = decay_result.forgotten

    inbox_items = scan_inbox(root, existing_inbox_paths, bool(args.dry_run))
    stats.inbox_suggestions = len(inbox_items)

    append_suggestions(root, pending, inbox_items, started_at, bool(args.dry_run))

    stats.duration_seconds = round(time.perf_counter() - started_perf, 3)
    if args.dry_run:
        LOGGER.info("[dry-run] 不写 state/last_run.json")
    else:
        save_state(root, started_at, stats, scanned_hashes, state)
    return stats, emotion_result.text


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""

    default_root = os.environ.get("DSH_MEMORY_ROOT") or str(Path.home() / ".dsh" / "memory-system")
    parser = argparse.ArgumentParser(
        description="DSH 本地记忆系统后台整理脚本：提炼短期记忆、衰减情绪、生成归档建议。"
    )
    parser.add_argument("--root", default=default_root, help=f"记忆根目录（默认 {default_root}）")
    parser.add_argument("--force", action="store_true", help="绕过 6 小时幂等窗口")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要执行的动作，不写任何文件")
    parser.add_argument("--no-llm", action="store_true", help="强制使用规则提炼，不调用 LLM")
    parser.add_argument(
        "--reason",
        default="manual",
        choices=("daily", "session-flush", "manual"),
        help="触发来源，仅用于日志（默认 manual）",
    )
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口；返回进程退出码。"""

    args = parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"错误：记忆根目录不存在：{root}", file=sys.stderr)
        return 1

    started_at = local_now()
    started_perf = time.perf_counter()
    try:
        setup_logging(root, bool(args.verbose), bool(args.dry_run))
    except Exception:
        traceback.print_exc()
        return 1

    LOGGER.info(
        "开始 root=%s reason=%s force=%s dry_run=%s no_llm=%s",
        root,
        args.reason,
        bool(args.force),
        bool(args.dry_run),
        bool(args.no_llm),
    )

    process_lock = ProcessLock(root / ".consolidate.lock", create=not bool(args.dry_run))
    try:
        acquired = process_lock.acquire()
    except Exception:
        LOGGER.exception("获取进程级锁失败")
        LOGGER.info("耗时=%.3fs", time.perf_counter() - started_perf)
        LOGGER.info("退出码=1")
        return 1

    if not acquired:
        LOGGER.info("跳过原因=已有实例在运行（.consolidate.lock 被占用）")
        LOGGER.info("耗时=%.3fs", time.perf_counter() - started_perf)
        LOGGER.info("退出码=0")
        return 0
    if process_lock.no_lock:
        LOGGER.debug("dry-run 且 .consolidate.lock 不存在：为满足零写入，不创建进程锁")

    try:
        try:
            stats, emotion_text = run_consolidation(root, args, started_at, started_perf)
        except RunSkipped as exc:
            duration = time.perf_counter() - started_perf
            LOGGER.info("跳过原因=%s", exc)
            log_stats(RunStats(), "未处理", duration, 0)
            return 0
        except Exception as exc:
            LOGGER.exception("运行失败：%s", exc)
            LOGGER.info("耗时=%.3fs", time.perf_counter() - started_perf)
            LOGGER.info("退出码=1")
            return 1

        duration = time.perf_counter() - started_perf
        log_stats(stats, emotion_text, duration, 0)
        return 0
    finally:
        process_lock.release()


if __name__ == "__main__":
    sys.exit(main())
