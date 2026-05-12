#!/usr/bin/env python3
"""
DevMind Pro — Personal Knowledge Base Tool
===========================================
A lightweight CLI knowledge base driven by DeepSeek.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from html import unescape
from pathlib import Path

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DB_PATH = Path(__file__).parent / "devmind.db"
API_URL = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-v4-pro"
REQUEST_TIMEOUT = 120

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    """Open (or create) the SQLite database and ensure table schemas exist."""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS notes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            url         TEXT,
            title       TEXT,
            content     TEXT,
            concepts    TEXT,
            summary     TEXT,
            added_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
            processed_at DATETIME,
            status      TEXT DEFAULT 'raw'
        );

        CREATE TABLE IF NOT EXISTS relations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source_note_id  INTEGER,
            target_note_id  INTEGER,
            reason          TEXT,
            strength        TEXT
        );
    """)
    db.commit()
    return db

# ---------------------------------------------------------------------------
# HTML scraping
# ---------------------------------------------------------------------------

def _clean_html(html: str) -> str:
    """Strip scripts/styles/tags and collapse whitespace."""
    # Remove script and style blocks wholly
    text = re.sub(
        r'<(script|style)\b[^>]*>.*?</\1>',
        '', html, flags=re.DOTALL | re.IGNORECASE,
    )
    # Strip remaining tags
    text = re.sub(r'<[^>]+>', ' ', text)
    # Decode entities
    text = unescape(text)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _extract_title(html: str) -> str:
    m = re.search(r'<title\b[^>]*>(.*?)</title>', html, flags=re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else "Untitled"

# ---------------------------------------------------------------------------
# DeepSeek API
# ---------------------------------------------------------------------------

def _call_deepseek(messages: list[dict], reasoning_effort: str) -> dict:
    """Send a chat completion request. Retries once on failure."""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("Error: DEEPSEEK_API_KEY not found in environment or .env file")
        sys.exit(1)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": MODEL,
        "messages": messages,
        "reasoning_effort": reasoning_effort,
    }

    for attempt in (1, 2):
        try:
            resp = requests.post(API_URL, json=body, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            if attempt == 1:
                print(f"  API call failed ({exc}), retrying in 2s …")
                time.sleep(2)
            else:
                raise RuntimeError(f"DeepSeek API call failed: {exc}") from exc


def _parse_json_response(raw: str) -> dict:
    """Best-effort JSON extraction (handles markdown code fences)."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r'^```(?:json)?\s*', '', text, count=1)
        text = re.sub(r'\s*```$', '', text, count=1)
        text = text.strip()
    return json.loads(text)

# ---------------------------------------------------------------------------
# build-context helper
# ---------------------------------------------------------------------------

def _build_processed_context(db: sqlite3.Connection) -> str:
    """Return a string listing every already-processed note."""
    rows = db.execute(
        "SELECT id, title, concepts, summary FROM notes WHERE status = 'processed'"
    ).fetchall()
    parts = []
    for r in rows:
        parts.append(
            f"ID: {r['id']}, 标题: {r['title']}, "
            f"概念: {r['concepts'] or '(无)'}, "
            f"摘要: {r['summary'] or '(无)'}"
        )
    return "\n".join(parts)


PROCESS_PROMPT_TEMPLATE = """你是一个知识库分析引擎。请对以下文章进行深度分析，并输出一个严格的 JSON 对象。
已有知识库中的笔记如下（可能很长，请全部参考）：
{context}

现在，请分析以下文章：
标题：{title}
内容：{content}

输出 JSON 格式：
{{
  "concepts": ["概念1", "概念2", ...],
  "summary": "200字左右的中文摘要",
  "relations": [
    {{"target_id": 关联笔记ID, "reason": "关联理由", "strength": "强/中/弱"}},
    ...
  ],
  "deep_insight": "如果与已有知识存在矛盾、进化或值得深思的点，请用一段话阐述（深推理模式下输出，普通模式输出空字符串）"
}}
请确保只输出 JSON，不要有任何额外文字。"""

ASK_PROMPT_TEMPLATE = """你是一个知识库问答助手。请严格基于以下上下文回答用户的问题。如果上下文不足以回答问题，请如实说明。

上下文：
{context}

用户问题：{question}

请提供回答，并在回答末尾列出引用的笔记标题。"""

# ---------------------------------------------------------------------------
# Subcommand: add
# ---------------------------------------------------------------------------

def cmd_add(args: argparse.Namespace) -> None:
    db = get_db()
    try:
        resp = requests.get(
            args.url, timeout=30,
            headers={"User-Agent": "DevMindPro/1.0"},
        )
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding  # best-effort charset detection
    except requests.RequestException as exc:
        print(f"Error fetching URL: {exc}")
        sys.exit(1)

    title = _extract_title(resp.text)
    content = _clean_html(resp.text)

    cur = db.execute(
        "INSERT INTO notes (url, title, content, status) VALUES (?, ?, ?, 'raw')",
        (args.url, title, content),
    )
    db.commit()
    note_id = cur.lastrowid
    print(f"Note added: id={note_id}, title={title}")
    db.close()

# ---------------------------------------------------------------------------
# Subcommand: process
# ---------------------------------------------------------------------------

def _process_one(note: sqlite3.Row, context_str: str, reasoning_effort: str, db: sqlite3.Connection) -> str | None:
    """Process a single raw note. Returns the updated context line on success, None on failure."""
    prompt = PROCESS_PROMPT_TEMPLATE.format(
        context=context_str or "(尚无已处理笔记)",
        title=note["title"],
        content=note["content"],
    )
    messages = [{"role": "user", "content": prompt}]

    try:
        result = _call_deepseek(messages, reasoning_effort)
        data = _parse_json_response(result["choices"][0]["message"]["content"])
    except (json.JSONDecodeError, KeyError, RuntimeError) as exc:
        print(f"  Failed for note {note['id']}: {exc}")
        return None

    concepts = ", ".join(data.get("concepts", []))
    summary = data.get("summary", "")
    deep = data.get("deep_insight", "")

    if deep:
        summary = f"{summary}\n\n[深度洞察] {deep}"

    db.execute(
        "UPDATE notes SET status='processed', concepts=?, summary=?, processed_at=datetime('now') WHERE id=?",
        (concepts, summary, note["id"]),
    )

    for rel in data.get("relations", []):
        db.execute(
            "INSERT INTO relations (source_note_id, target_note_id, reason, strength) VALUES (?,?,?,?)",
            (note["id"], rel["target_id"], rel["reason"], rel["strength"]),
        )

    db.commit()
    print(f"  Note {note['id']} processed OK.")
    return f"ID: {note['id']}, 标题: {note['title']}, 概念: {concepts}, 摘要: {summary}"


def cmd_process(args: argparse.Namespace) -> None:
    db = get_db()

    # Build context from ALL already-processed notes (intentionally unbounded)
    context_str = _build_processed_context(db)

    # Collect notes to process
    if args.all:
        raw_notes = db.execute("SELECT * FROM notes WHERE status='raw'").fetchall()
    else:
        raw_notes = db.execute("SELECT * FROM notes WHERE id=? AND status='raw'", (args.note_id,)).fetchall()

    if not raw_notes:
        print("No raw notes to process.")
        db.close()
        return

    reasoning_effort = "high" if args.deep else "medium"
    print(f"Processing {len(raw_notes)} note(s) [reasoning_effort={reasoning_effort}] …")

    for note in raw_notes:
        print(f"\n--- Note {note['id']}: {note['title']} ---")
        new_line = _process_one(note, context_str, reasoning_effort, db)
        if new_line:
            # Append so subsequent notes can discover this one
            if context_str:
                context_str += "\n" + new_line
            else:
                context_str = new_line

    db.close()

# ---------------------------------------------------------------------------
# Subcommand: ask
# ---------------------------------------------------------------------------

def cmd_ask(args: argparse.Namespace) -> None:
    db = get_db()
    question: str = args.question

    # Simple tokenisation: split on non-chinese-word boundaries, keep tokens >= 2 chars
    tokens = [t for t in re.split(r'[\s,，。.；;：:！!？?、]+', question) if len(t) >= 2]

    matched_ids: set[int] = set()
    for tok in tokens:
        rows = db.execute(
            "SELECT id FROM notes WHERE status='processed' AND (concepts LIKE ? OR title LIKE ? OR content LIKE ?)",
            (f"%{tok}%", f"%{tok}%", f"%{tok}%"),
        ).fetchall()
        matched_ids.update(r["id"] for r in rows)

    if not matched_ids:
        print("No relevant notes found in the knowledge base.")
        db.close()
        return

    # Grab up to 10 matching notes, newest first
    placeholders = ",".join("?" for _ in matched_ids)
    notes = db.execute(
        f"SELECT id, title, content, summary FROM notes WHERE id IN ({placeholders}) ORDER BY processed_at DESC LIMIT 10",
        tuple(matched_ids),
    ).fetchall()

    parts = []
    for n in notes:
        preview = (n["content"] or "")[:2000]
        parts.append(
            f"[笔记: {n['title']}]\n摘要: {n['summary'] or '(无)'}\n内容: {preview}"
        )
    context_str = "\n\n---\n\n".join(parts)

    prompt = ASK_PROMPT_TEMPLATE.format(context=context_str, question=question)
    reasoning_effort = "high" if args.deep else "medium"
    messages = [{"role": "user", "content": prompt}]

    try:
        result = _call_deepseek(messages, reasoning_effort)
        answer = result["choices"][0]["message"]["content"].strip()
        print(answer)
    except Exception as exc:
        print(f"Error: {exc}")

    db.close()

# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="DevMind Pro — Personal Knowledge Base Tool",
    )
    subs = parser.add_subparsers(dest="command")

    # ---- add ----
    p_add = subs.add_parser("add", help="Add a URL to the knowledge base")
    p_add.add_argument("--url", required=True, help="URL to fetch and store")

    # ---- process ----
    p_proc = subs.add_parser("process", help="Process raw notes with the LLM")
    group = p_proc.add_mutually_exclusive_group(required=True)
    group.add_argument("--note-id", type=int, help="Process a specific note by ID")
    group.add_argument("--all", action="store_true", help="Process all raw notes")
    p_proc.add_argument("--deep", action="store_true", help="Enable deep reasoning (reasoning_effort=high)")

    # ---- ask ----
    p_ask = subs.add_parser("ask", help="Ask a question against the knowledge base")
    p_ask.add_argument("question", help="The question to ask")
    p_ask.add_argument("--deep", action="store_true", help="Enable deep reasoning (reasoning_effort=high)")

    args = parser.parse_args()

    if args.command == "add":
        cmd_add(args)
    elif args.command == "process":
        cmd_process(args)
    elif args.command == "ask":
        cmd_ask(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
