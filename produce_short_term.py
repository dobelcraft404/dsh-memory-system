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
        LOGGER.warning("状态文件损坏，将重建：%s（%s）", path, exc)
        return {"version": PRODUCER_VERSION, "sessions": {}, "seen": {}, "runs": 0}
    if not isinstance(data, dict):
        return {"version": PRODUCER_VERSION, "sessions": {}, "seen": {}, "runs": 0}
    data.setdefault("sessions", {})
    data.setdefault("seen", {})
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
    memories = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
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
    if completed.returncode != 0 and not completed.stdout.strip():
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


def build_transcript(events: list[dict[str, Any]], max_chars: int) -> str:
    """按时间正序拼接；超长时从最旧的开始丢，保留最近上下文。"""

    lines: list[str] = []
    used = 0
    for event in events:
        text = redact_secrets(event["text"].strip())
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[:MAX_MESSAGE_CHARS] + "…（已截断）"
        line = f"{'用户' if event['role'] == 'user' else '助手'}：{text}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n\n".join(lines)


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
    """写入 short-term/<日期>_<时间>_<会话短名>_<序号>.md。"""

    now = now_local()
    entry_id = f"{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    short_session = re.sub(r"[^0-9A-Za-z]", "", session_id)[-8:] or "session"
    filename = f"{now.strftime('%Y-%m-%d_%H%M%S')}_{short_session}_{index:02d}.md"
    target = root / "short-term" / filename

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

    try:
        with open(target, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except FileExistsError:
        target = target.with_name(f"{target.stem}-{uuid.uuid4().hex[:4]}{target.suffix}")
        with open(target, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
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
) -> int:
    state_path = root / "state" / "producer_state.json"
    lock_path = root / ".locks" / "producer.lock"

    with FileLock(lock_path):
        state = load_state(state_path)
        sessions = find_sessions(sessions_root)
        LOGGER.info("发现会话文件 %d 个", len(sessions))
        config: dict[str, str] | None = None
        if not no_llm:
            config = load_llm_config(root)
            LOGGER.info("LLM：%s @ %s", config["model"], config["base_url"])

        cutoff = now_local() - timedelta(days=MAX_SESSION_AGE_DAYS)
        persona_text = read_persona_text(root)
        if persona_text:
            LOGGER.info("已有关系档案：%d 字", len(persona_text))
        written = 0
        processed_sessions = 0
        api_calls = 0  # 只在确实有新消息、且需要模型时才递增，用于证明空跑不花钱

        for session_file in sessions:
            if limit is not None and processed_sessions >= limit:
                break
            try:
                mtime = datetime.fromtimestamp(session_file.stat().st_mtime).astimezone()
            except OSError:
                continue

            key = str(session_file.relative_to(sessions_root)).replace("\\", "/")
            entry = state["sessions"].get(key)
            minimum_seq = -1
            if isinstance(entry, dict) and isinstance(entry.get("max_seq"), int):
                minimum_seq = int(entry["max_seq"])
            if minimum_seq < 0 and mtime < cutoff:
                LOGGER.debug("跳过过期会话：%s（最后修改 %s）", key, mtime.isoformat())
                continue

            events = read_session_events(node, helper, session_file)
            new_events = [event for event in events if event["seq"] > minimum_seq]
            if not new_events:
                continue
            if len(new_events) > MAX_BACKLOG_MESSAGES:
                # 按时间正序推进：先处理最早的一段，下次再往后走，避免中间消息被整段跳过。
                LOGGER.info("会话 %s 新增 %d 条，本轮处理最早 %d 条", key, len(new_events), MAX_BACKLOG_MESSAGES)
                new_events = new_events[:MAX_BACKLOG_MESSAGES]

            seq_to_mark = max(event["seq"] for event in new_events)
            transcript = build_transcript(new_events, MAX_TRANSCRIPT_CHARS)
            if not transcript:
                continue

            processed_sessions += 1
            session_id = session_file.parent.name
            LOGGER.info("会话 %s：新消息 %d 条，送模型 %d 字符", session_id, len(new_events), len(transcript))
            if no_llm:
                LOGGER.info("--no-llm：跳过模型抽取")
                continue

            assert config is not None
            prompt = build_prompt(transcript, key, persona_text)
            api_calls += 1
            raw = call_llm(config, prompt)
            try:
                parsed = parse_response(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                # 三合一输出比纯记忆更长，偶发截断/转义错误；重试一次，仍失败则放弃本次会话。
                LOGGER.warning("模型输出不是合法 JSON（%s），重试一次", exc)
                api_calls += 1
                raw = call_llm(config, prompt)
                parsed = parse_response(raw)
            items = parsed["memories"]
            candidates = [sanitize_candidate(item) for item in items]
            candidates = [item for item in candidates if item is not None]

            kept: list[dict[str, Any]] = []
            for candidate in candidates:
                digest = sha256_text(normalize_statement(candidate["statement"]))
                if digest in state["seen"]:
                    continue
                candidate["_digest"] = digest
                kept.append(candidate)
            kept = kept[: min(MAX_MEMORIES_PER_SESSION, MAX_MEMORIES_PER_RUN - written)]

            LOGGER.info(
                "模型返回 %d 条记忆，校验后 %d 条，去重后 %d 条；情绪=%s，人格重写=%s",
                len(items),
                len(candidates),
                len(kept),
                (parsed["emotion_event"] or {}).get("type", "无"),
                "是" if parsed["persona_update"] else "否",
            )

            for index, memory in enumerate(kept, start=1):
                if dry_run:
                    LOGGER.info("[dry-run] 将写入：%s", memory["statement"][:100])
                    continue
                path = write_entry(root, session_id, memory, index)
                state["seen"][memory["_digest"]] = isoformat_seconds(now_local())
                written += 1
                LOGGER.info("已写入短期记忆：%s", path.relative_to(root))

            # 情绪 / 关系档案 / 接续句：与记忆复用同一次模型调用，零额外请求。
            try:
                if parsed["emotion_event"]:
                    update_emotion(root, parsed["emotion_event"], dry_run)
                if parsed["persona_update"]:
                    update_persona(root, parsed["persona_update"], dry_run)
                if parsed["session_note"]:
                    update_last_session(root, session_id, parsed["session_note"], dry_run)
            except OSError as exc:
                LOGGER.warning("情绪/人格/接续句写入失败（不影响记忆）：%s", exc)

            if dry_run:
                continue

            if parsed["persona_update"]:
                persona_text = read_persona_text(root)

            # 只有成功处理完（哪怕 0 条）才推进 max_seq；模型失败时不推进，下次重试。
            state["sessions"][key] = {
                "max_seq": seq_to_mark,
                "updated_at": isoformat_seconds(now_local()),
                "mtime": int(mtime.timestamp()),
            }
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
        if api_calls == 0:
            # 空跑证明：没有新消息时一个 API 请求都不发。
            LOGGER.info("本次无新消息或未启用模型：未调用模型（0 API 请求）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DSH 短期记忆生产者（会话 → short-term）")
    parser.add_argument("--root", default=str(Path.home() / ".dsh" / "memory-system"))
    parser.add_argument("--sessions-root", default=str(Path.home() / ".dsh" / "sessions"))
    parser.add_argument("--limit", type=int, default=None, help="本次最多处理多少个会话")
    parser.add_argument("--dry-run", action="store_true", help="调用模型但不写文件/状态")
    parser.add_argument("--no-llm", action="store_true", help="只统计新增消息，不调用模型")
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
        )
    except (RuntimeError, OSError, urlerror.URLError) as exc:
        LOGGER.error("运行失败：%s", exc)
        return 3
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("未预期错误：%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
