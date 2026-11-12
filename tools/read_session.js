#!/usr/bin/env node
/**
 * read_session.js —— DSH 会话文件（多帧 zstd 压缩 JSONL）解压并抽取对话文本。
 *
 * 背景：
 *   DSH 的 session.v4.jsonl.zstd 不是“一个 zstd 帧”，而是每次落盘追加一个独立
 *   zstd 帧（多个帧首尾相接）。Node 的 zstdDecompressSync 只解第一帧就会停，
 *   因此这里按 zstd magic（28 B5 2F FD）切分后逐帧解压，再把文本拼起来。
 *
 * 用法：
 *   node tools/read_session.js <session.v4.jsonl.zstd>
 *
 * 输出（stdout，每行一个 JSON）：
 *   {"seq":12,"time":...,"role":"user","text":"..."}
 *   {"seq":13,"time":...,"role":"assistant","text":"..."}
 * 只输出 user/message 与 assistant/message 的 text 块；跳过 reasoning、工具调用、
 * 工具结果，避免把大量日志喂给提炼模型。解析失败的行静默跳过。
 *
 * 插件注入的 user 消息（source.kind 不是 "user"）一律跳过：
 * 例如 dsh-time-context 的 "[Time sampled ...]"、dsh-emotion-context 的
 * "[情绪状态 · 自动注入]"。它们不是用户说的话，喂给提炼模型会把噪音
 * 误当成用户情绪/偏好。
 */

'use strict';

const fs = require('fs');
const zlib = require('zlib');

const file = process.argv[2];
if (!file) {
  process.stderr.write('usage: read_session.js <session.v4.jsonl.zstd>\n');
  process.exit(2);
}

let buf;
try {
  buf = fs.readFileSync(file);
} catch (err) {
  process.stderr.write(`read failed: ${err.message}\n`);
  process.exit(1);
}

const MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd]);
const offsets = [];
let cursor = 0;
while ((cursor = buf.indexOf(MAGIC, cursor)) !== -1) {
  offsets.push(cursor);
  cursor += 4;
}
if (offsets.length === 0) {
  process.stderr.write('no zstd frame found\n');
  process.exit(2);
}

let text = '';
let failed = false;
for (let i = 0; i < offsets.length; i += 1) {
  const start = offsets[i];
  const end = i + 1 < offsets.length ? offsets[i + 1] : buf.length;
  try {
    text += zlib.zstdDecompressSync(buf.subarray(start, end)).toString('utf8');
  } catch (err) {
    // 可输出诊断用的完整帧，但非零退出，禁止生产者跨过缺口提交游标。
    failed = true;
    process.stderr.write(`frame ${i} failed: ${err.message}\n`);
  }
}

function collect(content) {
  if (!Array.isArray(content)) return '';
  return content
    .filter((block) => block && block.type === 'text' && typeof block.text === 'string')
    .map((block) => block.text)
    .join('\n')
    .trim();
}

process.exitCode = failed ? 1 : 0;
for (const line of text.split('\n')) {
  if (!line.trim()) continue;
  let event;
  try {
    event = JSON.parse(line);
  } catch {
    process.exitCode = 1;
    process.stderr.write('invalid JSONL record\n');
    continue;
  }
  let role = '';
  let body = '';
  if (event.type === 'user/message' && event.data) {
    const kind = event.data.source && event.data.source.kind;
    // 只保留真人输入；time-context / emotion-context 等插件的注入不算用户发言。
    if (typeof kind === 'string' && kind !== 'user') continue;
    role = 'user';
    body = collect(event.data.content);
  } else if (event.type === 'assistant/message' && event.data && event.data.message) {
    role = 'assistant';
    body = collect(event.data.message.content);
  }
  if (!role || !body) continue;
  process.stdout.write(
    JSON.stringify({ seq: event.seq, time: event.time, role, text: body }) + '\n',
  );
}
