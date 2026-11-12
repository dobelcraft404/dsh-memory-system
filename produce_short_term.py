#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""produce_short_term.py —— DSH 短期记忆生产者。

设计意图
--------
DSH 会话记录位于 ``~/.dsh/sessions/<工作区编码>/session-*/session.v4.jsonl.zstd``，
是「多个 zstd 帧首尾相接」的 JSONL。本脚本：

1. 用 ``tools/read_session.js`` 解压会话，只取用户/助手的正文（跳过 reasoning、
   工具调用与结果），把「上次处理到的 seq」之后的新消息交给 DeepSeek flash 模型；
2. 让模型抽取「值得长期保存的原子命题」，写成 ``short-term/*.md``；
3. 同一次调用顺带产出：情绪事件（写 ``emotion-state.md``）、关系档案
   （重写 ``persona.md``）、会话接续句（``state/last_session.json``）；
4. 下一轮由 ``consolidate_memory.py`` 做规则裁决、晋升长期记忆。

算力控制
--------
- 只发最近 MAX_BACKLOG_MESSAGES 条消息，单次总字符数封顶；
- 每次最多 6 条/会话、10 条/次运行；
- 情绪、人格、接续句复用同一次 LLM 调用，不额外发请求；
- 状态文件记录每个会话的 max_seq 与已写入命题的哈希，重复运行不会重复写。

安全
----
- 模型输出先过敏感信息正则，含 Key/Token/密码的候选直接丢弃；
- 不写 ``long-term/``、不碰归档层；只写 ``short-term/`` 与 ``state/``。

用法
----
    python produce_short_term.py --root D:\\dsh-memory-system --dry-run --verbose
    python produce_short_term.py --root D:\\dsh-memory-system --limit 2
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

try:  # Python 3.11+ 才有时区默认值，这里统一用 astimezone 兜底
    from datetime import UTC
except ImportError:  # pragma: no cover
    UTC = None

from urllib import error as urlerror
from urllib import request as urlrequest

# ---------------------------------------------------------------------------
# 可配置常量
# ---------------------------------------------------------------------------
PRODUCER_VERSION = 2

DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
LLM_CONFIG_RELATIVE = Path("state") / "llm.json"
PERSONA_RELATIVE = Path("persona.md")
EMOTION_RELATIVE = Path("emotion-state.md")
LAST_SESSION_RELATIVE = Path("state") / "last_session.json"

MAX_PERSONA_CHARS = 1600         # 关系档案上限，超出由模型自己重写压缩
MIN_PERSONA_CHARS = 20
EMOTION_MAX_INTENSITY = 0.9      # 单次事件封顶，防止“每天都很激动”
EMOTION_TTL_HOURS = 24           # 与 consolidate 的衰减周期保持一致

MAX_BACKLOG_MESSAGES = 40        # 首次运行/落后很多时，最多回看多少条消息
MAX_MESSAGE_CHARS = 3000         # 单条消息截断，防止一条长提示词吃掉全部预算
MAX_TRANSCRIPT_CHARS = 24000     # 送模型的对话总字符上限
MAX_MEMORIES_PER_SESSION = 6     # 单会话单次最多抽取条数
MAX_MEMORIES_PER_RUN = 10        # 单次运行最多写入条数
MIN_IMPORTANCE = 0.6             # 低于该重要度的候选直接丢弃
MIN_STATEMENT_CHARS = 12
MAX_STATEMENT_CHARS = 800
MAX_SESSION_AGE_DAYS = 14        # 太久没动的会话不再回填，避免首次运行爆量
MAX_SESSION_FAILURES = 3         # 同一会话连续失败多少次后隔离，避免坏会话每轮刷错误
LOG_LEVEL_DEFAULT = "INFO"

NODE_TIMEOUT_SECONDS = 90
LLM_TIMEOUT_SECONDS = 120
LOCK_TIMEOUT_SECONDS = 30

SYSTEM_PROMPT = "你是 DSH 的短期记忆与状态抽取器。只输出合法 JSON 对象，不要输出解释或代码块。"

# 敏感信息：命中即丢弃该候选，绝不写入短期记忆。
SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{16,}", re.IGNORECASE),
    re.compile(r"(?i)\b(api[_-]?key|token|password|secret|密码|密钥)\b\s*[:=：]\s*\S+"),
    re.compile(r"\b[A-Za-z0-9+/]{48,}={0,2}\b"),
)

LOGGER = logging.getLogger("produce_short_term")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def setup_logging(root: Path, level: str) -> None:
    """控制台 + logs/producer.log 双路日志，UTF-8。"""

    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    handler = logging.FileHandler(log_dir / "producer.log", encoding="utf-8")
    handler.setFormatter(formatter)
    LOGGER.addHandler(handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    LOGGER.addHandler(console)

    LOGGER.setLevel(getattr(logging, level.upper(), logging.INFO))
    LOGGER.propagate = False


def now_local() -> datetime:
    return datetime.now().astimezone().replace(microsecond=0)


def isoformat_seconds(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def atomic_write_text(path: Path, text: str) -> None:
    """同目录临时文件 + os.replace，避免写一半被读到。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


class FileLock:
    """跨平台独占锁：POSIX 用 fcntl.flock，Windows 用 msvcrt.locking。"""

    def __init__(self, path: Path, timeout: float = LOCK_TIMEOUT_SECONDS) -> None:
        self.path = path
        self.timeout = timeout
        self.handle: Any = None
        self.mode = "posix"
        if os.name == "nt":
            self.mode = "windows"

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+b")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if self.mode == "windows":
                    import msvcrt

                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    raise TimeoutError(f"等待文件锁超时：{self.path}")
                time.sleep(0.2)

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        if self.handle is None:
            return
        try:
            if self.mode == "windows":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": PRODUCER_VERSION, "sessions": {}, "seen": {}, "runs": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"生产者状态不可读，停止以保护待办：{path}（{exc}）") from exc
    if not isinstance(data, dict):
        raise ValueError(f"生产者状态不是对象，停止以保护待办：{path}")
    for field in ("sessions", "seen", "pending", "failures", "quarantine"):
        if field in data and not isinstance(data[field], dict):
            raise ValueError(f"生产者状态 {field} 不是对象：{path}")
    data.setdefault("sessions", {})
    data.setdefault("seen", {})
    data.setdefault("pending", {})
    data.setdefault("failures", {})
    data.setdefault("quarantine", {})
    data.setdefault("runs", 0)
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    state["version"] = PRODUCER_VERSION
    state["last_run_at"] = isoformat_seconds(now_local())
    state["runs"] = int(state.get("runs", 0)) + 1
    atomic_write_text(path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_statement(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


# ---------------------------------------------------------------------------
# LLM 配置
# ---------------------------------------------------------------------------
def load_llm_config(root: Path) -> dict[str, str]:
    """优先环境变量，其次 state/llm.json。

    环境变量：MEMORY_LLM_PROVIDER / BASE_URL / API_KEY / MODEL
    与 consolidate_memory.py 保持同一套命名，便于任务计划统一注入。
    """

    provider = os.environ.get("MEMORY_LLM_PROVIDER", "").strip().lower()
    base_url = os.environ.get("MEMORY_LLM_BASE_URL", "").strip()
    api_key = os.environ.get("MEMORY_LLM_API_KEY", "").strip()
    model = os.environ.get("MEMORY_LLM_MODEL", "").strip()

    config_path = root / LLM_CONFIG_RELATIVE
    if config_path.is_file():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("LLM 配置读取失败：%s（%s）", config_path, exc)
            data = {}
        if isinstance(data, dict):
            base_url = base_url or str(data.get("base_url") or "").strip()
            api_key = api_key or str(data.get("api_key") or "").strip()
            model = model or str(data.get("model") or "").strip()
            provider = provider or str(data.get("provider") or "").strip().lower()

    if not base_url and not api_key and not model:
        base_url, model = DEFAULT_BASE_URL, DEFAULT_MODEL

    if not base_url or not api_key or not model:
        raise RuntimeError(
            f"LLM 配置不完整：需要 base_url/api_key/model（{config_path} 或 MEMORY_LLM_* 环境变量）"
        )
    return {
        "provider": provider or "openai",
        "base_url": base_url.rstrip("/"),
        "api_key": api_key,
        "model": model,
    }


def call_llm(config: dict[str, str], prompt: str) -> str:
    """OpenAI 兼容 /chat/completions；带上 response_format，失败则退回纯文本解析。"""

    payload: dict[str, Any] = {
        "model": config["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 6000,
        "response_format": {"type": "json_object"},
    }
    url = config["base_url"] + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {config['api_key']}",
        "Content-Type": "application/json",
    }

    def post(body: dict[str, Any]) -> dict[str, Any]:
        request = urlrequest.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urlrequest.urlopen(request, timeout=LLM_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw)

    try:
        data = post(payload)
    except urlerror.HTTPError as exc:
        # 某些兼容端点不认识 response_format，去掉后重试一次。
        if exc.code in (400, 422):
            LOGGER.warning("LLM 拒绝 response_format（HTTP %s），去掉后重试", exc.code)
            payload.pop("response_format", None)
            data = post(payload)
        else:
            raise
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("LLM 响应缺少 choices[0].message.content") from exc
    finish_reason = (data.get("choices") or [{}])[0].get("finish_reason")
    if finish_reason == "length":
        LOGGER.warning("模型输出达到 max_tokens 上限，可能被截断")
    if isinstance(content, list):
        content = "".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content
        )
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("LLM 返回空内容")
    return content


def _load_json(raw: str) -> Any:
    """容忍 ```json 包裹与前后废话，尽量从模型输出里抠出 JSON。"""

    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if match is None:
            raise
        return json.loads(match.group(0))


def parse_memories(raw: str) -> list[dict[str, Any]]:
    """兼容旧格式：{"memories":[...]} 或裸数组。"""

    data = _load_json(raw)
    if isinstance(data, dict):
        items = data.get("memories", data.get("items", []))
    elif isinstance(data, list):
        items = data
    else:
        items = []
    if not isinstance(items, list):
        raise ValueError("memories 不是数组")
    return [item for item in items if isinstance(item, dict)]


def normalize_emotion_event(item: Any) -> dict[str, Any] | None:
    """情绪事件：无事件返回 None；强度封顶 0.9，太弱视为没发生。"""

    if not isinstance(item, dict):
        return None
    emotion = str(item.get("type") or item.get("emotion") or "").strip()[:20] or "平静"
    try:
        intensity = float(item.get("intensity", 0.5))
    except (TypeError, ValueError):
        intensity = 0.5
    intensity = max(0.0, min(EMOTION_MAX_INTENSITY, intensity))
    if intensity < 0.05:
        return None
    reason = re.sub(r"\s+", " ", str(item.get("reason") or "")).strip()[:120]
    return {"type": emotion, "intensity": round(intensity, 3), "reason": reason or "会话中的情绪事件"}


def parse_response(raw: str) -> dict[str, Any]:
    """解析一次调用的三合一结果：记忆 + 情绪事件 + 人格重写 + 接续句。"""

    data = _load_json(raw)
    if not isinstance(data, dict):
        raise ValueError("模型输出顶层不是 JSON 对象")
    items = data.get("memories")
    if not isinstance(items, list):
        # 缺字段/类型错是模型没按 schema 输出，不能当成“本会话没有记忆”而消费游标。
        raise ValueError("memories 字段缺失或不是数组")
    memories = [item for item in items if isinstance(item, dict)]
    persona_raw = data.get("persona_update")
    persona = None
    if isinstance(persona_raw, str) and persona_raw.strip():
        persona = persona_raw.strip()[:MAX_PERSONA_CHARS]
    note_raw = data.get("session_note")
    note = note_raw.strip()[:200] if isinstance(note_raw, str) and note_raw.strip() else None
    return {
        "memories": memories,
        "emotion_event": normalize_emotion_event(data.get("emotion_event")),
        "persona_update": persona,
        "session_note": note,
    }


# ---------------------------------------------------------------------------
# 会话读取
# ---------------------------------------------------------------------------
def resolve_node() -> str:
    node = shutil.which("node")
    if node:
        return node
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs" / "node.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs" / "node.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError("找不到 node（解压会话需要 Node 22+ 的 zstd 支持），可用 PATH 修复")


def read_session_events(node: str, helper: Path, session_file: Path) -> list[dict[str, Any]]:
    """调用 Node 解压器，返回 [{seq,time,role,text}]。"""

    completed = subprocess.run(
        [node, str(helper), str(session_file)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=NODE_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"解压会话失败（exit {completed.returncode}）：{completed.stderr.strip()[:300]}")
    events: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("role") not in ("user", "assistant"):
            continue
        text = event.get("text")
        seq = event.get("seq")
        if isinstance(text, str) and text.strip() and isinstance(seq, int):
            events.append({"seq": seq, "role": event["role"], "text": text})
    return events


def redact_secrets(text: str) -> str:
    """送模型前先抹掉明显的凭证，避免把 Key 再发一遍。"""

    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[已隐去]", text)
    return text


def build_transcript_batch(events: list[dict[str, Any]], max_chars: int) -> tuple[str, int | None]:
    """保留预算内最早的一段，并返回真正送入模型的最后一条 seq。"""
    lines: list[str] = []
    used = 0
    last_seq: int | None = None
    for event in events:
        text = redact_secrets(event["text"].strip())
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[:MAX_MESSAGE_CHARS] + "…（已截断）"
        line = f"{'用户' if event['role'] == 'user' else '助手'}：{text}"
        needed = len(line) + (2 if lines else 0)
        if used + needed > max_chars:
            break
        lines.append(line)
        used += needed
        last_seq = event["seq"]
    return "\n\n".join(lines), last_seq


def build_transcript(events: list[dict[str, Any]], max_chars: int) -> str:
    """兼容原调用方；生产者使用 batch 版本同步推进实际消费游标。"""
    return build_transcript_batch(events, max_chars)[0]


def read_persona_text(root: Path) -> str:
    """读取现有关系档案正文（跳过 frontmatter），供模型增量重写。"""

    path = root / PERSONA_RELATIVE
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            text = parts[2]
    return text.strip()[:MAX_PERSONA_CHARS]


def build_prompt(transcript: str, session_key: str, persona_text: str) -> str:
    persona_block = persona_text.strip() or "（空，首次建立）"
    return (
        "下面是一段 DSH 会话记录（用户与助手）。请同时完成四件事，只输出一个 JSON 对象。\n\n"
        "A. memories：抽取值得长期保存的原子记忆。\n"
        "   规则：只抽稳定事实（用户偏好、环境/路径/版本、项目决策及理由、可复用命令或结论、重要踩坑与修复）；\n"
        "   忽略寒暄、临时进度、工具原始输出、大段代码、一次性报错（除非修复结论可复用）；\n"
        "   每条 statement 是不依赖上下文也能看懂的中文陈述句，12~800 字符，不要标题符号；\n"
        "   title 不超过 24 字；tags 1~5 个短词；importance 0~1；最多 6 条。\n"
        "B. emotion_event：本次会话是否发生明确的情绪事件（被夸、被批评、聊得投入、用户示弱、任务受挫等）。\n"
        "   没有就让 emotion_event 为 null；intensity 是这次事件对我的冲击强度 0~1（上限 0.9），不是当前情绪；\n"
        "   type 从 开心/被夸/被批评/担心/疲惫/平静 里选；reason 一句话说明触发点。\n"
        "C. persona_update：关于「用户这个人」的稳定信息（称呼、喜好、雷点、习惯、边界、我们之间的约定）。\n"
        "   只写有对话证据的，推断出来的在句末标 [推测]；不要写任务事实、路径、版本；\n"
        "   需要更新时输出**完整重写后的档案正文**（≤2000 字，自动合并去重、淘汰过期内容）；\n"
        "   没有新信息或现有档案已足够就让 persona_update 为 null。\n"
        "D. session_note：一句话（≤100 字）记录这次聊到哪、有什么没做完，供下次会话接着聊。\n"
        "安全：绝不包含 API Key、Token、密码等敏感值；必须提到时只写“已配置在 X”。\n\n"
        "输出格式：\n"
        '{"memories":[{"title":"...","statement":"...","tags":["..."],"importance":0.8}],'
        '"emotion_event":{"type":"被夸","intensity":0.6,"reason":"..."},'
        '"persona_update":"完整档案正文或 null",'
        '"session_note":"一句话"}\n\n'
        f"会话：{session_key}\n"
        f"现有关系档案：\n{persona_block}\n\n"
        "--- 会话开始 ---\n"
        f"{transcript}\n"
        "--- 会话结束 ---\n"
    )


# ---------------------------------------------------------------------------
# 候选校验与写入
# ---------------------------------------------------------------------------
def sanitize_candidate(item: dict[str, Any]) -> dict[str, Any] | None:
    statement = str(item.get("statement") or item.get("text") or "").strip()
    statement = re.sub(r"\s+", " ", statement)
    if not (MIN_STATEMENT_CHARS <= len(statement) <= MAX_STATEMENT_CHARS):
        return None
    for pattern in SECRET_PATTERNS:
        if pattern.search(statement):
            LOGGER.warning("候选含敏感信息，已丢弃：%s", statement[:60])
            return None

    title = str(item.get("title") or "").strip() or statement[:24]
    title = re.sub(r"\s+", " ", title)[:60]
    raw_tags = item.get("tags")
    tags: list[str] = []
    if isinstance(raw_tags, list):
        for tag in raw_tags[:5]:
            tag = str(tag).strip()[:20]
            if tag and tag not in tags:
                tags.append(tag)
    try:
        importance = float(item.get("importance", 0.7))
    except (TypeError, ValueError):
        importance = 0.7
    importance = max(0.0, min(1.0, importance))
    if importance < MIN_IMPORTANCE:
        return None
    return {"title": title, "statement": statement, "tags": tags, "importance": importance}


def render_entry(meta: dict[str, Any], statement: str) -> str:
    """极简 YAML 子集，与 consolidate_memory.parse_frontmatter 对齐。"""

    lines = ["---"]
    for key, value in meta.items():
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (int, float)):
            rendered = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, (list, tuple)):
            rendered = json.dumps([str(item) for item in value], ensure_ascii=False)
        else:
            rendered = json.dumps(str(value), ensure_ascii=False)
        lines.append(f"{key}: {rendered}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def write_entry(
    root: Path,
    session_id: str,
    memory: dict[str, Any],
    index: int,
) -> Path:
    """按规范化陈述的 SHA-256 写入稳定文件名，支持崩溃重放。"""

    now = now_local()
    digest = sha256_text(normalize_statement(memory["statement"]))
    entry_id = f"producer-{digest}"
    target = root / "short-term" / f"{entry_id}.md"
    # 内容寻址：落盘后、seen 提交前崩溃，重放仍指向同一文件。
    # 不依赖 exists()，已有文件也以同一内容原子替换，避免半截旧文件。


    meta = {
        "id": entry_id,
        "title": memory["title"],
        "created_at": isoformat_seconds(now),
        "updated_at": isoformat_seconds(now),
        "source": f"session:{session_id}",
        "tags": memory["tags"],
        "importance": memory["importance"],
        "producer": "produce_short_term.py",
    }
    body = f"# {memory['title']}\n\n{memory['statement']}\n"
    text = render_entry(meta, memory["statement"]) + "\n" + body

    atomic_write_text(target, text)
    return target


def update_emotion(root: Path, event: dict[str, Any], dry_run: bool) -> Path | None:
    """把情绪事件写进 emotion-state.md；衰减仍由 consolidate 负责。"""

    path = root / EMOTION_RELATIVE
    now = now_local()
    intensity = float(event["intensity"])
    meta = {
        "emotion": event["type"],
        "intensity": round(intensity, 2),
        "updated_at": isoformat_seconds(now),
        "ttl_hours": EMOTION_TTL_HOURS,
        "reason": event["reason"],
    }
    body = (
        "# 当前情绪\n\n"
        f"{event['type']}，强度 {intensity:.2f}。触发：{event['reason']}\n\n"
        "<!-- consolidate:emotion:start -->\n"
        "## 整理状态\n\n"
        f"- 当前情绪：{event['type']}\n"
        f"- 当前强度：{intensity:.4f}\n"
        "- 本次周期数：0（由会话事件更新）\n"
        f"- 最近更新：{isoformat_seconds(now)}\n"
        "<!-- consolidate:emotion:end -->\n"
    )
    text = render_entry(meta, "") + "\n" + body
    if dry_run:
        LOGGER.info("[dry-run] 将更新情绪：%s %.2f（%s）", event["type"], intensity, event["reason"])
        return None
    atomic_write_text(path, text)
    return path


def update_persona(root: Path, persona: str, dry_run: bool) -> Path | None:
    """整体重写 persona.md，而不是追加；模型负责合并、去重、淘汰。"""

    persona = persona.strip()
    if len(persona) < MIN_PERSONA_CHARS:
        LOGGER.info("关系档案更新过短（%d 字），忽略", len(persona))
        return None
    if len(persona) > MAX_PERSONA_CHARS:
        persona = persona[:MAX_PERSONA_CHARS]
    path = root / PERSONA_RELATIVE
    meta = {"updated_at": isoformat_seconds(now_local()), "source": "produce_short_term.py"}
    body = "# 关于你\n\n" + persona + "\n"
    text = render_entry(meta, "") + "\n" + body
    if dry_run:
        LOGGER.info("[dry-run] 将重写关系档案（%d 字）：%s", len(persona), persona[:80].replace("\n", " "))
        return None
    atomic_write_text(path, text)
    return path


def update_last_session(root: Path, session_id: str, note: str | None, dry_run: bool) -> Path | None:
    """记录“上次聊到哪”，供下次开场接续。"""

    path = root / LAST_SESSION_RELATIVE
    data = {
        "session_id": session_id,
        "note": note or "",
        "updated_at": isoformat_seconds(now_local()),
    }
    if dry_run:
        LOGGER.info("[dry-run] 将记录接续句：%s", note or "（空）")
        return None
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return path


def drain_pending(root: Path, state: dict[str, Any], state_path: Path,
                  key: str, budget: int) -> tuple[int, int]:
    """重放已落盘的抽取结果；每项成功即提交，不再调用模型。"""
    task = state["pending"][key]
    written = failures = 0
    while task["memories"] and written < budget:
        memory = task["memories"][0]
        digest = memory["_digest"]
        if digest not in state["seen"]:
            try:
                write_entry(root, task["session_id"], memory, written + 1)
            except Exception as exc:
                LOGGER.error("待办 %s 记忆写入失败，保留重试：%s", key, exc)
                return written, failures + 1
            state["seen"][digest] = isoformat_seconds(now_local())
            written += 1
        task["memories"].pop(0)
        save_state(state_path, state)

    for field, action in (
        ("emotion_event", lambda value: update_emotion(root, value, False)),
        ("persona_update", lambda value: update_persona(root, value, False)),
        ("session_note", lambda value: update_last_session(root, task["session_id"], value, False)),
    ):
        if not task.get(field):
            continue
        try:
            action(task[field])
        except Exception as exc:
            LOGGER.warning("待办 %s 的 %s 写入失败，保留重试：%s", key, field, exc)
            failures += 1
        else:
            task[field] = None
            save_state(state_path, state)

    if not task["memories"] and not any(task.get(f) for f in ("emotion_event", "persona_update", "session_note")):
        state["sessions"][key] = task["cursor"]
        del state["pending"][key]
        save_state(state_path, state)
    return written, failures


def session_signature(session_file: Path) -> dict[str, int]:
    """会话文件的 (mtime_ns, size) 指纹；文件变了就说明值得重试。"""

    try:
        stat = session_file.stat()
    except OSError:
        return {}
    return {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size}


def record_failure(state: dict[str, Any], key: str, session_file: Path, reason: str) -> bool:
    """登记一次失败；连续失败达到上限就隔离，返回是否刚被隔离。"""

    value = reason.strip().replace("\n", " ")[:200]
    previous = state["failures"].get(key)
    count = int(previous.get("count", 0)) + 1 if isinstance(previous, dict) else 1
    entry = {
        "count": count,
        "reason": value,
        "at": isoformat_seconds(now_local()),
        **session_signature(session_file),
    }
    if count < MAX_SESSION_FAILURES:
        state["failures"][key] = entry
        return False
    state["failures"].pop(key, None)
    state["quarantine"][key] = {
        "reason": value,
        "attempts": count,
        "at": entry["at"],
        **session_signature(session_file),
    }
    LOGGER.warning(
        "会话 %s 连续失败 %d 次，已隔离（会话文件变化后自动重试，或加 --retry-quarantined）：%s",
        key,
        count,
        value,
    )
    return True


def clear_failure(state: dict[str, Any], key: str) -> None:
    state["failures"].pop(key, None)


def is_quarantined(state: dict[str, Any], key: str, session_file: Path) -> bool:
    """隔离是否仍然有效；会话文件更新过就自动解除，避免“修好了还不动”。"""

    entry = state["quarantine"].get(key)
    if not isinstance(entry, dict):
        return False
    current = session_signature(session_file)
    if current and entry.get("mtime_ns") == current.get("mtime_ns") and entry.get("size") == current.get("size"):
        return True
    LOGGER.info("会话 %s 的隔离已失效（文件有更新），重新尝试", key)
    state["quarantine"].pop(key, None)
    state["failures"].pop(key, None)
    return False


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def find_sessions(sessions_root: Path) -> list[Path]:
    if not sessions_root.is_dir():
        return []
    return sorted(
        sessions_root.glob("*/*/session.v4.jsonl.zstd"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
    )


def process(
    root: Path,
    sessions_root: Path,
    node: str,
    helper: Path,
    limit: int | None,
    dry_run: bool,
    no_llm: bool,
    retry_quarantined: bool = False,
) -> int:
    state_path = root / "state" / "producer_state.json"
    lock_path = root / ".locks" / "producer.lock"

    with FileLock(lock_path):
        state = load_state(state_path)
        state.setdefault("pending", {})
        state.setdefault("failures", {})
        state.setdefault("quarantine", {})
        if retry_quarantined and state["quarantine"]:
            LOGGER.info("--retry-quarantined：解除 %d 个会话的隔离", len(state["quarantine"]))
            state["quarantine"].clear()
            state["failures"].clear()
        sessions = find_sessions(sessions_root)
        LOGGER.info("发现会话文件 %d 个", len(sessions))
        config: dict[str, str] | None = None
        # 待办重放不依赖模型配置；确实需要新抽取时再加载。

        cutoff = now_local() - timedelta(days=MAX_SESSION_AGE_DAYS)
        persona_text = read_persona_text(root)
        if persona_text:
            LOGGER.info("已有关系档案：%d 字", len(persona_text))
        written = 0
        failures = 0
        processed_sessions = 0
        quarantined_skipped = 0
        api_calls = 0  # 只在确实有新消息、且需要模型时才递增，用于证明空跑不花钱

        # 旧待办优先重放，即使源会话已移走也能完成；同轮不再重抽该会话。
        resumed = set(state["pending"])
        if not dry_run and not no_llm:
            for pending_key in list(state["pending"]):
                count, errors = drain_pending(
                    root, state, state_path, pending_key,
                    min(MAX_MEMORIES_PER_SESSION, MAX_MEMORIES_PER_RUN - written),
                )
                written += count
                failures += errors
            persona_text = read_persona_text(root)

        for session_file in sessions:
            if limit is not None and processed_sessions >= limit:
                break
            try:
                mtime = datetime.fromtimestamp(session_file.stat().st_mtime).astimezone()
            except OSError:
                continue

            key = str(session_file.relative_to(sessions_root)).replace("\\", "/")
            if is_quarantined(state, key, session_file):
                quarantined_skipped += 1
                continue
            if key in resumed:
                continue
            entry = state["sessions"].get(key)
            minimum_seq = -1
            if isinstance(entry, dict) and isinstance(entry.get("max_seq"), int):
                minimum_seq = int(entry["max_seq"])
            if minimum_seq < 0 and mtime < cutoff:
                LOGGER.debug("跳过过期会话：%s（最后修改 %s）", key, mtime.isoformat())
                continue

            try:
                events = read_session_events(node, helper, session_file)
            except Exception as exc:  # 单个坏会话不能拖死其它会话
                LOGGER.error("会话 %s 读取失败，跳过（下次重试）：%s", key, exc)
                failures += 1
                if not dry_run:
                    record_failure(state, key, session_file, f"读取失败：{exc}")
                    save_state(state_path, state)
                continue
            new_events = [event for event in events if event["seq"] > minimum_seq]
            if not new_events:
                continue
            if len(new_events) > MAX_BACKLOG_MESSAGES:
                # 按时间正序推进：先处理最早的一段，下次再往后走，避免中间消息被整段跳过。
                LOGGER.info("会话 %s 新增 %d 条，本轮处理最早 %d 条", key, len(new_events), MAX_BACKLOG_MESSAGES)
                new_events = new_events[:MAX_BACKLOG_MESSAGES]

            # 不够容纳一个会话的最大产出时留到下轮，不能截掉候选后提交游标。
            if not dry_run and MAX_MEMORIES_PER_RUN - written < MAX_MEMORIES_PER_SESSION:
                LOGGER.info("本轮剩余写入额度不足一个完整会话，留到下次")
                break
            transcript, seq_to_mark = build_transcript_batch(new_events, MAX_TRANSCRIPT_CHARS)
            if not transcript or seq_to_mark is None:
                continue

            processed_sessions += 1
            session_id = session_file.parent.name
            LOGGER.info("会话 %s：新消息 %d 条，送模型 %d 字符", session_id, len(new_events), len(transcript))
            if no_llm:
                LOGGER.info("--no-llm：跳过模型抽取")
                continue

            if config is None:
                config = load_llm_config(root)
                LOGGER.info("LLM：%s @ %s", config["model"], config["base_url"])
            prompt = build_prompt(transcript, key, persona_text)
            api_calls += 1
            try:
                raw = call_llm(config, prompt)
                try:
                    parsed = parse_response(raw)
                except (json.JSONDecodeError, ValueError) as exc:
                    # 三合一输出比纯记忆更长，偶发截断/转义错误；重试一次，仍失败则跳过本会话。
                    LOGGER.warning("模型输出不可用（%s），重试一次", exc)
                    api_calls += 1
                    raw = call_llm(config, prompt)
                    parsed = parse_response(raw)
            except Exception as exc:  # 单个会话失败不阻塞其它会话，游标也不推进
                LOGGER.error("会话 %s 模型调用/解析失败，跳过（下次重试）：%s", session_id, exc)
                failures += 1
                if not dry_run:
                    record_failure(state, key, session_file, f"模型调用/解析失败：{exc}")
                    save_state(state_path, state)
                continue
            items = parsed["memories"]
            candidates = [sanitize_candidate(item) for item in items]
            candidates = [item for item in candidates if item is not None]
            # 模型确实返回了条目却一条都过不了字段校验：这是 schema 违约，
            # 不能当成“本会话没有记忆”而消费游标，否则整段内容被静默丢掉。
            if items and not candidates:
                LOGGER.error("会话 %s：模型返回 %d 条但全部校验失败，不推进游标（下次重试）", session_id, len(items))
                failures += 1
                if not dry_run:
                    record_failure(state, key, session_file, "memories 字段全部校验失败")
                    save_state(state_path, state)
                continue

            kept: list[dict[str, Any]] = []
            batch_seen: set[str] = set()
            for candidate in candidates:
                digest = sha256_text(normalize_statement(candidate["statement"]))
                if digest in state["seen"] or digest in batch_seen:
                    continue
                batch_seen.add(digest)
                candidate["_digest"] = digest
                kept.append(candidate)
            # 单会话最多写 6 条；超出时本轮先写前 6 条且不推进游标，
            # 已写条目记入 seen，下轮去重后继续，避免超额候选被静默丢弃。
            overflow = len(kept) > MAX_MEMORIES_PER_SESSION
            if overflow:
                LOGGER.warning(
                    "会话 %s 候选 %d 条，超过单会话上限 %d；本轮先写 %d 条，游标不推进",
                    session_id,
                    len(kept),
                    MAX_MEMORIES_PER_SESSION,
                    MAX_MEMORIES_PER_SESSION,
                )
            # 保留完整候选列表，超额部分进入持久化待办。

            LOGGER.info(
                "模型返回 %d 条记忆，校验后 %d 条，去重后 %d 条；情绪=%s，人格重写=%s",
                len(items),
                len(candidates),
                len(kept),
                (parsed["emotion_event"] or {}).get("type", "无"),
                "是" if parsed["persona_update"] else "否",
            )

            if dry_run:
                for memory in kept:
                    LOGGER.info("[dry-run] 将写入：%s", memory["statement"][:100])
                continue

            state["pending"][key] = {
                "session_id": session_id,
                "memories": kept,
                "emotion_event": parsed["emotion_event"],
                "persona_update": parsed["persona_update"],
                "session_note": parsed["session_note"],
                "cursor": {
                    "max_seq": seq_to_mark,
                    "updated_at": isoformat_seconds(now_local()),
                    "mtime": int(mtime.timestamp()),
                },
            }
            # 必须先提交抽取结果，再产生任何外部文件副作用。
            save_state(state_path, state)
            count, errors = drain_pending(root, state, state_path, key, MAX_MEMORIES_PER_SESSION)
            written += count
            failures += errors
            if not errors:
                clear_failure(state, key)
            if parsed["persona_update"]:
                persona_text = read_persona_text(root)
            if written >= MAX_MEMORIES_PER_RUN:
                LOGGER.warning("本次已达到写入上限 %d 条，剩余会话留到下次", MAX_MEMORIES_PER_RUN)
                break

        if not dry_run:
            save_state(state_path, state)
        LOGGER.info(
            "完成：处理会话 %d 个，写入短期记忆 %d 条，模型调用 %d 次",
            processed_sessions,
            written,
            api_calls,
        )
        if quarantined_skipped:
            LOGGER.warning(
                "跳过已隔离会话 %d 个（文件更新后自动重试，或加 --retry-quarantined 强制重试）",
                quarantined_skipped,
            )
        if api_calls == 0:
            # 空跑证明：没有新消息时一个 API 请求都不发。
            LOGGER.info("本次无新消息或未启用模型：未调用模型（0 API 请求）")
        if failures:
            LOGGER.error("本轮发生 %d 处处理失败；退出码 1，请检查以上日志", failures)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DSH 短期记忆生产者（会话 → short-term）")
    parser.add_argument("--root", default=str(Path.home() / ".dsh" / "memory-system"))
    parser.add_argument("--sessions-root", default=str(Path.home() / ".dsh" / "sessions"))
    parser.add_argument("--limit", type=int, default=None, help="本次最多处理多少个会话")
    parser.add_argument("--dry-run", action="store_true", help="调用模型但不写文件/状态")
    parser.add_argument("--no-llm", action="store_true", help="只统计新增消息，不调用模型")
    parser.add_argument(
        "--retry-quarantined",
        action="store_true",
        help="解除所有已隔离会话，强制重试（会话文件变化时会自动解除，一般不需要）",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"根目录不存在：{root}", file=sys.stderr)
        return 2
    setup_logging(root, "DEBUG" if args.verbose else LOG_LEVEL_DEFAULT)

    helper = root / "tools" / "read_session.js"
    if not helper.is_file():
        LOGGER.error("缺少会话解压脚本：%s", helper)
        return 2
    try:
        node = resolve_node()
    except RuntimeError as exc:
        LOGGER.error("%s", exc)
        return 4

    try:
        return process(
            root=root,
            sessions_root=Path(args.sessions_root).expanduser().resolve(),
            node=node,
            helper=helper,
            limit=args.limit,
            dry_run=args.dry_run,
            no_llm=args.no_llm,
            retry_quarantined=args.retry_quarantined,
        )
    except (RuntimeError, OSError, urlerror.URLError) as exc:
        LOGGER.error("运行失败：%s", exc)
        return 3
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("未预期错误：%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
