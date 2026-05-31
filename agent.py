import os
from dataclasses import dataclass

from anthropic import Anthropic
from dotenv import load_dotenv

from messages import dump_debug, extract_text, normalize_messages
from todoManager import TodoManager
from tools import TOOLS, build_tool_handlers
from skill import SkillRegistry
from pathlib import Path
from hooks import trigger_hooks
from context import AgentContext
from memory import tool_result_budget, snip_compact, micro_compact, compact_history, CONTEXT_LIMIT, reactive_compact, estimate_size

load_dotenv()

from tools import WORKDIR 

# ── Client & model ───────────────────────────────────────────────────────────

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

MODEL = os.getenv("MODEL")
SKILLS_DIR = Path.cwd() / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"

# ── State ────────────────────────────────────────────────────────────────────

TODO = TodoManager()
SKILL_REGISTRY = SkillRegistry(SKILLS_DIR)

# The lambda lets us reference run_subagent before it's defined — standard
# Python forward-reference pattern; the lambda is only called at runtime.
TOOL_HANDLERS = build_tool_handlers(TODO, SKILL_REGISTRY, lambda prompt: run_subagent(prompt))

# ── Prompts ──────────────────────────────────────────────────────────────────

SYSTEM = (
    f"You are an autonomous coding agent working in {WORKDIR}. "
    "For multi-step tasks, call todo first and keep exactly one item in_progress. "
    "Prefer edit over write for small changes. Read before you write. "
    "Verify work with bash or read after edits. Prefer tools over prose."
    f"Skills available: {SKILL_REGISTRY.describe_available()}"
    "Use load_skill when a task needs specialized instructions before you act."
)
SUBAGENT_SYSTEM = (
    f"You are a coding subagent working in {WORKDIR}. "
    "Complete the delegated task, verify your work, then return a concise summary."
)

# ── LoopState ────────────────────────────────────────────────────────────────


@dataclass
class LoopState:
    messages: list
    turn_count: int = 1
    transition_reason: str | None = None


# ── Tool dispatch ─────────────────────────────────────────────────────────────


def execute_tool_calls(response_content, messages: list) -> list[dict]:
    results = []
    used_todo = False

    for block in response_content:
        if block.type != "tool_use":
            continue

        if block.name == "compact":
            messages[:] = reactive_compact(messages)
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": "[Compacted. Conversation history has been summarized.]"})
            break

        # Trigger PreToolUse hooks (handles permissions and logging)
        blocked_reason = trigger_hooks("PreToolUse", block)
        if blocked_reason is not None:
            output = f"Permission denied: {blocked_reason}"
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })
            continue

        handler = TOOL_HANDLERS.get(block.name)
        if block.name == "task":
            prompt = block.input.get("prompt", "")
            print(f"{AgentContext.get_prefix()}> task: {prompt}")
        
        try:
            output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
        except Exception as exc:
            output = f"Error: {exc}"

        if block.name != "task":
            print(f"{AgentContext.get_prefix()}> {block.name}:")
            indent = AgentContext.get_indent()
            output_str = str(output)[:1000]
            for line in output_str.splitlines():
                print(f"{indent}{line}")

        results.append({
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": output,
        })

        if block.name == "todo":
            used_todo = True

        # Trigger PostToolUse hooks
        trigger_hooks("PostToolUse", block, output)

    if used_todo:
        TODO.state.rounds_since_update = 0
    else:
        TODO.note_round_without_update()
        reminder = TODO.reminder()
        if reminder:
            results.append({"type": "text", "text": reminder})

    return results


# ── Subagent ─────────────────────────────────────────────────────────────────


def run_subagent(prompt: str) -> str:
    AgentContext.push("subagent")
    try:
        subagent_tools = [t for t in TOOLS if t["name"] not in {"todo", "task"}]
        sub_messages = [{"role": "user", "content": prompt}]

        for _ in range(30):
            response = client.messages.create(
                model=MODEL,
                system=SUBAGENT_SYSTEM,
                messages=normalize_messages(sub_messages),
                tools=subagent_tools,
                max_tokens=8000,
            )
            sub_messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason != "tool_use":
                return extract_text(response.content) or "(no summary)"

            results = execute_tool_calls(response.content, sub_messages)
            sub_messages.append({"role": "user", "content": results})

        return (
            extract_text(response.content)
            + "\n(Warning: subagent stopped after reaching 30 turn limit)"
        )
    finally:
        AgentContext.pop()


# ── Agent loop ────────────────────────────────────────────────────────────────


def agent_loop(state: LoopState) -> None:
    dump_debug(state)
    while True:
        state.messages[:] = tool_result_budget(state.messages)
        state.messages[:] = snip_compact(state.messages)
        state.messages[:] = micro_compact(state.messages)

        if estimate_size(state.messages) > CONTEXT_LIMIT:
            print("[auto compact]")
            state.messages[:] = compact_history(state.messages)

        print(f"{AgentContext.get_prefix()}> Thinking")

        response = client.messages.create(
            model=MODEL,
            tools=TOOLS,
            system=SYSTEM,
            max_tokens=8000,
            messages=normalize_messages(state.messages),
        )
        state.messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            state.transition_reason = None
            trigger_hooks("Stop", state.messages)
            return

        results = execute_tool_calls(response.content, state.messages)
        if not results:
            state.transition_reason = None
            trigger_hooks("Stop", state.messages)
            return

        state.messages.append({"role": "user", "content": results})
        state.turn_count += 1
        state.transition_reason = "tool_result"
