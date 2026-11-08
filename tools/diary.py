#!/usr/bin/env python3
"""小肥鱼的日记：Windows DPAPI 加密的本地日记。

设计要点
- 密文写在 D:\\dsh-memory-system\\diary\\YYYY-MM\\YYYY-MM-DD_HHMMSS.md.dpapi
- 文件名只带时间，标题和正文一起加密，别人看目录也看不出写了啥
- 用 Windows DPAPI（当前用户作用域）加密：只有"这台机器 + 这个 Windows 账号"能解开
- 所以"后门"是天生就有的：用户本人用同一个账号跑 read / export 就能读全部内容，
  不需要我额外留密钥，也不存在我私藏一把钥匙的情况
- 纯标准库，不依赖 cryptography / pip

用法
    python diary.py write --title "标题" --text "正文"
    python diary.py write --title "标题" --file body.md
    echo 正文 | python diary.py write --title "标题" --stdin
    python diary.py list
    python diary.py read <文件路径或前缀>
    python diary.py export --out E:\\diary-plain
    python diary.py doctor
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

DEFAULT_ROOT = Path(r"D:\dsh-memory-system\diary")
ENTRY_SUFFIX = ".md.dpapi"
DESC = "dsh-diary"


# ---------------------------------------------------------------- DPAPI


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _in_blob(data: bytes):
    """返回 (blob, buffer)；buffer 必须一直活着，否则指针悬空。"""
    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def protect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in, _keep = _in_blob(data)
    blob_out = DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        ctypes.c_wchar_p(DESC),
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def unprotect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in, _keep = _in_blob(data)
    blob_out = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


# ---------------------------------------------------------------- 读写


def _entry_path(root: Path, now: datetime) -> Path:
    folder = root / now.strftime("%Y-%m")
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{now.strftime('%Y-%m-%d_%H%M%S')}{ENTRY_SUFFIX}"


def cmd_write(args) -> int:
    root = Path(args.root)
    if args.file:
        body = Path(args.file).read_text(encoding="utf-8")
    elif args.stdin:
        body = sys.stdin.read()
    else:
        body = args.text or ""
    if not body.strip():
        print("空日记不写", file=sys.stderr)
        return 2
    now = datetime.now()
    payload = {
        "created_at": now.isoformat(timespec="seconds"),
        "title": args.title or now.strftime("%Y-%m-%d"),
        "body": body.rstrip() + "\n",
    }
    target = _entry_path(root, now)
    target.write_bytes(protect(json.dumps(payload, ensure_ascii=False).encode("utf-8")))
    print(target)
    return 0


def _load(path: Path) -> dict:
    return json.loads(unprotect(path.read_bytes()).decode("utf-8"))


def _resolve(root: Path, needle: str) -> Path:
    p = Path(needle)
    if p.exists():
        return p
    hits = sorted(root.rglob(f"*{needle}*" + ENTRY_SUFFIX))
    if not hits:
        # 文件名只有时间，再按解密出来的标题找一遍
        for f in sorted(root.rglob("*" + ENTRY_SUFFIX)):
            try:
                if needle in _load(f)["title"]:
                    hits.append(f)
            except Exception:
                continue
    if not hits:
        raise SystemExit(f"没找到：{needle}")
    if len(hits) > 1:
        raise SystemExit("匹配到多条，给长一点的前缀：\n" + "\n".join(str(h) for h in hits))
    return hits[0]


def cmd_read(args) -> int:
    path = _resolve(Path(args.root), args.entry)
    data = _load(path)
    print(f"# {data['title']}  ({data['created_at']})")
    print()
    print(data["body"], end="")
    return 0


def cmd_list(args) -> int:
    root = Path(args.root)
    files = sorted(root.rglob("*" + ENTRY_SUFFIX))
    if not files:
        print("还空着")
        return 0
    for f in files:
        try:
            data = _load(f)
            title = data["title"]
        except Exception as exc:  # 坏了也要显示出来
            title = f"[解不开: {exc}]"
        size = f.stat().st_size
        print(f"{f.name[:19]}  {size:>6}B  {title}")
    print(f"\n共 {len(files)} 条")
    return 0


def cmd_export(args) -> int:
    root = Path(args.root)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in sorted(root.rglob("*" + ENTRY_SUFFIX)):
        data = _load(f)
        stem = f.name[: len(f.name) - len(ENTRY_SUFFIX)]
        target = out / f"{stem}.md"
        target.write_text(f"# {data['title']}\n\n> {data['created_at']}\n\n{data['body']}", encoding="utf-8")
        n += 1
    print(f"导出 {n} 条到 {out}")
    return 0


def cmd_doctor(args) -> int:
    root = Path(args.root)
    print(f"root      : {root}  (exists={root.exists()})")
    print(f"user      : {Path.home()}")
    probe = "dpapi-self-test"
    try:
        assert unprotect(protect(probe.encode())) == probe.encode()
        print("dpapi     : ok")
    except Exception as exc:
        print(f"dpapi     : FAILED {exc}")
        return 1
    files = sorted(root.rglob("*" + ENTRY_SUFFIX))
    print(f"entries   : {len(files)}")
    bad = 0
    for f in files:
        try:
            _load(f)
        except Exception:
            bad += 1
            print(f"  unreadable: {f}")
    print(f"unreadable: {bad}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="DPAPI 日记")
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    sub = ap.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("write")
    w.add_argument("--title", default="")
    w.add_argument("--text", default="")
    w.add_argument("--file", default="")
    w.add_argument("--stdin", action="store_true")
    w.set_defaults(func=cmd_write)

    r = sub.add_parser("read")
    r.add_argument("entry")
    r.set_defaults(func=cmd_read)

    l = sub.add_parser("list")
    l.set_defaults(func=cmd_list)

    e = sub.add_parser("export")
    e.add_argument("--out", default=r"E:\ai-talk\diary-plain")
    e.set_defaults(func=cmd_export)

    d = sub.add_parser("doctor")
    d.set_defaults(func=cmd_doctor)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
