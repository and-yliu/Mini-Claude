import os
import json
import time

from pathlib import Path
from anthropic import Anthropic
from tools import WORKDIR
from dotenv import load_dotenv

load_dotenv()

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
MODEL = os.getenv("MODEL")

TRANSCRIPT_DIR = Path.cwd() / ".transcripts"
TOOL_RESULTS_DIR = Path.cwd() / ".task_outputs" / "tool-results"

CONTEXT_LIMIT = 50000
KEEP_RECENT = 3
TOOL_RESULT_LIMIT = 30000
PERSIST_THRESHOLD = 200000

def estimate_size(messages):
    return len(str(messages))

def snip_compact(messages, max_messages=50):
    if len(messages) <= max_messages:
        return messages
    
    keep_head, keep_tail = KEEP_RECENT,  max_messages - KEEP_RECENT
    snipped = len(messages) - keep_head - keep_tail

    return messages[:keep_head] + [{"role": "user", "content": f"[Context snipped: {snipped} middle messages omitted]"}] + messages[-keep_tail:]

def collect_tool_results(messages):
    blocks = []
    for mi, msg in enumerate(messages):
        if msg.get("role") != "user" or not isinstance(msg.get("content", list)):
            continue
        for bi, block in enumerate(msg["content"]):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                blocks.append((mi, bi, block))
    return blocks

def micro_compact(messages):
    tool_results = collect_tool_results(messages)
    if len(tool_results) <= KEEP_RECENT: return messages
    for _, _, block in tool_results[:-KEEP_RECENT]:
        if len(block.get("content", "")) > 120:
            block["content"] = "[Earlier tool result compacted. Re-run if needed.]"
    return messages

def persist_large_output(tool_use_id, output):
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = TOOL_RESULTS_DIR / f"{tool_use_id}.txt"
    if not path.exists():
        path.write_text(output, encoding="utf-8")
    return f"<persisted-output>\nFull output: {path}\nPreview:\n{output[:2000]}\n</persisted-output>"

def tool_result_budget(messages, max_budget = PERSIST_THRESHOLD):
    last = messages[-1] if messages else None
    if not last or last.get("role") != "user" or not isinstance(last.get("content"), list): 
        return messages
    blocks = [(i, b) for i, b in enumerate(last["content"]) if isinstance(b, dict) and b.get("type") == "tool_result"]
    total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    if total <= max_budget: 
        return messages

    ranked = sorted(blocks, key=lambda p: len(str(p[1].get("content", ""))), reverse=True)
    for _, block in ranked:
        if total <= max_budget: break
        content = str(block.get("content", ""))
        if len(content) <= TOOL_RESULT_LIMIT: 
            continue
        tid = block.get("tool_use_id", "unknown")
        block["content"] = persist_large_output(tid, content)
        total = sum(len(str(b.get("content", ""))) for _, b in blocks)

    return messages

    
def write_transcript(messages):
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with path.open("w") as f:
        for msg in messages: 
            f.write(json.dumps(msg, default=str) + "\n")
    return path

def summarize_history(messages):
    conversation = json.dumps(messages, default=str)[:80000]
    prompt = """Summarize this agent conversation so work can continue.\n
              Preserve: 
              1. current goal, 
              2. key findings/decisions, 
              3. files read/changed, 
              4. remaining work, 
              5. user constraints.\nBe compact but concrete.\n\n""" + conversation
    response = client.messages.create(model=MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=2000)
    return "\n".join(
        getattr(block, "text", "")
        for block in response.content
        if getattr(block, "type", None) == "text").strip() or "(empty summary)"

def compact_history(messages):
    transcript_path = write_transcript(messages)
    print(f"[transcript saved: {transcript_path}]")
    summary = summarize_history(messages)
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]

def reactive_compact(messages):
    transcript = write_transcript(messages)
    summary = summarize_history(messages)
    return [{"role": "user", "content": f"[Reactive compact]\n\n{summary}"}, *messages[-5:]]
