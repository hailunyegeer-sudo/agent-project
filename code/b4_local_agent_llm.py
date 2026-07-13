from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common.io_utils import append_jsonl, read_json, read_yaml, write_json
from common.logging_utils import now_iso
from common.path_utils import resolve_cli_path, resolve_from_file
from common.schemas import (
    make_ai_message,
    validate_ai_message,
    validate_execution_plan,
    validate_messages,
)


PARSE_ERROR_CONTENT = "模型输出解析失败，无法生成有效工具调用或最终回答。"
PLAN_STEP_INSTRUCTION_MARKER = "[PLAN_STEP_INSTRUCTION]"
_MODEL_CACHE: dict[tuple[str, ...], tuple[Any, Any]] = {}
_KV_CACHE: "OrderedDict[tuple[str, ...], KVCacheEntry]" = OrderedDict()
_KV_CPU_CACHE: "OrderedDict[tuple[str, ...], KVCacheEntry]" = OrderedDict()
_LAST_KV_CACHE_RECORD: dict | None = None


@dataclass
class KVCacheEntry:
    namespace: str
    input_ids_prefix: tuple[int, ...]
    attention_mask_prefix: tuple[int, ...]
    past_key_values: Any
    token_count: int
    created_at: float
    last_used_at: float
    bytes_estimate: int = 0
    hit_count: int = 0
    residency: str = "gpu"
    source_roles: tuple[str, ...] = ()
    source_message_count: int = 0


def _load_model_config(model_config: str | Path) -> tuple[Path, dict]:
    path = Path(model_config).resolve()
    config = read_yaml(path)
    if not isinstance(config, dict):
        raise ValueError("model.yaml must contain an object")
    return path, config


def _artifact_paths(artifact_dir: str | Path, stem: str | None) -> tuple[Path, Path, Path]:
    directory = Path(artifact_dir)
    prefix = f"{stem}_" if stem else ""
    return (
        directory / f"{prefix}raw_model_output.json",
        directory / f"{prefix}ai_message.json",
        directory / "llm_run_log.jsonl",
    )


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except TypeError:
        return repr(value)


def _short_hash(value: Any) -> str:
    text = value if isinstance(value, str) else _canonical_json(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _kv_cache_config(config: dict) -> dict:
    raw = config.get("kv_cache", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("kv_cache must be an object when provided")
    return {
        "enabled": bool(raw.get("enabled", True)),
        "ttl_seconds": int(raw.get("ttl_seconds", 300)),
        "max_entries": int(raw.get("max_entries", 8)),
        "max_tokens_per_entry": int(raw.get("max_tokens_per_entry", 8192)),
        "max_total_gpu_mb": int(raw.get("max_total_gpu_mb", 2048)),
        "cpu_enabled": bool(raw.get("cpu_enabled", True)),
        "cpu_max_entries": int(raw.get("cpu_max_entries", 32)),
        "cpu_max_total_mb": int(raw.get("cpu_max_total_mb", 8192)),
    }


def _set_last_kv_cache_record(record: dict | None) -> None:
    global _LAST_KV_CACHE_RECORD
    _LAST_KV_CACHE_RECORD = record


def _consume_last_kv_cache_record() -> dict | None:
    global _LAST_KV_CACHE_RECORD
    record = _LAST_KV_CACHE_RECORD
    _LAST_KV_CACHE_RECORD = None
    return record


def _tensor_to_token_tuple(value: Any) -> tuple[int, ...]:
    if hasattr(value, "detach"):
        value = value.detach().cpu().reshape(-1).tolist()
    elif hasattr(value, "reshape") and hasattr(value, "tolist"):
        value = value.reshape(-1).tolist()
    elif isinstance(value, tuple):
        value = list(value)
    if isinstance(value, list) and value and isinstance(value[0], list):
        value = value[0]
    return tuple(int(item) for item in value)


def _token_ids_are_prefix(cached_ids: tuple[int, ...], current_ids: tuple[int, ...]) -> bool:
    return len(cached_ids) <= len(current_ids) and current_ids[: len(cached_ids)] == cached_ids


def _message_roles(messages: list[dict]) -> tuple[str, ...]:
    return tuple(str(message.get("role", "")) for message in messages)


def _classify_kv_hit(entry: KVCacheEntry | None, source_messages: list[dict] | None) -> str | None:
    if entry is None or source_messages is None:
        return None
    current_roles = _message_roles(source_messages)
    if not entry.source_roles or current_roles[: len(entry.source_roles)] != entry.source_roles:
        return "unknown"
    appended_roles = current_roles[len(entry.source_roles) :]
    if not appended_roles:
        return "exact"
    if "tool" in appended_roles:
        return "turn_internal"
    if "user" in appended_roles:
        return "conversation"
    return "append"


def _kv_namespace(stage: str, schema_injection: str) -> str:
    suffix = "native_tools" if schema_injection == "native" else "prompt_schema"
    return f"{stage}.{suffix}" if stage.startswith("ai_message") else stage


def _make_kv_cache_key(
    *,
    model_identity: tuple[str, ...],
    tokenizer: Any,
    backend: str,
    namespace: str,
    schema_injection: str,
    tools_schema: list[dict],
    prompt_messages: list[dict],
    template_kwargs: dict,
    conversation_id: str | None = None,
) -> tuple[str, ...]:
    system_content = ""
    if prompt_messages and prompt_messages[0].get("role") == "system":
        system_content = str(prompt_messages[0].get("content", ""))
    tokenizer_template = getattr(tokenizer, "chat_template", None)
    tokenizer_identity = (
        type(tokenizer).__name__,
        _short_hash(tokenizer_template or ""),
    )
    return (
        "kv_cache_v1",
        *model_identity,
        *tokenizer_identity,
        backend,
        conversation_id or "global",
        namespace,
        schema_injection,
        _short_hash(tools_schema),
        _short_hash(system_content),
        _short_hash(template_kwargs),
    )


def _kv_cache_record(
    *,
    enabled: bool,
    event: str,
    namespace: str,
    cache_key: tuple[str, ...] | None,
    reason: str | None,
    cached_token_count: int,
    current_token_count: int,
    reused_token_count: int,
    new_prefill_token_count: int,
    entry_token_count_after: int,
    bytes_estimate: int,
    residency: str | None = None,
    cache_tier: str | None = None,
    hit_scope: str | None = None,
) -> dict:
    return {
        "enabled": enabled,
        "event": event,
        "namespace": namespace,
        "cache_key_hash": _short_hash(cache_key) if cache_key is not None else None,
        "reason": reason,
        "cached_token_count": cached_token_count,
        "current_token_count": current_token_count,
        "reused_token_count": reused_token_count,
        "new_prefill_token_count": new_prefill_token_count,
        "entry_token_count_after": entry_token_count_after,
        "bytes_estimate": bytes_estimate,
        "residency": residency,
        "cache_tier": cache_tier,
        "hit_scope": hit_scope,
    }


def _estimate_past_key_values_bytes(past_key_values: Any) -> int:
    total = 0

    def visit(value: Any) -> None:
        nonlocal total
        if hasattr(value, "numel") and hasattr(value, "element_size"):
            total += int(value.numel()) * int(value.element_size())
            return
        if isinstance(value, dict):
            for child in value.values():
                visit(child)
            return
        if isinstance(value, (list, tuple)):
            for child in value:
                visit(child)

    if hasattr(past_key_values, "key_cache"):
        visit(getattr(past_key_values, "key_cache"))
    if hasattr(past_key_values, "value_cache"):
        visit(getattr(past_key_values, "value_cache"))
    if total == 0:
        visit(past_key_values)
    return total


def _move_value_to_device(value: Any, device: Any) -> Any:
    if hasattr(value, "to"):
        try:
            return value.to(device)
        except (TypeError, RuntimeError, AttributeError):
            pass
    if isinstance(value, tuple):
        return tuple(_move_value_to_device(item, device) for item in value)
    if isinstance(value, list):
        return [_move_value_to_device(item, device) for item in value]
    if isinstance(value, dict):
        return {key: _move_value_to_device(item, device) for key, item in value.items()}
    return value


def _move_entry_to_device(entry: KVCacheEntry, device: Any, residency: str) -> KVCacheEntry:
    entry.past_key_values = _move_value_to_device(entry.past_key_values, device)
    entry.residency = residency
    return entry


def _kv_cache_total_bytes() -> int:
    return sum(entry.bytes_estimate for entry in _KV_CACHE.values())


def _kv_cpu_cache_total_bytes() -> int:
    return sum(entry.bytes_estimate for entry in _KV_CPU_CACHE.values())


def _store_cpu_kv_entry(key: tuple[str, ...], entry: KVCacheEntry, kv_config: dict) -> None:
    if not kv_config.get("cpu_enabled", True):
        return
    _KV_CPU_CACHE[key] = _move_entry_to_device(entry, "cpu", "cpu")
    _KV_CPU_CACHE.move_to_end(key)
    max_entries = max(0, int(kv_config.get("cpu_max_entries", 32)))
    while max_entries and len(_KV_CPU_CACHE) > max_entries:
        _KV_CPU_CACHE.popitem(last=False)
    max_total_bytes = max(0, int(kv_config.get("cpu_max_total_mb", 8192))) * 1024 * 1024
    while max_total_bytes and _kv_cpu_cache_total_bytes() > max_total_bytes and _KV_CPU_CACHE:
        _KV_CPU_CACHE.popitem(last=False)


def _evict_kv_cache(now: float, kv_config: dict) -> None:
    ttl_seconds = max(0, int(kv_config["ttl_seconds"]))
    expired = [
        key
        for key, entry in _KV_CACHE.items()
        if ttl_seconds and now - entry.last_used_at > ttl_seconds
    ]
    for key in expired:
        _KV_CACHE.pop(key, None)

    cpu_expired = [
        key
        for key, entry in _KV_CPU_CACHE.items()
        if ttl_seconds and now - entry.last_used_at > ttl_seconds
    ]
    for key in cpu_expired:
        _KV_CPU_CACHE.pop(key, None)

    max_entries = max(0, int(kv_config["max_entries"]))
    while max_entries and len(_KV_CACHE) > max_entries:
        key, entry = _KV_CACHE.popitem(last=False)
        _store_cpu_kv_entry(key, entry, kv_config)

    max_total_bytes = max(0, int(kv_config["max_total_gpu_mb"])) * 1024 * 1024
    while max_total_bytes and _kv_cache_total_bytes() > max_total_bytes and _KV_CACHE:
        key, entry = _KV_CACHE.popitem(last=False)
        _store_cpu_kv_entry(key, entry, kv_config)


def _clear_kv_cache() -> None:
    _KV_CACHE.clear()
    _KV_CPU_CACHE.clear()


def _crop_past_key_values(past_key_values: Any, token_count: int) -> Any:
    if hasattr(past_key_values, "crop"):
        past_key_values.crop(token_count)
    return past_key_values


def _chat_context_token_count(tokenizer: Any, prompt_messages: list[dict], template_kwargs: dict) -> int:
    context_inputs = tokenizer.apply_chat_template(
        prompt_messages,
        tokenize=True,
        add_generation_prompt=False,
        return_tensors="pt",
        return_dict=True,
        enable_thinking=False,
        **template_kwargs,
    )
    return int(context_inputs["input_ids"].shape[-1])


def _has_plan_step_instruction_suffix(messages: list[dict]) -> bool:
    if not messages:
        return False
    last = messages[-1]
    content = last.get("content")
    return (
        last.get("role") == "user"
        and isinstance(content, str)
        and content.startswith(PLAN_STEP_INSTRUCTION_MARKER)
    )


def _extract_tool_result(message: dict) -> dict:
    try:
        result = json.loads(message["content"])
    except (KeyError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError("ToolMessage content is not a SkillResult JSON string") from exc
    if not isinstance(result, dict):
        raise ValueError("ToolMessage content must decode to an object")
    return result


def _three_points(text: str) -> list[str]:
    parts = [part.strip(" \t\r\n。") for part in re.split(r"\n+|(?<=[。！？!?])", text) if part.strip()]
    points = []
    for part in parts:
        if part not in points:
            points.append(part)
        if len(points) == 3:
            break
    while len(points) < 3:
        points.append("工具结果未提供更多可提取内容")
    return points


def _mock_generate(messages: list[dict]) -> dict:
    tool_messages = [message for message in messages if message.get("role") == "tool"]
    if not tool_messages:
        return make_ai_message(
            "",
            [
                {
                    "id": "call_001",
                    "name": "file_reader",
                    "args": {"path": "docs/agent_intro.txt", "max_chars": 2000},
                }
            ],
        )
    latest = tool_messages[-1]
    result = _extract_tool_result(latest)
    if latest.get("status") != "success" or result.get("status") != "success":
        error = result.get("error") or {}
        detail = error.get("message", "未知工具错误") if isinstance(error, dict) else str(error)
        return make_ai_message(f"工具调用失败，无法完成请求：{detail}", [])
    output = result.get("output") or {}
    content = output.get("content") if isinstance(output, dict) else None
    if not isinstance(content, str) or not content.strip():
        content = json.dumps(output, ensure_ascii=False)
    points = _three_points(content)
    answer = "三条中文要点如下：\n" + "\n".join(f"{index}. {point}" for index, point in enumerate(points, 1))
    return make_ai_message(answer, [])


def _parse_tool_calls_fragment(raw_text: str, original_error: json.JSONDecodeError) -> dict:
    markers = ['"tool_calls":[', '\\"tool_calls\\":[']
    marker_index = -1
    marker = ""
    for item in markers:
        marker_index = raw_text.find(item)
        if marker_index != -1:
            marker = item
            break
    if marker_index == -1:
        raise original_error
    array_start = marker_index + marker.index("[")
    array_end = raw_text.rfind("]")
    if array_end < array_start:
        raise ValueError("model output contains tool_calls marker but no closing array")
    array_text = raw_text[array_start : array_end + 1]
    try:
        tool_calls = json.loads(array_text)
    except json.JSONDecodeError:
        tool_calls = json.loads(array_text.replace('\\"', '"'))
    if not isinstance(tool_calls, list) or not tool_calls:
        raise original_error
    return {"content": "", "tool_calls": tool_calls}


def _extract_first_json_object(raw_text: str, *, kind: str) -> dict:
    """
    提取第一个 JSON 对象；仅容错补齐模型遗漏的尾部 ] 或 }。
    """
    text = raw_text.lstrip("\ufeff \t\r\n")
    decoder = json.JSONDecoder()
    closing = {"{": "}", "[": "]"}

    for index, char in enumerate(text):
        if char != "{":
            continue
        fragment = text[index:].rstrip()
        try:
            candidate, _ = decoder.raw_decode(fragment)
        except json.JSONDecodeError:
            stack = []
            in_string = escaped = False
            valid = True
            for token in fragment:
                if in_string:
                    if escaped:
                        escaped = False
                    elif token == "\\":
                        escaped = True
                    elif token == '"':
                        in_string = False
                elif token == '"':
                    in_string = True
                elif token in closing:
                    stack.append(token)
                elif token in closing.values():
                    if not stack or closing[stack.pop()] != token:
                        valid = False
                        break
            if not valid or in_string or not stack:
                continue
            try:
                candidate, _ = decoder.raw_decode(
                    fragment + "".join(closing[token] for token in reversed(stack))
                )
            except json.JSONDecodeError:
                continue
        if isinstance(candidate, dict):
            return candidate

    raise ValueError(f"{kind} output contains no valid JSON object")


def _parse_execution_plan(raw_text: str) -> dict:
    """把模型输出解析为 execution_plan JSON 对象，并验证其结构。同时可以对单步计划做降级处理，避免模型误判为复杂任务。"""
    plan = _extract_first_json_object(raw_text, kind="execution plan")
    steps = plan.get("steps")
    if plan.get("use_plan") is True and isinstance(steps, list) and len(steps) == 1:
        return {
            "use_plan": False,
            "steps": [],
        }
    #如果选择采用计划但是计划只有一步，那就采用direct方法
    validate_execution_plan(plan)
    return plan


def _candidate_to_message(candidate: dict) -> tuple[dict, dict]:
    if not isinstance(candidate, dict):
        raise ValueError("model output JSON must be an object")
    expected_keys = {"content", "tool_calls"}
    unknown_keys = set(candidate) - expected_keys #返回不知名的键
    if unknown_keys:
        raise ValueError(f"model output JSON contains unknown keys: {', '.join(sorted(unknown_keys))}")
    message = {
        "role": "assistant",
        "content": candidate.get("content", ""),
        "tool_calls": candidate.get("tool_calls", []),
    }
    validate_ai_message(message)
    has_content = bool(message["content"].strip())
    has_tool_calls = bool(message["tool_calls"])
    if has_content == has_tool_calls:
        raise ValueError("model output must contain either final content or tool calls, but not both")#不允许同时返回工具调用和content
    parsed_candidate = {"content": message["content"], "tool_calls": message["tool_calls"]}
    return parsed_candidate, message


def _parse_native_tool_calls(
    raw_text: str,
    original_error: json.JSONDecodeError,
) -> dict:
    """
    解析 Qwen 原生 tools 输出，例如：

    <tool_call>
    <function=file_reader>
    <parameter=path>
    docs/agent_intro.txt
    </parameter>
    </function>
    </tool_call>
    """
    tool_pattern = re.compile(
        r"<tool_call>\s*"
        r"<function=(?P<name>[^>\s]+)>\s*"
        r"(?P<body>.*?)"
        r"</function>\s*"
        r"</tool_call>",
        flags=re.DOTALL,
    )

    parameter_pattern = re.compile(
        r"<parameter=(?P<key>[^>\s]+)>\s*"
        r"(?P<value>.*?)"
        r"</parameter>",
        flags=re.DOTALL,
    )

    matches = list(tool_pattern.finditer(raw_text))
    if not matches:
        raise original_error

    tool_calls = []

    for index, match in enumerate(matches, start=1):
        args = {}

        for parameter in parameter_pattern.finditer(match.group("body")):
            key = parameter.group("key").strip()
            raw_value = parameter.group("value").strip()

            # 数字、布尔值、JSON 数组等尽量恢复原类型；
            # 普通文本、路径、表达式则保留为字符串。
            try:
                args[key] = json.loads(raw_value)
            except json.JSONDecodeError:
                args[key] = raw_value

        tool_calls.append(
            {
                "id": f"call_{index:03d}",
                "name": match.group("name").strip(),
                "args": args,
            }
        )

    return {
        "content": "",
        "tool_calls": tool_calls,
    }


def _parse_model_output(raw_text: str) -> tuple[dict, dict]:
    """Parse an AIMessage while tolerating accidental duplicated JSON output."""
    try:
        candidate = json.loads(raw_text.strip())
    except json.JSONDecodeError as exc:
        try:
            candidate = _extract_first_json_object(raw_text, kind="AIMessage")
        except ValueError:
            try:
                candidate = _parse_tool_calls_fragment(raw_text, exc)
            except (json.JSONDecodeError, ValueError):
                candidate = _parse_native_tool_calls(raw_text, exc)#先尝试解析json，如果失败再尝试解析原生工具调用格式

    return _candidate_to_message(candidate)


def _parse_native_model_output(raw_text: str) -> tuple[dict, dict]:
    """Normalize native XML tool calls or a plain-text final answer."""
    text = raw_text.strip()
    if not text:
        raise ValueError("native model output is empty")

    if "<tool_call" in text:
        missing_native_call = json.JSONDecodeError(
            "model output contains no valid native tool call",
            text,
            0,
        )
        candidate = _parse_native_tool_calls(text, missing_native_call)
        return _candidate_to_message(candidate)

    # Accept a JSON envelope for compatibility, but native mode does not ask
    # the model to produce one. Ordinary text is the final assistant answer.
    try:
        return _parse_model_output(text)
    except (ValueError, json.JSONDecodeError):
        return _candidate_to_message({"content": text, "tool_calls": []})


def _dtype_value(torch_module: Any, configured: str) -> Any:
    if configured == "auto":
        return "auto"
    mapping = {
        "bfloat16": torch_module.bfloat16,
        "float16": torch_module.float16,
        "float32": torch_module.float32,
    }
    if configured not in mapping:
        raise ValueError(f"unsupported torch_dtype: {configured}")
    return mapping[configured]


def _model_cache_key(
    model_path: Path,
    tokenizer_path: Path,
    local_only: bool,
    trust_remote_code: bool,
    dtype: Any,
    device_map: Any,
    max_memory: Any,
) -> tuple[str, ...]:
    try:
        device_map_key = json.dumps(device_map, sort_keys=True, separators=(",", ":"))
    except TypeError:
        device_map_key = repr(device_map)
    try:
        max_memory_key = json.dumps(max_memory, sort_keys=True, separators=(",", ":"))
    except TypeError:
        max_memory_key = repr(max_memory)
    return (
        str(model_path),
        str(tokenizer_path),
        str(local_only),
        str(trust_remote_code),
        str(dtype),
        device_map_key,
        max_memory_key,
    )


def _load_model_bundle(
    auto_model: Any,
    auto_tokenizer: Any,
    model_path: Path,
    tokenizer_path: Path,
    local_only: bool,
    trust_remote_code: bool,
    dtype: Any,
    device_map: Any,
    max_memory: Any,
) -> tuple[Any, Any]:
    cache_key = _model_cache_key(
        model_path,
        tokenizer_path,
        local_only,
        trust_remote_code,
        dtype,
        device_map,
        max_memory,
    )
    cached = _MODEL_CACHE.get(cache_key)
    if cached is not None:
        print("model_cache=hit", file=sys.stderr, flush=True)
        return cached

    print("model_cache=miss", file=sys.stderr, flush=True)
    tokenizer = auto_tokenizer.from_pretrained(
        str(tokenizer_path),
        local_files_only=local_only,
        trust_remote_code=trust_remote_code,
    )
    model = auto_model.from_pretrained(
        str(model_path),
        local_files_only=local_only,
        trust_remote_code=trust_remote_code,
        dtype=dtype,
        device_map=device_map,
        max_memory=max_memory,
    )
    _MODEL_CACHE[cache_key] = (tokenizer, model)
    return tokenizer, model


def _standard_generate_text(
    *,
    torch_module: Any,
    tokenizer: Any,
    model: Any,
    inputs: Any,
    options: dict,
) -> str:
    input_length = inputs["input_ids"].shape[-1]
    with torch_module.no_grad():
        generated = model.generate(**inputs, **options)
    new_tokens = generated[0][input_length:]
    return tokenizer.decode(new_tokens, skip_special_tokens=True)


def _prefill_prompt_prefix(
    *,
    model: Any,
    input_ids: Any,
    attention_mask: Any,
    past_key_values: Any | None,
    start: int,
    end: int,
) -> Any:
    if end <= start:
        return past_key_values

    model_inputs = {
        "input_ids": input_ids[:, start:end],
        "attention_mask": attention_mask[:, :end],
        "use_cache": True,
    }
    if past_key_values is not None:
        model_inputs["past_key_values"] = past_key_values

    outputs = model(**model_inputs)
    return outputs.past_key_values


def _generate_text_with_kv_cache(
    *,
    torch_module: Any,
    tokenizer: Any,
    model: Any,
    inputs: Any,
    options: dict,
    kv_config: dict,
    cache_key: tuple[str, ...],
    namespace: str,
    cacheable_token_count: int | None = None,
    source_messages: list[dict] | None = None,
) -> tuple[str, dict]:
    input_ids = inputs["input_ids"]
    attention_mask = inputs.get("attention_mask")
    if attention_mask is None:
        attention_mask = torch_module.ones_like(input_ids)
        inputs["attention_mask"] = attention_mask
    current_token_count = int(input_ids.shape[-1])
    current_ids_tuple = _tensor_to_token_tuple(input_ids)
    current_mask_tuple = (
        _tensor_to_token_tuple(attention_mask)
        if attention_mask is not None
        else tuple(1 for _ in current_ids_tuple)
    )

    if not kv_config["enabled"]:
        text = _standard_generate_text(
            torch_module=torch_module,
            tokenizer=tokenizer,
            model=model,
            inputs=inputs,
            options=options,
        )
        record = _kv_cache_record(
            enabled=False,
            event="skipped",
            namespace=namespace,
            cache_key=cache_key,
            reason="disabled",
            cached_token_count=0,
            current_token_count=current_token_count,
            reused_token_count=0,
            new_prefill_token_count=current_token_count,
            entry_token_count_after=0,
            bytes_estimate=0,
        )
        return text, record

    if cacheable_token_count is None:
        cacheable_token_count = current_token_count - 1
    cacheable_token_count = max(0, min(int(cacheable_token_count), current_token_count - 1))

    if current_token_count <= 1 or cacheable_token_count <= 0:
        text = _standard_generate_text(
            torch_module=torch_module,
            tokenizer=tokenizer,
            model=model,
            inputs=inputs,
            options=options,
        )
        record = _kv_cache_record(
            enabled=True,
            event="skipped",
            namespace=namespace,
            cache_key=cache_key,
            reason="too_short",
            cached_token_count=0,
            current_token_count=current_token_count,
            reused_token_count=0,
            new_prefill_token_count=current_token_count,
            entry_token_count_after=0,
            bytes_estimate=0,
        )
        return text, record

    max_tokens = int(kv_config["max_tokens_per_entry"])
    if max_tokens and current_token_count > max_tokens:
        text = _standard_generate_text(
            torch_module=torch_module,
            tokenizer=tokenizer,
            model=model,
            inputs=inputs,
            options=options,
        )
        record = _kv_cache_record(
            enabled=True,
            event="skipped",
            namespace=namespace,
            cache_key=cache_key,
            reason="max_tokens_per_entry",
            cached_token_count=0,
            current_token_count=current_token_count,
            reused_token_count=0,
            new_prefill_token_count=current_token_count,
            entry_token_count_after=0,
            bytes_estimate=0,
        )
        return text, record

    now = time.time()
    _evict_kv_cache(now, kv_config)
    entry = _KV_CACHE.get(cache_key)
    cache_tier = "gpu" if entry is not None else None
    if entry is None:
        entry = _KV_CPU_CACHE.pop(cache_key, None)
        if entry is not None:
            try:
                device = next(model.parameters()).device
            except StopIteration:
                device = input_ids.device
            entry = _move_entry_to_device(entry, device, "gpu")
            _KV_CACHE[cache_key] = entry
            _KV_CACHE.move_to_end(cache_key)
            cache_tier = "cpu"
    cached_token_count = entry.token_count if entry is not None else 0
    event = "miss"
    reason = "no_entry"
    reused_token_count = 0
    past_key_values = None
    hit_scope = None

    if entry is not None:
        if _token_ids_are_prefix(entry.input_ids_prefix, current_ids_tuple):
            event = "hit"
            reason = None
            reused_token_count = entry.token_count
            hit_scope = _classify_kv_hit(entry, source_messages)
            entry.hit_count += 1
            entry.last_used_at = now
            _KV_CACHE.move_to_end(cache_key)
            past_key_values = entry.past_key_values
        else:
            event = "invalidated"
            reason = "prefix_mismatch"
            _KV_CACHE.pop(cache_key, None)

    try:
        with torch_module.no_grad():
            if event == "hit":
                past_key_values = _prefill_prompt_prefix(
                    model=model,
                    input_ids=input_ids,
                    attention_mask=attention_mask,
                    past_key_values=past_key_values,
                    start=reused_token_count,
                    end=cacheable_token_count,
                )
            else:
                past_key_values = _prefill_prompt_prefix(
                    model=model,
                    input_ids=input_ids,
                    attention_mask=attention_mask,
                    past_key_values=None,
                    start=0,
                    end=cacheable_token_count,
                )

            generate_inputs = {
                "input_ids": input_ids[:, cacheable_token_count:current_token_count],
                "attention_mask": attention_mask[:, :current_token_count],
                "past_key_values": past_key_values,
            }
            generated = model.generate(**generate_inputs, **options)

        past_key_values = _crop_past_key_values(past_key_values, cacheable_token_count)
        bytes_estimate = _estimate_past_key_values_bytes(past_key_values)
        _KV_CACHE[cache_key] = KVCacheEntry(
            namespace=namespace,
            input_ids_prefix=current_ids_tuple[:cacheable_token_count],
            attention_mask_prefix=current_mask_tuple[:cacheable_token_count],
            past_key_values=past_key_values,
            token_count=cacheable_token_count,
            created_at=entry.created_at if event == "hit" and entry is not None else now,
            last_used_at=now,
            bytes_estimate=bytes_estimate,
            hit_count=entry.hit_count if event == "hit" and entry is not None else 0,
            residency="gpu",
            source_roles=_message_roles(source_messages or []),
            source_message_count=len(source_messages or []),
        )
        _KV_CACHE.move_to_end(cache_key)
        _evict_kv_cache(time.time(), kv_config)

        driver_len = current_token_count - cacheable_token_count
        new_tokens = generated[0][driver_len:]
        text = tokenizer.decode(new_tokens, skip_special_tokens=True)
        new_prefill_token_count = max(0, cacheable_token_count - reused_token_count)
        record = _kv_cache_record(
            enabled=True,
            event=event,
            namespace=namespace,
            cache_key=cache_key,
            reason=reason,
            cached_token_count=cached_token_count,
            current_token_count=current_token_count,
            reused_token_count=reused_token_count,
            new_prefill_token_count=new_prefill_token_count,
            entry_token_count_after=cacheable_token_count,
            bytes_estimate=bytes_estimate,
            residency="gpu",
            cache_tier=cache_tier,
            hit_scope=hit_scope,
        )
        scope_text = f" scope={hit_scope}" if hit_scope else ""
        tier_text = f" tier={cache_tier}" if cache_tier else ""
        print(
            "kv_cache="
            f"{event}{scope_text}{tier_text} ns={namespace} "
            f"reused={reused_token_count} new={new_prefill_token_count}",
            file=sys.stderr,
            flush=True,
        )
        return text, record

    except Exception as exc:
        _KV_CACHE.pop(cache_key, None)
        text = _standard_generate_text(
            torch_module=torch_module,
            tokenizer=tokenizer,
            model=model,
            inputs=inputs,
            options=options,
        )
        record = _kv_cache_record(
            enabled=True,
            event="invalidated",
            namespace=namespace,
            cache_key=cache_key,
            reason=f"backend_error:{type(exc).__name__}",
            cached_token_count=cached_token_count,
            current_token_count=current_token_count,
            reused_token_count=0,
            new_prefill_token_count=current_token_count,
            entry_token_count_after=0,
            bytes_estimate=0,
        )
        print(
            f"kv_cache=invalidated namespace={namespace} reason={type(exc).__name__}",
            file=sys.stderr,
            flush=True,
        )
        return text, record


def _build_prompt_messages(
    messages: list[dict],
    tools_schema: list[dict],
    include_tools_schema: bool = True,
) -> list[dict]:
    prompt_messages = deepcopy(messages)
    if not include_tools_schema:
        return prompt_messages

    format_instruction = (
        "IMPORTANT OUTPUT FORMAT:\n"
        "You must return exactly one valid JSON object.\n"
        "Do not output markdown.\n"
        "Do not output explanations.\n"
        "Do not output code fences or backticks.\n"
        'The first output character must be "{" and the last output character must be "}".\n\n'
        "Valid schema A:\n"
        '{"content":"final answer text","tool_calls":[]}\n\n'
        "Valid schema B:\n"
        '{"content":"","tool_calls":[{"id":"call_001","name":"file_reader",'
        '"args":{"path":"docs/agent_intro.txt","max_chars":2000}}]}\n\n'
        "The top-level keys must be exactly:\n"
        "- content: string\n"
        "- tool_calls: array\n\n"
        "Never put tool_calls inside content.\n"
        'Never output {"content":"tool_calls": ...}.'
    )
    system_instruction = (
        "\n\nAvailable tools JSON schema:\n"
        + json.dumps(tools_schema, ensure_ascii=False)
        + "\n"
        + format_instruction
    )

    if prompt_messages and prompt_messages[0].get("role") == "system":
        prompt_messages[0]["content"] += system_instruction
    else:
        prompt_messages.insert(0, {"role": "system", "content": system_instruction.strip()})

    return prompt_messages


def _callable_tools_schema(tools_schema: list[dict]) -> list[dict]:
    """Remove B3 output metadata unsupported by native tokenizer tool templates."""
    callable_schema = deepcopy(tools_schema)
    for tool in callable_schema:
        function = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(function, dict):
            function.pop("x-returns", None)
    return callable_schema


def _prompt_json_generate(
    config_path: Path,
    config: dict,
    messages: list[dict],
    tools_schema: list[dict],
    conversation_id: str | None = None,
) -> str:
    _set_last_kv_cache_record(None)
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        raise RuntimeError("prompt_json mode requires requirements-llm.txt") from exc
    model_config = config.get("model", {})
    generation_config = config.get("generation", {})
    model_setting = model_config.get("model_name_or_path")
    tokenizer_setting = model_config.get("tokenizer_name_or_path", model_setting)
    if not isinstance(model_setting, str) or not isinstance(tokenizer_setting, str):
        raise ValueError("model_name_or_path and tokenizer_name_or_path are required")
    model_path = resolve_from_file(model_setting, config_path)
    tokenizer_path = resolve_from_file(tokenizer_setting, config_path)
    if not model_path.exists() or not tokenizer_path.exists():
        raise FileNotFoundError(f"local model path does not exist: {model_path}")
    local_only = bool(model_config.get("local_files_only", True))
    trust_remote_code = bool(model_config.get("trust_remote_code", False))
    dtype = _dtype_value(torch, str(model_config.get("torch_dtype", "auto")))
    tokenizer, model = _load_model_bundle(
        AutoModelForCausalLM,
        AutoTokenizer,
        model_path,
        tokenizer_path,
        local_only,
        trust_remote_code,
        dtype,
        model_config.get("device_map", "auto"),
        model_config.get("max_memory"),
    )
    model_identity = _model_cache_key(
        model_path,
        tokenizer_path,
        local_only,
        trust_remote_code,
        dtype,
        model_config.get("device_map", "auto"),
        model_config.get("max_memory"),
    )
    backend = str(model_config.get("backend", "transformers"))
    tool_calling_config = config.get("tool_calling", {})
    schema_injection = tool_calling_config.get("schema_injection", "prompt")

    if schema_injection not in {"prompt", "native"}:
        raise ValueError(
            "tool_calling.schema_injection must be prompt or native"
        )

    template_kwargs = {}
    is_plan_step_suffix = _has_plan_step_instruction_suffix(messages)
    cache_base_prompt_messages = None
    cache_source_messages = messages[:-1] if is_plan_step_suffix else messages

    if schema_injection == "prompt":
        # 原有方案：schema 转成文字后拼接到 system prompt。
        prompt_messages = _build_prompt_messages(
            messages,
            tools_schema,
            include_tools_schema=True,
        )
        cache_base_prompt_messages = _build_prompt_messages(
            messages[:-1] if is_plan_step_suffix else messages,
            tools_schema,
            include_tools_schema=True,
        )

    else:
        # 新方案：schema 不写入 prompt，通过 tokenizer 的 tools 参数注入。
        callable_tools_schema = _callable_tools_schema(tools_schema)
        prompt_messages = _build_prompt_messages(
            messages,
            callable_tools_schema,
            include_tools_schema=False,
        )
        if is_plan_step_suffix:
            cache_base_prompt_messages = _build_prompt_messages(
                messages[:-1],
                callable_tools_schema,
                include_tools_schema=False,
            )

        template_kwargs["tools"] = callable_tools_schema

    inputs = tokenizer.apply_chat_template(
        prompt_messages,
        tokenize=True,
        add_generation_prompt=True,
        return_tensors="pt",
        return_dict=True,
        enable_thinking=False,
        **template_kwargs,
    )
    cacheable_token_count = _chat_context_token_count(
        tokenizer,
        cache_base_prompt_messages or prompt_messages,
        template_kwargs,
    )
    device = next(model.parameters()).device
    inputs = inputs.to(device)
    options = {
        "max_new_tokens": int(generation_config.get("max_new_tokens", 1024)),
        "do_sample": bool(generation_config.get("do_sample", False)),
    }
    stage = (
        "ai_message.plan_step"
        if is_plan_step_suffix
        else "ai_message.normal"
    )
    namespace = _kv_namespace(stage, schema_injection)
    cache_key = _make_kv_cache_key(
        model_identity=model_identity,
        tokenizer=tokenizer,
        backend=backend,
        namespace=namespace,
        schema_injection=schema_injection,
        tools_schema=callable_tools_schema if schema_injection == "native" else tools_schema,
        prompt_messages=prompt_messages,
        template_kwargs=template_kwargs,
        conversation_id=conversation_id,
    )
    raw_text, kv_record = _generate_text_with_kv_cache(
        torch_module=torch,
        tokenizer=tokenizer,
        model=model,
        inputs=inputs,
        options=options,
        kv_config=_kv_cache_config(config),
        cache_key=cache_key,
        namespace=namespace,
        cacheable_token_count=cacheable_token_count,
        source_messages=cache_source_messages,
    )
    _set_last_kv_cache_record(kv_record)
    return raw_text


def _plan_tool_names(tools_schema: list[dict]) -> list[str]:
    """Return the operational tool names that a plan may reference."""
    names: list[str] = []
    for tool in tools_schema:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if isinstance(name, str) and name and name not in names:
            names.append(name)

    if not names:
        raise ValueError("tools_schema does not contain any function names")

    return names


def _build_plan_prompt_messages(
    messages: list[dict],
    tools_schema: list[dict],
) -> list[dict]:
    """
    为复杂任务生成轻量执行计划。
    这里只决定是否需要计划及计划步骤；
    不生成 tool_calls，不执行工具。
    """
    prompt_messages = deepcopy(messages)
    tool_names = _plan_tool_names(tools_schema)

    plan_instruction = (
        "\n\nPLAN DECISION MODE\n"
        "This is a decision-only turn, not a tool-execution turn.\n"
        "No operational tool may be called in this turn.\n"
        "Return exactly one JSON object and nothing else.\n"
        'The first non-whitespace output character must be "{" and the last must be "}".\n'
        "Do not prefix the JSON with DIRECT:, PLAN:, JSON:, Markdown, or an explanation.\n"
        "Do not emit native function-call markup of any kind.\n"
        'For a direct decision, return exactly: {"use_plan":false,"steps":[]}.\n'
        'For a planned decision, return: {"use_plan":true,"steps":[...]}.\n\n'
        "Use DIRECT for every task that needs zero or one tool call before answering. "
        "This includes one calculator call, one file_reader call, one table_analyzer call, "
        "or one format_converter call.\n"
        "Use PLAN only when the task requires at least two dependent tool calls before "
        "the final answer, for example local_file_search -> file_reader -> answer.\n"
        "A final natural-language answer does not count as a tool step.\n"
        "If a previous local_file_search ToolMessage has results=[], do not plan file_reader.\n"
        "Do not repeat the same search scope after an empty result.\n"
        "Never return use_plan=true with only one step.\n\n"
        f"Available operational tool names for suggested_tool only:\n{', '.join(tool_names)}\n\n"
        "The names above are references for suggested_tool only; they are not callable in this decision turn.\n"
        "For PLAN mode, each step must contain "
        '"step_id", "goal", and "suggested_tool".\n'
        "step_id starts at 1 and increases by 1. "
        "suggested_tool must be an available tool name or null. "
        "If a plan contains file_reader, its path must come from either an explicit user-provided "
        "path or a previous successful local_file_search ToolMessage. "
        "Never guess or invent a file path in a plan. "
        "The final answer step uses null. Do not include tool arguments.\n\n"
        "Example direct output:\n"
        '{"use_plan":false,"steps":[]}\n\n'
        "Example plan output:\n"
        '{"use_plan":true,"steps":['
        '{"step_id":1,"goal":"search related documents","suggested_tool":"local_file_search"},'
        '{"step_id":2,"goal":"read the most relevant document","suggested_tool":"file_reader"},'
        '{"step_id":3,"goal":"answer based on tool results","suggested_tool":null}'
        "]}\n\n"
        "Final check: if use_plan is false, steps must be exactly []. "
        "Output the JSON object now, with no leading label or explanation."
    )

    if prompt_messages and prompt_messages[0].get("role") == "system":
        prompt_messages[0]["content"] += plan_instruction
    else:
        prompt_messages.insert(
            0,
            {
                "role": "system",
                "content": plan_instruction.strip(),
            },
        )

    for message in reversed(prompt_messages):
        if message.get("role") == "user":
            message["content"] += (
                "\n\nReturn the execution-plan JSON now. "
                "This is planning only: do not answer the task and do not emit a native tool call."
            )
            break

    return prompt_messages


def _plan_prompt_json_generate(
    config_path: Path,
    config: dict,
    messages: list[dict],
    tools_schema: list[dict],
    conversation_id: str | None = None,
) -> str:
    _set_last_kv_cache_record(None)
    """
    使用与 prompt_json 相同的模型、配置和完整 tools_schema，
    但生成内容是 execution plan，而不是普通 AIMessage。
    """
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        raise RuntimeError("prompt_json mode requires requirements-llm.txt") from exc

    model_config = config.get("model", {})
    generation_config = config.get("generation", {})

    model_setting = model_config.get("model_name_or_path")
    tokenizer_setting = model_config.get("tokenizer_name_or_path", model_setting)

    if not isinstance(model_setting, str) or not isinstance(tokenizer_setting, str):
        raise ValueError("model_name_or_path and tokenizer_name_or_path are required")

    model_path = resolve_from_file(model_setting, config_path)
    tokenizer_path = resolve_from_file(tokenizer_setting, config_path)

    if not model_path.exists() or not tokenizer_path.exists():
        raise FileNotFoundError(f"local model path does not exist: {model_path}")

    local_only = bool(model_config.get("local_files_only", True))
    trust_remote_code = bool(model_config.get("trust_remote_code", False))
    dtype = _dtype_value(torch, str(model_config.get("torch_dtype", "auto")))

    tokenizer, model = _load_model_bundle(
        AutoModelForCausalLM,
        AutoTokenizer,
        model_path,
        tokenizer_path,
        local_only,
        trust_remote_code,
        dtype,
        model_config.get("device_map", "auto"),
        model_config.get("max_memory"),
    )

    # 计划阶段必须输出 execution-plan JSON，因此不注入真实业务 tools。
    # 原生 tools schema 注入仍保留在 _prompt_json_generate() 的执行阶段。
    model_identity = _model_cache_key(
        model_path,
        tokenizer_path,
        local_only,
        trust_remote_code,
        dtype,
        model_config.get("device_map", "auto"),
        model_config.get("max_memory"),
    )
    backend = str(model_config.get("backend", "transformers"))
    prompt_messages = _build_plan_prompt_messages(messages, tools_schema)

    inputs = tokenizer.apply_chat_template(
        prompt_messages,
        tokenize=True,
        add_generation_prompt=True,
        return_tensors="pt",
        return_dict=True,
        enable_thinking=False,
    )
    cacheable_token_count = _chat_context_token_count(
        tokenizer,
        prompt_messages,
        {},
    )

    device = next(model.parameters()).device
    inputs = inputs.to(device)

    options = {
        "max_new_tokens": int(generation_config.get("max_new_tokens", 1024)),
        "do_sample": bool(generation_config.get("do_sample", False)),
    }

    namespace = "plan_decision"
    cache_key = _make_kv_cache_key(
        model_identity=model_identity,
        tokenizer=tokenizer,
        backend=backend,
        namespace=namespace,
        schema_injection="plan_decision",
        tools_schema=tools_schema,
        prompt_messages=prompt_messages,
        template_kwargs={},
        conversation_id=conversation_id,
    )
    raw_text, kv_record = _generate_text_with_kv_cache(
        torch_module=torch,
        tokenizer=tokenizer,
        model=model,
        inputs=inputs,
        options=options,
        kv_config=_kv_cache_config(config),
        cache_key=cache_key,
        namespace=namespace,
        cacheable_token_count=cacheable_token_count,
        source_messages=messages,
    )
    _set_last_kv_cache_record(kv_record)
    return raw_text


def generate_execution_plan(
    model_config: str,
    messages: list[dict],
    tools_schema: list[dict],
    mode: str = "prompt_json",
    artifact_dir: str | None = None,
    artifact_stem: str | None = None,
    conversation_id: str | None = None,
) -> dict:
    """
    生成轻量执行计划。

    返回的 execution_plan 只描述是否需要多步执行及建议工具，
    不直接产生 AIMessage，也不执行工具。
    """
    config_path, config = _load_model_config(model_config)
    messages = validate_messages(deepcopy(messages))

    if not isinstance(tools_schema, list):
        raise ValueError("tools_schema must be an array")

    generated_at = now_iso()
    backend = (
        "mock"
        if mode == "mock"
        else config.get("model", {}).get("backend", "transformers")
    )

    if mode == "mock":
        # mock 仅用于验证接口、解析和产物保存。
        # 不虚构复杂计划，真实计划判断交给 prompt_json 模式。
        execution_plan = {
            "use_plan": False,
            "steps": [],
        }
        raw_text = json.dumps(execution_plan, ensure_ascii=False)
        status = "success"
        error = None
        kv_cache = _kv_cache_record(
            enabled=False,
            event="skipped",
            namespace="plan_decision",
            cache_key=None,
            reason="mock_mode",
            cached_token_count=0,
            current_token_count=0,
            reused_token_count=0,
            new_prefill_token_count=0,
            entry_token_count_after=0,
            bytes_estimate=0,
        )

    elif mode == "prompt_json":
        raw_text = _plan_prompt_json_generate(
            config_path,
            config,
            messages,
            tools_schema,
            conversation_id,
        )
        kv_cache = _consume_last_kv_cache_record()

        try:
            execution_plan = _parse_execution_plan(raw_text)
            status = "success"
            error = None
        except Exception as exc:
            # 解析失败时不伪造 use_plan=false，
            # 让调用方明确知道这是计划生成失败。
            execution_plan = None
            status = "error"
            error = {
                "type": type(exc).__name__,
                "message": str(exc),
                "raw_text_preview": raw_text[:500],
            }

    else:
        raise ValueError("mode must be mock or prompt_json")

    raw_record = {
        "kind": "execution_plan",
        "mode": mode,
        "backend": backend,
        "raw_text": raw_text,
        "execution_plan": execution_plan,
        "status": status,
        "error": error,
        "generated_at": generated_at,
        "kv_cache": kv_cache,
    }

    if artifact_dir:
        directory = Path(artifact_dir)
        prefix = f"{artifact_stem}_" if artifact_stem else ""

        raw_path = directory / f"{prefix}plan_raw_model_output.json"
        plan_path = directory / f"{prefix}execution_plan.json"
        log_path = directory / "llm_run_log.jsonl"

        write_json(raw_record, raw_path)

        if execution_plan is not None:
            write_json(execution_plan, plan_path)

        append_jsonl(
            {
                "timestamp": generated_at,
                "kind": "execution_plan",
                "mode": mode,
                "status": status,
                "raw_output_path": str(raw_path),
                "execution_plan_path": (
                    str(plan_path) if execution_plan is not None else None
                ),
                "error": error,
                "kv_cache": kv_cache,
            },
            log_path,
        )

    return {
        "execution_plan": execution_plan,
        "status": status,
        "error": error,
    }


def generate_agent_decision(
    model_config: str,
    messages: list[dict],
    tools_schema: list[dict],
    mode: str = "prompt_json",
    artifact_dir: str | None = None,
    artifact_stem: str | None = None,
    conversation_id: str | None = None,
) -> dict:
    """
    由模型自主判断任务是否需要多步计划。

    简单任务：
    execution_mode = "direct"
    直接返回普通 AIMessage。

    复杂任务：
    execution_mode = "plan"
    返回 execution_plan，后续由 B1 逐步执行。
    """
    plan_result = generate_execution_plan(
        model_config=model_config,
        messages=messages,
        tools_schema=tools_schema,
        mode=mode,
        artifact_dir=artifact_dir,
        artifact_stem=artifact_stem,
        conversation_id=conversation_id,
    )

    if plan_result["status"] != "success":
        return {
            "execution_mode": None,
            "execution_plan": None,
            "ai_message": None,
            "status": "error",
            "error": plan_result["error"],
        }

    execution_plan = plan_result["execution_plan"]

    if execution_plan is None:
        return {
            "execution_mode": None,
            "execution_plan": None,
            "ai_message": None,
            "status": "error",
            "error": {
                "type": "RuntimeError",
                "message": "execution plan is missing after successful plan generation",
            },
        }

    # 模型认为是复杂任务：暂不生成普通 AIMessage。
    # 后续 B1 根据计划步骤推进工具调用。
    if execution_plan["use_plan"]:
        return {
            "execution_mode": "plan",
            "execution_plan": execution_plan,
            "ai_message": None,
            "status": "success",
            "error": None,
        }

    # 模型认为是简单任务：沿用原来的直接工具调用逻辑。
    ai_result = generate_ai_message(
        model_config=model_config,
        messages=messages,
        tools_schema=tools_schema,
        mode=mode,
        artifact_dir=artifact_dir,
        artifact_stem=artifact_stem,
        conversation_id=conversation_id,
    )

    return {
        "execution_mode": "direct",
        "execution_plan": execution_plan,
        "ai_message": ai_result["ai_message"],
        "status": ai_result["status"],
        "error": ai_result["error"],
    }


def generate_ai_message(
    model_config: str,
    messages: list[dict],
    tools_schema: list[dict],
    mode: str = "prompt_json",
    artifact_dir: str | None = None,
    artifact_stem: str | None = None,
    conversation_id: str | None = None,
) -> dict:
    config_path, config = _load_model_config(model_config)
    messages = validate_messages(deepcopy(messages))
    if not isinstance(tools_schema, list):
        raise ValueError("tools_schema must be an array")
    generated_at = now_iso()
    backend = "mock" if mode == "mock" else config.get("model", {}).get("backend", "transformers")
    if mode == "mock":
        ai_message = _mock_generate(messages)
        raw_text = json.dumps({"content": ai_message["content"], "tool_calls": ai_message["tool_calls"]}, ensure_ascii=False)
        parsed_candidate = {"content": ai_message["content"], "tool_calls": ai_message["tool_calls"]}
        status = "success"
        error = None
        kv_cache = _kv_cache_record(
            enabled=False,
            event="skipped",
            namespace="ai_message.mock",
            cache_key=None,
            reason="mock_mode",
            cached_token_count=0,
            current_token_count=0,
            reused_token_count=0,
            new_prefill_token_count=0,
            entry_token_count_after=0,
            bytes_estimate=0,
        )
    elif mode == "prompt_json":
        raw_text = _prompt_json_generate(
            config_path,
            config,
            messages,
            tools_schema,
            conversation_id,
        )
        kv_cache = _consume_last_kv_cache_record()
        try:
            schema_injection = config.get("tool_calling", {}).get(
                "schema_injection",
                "prompt",
            )
            parser = (
                _parse_native_model_output
                if schema_injection == "native"
                else _parse_model_output
            )
            parsed_candidate, ai_message = parser(raw_text)
            status = "success"
            error = None
        except Exception as exc:
            parsed_candidate = None
            ai_message = make_ai_message(PARSE_ERROR_CONTENT, [])
            status = "error"
            error = {"type": type(exc).__name__, "message": str(exc)}
    else:
        raise ValueError("mode must be mock or prompt_json")
    raw_record = {
        "mode": mode,
        "backend": backend,
        "raw_text": raw_text,
        "parsed_candidate": parsed_candidate,
        "status": status,
        "error": error,
        "generated_at": generated_at,
        "kv_cache": kv_cache,
    }
    if artifact_dir:
        raw_path, message_path, log_path = _artifact_paths(artifact_dir, artifact_stem)
        write_json(raw_record, raw_path)
        write_json(ai_message, message_path)
        append_jsonl(
            {
                "timestamp": generated_at,
                "mode": mode,
                "status": status,
                "raw_output_path": str(raw_path),
                "ai_message_path": str(message_path),
                "error": error,
                "kv_cache": kv_cache,
            },
            log_path,
        )
    return {
        "ai_message": ai_message,
        "status": status,
        "error": error,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate one AIMessage with a local or mock LLM.")
    parser.add_argument("--model_config", required=True)
    parser.add_argument("--messages", required=True)
    parser.add_argument("--tools_schema", required=True)
    parser.add_argument("--mode", choices=["mock", "prompt_json"], required=True)
    parser.add_argument("--outdir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        outdir = resolve_cli_path(args.outdir)
        decision = generate_agent_decision(
            model_config=str(resolve_cli_path(args.model_config)),
            messages=read_json(resolve_cli_path(args.messages)),
            tools_schema=read_json(resolve_cli_path(args.tools_schema)),
            mode=args.mode,
            artifact_dir=str(outdir),
        )

        decision_path = outdir / "agent_decision.json"
        write_json(decision, decision_path)

        if decision["status"] != "success":
            error = decision["error"] or {}
            raise RuntimeError(
                error.get("message", "agent decision generation failed")
            )

        print(decision_path)
        return 0
    except Exception as exc:
        print(f"fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
