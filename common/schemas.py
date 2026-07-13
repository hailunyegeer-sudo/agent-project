from __future__ import annotations

from typing import Any


VALID_ROLES = {"system", "user", "assistant", "tool"}


def make_ai_message(content: str = "", tool_calls: list[dict] | None = None) -> dict:
    message = {
        "role": "assistant",
        "content": content,
        "tool_calls": tool_calls or [],
    }
    validate_ai_message(message)
    return message


def make_tool_message(
    tool_call_id: str,
    name: str,
    content: str,
    status: str = "success",
) -> dict:
    if status not in {"success", "error"}:
        raise ValueError(f"invalid tool status: {status}")
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "name": name,
        "content": content,
        "status": status,
    }


def make_skill_result(
    skill_name: str,
    status: str,
    input_data: dict,
    output: dict | None = None,
    error: dict | None = None,
    latency_ms: float | None = None,
) -> dict:
    if status not in {"success", "error"}:
        raise ValueError(f"invalid skill status: {status}")
    return {
        "skill_name": skill_name,
        "status": status,
        "input": input_data,
        "output": output,
        "error": error,
        "latency_ms": latency_ms,
    }


def normalize_tool_call(tool_call: dict[str, Any], index: int = 0) -> dict:
    if not isinstance(tool_call, dict):
        raise ValueError("tool call must be an object")
    if "function" in tool_call:
        function = tool_call.get("function") or {}
        name = function.get("name")
        args = function.get("arguments", {})
    else:
        name = tool_call.get("name")
        args = tool_call.get("args", {})
    if isinstance(args, str):
        import json

        args = json.loads(args)
    if not isinstance(name, str) or not name:
        raise ValueError("tool call name must be a non-empty string")
    if not isinstance(args, dict):
        raise ValueError("tool call args must be an object")
    call_id = tool_call.get("id") or f"call_{index + 1:03d}"
    if not isinstance(call_id, str) or not call_id:
        raise ValueError("tool call id must be a non-empty string")
    return {"id": call_id, "name": name, "args": args}


def validate_ai_message(message: dict) -> None:
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ValueError("AIMessage role must be assistant")
    if not isinstance(message.get("content"), str):
        raise ValueError("AIMessage content must be a string")
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        raise ValueError("AIMessage tool_calls must be a list")
    normalized = [normalize_tool_call(call, index) for index, call in enumerate(tool_calls)]
    message["tool_calls"] = normalized
    if not message["content"] and not normalized:
        raise ValueError("AIMessage must contain content or tool_calls")


def validate_messages(messages: Any) -> list[dict]:
    if not isinstance(messages, list):
        raise ValueError("messages must be a top-level array")
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ValueError(f"message {index} must be an object")
        role = message.get("role")
        if role not in VALID_ROLES:
            raise ValueError(f"message {index} has invalid role: {role}")
        if not isinstance(message.get("content", ""), str):
            raise ValueError(f"message {index} content must be a string")
        if role == "assistant":
            message.setdefault("tool_calls", [])
            validate_ai_message(message)
        if role == "tool":
            for field in ("tool_call_id", "name", "status"):
                if field not in message:
                    raise ValueError(f"tool message {index} missing {field}")
    return messages

def validate_execution_plan(plan: dict) -> None:
    if not isinstance(plan, dict):
        raise ValueError("execution plan must be an object")

    expected_keys = {"use_plan", "steps"}
    missing_keys = expected_keys - set(plan)
    unknown_keys = set(plan) - expected_keys

    if missing_keys:
        raise ValueError(
            "execution plan missing keys: "
            + ", ".join(sorted(missing_keys))
        )

    if unknown_keys:
        raise ValueError(
            "execution plan contains unknown keys: "
            + ", ".join(sorted(unknown_keys))
        )

    if not isinstance(plan["use_plan"], bool):
        raise ValueError("execution plan use_plan must be boolean")

    steps = plan["steps"]
    if not isinstance(steps, list):
        raise ValueError("execution plan steps must be a list")

    if not plan["use_plan"]:
        if steps:
            raise ValueError(
                "execution plan with use_plan=false must contain no steps"
            )
        return

    if len(steps) < 2:
        raise ValueError(
            "execution plan with use_plan=true must contain at least two steps"
        )

    expected_step_keys = {"step_id", "goal", "suggested_tool"}

    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise ValueError(f"execution plan step {index} must be an object")

        missing_step_keys = expected_step_keys - set(step)
        unknown_step_keys = set(step) - expected_step_keys

        if missing_step_keys:
            raise ValueError(
                f"execution plan step {index} missing keys: "
                + ", ".join(sorted(missing_step_keys))
            )

        if unknown_step_keys:
            raise ValueError(
                f"execution plan step {index} contains unknown keys: "
                + ", ".join(sorted(unknown_step_keys))
            )

        if step["step_id"] != index:
            raise ValueError(
                "execution plan step_id must start from 1 and increase in order"
            )

        if not isinstance(step["goal"], str) or not step["goal"].strip():
            raise ValueError(
                f"execution plan step {index} goal must be a non-empty string"
            )

        suggested_tool = step["suggested_tool"]
        if suggested_tool is not None and (
            not isinstance(suggested_tool, str) or not suggested_tool.strip()
        ):
            raise ValueError(
                f"execution plan step {index} suggested_tool "
                "must be a non-empty string or null"
            )