"""Parse Claude CLI stream-json output into structured events."""

import json
import logging

logger = logging.getLogger(__name__)


def parse_stream_line(line: str) -> dict | None:
    """Parse a single line of stream-json output from `claude --print --output-format stream-json`.

    Claude stream-json emits one JSON object per line. Key message types:
    - {"type": "assistant", "message": {"content": [...]}} - assistant turns (text, thinking, tool_use blocks)
    - {"type": "user", "message": {"content": [...]}}      - tool results (wrapped as user messages)
    - {"type": "system", "subtype": "init", ...}           - session init
    - {"type": "result", ...}                              - final result
    - {"type": "error", ...}                               - errors
    - {"type": "rate_limit_event", ...}                    - rate limit notifications

    Returns a dict with keys: event_type, summary, raw_json (the original JSON string)
    Returns None if the line cannot be parsed.
    """
    line = line.strip()
    if not line:
        return None

    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        logger.debug("Non-JSON line from stream: %s", line[:200])
        return None

    msg_type = data.get("type", "unknown")

    if msg_type == "assistant":
        message = data.get("message", {})
        content_blocks = message.get("content", [])
        text_parts = []
        for block in content_blocks:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_name = block.get("name", "tool")
                    tool_input = block.get("input", {})
                    if tool_name == "Bash":
                        text_parts.append(f"[$ {tool_input.get('command', '')[:80]}]")
                    elif tool_name == "Read":
                        text_parts.append(f"[Read {tool_input.get('file_path', '?')}]")
                    elif tool_name in ("Edit", "Write"):
                        text_parts.append(f"[{tool_name} {tool_input.get('file_path', '?')}]")
                    elif tool_name == "WebSearch":
                        text_parts.append(f"[WebSearch: {tool_input.get('query', '?')}]")
                    elif tool_name == "WebFetch":
                        text_parts.append(f"[WebFetch: {tool_input.get('url', '?')}]")
                    elif tool_name == "Grep":
                        text_parts.append(f"[Grep: {tool_input.get('pattern', '?')}]")
                    elif tool_name == "Glob":
                        text_parts.append(f"[Glob: {tool_input.get('pattern', '?')}]")
                    elif tool_name == "Agent":
                        text_parts.append(f"[Agent: {tool_input.get('description', '?')}]")
                    elif tool_name == "TodoWrite":
                        text_parts.append("[TodoWrite]")
                    else:
                        text_parts.append(f"[{tool_name}]")
                elif block.get("type") == "thinking":
                    thinking_text = block.get("thinking", "")
                    if thinking_text:
                        text_parts.append(f"(thinking) {thinking_text}")
                    elif not text_parts:
                        text_parts.append("(thinking...)")
            elif isinstance(block, str):
                text_parts.append(block)
        summary = " ".join(text_parts) or "(thinking...)"
        return {"event_type": "assistant", "summary": summary, "raw_json": line}

    elif msg_type == "tool_result":
        return {"event_type": "tool_result", "summary": "(tool result)", "raw_json": line}

    elif msg_type == "result":
        result_text = ""
        result_data = data.get("result", "")
        if isinstance(result_data, str):
            result_text = result_data[:200]
        elif isinstance(result_data, dict):
            result_text = json.dumps(result_data)[:200]
        return {"event_type": "result", "summary": result_text or "Agent finished", "raw_json": line}

    elif msg_type == "error":
        error_msg = data.get("error", {})
        if isinstance(error_msg, dict):
            error_msg = error_msg.get("message", str(error_msg))
        return {"event_type": "error", "summary": str(error_msg)[:200], "raw_json": line}

    elif msg_type == "system":
        return {"event_type": "system", "summary": data.get("subtype", "system"), "raw_json": line}

    elif msg_type == "rate_limit_event":
        return {"event_type": "rate_limit_event", "summary": "Rate limited - waiting...", "raw_json": line}

    else:
        return {"event_type": msg_type, "summary": json.dumps(data)[:200], "raw_json": line}
