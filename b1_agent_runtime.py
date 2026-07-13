from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from time import perf_counter

from common.io_utils import append_jsonl, read_json, read_text, read_yaml, write_json, write_text
from common.logging_utils import now_iso
from common.path_utils import resolve_cli_path, resolve_from_file
from common.schemas import validate_ai_message


def _validate_runtime_input(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("runtime_input.json must contain an object")

    execution_mode = payload.setdefault("execution_mode", "integrated")
    if execution_mode not in {"integrated", "fixture"}:
        raise ValueError("execution_mode must be integrated or fixture")

    input_type = payload.setdefault("input_type", "text")
    if input_type not in {"text", "audio"}:
        raise ValueError("input_type must be text or audio")

    required = ["conversation_id", "system_prompt_path", "toolset", "max_turns", "save_memory"]
    missing = [field for field in required if field not in payload]
    if missing:
        raise ValueError(f"runtime input missing: {', '.join(missing)}")

    if not isinstance(payload["conversation_id"], str) or not payload["conversation_id"].strip():
        raise ValueError("conversation_id must be a non-empty string")
    if not isinstance(payload["system_prompt_path"], str) or not payload["system_prompt_path"].strip():
        raise ValueError("system_prompt_path must be a non-empty string")
    if not isinstance(payload["toolset"], str) or not payload["toolset"].strip():
        raise ValueError("toolset must be a non-empty string")
    if not isinstance(payload["max_turns"], int) or isinstance(payload["max_turns"], bool) or payload["max_turns"] < 1:
        raise ValueError("max_turns must be a positive integer")
    if payload["save_memory"] not in {"none", "conversation", "global"}:
        raise ValueError("save_memory must be none, conversation, or global")

    payload.setdefault("enable_tts", False)
    if not isinstance(payload["enable_tts"], bool):
        raise ValueError("enable_tts must be boolean")

    tts_language = payload.setdefault("tts_language", "auto")
    if not isinstance(tts_language, str) or tts_language.strip().lower() not in {"auto", "zh", "en"}:
        raise ValueError("tts_language must be auto, zh, or en")
    payload["tts_language"] = tts_language.strip().lower()

    if input_type == "text":
        if not isinstance(payload.get("user_input"), str) or not payload["user_input"].strip():
            raise ValueError("text input requires a non-empty user_input")
        payload["user_input"] = payload["user_input"].strip()
    else:
        if not isinstance(payload.get("audio_path"), str) or not payload["audio_path"].strip():
            raise ValueError("audio input requires a non-empty audio_path")
        payload["audio_path"] = payload["audio_path"].strip()
        language_hint = payload.get("audio_language")
        if language_hint is not None and (
            not isinstance(language_hint, str) or not language_hint.strip()
        ):
            raise ValueError("audio_language must be a non-empty language code when provided")
        if language_hint is not None:
            payload["audio_language"] = language_hint.strip()
        memory_query = payload.setdefault("memory_query", "")
        if not isinstance(memory_query, str):
            raise ValueError("memory_query must be a string when provided")
        if payload["max_turns"] < 2:
            raise ValueError("audio input requires max_turns >= 2 for preprocessing and ASR")
        if execution_mode == "fixture":
            raise ValueError("audio input is supported only in integrated mode")

    if execution_mode == "fixture":
        fixtures = payload.get("fixtures")
        if not isinstance(fixtures, dict):
            raise ValueError("fixture mode requires a fixtures object")
        required_fixtures = [
            "selected_memory_path",
            "tools_schema_path",
            "ai_messages_path",
            "tool_messages_path",
        ]
        missing_fixtures = [field for field in required_fixtures if not isinstance(fixtures.get(field), str)]
        if missing_fixtures:
            raise ValueError(f"fixtures missing paths: {', '.join(missing_fixtures)}")
        if payload["save_memory"] != "none":
            raise ValueError("fixture mode requires save_memory=none")
        if payload["enable_tts"]:
            raise ValueError("fixture mode does not execute text_to_speech; set enable_tts=false")
    else:
        selected_ids = payload.setdefault("selected_memory_ids", [])
        if not isinstance(selected_ids, list) or not all(isinstance(item, str) for item in selected_ids):
            raise ValueError("selected_memory_ids must be a list of strings")
        payload.setdefault("use_global_memory", False)
        if not isinstance(payload["use_global_memory"], bool):
            raise ValueError("use_global_memory must be boolean")

    return payload


def _memory_context(selected_memory: dict) -> str:
    sections = []
    for document in selected_memory.get("selected_memory_docs", []):
        sections.append(
            f'<memory id="{document["memory_id"]}" type="{document["memory_type"]}">\n'
            f'{document["content"].strip()}\n</memory>'
        )
    return "\n\n".join(sections)


def _initial_user_content(runtime: dict) -> str:
    """Build the first HumanMessage for either text or audio input."""
    if runtime["input_type"] == "text":
        return runtime["user_input"]

    language_hint = runtime.get("audio_language") or "auto"
    return (
        "[INPUT_TYPE: AUDIO]\n"
        f"audio_path: {runtime['audio_path']}\n"
        f"language_hint: {language_hint}\n\n"
        "The runtime will preprocess and transcribe this audio before Agent reasoning starts. "
        "Wait for the later [TRANSCRIBED_USER_REQUEST] message. "
        "Do not repeat audio preprocessing or speech recognition for this input "
        "unless the user explicitly asks to process another audio file."
    )


def _append_system_instruction(messages: list[dict], instruction: str) -> list[dict]:
    """Return a copied message sequence with one transient B1 control instruction."""
    call_messages = deepcopy(messages)
    for message in call_messages:
        if message.get("role") == "system":
            message["content"] = message.get("content", "").rstrip() + "\n\n" + instruction.strip()
            return call_messages
    raise ValueError("messages must contain a system message")


def _append_plan_step_instruction(messages: list[dict], instruction: str) -> list[dict]:
    """Return a copied message sequence with one transient plan-step suffix."""
    call_messages = deepcopy(messages)
    call_messages.append(
        {
            "role": "user",
            "content": "[PLAN_STEP_INSTRUCTION]\n" + instruction.strip(),
        }
    )
    return call_messages


def _skill_output(
    tool_messages: list[dict],
    expected_name: str,
    required_fields: tuple[str, ...] = (),
) -> dict:
    """Validate one successful ToolMessage and return its SkillResult output."""
    if len(tool_messages) != 1:
        raise ValueError(f"{expected_name} expects exactly one ToolMessage")
    message = tool_messages[0]
    if message.get("name") != expected_name:
        raise ValueError(f"expected {expected_name} ToolMessage, got {message.get('name')}")
    if message.get("status") != "success":
        raise ValueError(f"{expected_name} did not complete successfully")

    try:
        result = json.loads(message.get("content", ""))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{expected_name} ToolMessage content is not valid JSON") from exc

    if not isinstance(result, dict) or result.get("status") != "success":
        error = result.get("error") if isinstance(result, dict) else None
        raise ValueError(f"{expected_name} returned an error SkillResult: {error}")
    output = result.get("output")
    if not isinstance(output, dict):
        raise ValueError(f"{expected_name} SkillResult has no output object")
    missing = [field for field in required_fields if not output.get(field)]
    if missing:
        raise ValueError(f"{expected_name} SkillResult missing output: {', '.join(missing)}")
    return output


def _tool_failures(tool_messages: list[dict]) -> list[dict]:
    """Return protocol, execution, and unusable-result failures for any skill."""
    failures = []
    for message in tool_messages:
        if not isinstance(message, dict):
            failures.append(
                {
                    "tool_call_id": None,
                    "tool": "unknown",
                    "reason": "invalid_tool_message",
                    "error": "ToolMessage must be an object",
                }
            )
            continue
        failure = {
            "tool_call_id": message.get("tool_call_id"),
            "tool": message.get("name", "unknown"),
        }
        try:
            result = json.loads(message.get("content", ""))
            if not isinstance(result, dict):
                raise ValueError("SkillResult must be an object")
        except (TypeError, json.JSONDecodeError, ValueError) as exc:
            failures.append({**failure, "reason": "invalid_skill_result", "error": str(exc)})
            continue

        if message.get("status") != "success" or result.get("status") != "success":
            failures.append({**failure, "reason": "skill_error", "error": result.get("error")})
            continue

        output = result.get("output")
        if (
            message.get("name") == "local_file_search"
            and isinstance(output, dict)
            and output.get("results") == []
        ):
            failures.append({**failure, "reason": "empty_result", "error": None})
    return failures


def _replan_instruction(failures: list[dict]) -> str:
    details = json.dumps(failures, ensure_ascii=False, separators=(",", ":"))
    return (
        f"REPLAN: one or more tool calls failed or returned an unusable result: {details}. "
        "The previous decision is invalid. Use the ToolMessages to diagnose the failure. "
        "Do not repeat an identical failed call unless you correct its arguments or have a clear reason. "
        "Return DIRECT, a new valid tool plan, or explain that the task cannot be completed."
    )


def _interpret_agent_decision(decision: dict) -> dict:
    """Normalize B4's direct/plan decision so B1 can reuse it before and after ASR."""
    if not isinstance(decision, dict):
        return {
            "ok": False,
            "status": "plan_decision_error",
            "error": {
                "type": "PlanDecisionError",
                "message": "B4 agent decision result must be an object.",
            },
        }
    if decision.get("status") != "success":
        return {
            "ok": False,
            "status": "plan_decision_error",
            "error": {
                "type": "PlanDecisionError",
                "message": "B4 failed while deciding whether to use a plan.",
                "cause": decision.get("error"),
            },
        }

    decision_mode = decision.get("execution_mode")
    execution_plan = decision.get("execution_plan")
    if decision_mode == "direct":
        ai_message = decision.get("ai_message")
        if not isinstance(ai_message, dict):
            return {
                "ok": False,
                "status": "plan_decision_error",
                "error": {
                    "type": "PlanDecisionError",
                    "message": "Direct decision does not contain an AIMessage.",
                },
            }
        try:
            validate_ai_message(ai_message)
        except Exception as exc:
            return {
                "ok": False,
                "status": "plan_decision_error",
                "error": {
                    "type": "PlanDecisionError",
                    "message": "Direct decision contains an invalid AIMessage.",
                    "cause": {"type": type(exc).__name__, "message": str(exc)},
                },
            }
        return {
            "ok": True,
            "plan_mode": "direct",
            "execution_plan": execution_plan,
            "plan_steps": [],
            "pending_llm_result": {
                "ai_message": ai_message,
                "status": decision.get("status"),
                "error": decision.get("error"),
            },
        }

    if decision_mode == "plan":
        if (
            not isinstance(execution_plan, dict)
            or execution_plan.get("use_plan") is not True
            or not isinstance(execution_plan.get("steps"), list)
            or not execution_plan["steps"]
        ):
            return {
                "ok": False,
                "status": "plan_decision_error",
                "error": {
                    "type": "PlanDecisionError",
                    "message": "Plan decision does not contain valid steps.",
                },
            }
        return {
            "ok": True,
            "plan_mode": "plan",
            "execution_plan": execution_plan,
            "plan_steps": execution_plan["steps"],
            "pending_llm_result": None,
        }

    return {
        "ok": False,
        "status": "plan_decision_error",
        "error": {
            "type": "PlanDecisionError",
            "message": f"Unknown B4 decision mode: {decision_mode}",
        },
    }


def _default_llm_mode(model_config: Path) -> str:
    config = read_yaml(model_config)
    return config.get("runtime", {}).get("default_mode", "mock")


def generate_ai_message(*args, **kwargs) -> dict:
    """Lazy B4 proxy retained as the integrated-mode injection point."""
    from b4_local_agent_llm import generate_ai_message as b4_generate_ai_message
    return b4_generate_ai_message(*args, **kwargs)


def generate_agent_decision(*args, **kwargs) -> dict:
    """Lazy B4 proxy for model-selected direct or plan execution."""
    from b4_local_agent_llm import (
        generate_agent_decision as b4_generate_agent_decision,
    )

    return b4_generate_agent_decision(*args, **kwargs)


def _load_fixture_inputs(input_file: Path, runtime: dict) -> dict:
    fixtures = runtime["fixtures"]
    selected_memory = read_json(resolve_from_file(fixtures["selected_memory_path"], input_file))
    tools_schema = read_json(resolve_from_file(fixtures["tools_schema_path"], input_file))
    ai_messages = read_json(resolve_from_file(fixtures["ai_messages_path"], input_file))
    tool_messages = read_json(resolve_from_file(fixtures["tool_messages_path"], input_file))
    if not isinstance(selected_memory, dict):
        raise ValueError("preset memory must be a JSON object")
    if not isinstance(tools_schema, list):
        raise ValueError("preset tools_schema must be a JSON array")
    if not isinstance(ai_messages, list) or not ai_messages:
        raise ValueError("preset AI messages must be a non-empty JSON array")
    if not isinstance(tool_messages, dict):
        raise ValueError("preset ToolMessages must be an object keyed by tool_call_id")
    for message in ai_messages:
        validate_ai_message(message)
    return {
        "selected_memory": selected_memory,
        "tools_schema": tools_schema,
        "ai_messages": ai_messages,
        "tool_messages": tool_messages,
    }


def _fixture_tool_messages(tool_calls: list[dict], preset_messages: dict) -> list[dict]:
    results = []
    for call in tool_calls:
        call_id = call.get("id")
        message = deepcopy(preset_messages.get(call_id))
        if not isinstance(message, dict):
            raise ValueError(f"fixture ToolMessage does not exist for tool_call_id: {call_id}")
        if message.get("role") != "tool" or message.get("tool_call_id") != call_id:
            raise ValueError(f"invalid fixture ToolMessage for tool_call_id: {call_id}")
        if message.get("name") != call.get("name"):
            raise ValueError(f"fixture ToolMessage name does not match call: {call_id}")
        results.append(message)
    return results


def run_agent(
    input_path: str,
    tools_config: str | None,
    memory_config: str | None,
    model_config: str | None,
    outdir: str,
    llm_mode: str | None = None,
) -> dict:
    started = perf_counter()
    input_file = Path(input_path).resolve()
    output_dir = Path(outdir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    runtime = _validate_runtime_input(read_json(input_file))
    input_type = runtime["input_type"]
    input_display = runtime["user_input"] if input_type == "text" else runtime["audio_path"]
    print(f"input_type: {input_type}")
    print(f"user_input: {input_display}")
    execution_mode = runtime["execution_mode"]
    # 支持绝对路径，避免交互模式下临时文件路径解析错误
    prompt_path_str = runtime["system_prompt_path"]
    prompt_path = Path(prompt_path_str)
    if not prompt_path.is_absolute():
        prompt_path = resolve_from_file(prompt_path_str, input_file)
    system_prompt = read_text(prompt_path).strip()
    fixture_data = None
    tools_file = memory_file = model_file = None
    if execution_mode == "fixture":
        fixture_data = _load_fixture_inputs(input_file, runtime)
        selected_memory = fixture_data["selected_memory"]
        tools_schema = fixture_data["tools_schema"]
        mode = "fixture"
    else:
        if not tools_config or not memory_config or not model_config:
            raise ValueError("integrated mode requires tools_config, memory_config, and model_config")
        from b3_tool_layer import execute_tool_calls, get_tools_schema
        from b5_memory import load_memory

        tools_file = Path(tools_config).resolve()
        memory_file = Path(memory_config).resolve()
        model_file = Path(model_config).resolve()
        memory_query = (
            runtime["user_input"]
            if input_type == "text"
            else runtime.get("memory_query", "")
        )
        selected_memory = load_memory(
            str(memory_file),
            runtime["selected_memory_ids"],
            runtime["use_global_memory"],
            memory_query,
            str(output_dir),
        )
        tools_schema = get_tools_schema(str(tools_file), runtime["toolset"], str(output_dir))
        mode = llm_mode or _default_llm_mode(model_file)
    memory_context = _memory_context(selected_memory)
    if memory_context:
        system_prompt = f"{system_prompt}\n\n{memory_context}"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": _initial_user_content(runtime)},
    ]
    tool_rounds = 0
    llm_calls = 0
    turns = []
    all_tool_messages = []
    final_answer = ""
    status = "success"
    terminal_error = None
    warnings = []
    if selected_memory.get("status") in {"partial", "error"}:
        warnings.append("memory selection completed with errors")

    plan_mode = "fixture" if execution_mode == "fixture" else "not_started"
    execution_plan = None
    plan_steps = []
    plan_step_index = 0
    pending_llm_result = None
    planning_llm_calls = 0
    planning_latency_ms = 0.0
    planning_events = []
    audio_transcript = None
    audio_bootstrap = []
    output_tool_rounds = 0
    tts_output = {
        "enabled": runtime["enable_tts"],
        "status": "not_requested" if runtime["enable_tts"] else "disabled",
    }

    # 音频前处理和 ASR 是输入适配流程，不由 B4 自主决定。
    # B4 在收到转写文本后，才负责后续 Direct / Plan 决策。
    agent_tools_schema = tools_schema

    if execution_mode == "integrated" and input_type == "audio":
        # 后续 Agent 推理不再开放本次输入已经完成的两个前置工具，
        # 避免 B4 重复对同一个音频做预处理或转写。
        agent_tools_schema = [
            tool
            for tool in tools_schema
            if not (
                isinstance(tool, dict)
                and isinstance(tool.get("function"), dict)
                and tool["function"].get("name")
                in {"audio_preprocess", "speech_to_text"}
            )
        ]

        def _run_forced_audio_step(stage: str, tool_call: dict) -> list[dict]:
            nonlocal tool_rounds

            forced_ai_message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [tool_call],
            }

            messages.append(forced_ai_message)
            step_started = perf_counter()
            tool_messages = execute_tool_calls(
                [tool_call],
                str(tools_file),
                runtime["toolset"],
                str(output_dir),
            )
            latency_ms = round((perf_counter() - step_started) * 1000, 3)

            tool_rounds += 1
            messages.extend(tool_messages)
            all_tool_messages.extend(tool_messages)
            audio_bootstrap.append(
                {
                    "stage": stage,
                    "mode": "runtime_forced",
                    "ai_message": forced_ai_message,
                    "tool_messages": tool_messages,
                    "latency_ms": latency_ms,
                }
            )
            return tool_messages

        try:
            preprocess_messages = _run_forced_audio_step(
                "audio_preprocess",
                {
                    "id": "runtime_audio_preprocess_001",
                    "name": "audio_preprocess",
                    "args": {"audio_path": runtime["audio_path"]},
                },
            )
            processed_audio_path = _skill_output(
                preprocess_messages,
                "audio_preprocess",
                ("processed_audio_path",),
            )["processed_audio_path"].strip()

            asr_args = {"audio_path": processed_audio_path}
            if runtime.get("audio_language"):
                asr_args["language"] = runtime["audio_language"]

            asr_messages = _run_forced_audio_step(
                "speech_to_text",
                {
                    "id": "runtime_speech_to_text_001",
                    "name": "speech_to_text",
                    "args": asr_args,
                },
            )
            audio_transcript = _skill_output(
                asr_messages,
                "speech_to_text",
                ("text",),
            )["text"].strip()

            messages.append(
                {
                    "role": "user",
                    "content": (
                        "[TRANSCRIBED_USER_REQUEST]\n"
                        f"{audio_transcript}\n\n"
                        "Audio preprocessing and speech recognition are complete. "
                        "Treat the transcript above as the actual user request. "
                        "Now decide whether to answer directly or invoke downstream tools. "
                        "Do not call audio_preprocess or speech_to_text again for this input."
                    ),
                }
            )

        except Exception as exc:
            status = "audio_bootstrap_tool_error"
            terminal_error = {
                "type": type(exc).__name__,
                "message": str(exc),
            }

    if execution_mode == "integrated" and status == "success":
        decision_stage = (
            "initial_text_decision"
            if input_type == "text"
            else "after_asr_decision"
        )
        decision_stem = (
            "initial_decision"
            if input_type == "text"
            else "after_asr_decision"
        )
        decision_output_path = (
            output_dir / "agent_decision.json"
            if input_type == "text"
            else output_dir / "after_asr_agent_decision.json"
        )

        planning_started = perf_counter()
        planning_llm_calls += 1
        decision = generate_agent_decision(
            str(model_file),
            messages,
            agent_tools_schema,
            mode,
            str(output_dir / "agent_decision"),
            decision_stem,
            conversation_id=runtime["conversation_id"],
        )
        decision_latency_ms = round((perf_counter() - planning_started) * 1000, 3)
        planning_latency_ms = round(planning_latency_ms + decision_latency_ms, 3)
        planning_events.append(
            {"stage": decision_stage, "latency_ms": decision_latency_ms}
        )
        write_json(decision, decision_output_path)

        interpreted = _interpret_agent_decision(decision)
        if not interpreted["ok"]:
            status = interpreted["status"]
            terminal_error = interpreted["error"]
        else:
            plan_mode = interpreted["plan_mode"]
            execution_plan = interpreted["execution_plan"]
            plan_steps = interpreted["plan_steps"]
            pending_llm_result = interpreted["pending_llm_result"]

    while status == "success":
        llm_calls += 1
        turn_start = perf_counter()
        current_plan_step = None
        if execution_mode == "fixture":
            if llm_calls > len(fixture_data["ai_messages"]):
                raise ValueError("fixture AIMessage sequence ended before a final answer")
            ai_message = deepcopy(fixture_data["ai_messages"][llm_calls - 1])
            llm_status = "success"
            llm_error = None
        else:
            current_plan_step = None

            if pending_llm_result is not None:
                llm_result = pending_llm_result
                pending_llm_result = None

            else:
                call_messages = messages

                if plan_mode == "plan":
                    if plan_step_index >= len(plan_steps):
                        raise RuntimeError(
                            "execution plan ended before the Agent produced a final answer"
                        )

                    current_plan_step = plan_steps[plan_step_index]
                    suggested_tool = current_plan_step["suggested_tool"]

                    if suggested_tool is None:
                        step_instruction = (
                            "PLAN EXECUTION MODE\n"
                            f"Current step: {current_plan_step['step_id']}\n"
                            f"Goal: {current_plan_step['goal']}\n"
                            "This is the final answer step. "
                            "Do not call a tool. Use previous ToolMessages "
                            "and return the final answer for the user."
                        )
                    else:
                        step_instruction = (
                            "PLAN EXECUTION MODE\n"
                            f"Current step: {current_plan_step['step_id']}\n"
                            f"Goal: {current_plan_step['goal']}\n"
                            f"Required tool: {suggested_tool}\n"
                            "Return exactly one tool call using the required tool. "
                            "Do not provide the final user answer yet."
                        )

                    call_messages = _append_plan_step_instruction(
                        messages,
                        step_instruction,
                    )

                llm_result = generate_ai_message(
                    str(model_file),
                    call_messages,
                    agent_tools_schema,
                    mode,
                    str(output_dir / "llm_calls"),
                    f"llm_call_{llm_calls:03d}",
                    conversation_id=runtime["conversation_id"],
                )
            if not isinstance(llm_result, dict) or not isinstance(llm_result.get("ai_message"), dict):
                raise ValueError("B4 result must contain an ai_message object")
            ai_message = llm_result["ai_message"]
            llm_status = llm_result.get("status")
            llm_error = llm_result.get("error")
        messages.append(ai_message)
        turn = {
            "turn_index": llm_calls,
            "ai_message": ai_message,
            "llm_status": llm_status,
            "llm_error": llm_error,
            "tool_messages": [],
            "latency_ms": None,
        }
        if llm_status != "success":
            status = "llm_parse_error"
            terminal_error = {
                "type": "LLMParseError",
                "message": "B4 failed to parse the model output as a valid AIMessage JSON object.",
                "llm_call_index": llm_calls,
                "cause": llm_error,
            }
            turn["latency_ms"] = round((perf_counter() - turn_start) * 1000, 3)
            turns.append(turn)
            break
        tool_calls = ai_message.get("tool_calls", [])
        if current_plan_step is not None:
            expected_tool = current_plan_step["suggested_tool"]

            if expected_tool is None and tool_calls:
                final_answer = "任务计划执行失败：最终回答步骤不允许继续调用工具。"
                status = "plan_step_violation"
                terminal_error = {
                    "type": "PlanStepViolation",
                    "message": final_answer,
                    "step": current_plan_step,
                    "tool_calls": tool_calls,
                }
                turn["latency_ms"] = round(
                    (perf_counter() - turn_start) * 1000,
                    3,
                )
                turns.append(turn)
                break

            if expected_tool is not None and (
                not tool_calls
                or tool_calls[0].get("name") != expected_tool
            ):
                final_answer = (
                    "任务计划执行失败：模型没有按当前计划步骤调用指定工具。"
                )
                status = "plan_step_violation"
                terminal_error = {
                    "type": "PlanStepViolation",
                    "message": final_answer,
                    "step": current_plan_step,
                    "expected_tool": expected_tool,
                    "tool_calls": tool_calls,
                }
                turn["latency_ms"] = round(
                    (perf_counter() - turn_start) * 1000,
                    3,
                )
                turns.append(turn)
                break
        if not tool_calls:
            if current_plan_step is not None:
                plan_step_index += 1
            final_answer = ai_message["content"]
            print(f"content: {final_answer}")
            turn["latency_ms"] = round((perf_counter() - turn_start) * 1000, 3)
            turns.append(turn)
            break
        if tool_rounds >= runtime["max_turns"]:
            requested = ", ".join(call.get("name", "unknown") for call in tool_calls)
            final_answer = (
                "任务因超过最大工具调用轮次而终止，"
                f"最后一次模型仍请求调用工具：{requested}。"
            )
            status = "max_turns_exceeded"
            terminal_error = {
                "type": "MaxTurnsExceeded",
                "message": final_answer,
                "unexecuted_tool_calls": tool_calls,
            }
            turn["latency_ms"] = round((perf_counter() - turn_start) * 1000, 3)
            turns.append(turn)
            break
        if execution_mode == "fixture":
            tool_messages = _fixture_tool_messages(
                tool_calls,
                fixture_data["tool_messages"],
            )
        else:
            tool_messages = execute_tool_calls(
                tool_calls,
                str(tools_file),
                runtime["toolset"],
                str(output_dir),
            )
        tool_rounds += 1

        # Always expose real tool results to B4 before asking it to replan.
        messages.extend(tool_messages)
        all_tool_messages.extend(tool_messages)
        turn["tool_messages"] = tool_messages

        # This is protocol-driven: every current or future skill participates
        # without a per-skill branch. Empty search remains an unusable outcome.
        tool_failures = _tool_failures(tool_messages)
        if tool_failures:
            failed_tools = ", ".join(failure["tool"] for failure in tool_failures)
            warnings.append(
                f"tool result invalidated the previous Agent decision; replanning started: {failed_tools}"
            )

            messages = _append_system_instruction(
                messages,
                _replan_instruction(tool_failures),
            )

            planning_started = perf_counter()
            planning_llm_calls += 1

            decision = generate_agent_decision(
                str(model_file),
                messages,
                agent_tools_schema,
                mode,
                str(output_dir / "agent_decision"),
                f"replan_after_tool_failure_turn_{llm_calls:03d}",
                conversation_id=runtime["conversation_id"],
            )

            decision_latency_ms = round(
                (perf_counter() - planning_started) * 1000,
                3,
            )

            planning_latency_ms = round(
                planning_latency_ms + decision_latency_ms,
                3,
            )

            planning_events.append(
                {
                    "stage": "replan_after_tool_failure",
                    "latency_ms": decision_latency_ms,
                    "trigger_tools": [failure["tool"] for failure in tool_failures],
                    "failures": tool_failures,
                }
            )

            write_json(
                decision,
                output_dir / f"replan_after_tool_failure_turn_{llm_calls:03d}.json",
            )

            interpreted = _interpret_agent_decision(decision)

            if not interpreted["ok"]:
                status = interpreted["status"]
                terminal_error = interpreted["error"]

                turn["latency_ms"] = round(
                    (perf_counter() - turn_start) * 1000,
                    3,
                )
                turns.append(turn)
                break

            # 用新决策完整覆盖旧计划状态。
            plan_mode = interpreted["plan_mode"]
            execution_plan = interpreted["execution_plan"]
            plan_steps = interpreted["plan_steps"]
            plan_step_index = 0
            pending_llm_result = interpreted["pending_llm_result"]

        elif current_plan_step is not None:
            # 只有当前步骤正常产生了可继续使用的结果时，才推进旧计划。
            plan_step_index += len(tool_calls)


        turn["latency_ms"] = round((perf_counter() - turn_start) * 1000, 3)
        turns.append(turn)

    # TTS 是确定性的输出适配步骤：B4 不参与选择，B1 在最终文本回答后通过 B3 强制调用。
    # 该 ToolMessage 不写入 messages，避免污染后续多轮对话记忆；完整产物写入 trace.json。
    if runtime["enable_tts"]:
        if execution_mode != "integrated":
            tts_output = {
                "enabled": True,
                "status": "skipped",
                "reason": "fixture_mode",
            }
        elif status != "success":
            tts_output = {
                "enabled": True,
                "status": "skipped",
                "reason": f"agent_status_{status}",
            }
        elif not final_answer.strip():
            tts_output = {
                "enabled": True,
                "status": "skipped",
                "reason": "empty_final_answer",
            }
        else:
            tts_call = {
                "id": "runtime_text_to_speech_001",
                "name": "text_to_speech",
                "args": {
                    "text": final_answer.strip(),
                    "language": runtime["tts_language"],
                    "output_filename": "final_answer.wav",
                },
            }
            tts_started = perf_counter()
            try:
                tts_tool_messages = execute_tool_calls(
                    [tts_call],
                    str(tools_file),
                    "output_tools",
                    str(output_dir),
                )
                output_tool_rounds += 1
                all_tool_messages.extend(tts_tool_messages)
                tts_result = _skill_output(
                    tts_tool_messages,
                    "text_to_speech",
                    ("audio_path",),
                )
                tts_output = {
                    "enabled": True,
                    "status": "success",
                    "tool_call_id": tts_call["id"],
                    "latency_ms": round((perf_counter() - tts_started) * 1000, 3),
                    **tts_result,
                }
            except Exception as exc:
                tts_output = {
                    "enabled": True,
                    "status": "error",
                    "tool_call_id": tts_call["id"],
                    "latency_ms": round((perf_counter() - tts_started) * 1000, 3),
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                }
                warnings.append("text_to_speech failed; the text final answer is still available")

    write_json(messages, output_dir / "messages.json")
    if execution_mode == "integrated":
        write_json(all_tool_messages, output_dir / "tool_messages.json")
    write_text(final_answer.strip() + "\n", output_dir / "final_answer.md")
    memory_save = {"requested": runtime["save_memory"], "status": "not_requested"}
    if status != "success" and runtime["save_memory"] != "none":
        memory_save = {"requested": runtime["save_memory"], "status": "skipped", "reason": status}
    trace = {
        "conversation_id": runtime["conversation_id"],
        "execution_mode": execution_mode,
        "input_type": input_type,
        "input": (
            {"user_input": runtime["user_input"]}
            if input_type == "text"
            else {
                "audio_path": runtime["audio_path"],
                "audio_language": runtime.get("audio_language"),
                "transcript": audio_transcript,
            }
        ),
        "audio_bootstrap": audio_bootstrap if input_type == "audio" else [],
        "tts_output": tts_output,
        "status": status,
        "toolset": runtime["toolset"],
        "max_turns": runtime["max_turns"],
        "tool_rounds_used": tool_rounds,
        "output_tool_rounds_used": output_tool_rounds,
        "llm_call_count": llm_calls,
        "turns": turns,
        "final_answer_path": "final_answer.md",
        "memory_save": memory_save,
        "agent_execution_mode": plan_mode,
        "execution_plan": execution_plan,
        "planning_latency_ms": planning_latency_ms if planning_llm_calls else None,
        "planning_llm_calls": planning_llm_calls,
        "planning_events": planning_events,
        "warnings": warnings,
        "error": terminal_error,
    }
    write_json(trace, output_dir / "trace.json")

    saved_memory = None
    if execution_mode == "integrated" and runtime["save_memory"] != "none" and trace["status"] == "success":
        try:
            from b5_memory import save_memory

            saved_memory = save_memory(
                str(memory_file),
                runtime["conversation_id"],
                runtime["save_memory"],
                str(output_dir / "messages.json"),
                str(output_dir / "trace.json"),
                str(output_dir / "final_answer.md"),
                str(output_dir),
            )
            trace["memory_save"] = {"requested": runtime["save_memory"], "status": "success"}
        except Exception as exc:
            trace["memory_save"] = {
                "requested": runtime["save_memory"],
                "status": "error",
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
            trace["warnings"].append("memory save failed")
            if trace["status"] == "success":
                trace["status"] = "partial"
        write_json(trace, output_dir / "trace.json")

    result = {
        "conversation_id": runtime["conversation_id"],
        "execution_mode": execution_mode,
        "input_type": input_type,
        "status": trace["status"],
        "final_answer": final_answer,
        "messages_path": str(output_dir / "messages.json"),
        "trace_path": str(output_dir / "trace.json"),
        "final_answer_path": str(output_dir / "final_answer.md"),
        "tts_output": tts_output,
        "final_audio_path": (
            tts_output.get("audio_path")
            if tts_output.get("status") == "success"
            else None
        ),
        "selected_memory": selected_memory,
        "saved_memory": saved_memory,
        "elapsed_ms": round((perf_counter() - started) * 1000, 3),
    }
    if execution_mode == "integrated":
        append_jsonl(
            {
                "timestamp": now_iso(),
                "conversation_id": runtime["conversation_id"],
                "execution_mode": execution_mode,
                "input_type": input_type,
                "status": trace["status"],
                "llm_mode": mode,
                "tool_rounds_used": tool_rounds,
                "llm_call_count": llm_calls,
                "elapsed_ms": result["elapsed_ms"],
            },
            output_dir / "runtime_log.jsonl",
        )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local Agent message and tool loop.")
    # 单任务模式参数（非必须，若提供 --batch 则忽略）
    parser.add_argument("--input", help="Single task input JSON file path")
    parser.add_argument("--outdir", help="Output directory for single task mode")
    parser.add_argument("--tools_config", help="Path to tools.yaml (integrated mode)")
    parser.add_argument("--memory_config", help="Path to memory.yaml (integrated mode)")
    parser.add_argument("--model_config", help="Path to model.yaml (integrated mode)")
    parser.add_argument("--llm_mode", choices=["mock", "prompt_json"], default=None,
                        help="LLM mode (integrated mode)")
    # 批量任务参数
    parser.add_argument("--batch", help="Batch input file (JSONL), each line: {\"input\": \"...\", \"outdir\": \"...\"}")
    parser.add_argument("--interactive", action="store_true", help="Enable multi-turn interactive mode")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # ==================== 批量模式 ====================
    if args.batch:
        batch_file = Path(args.batch).resolve()
        if not batch_file.exists():
            print(f"fatal: batch file not found: {batch_file}", file=sys.stderr)
            return 1

        try:
            with open(batch_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as e:
            print(f"fatal: failed to read batch file: {e}", file=sys.stderr)
            return 1

        total = len([l for l in lines if l.strip()])
        print(f"[Batch] 开始批量运行，共 {total} 个任务")
        success_count = 0
        fail_count = 0

        for idx, line in enumerate(lines, 1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                input_path = item.get("input")
                outdir = item.get("outdir")
                if not input_path or not outdir:
                    print(f"[Batch] 警告：第 {idx} 行缺少 'input' 或 'outdir'，跳过")
                    fail_count += 1
                    continue

                input_abs = Path(input_path).resolve()
                outdir_abs = Path(outdir).resolve()

                print(f"[Batch] 正在执行任务 {idx}/{total}: {input_abs} -> {outdir_abs}")

                result = run_agent(
                    input_path=str(input_abs),
                    tools_config=None,
                    memory_config=None,
                    model_config=None,
                    outdir=str(outdir_abs),
                    llm_mode=None,
                )
                print(f"[Batch] 任务 {idx} 完成 ✓ (status: {result.get('status')})")
                success_count += 1
            except json.JSONDecodeError as e:
                print(f"[Batch] 第 {idx} 行 JSON 解析失败: {e}", file=sys.stderr)
                fail_count += 1
            except Exception as e:
                print(f"[Batch] 第 {idx} 行执行失败: {type(e).__name__}: {e}", file=sys.stderr)
                fail_count += 1

        print(f"[Batch] 全部任务执行完毕。成功: {success_count}, 失败: {fail_count}")
        return 0 if fail_count == 0 else 1

    # ==================== 交互式多轮模式（新增） ====================
    if args.interactive:
        if not args.input or not args.outdir:
            print("fatal: interactive mode requires --input and --outdir", file=sys.stderr)
            return 1

        base_input_file = Path(args.input).resolve()
        base_runtime = read_json(base_input_file)
        abs_system_prompt = str(resolve_from_file(base_runtime["system_prompt_path"], base_input_file))
        original_system_prompt = abs_system_prompt 
        output_base = Path(args.outdir).resolve()

        # 定义可切换的 prompt 模板（相对 base_input_file 所在目录）
        PROMPT_SWITCH_MAP = {
            "@creative": "../prompts/creative_system.txt",
            "@concise": "../prompts/concise_system.txt",
            "@default": None,   # 特殊处理，切回原始 prompt
        }

        print("[Interactive] 多轮对话模式启动，输入 'exit' 或 'quit' 退出")
        print("[Interactive] 提示：输入 @creative 切换创意模式，@concise 切换简洁模式")

        turn = 1
        memory_id = None

        while True:
            user_input = input("\n你: ").strip()
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit", "q"):
                print("[Interactive] 退出对话")
                break

            # ------------- prompt 切换 -------------
            if user_input in PROMPT_SWITCH_MAP:
                if user_input == "@default":
                    abs_system_prompt = original_system_prompt
                    print("[Interactive] 已切换回默认 system prompt。")
                else:
                    rel_path = PROMPT_SWITCH_MAP[user_input]
                    # 基于 base_input_file 的目录解析相对路径
                    new_prompt_path = (base_input_file.parent / rel_path).resolve()
                    if new_prompt_path.is_file():
                        abs_system_prompt = str(new_prompt_path)
                        print(f"[Interactive] 已切换到 {user_input} 模式，加载：{new_prompt_path}")
                    else:
                        print(f"[Interactive] 错误：找不到 prompt 文件 {new_prompt_path}，保持当前 prompt。")
                continue   # 切换完毕，不调用 Agent
            # ---------------------------------------

            # 1. 构造当前轮的 runtime 配置
            runtime = deepcopy(base_runtime)
            runtime["conversation_id"] = f"{base_runtime['conversation_id']}_turn_{turn:03d}"
            runtime["user_input"] = user_input
            runtime["save_memory"] = "conversation"  # 强制保存记忆，便于下一轮加载
            runtime["system_prompt_path"] = abs_system_prompt  # 👈 加这一行！

            # 2. 如果有上一轮的记忆 ID，加载它作为上下文
            if memory_id:
                runtime["selected_memory_ids"] = [memory_id]
                runtime["use_global_memory"] = False
            else:
                runtime["selected_memory_ids"] = []
                runtime["use_global_memory"] = False

            # 3. 写入临时输入文件
            temp_input = output_base / f"temp_input_turn_{turn:03d}.json"
            write_json(runtime, temp_input)

            # 4. 本轮输出目录
            turn_outdir = output_base / f"turn_{turn:03d}"

            # 5. 运行 Agent
            try:
                result = run_agent(
                    input_path=str(temp_input),
                    tools_config=str(resolve_cli_path(args.tools_config)) if args.tools_config else None,
                    memory_config=str(resolve_cli_path(args.memory_config)) if args.memory_config else None,
                    model_config=str(resolve_cli_path(args.model_config)) if args.model_config else None,
                    outdir=str(turn_outdir),
                    llm_mode=args.llm_mode,
                )
                print(f"[Interactive] 第 {turn} 轮完成，状态: {result.get('status')}")

                # 6. 读取本轮保存的记忆 ID（从 saved_memory.json）
                saved_memory_path = turn_outdir / "saved_memory.json"
                if saved_memory_path.exists():
                    saved = read_json(saved_memory_path)
                    memory_id = saved.get("memory_id")
                    print(f"[Interactive] 记忆已保存，ID: {memory_id}")
                else:
                    print("[Interactive] 警告：本轮没有保存记忆，下一轮将无法继承上下文")

                # 7. 显示 Agent 回答
                answer_path = turn_outdir / "final_answer.md"
                if answer_path.exists():
                    answer = read_text(answer_path)
                    print(f"\nAgent: {answer.strip()}")

                turn += 1

            except Exception as e:
                print(f"[Interactive] 第 {turn} 轮执行失败: {type(e).__name__}: {e}")
                # 失败后仍可继续下一轮
                turn += 1
                continue

            # 清理临时输入文件（可选）
            try:
                temp_input.unlink()
            except Exception:
                pass

        print("[Interactive] 对话结束，所有轮次输出保存在:", output_base)
        return 0

    # ==================== 单任务模式 ====================
    if not args.input or not args.outdir:
        print("fatal: single task mode requires --input and --outdir (or use --batch)", file=sys.stderr)
        return 1

    try:
        result = run_agent(
            str(resolve_cli_path(args.input)),
            str(resolve_cli_path(args.tools_config)) if args.tools_config else None,
            str(resolve_cli_path(args.memory_config)) if args.memory_config else None,
            str(resolve_cli_path(args.model_config)) if args.model_config else None,
            str(resolve_cli_path(args.outdir)),
            args.llm_mode,
        )
        print(result["final_answer_path"])
        return 0
    except Exception as exc:
        print(f"fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
