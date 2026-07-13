from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from copy import deepcopy
from pathlib import Path
from time import perf_counter

from b4_local_agent_llm import generate_ai_message
from common.io_utils import read_json, read_text, read_yaml, write_json, write_text
from common.path_utils import resolve_cli_path, resolve_from_file
from common.schemas import make_skill_result, make_tool_message


INJECTION_MODES = ("prompt", "native")


def _write_model_variant(source: Path, injection: str, target: Path) -> None:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required; install requirements.txt") from exc

    config = deepcopy(read_yaml(source))
    model = config.setdefault("model", {})
    for key in ("model_name_or_path", "tokenizer_name_or_path"):
        value = model.get(key)
        if isinstance(value, str) and not Path(value).expanduser().is_absolute():
            model[key] = str(resolve_from_file(value, source))
    config.setdefault("tool_calling", {})["schema_injection"] = injection
    write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), target)


def _required_by_tool(tools_schema: list[dict]) -> dict[str, list[str]]:
    required = {}
    for entry in tools_schema:
        function = entry.get("function") if isinstance(entry, dict) else None
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            continue
        parameters = function.get("parameters") or {}
        required[function["name"]] = list(parameters.get("required") or [])
    return required


def _tool_message(call: dict, status: str, output=None, error=None) -> dict:
    name = call.get("name", "unknown")
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    result = make_skill_result(name, status, args, output, error, 0.0)
    return make_tool_message(
        call.get("id") or "benchmark_call",
        name,
        json.dumps(result, ensure_ascii=False, separators=(",", ":")),
        status,
    )


def _run_case(
    task: dict,
    injection: str,
    repeat: int,
    model_config: Path,
    tools_schema: list[dict],
    system_prompt: str,
    required_by_tool: dict[str, list[str]],
    max_llm_calls: int,
    llm_mode: str,
    output_dir: Path,
) -> dict:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task["user_input"]},
    ]
    expected = task["expected_tools"]
    outputs = task["tool_outputs"]
    actual_calls = []
    required_present = required_total = 0
    expected_index = 0
    reasoning_latency_ms = 0.0
    status = "max_llm_calls"
    final_answer = ""

    for llm_call in range(1, max_llm_calls + 1):
        started = perf_counter()
        result = generate_ai_message(
            str(model_config),
            messages,
            tools_schema,
            llm_mode,
            str(output_dir / "llm_calls"),
            f"call_{llm_call:03d}",
        )
        reasoning_latency_ms += (perf_counter() - started) * 1000
        ai_message = result["ai_message"]
        messages.append(ai_message)
        if result.get("status") != "success":
            status = "llm_error"
            break

        calls = ai_message.get("tool_calls", [])
        if not calls:
            final_answer = ai_message.get("content", "")
            status = "success"
            break

        tool_messages = []
        for call in calls:
            actual_calls.append(call)
            name = call.get("name", "unknown")
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            required = required_by_tool.get(name, [])
            present = sum(key in args and args[key] is not None for key in required)
            required_present += present
            required_total += len(required)

            expected_name = expected[expected_index] if expected_index < len(expected) else None
            missing = [key for key in required if key not in args or args[key] is None]
            if name != expected_name:
                tool_messages.append(
                    _tool_message(
                        call,
                        "error",
                        error={"type": "UnexpectedTool", "message": f"expected {expected_name}, got {name}"},
                    )
                )
            elif missing:
                tool_messages.append(
                    _tool_message(
                        call,
                        "error",
                        error={"type": "MissingArguments", "message": ", ".join(missing)},
                    )
                )
            else:
                tool_messages.append(_tool_message(call, "success", outputs[expected_index], None))
                expected_index += 1
        messages.extend(tool_messages)

    actual_names = [call.get("name", "unknown") for call in actual_calls]
    write_json(messages, output_dir / "messages.json")
    return {
        "schema_injection": injection,
        "task_id": task["id"],
        "repeat": repeat,
        "status": status,
        "expected_tools": json.dumps(expected, ensure_ascii=False),
        "actual_tools": json.dumps(actual_names, ensure_ascii=False),
        "tool_call_correct": int(actual_names == expected),
        "required_args_present": required_present,
        "required_args_total": required_total,
        "tool_call_rounds": sum(
            message.get("role") == "assistant" and bool(message.get("tool_calls"))
            for message in messages
        ),
        "llm_calls": sum(message.get("role") == "assistant" for message in messages),
        "reasoning_latency_ms": round(reasoning_latency_ms, 3),
        "final_answer": final_answer,
    }


def _mean(rows: list[dict], key: str) -> float:
    return round(statistics.fmean(float(row[key]) for row in rows), 3) if rows else 0.0


def _summarize(rows: list[dict]) -> list[dict]:
    summary = []
    for injection in INJECTION_MODES:
        selected = [row for row in rows if row["schema_injection"] == injection]
        required_total = sum(row["required_args_total"] for row in selected)
        latencies = [row["reasoning_latency_ms"] for row in selected]
        summary.append(
            {
                "schema_injection": injection,
                "runs": len(selected),
                "success_rate": round(sum(row["status"] == "success" for row in selected) / len(selected), 4),
                "tool_call_accuracy": round(sum(row["tool_call_correct"] for row in selected) / len(selected), 4),
                "parameter_completeness": round(
                    sum(row["required_args_present"] for row in selected) / required_total, 4
                ) if required_total else 1.0,
                "avg_tool_call_rounds": _mean(selected, "tool_call_rounds"),
                "avg_llm_calls": _mean(selected, "llm_calls"),
                "avg_reasoning_latency_ms": _mean(selected, "reasoning_latency_ms"),
                "median_reasoning_latency_ms": round(statistics.median(latencies), 3),
            }
        )
    return summary


def _pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def _report(summary: list[dict], run_count: int, repeats: int) -> str:
    indexed = {row["schema_injection"]: row for row in summary}
    prompt, native = indexed["prompt"], indexed["native"]
    lines = [
        "# B4 Tools Schema Injection Benchmark",
        "",
        f"- Repeats per task and injection mode: `{repeats}`",
        f"- Measured B4 runs: `{run_count}` (warm-up excluded)",
        "- Tool-call accuracy: exact match of the complete emitted tool-name sequence.",
        "- Parameter completeness: non-null required arguments / required arguments in emitted calls.",
        "- Tool-call rounds: B4 responses containing at least one tool call.",
        "- Reasoning latency: cumulative wall time of B4 `generate_ai_message`; canned tools take no measured time.",
        "",
        "| Injection | Success | Tool accuracy | Parameter completeness | Avg tool rounds | Avg LLM calls | Avg latency ms | Median latency ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary:
        lines.append(
            f"| {row['schema_injection']} | {_pct(row['success_rate'])} | "
            f"{_pct(row['tool_call_accuracy'])} | {_pct(row['parameter_completeness'])} | "
            f"{row['avg_tool_call_rounds']:.3f} | {row['avg_llm_calls']:.3f} | "
            f"{row['avg_reasoning_latency_ms']:.3f} | {row['median_reasoning_latency_ms']:.3f} |"
        )
    lines.extend(
        [
            "",
            "## Native minus prompt",
            "",
            f"- Tool-call accuracy: `{(native['tool_call_accuracy'] - prompt['tool_call_accuracy']) * 100:+.2f}` percentage points",
            f"- Parameter completeness: `{(native['parameter_completeness'] - prompt['parameter_completeness']) * 100:+.2f}` percentage points",
            f"- Average tool-call rounds: `{native['avg_tool_call_rounds'] - prompt['avg_tool_call_rounds']:+.3f}`",
            f"- Average reasoning latency: `{native['avg_reasoning_latency_ms'] - prompt['avg_reasoning_latency_ms']:+.3f} ms`",
            "",
            "Positive accuracy/completeness deltas favor native injection; negative rounds/latency deltas favor native injection.",
            "",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="B4-only prompt vs native tools-schema benchmark.")
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--model_config", required=True)
    parser.add_argument("--outdir", required=True)
    parser.add_argument("--repeats", type=int, default=None)
    parser.add_argument("--llm_mode", choices=["mock", "prompt_json"], default="prompt_json")
    parser.add_argument("--skip_warmup", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    benchmark_path = resolve_cli_path(args.benchmark)
    model_path = resolve_cli_path(args.model_config)
    outdir = resolve_cli_path(args.outdir)
    benchmark = read_json(benchmark_path)
    tasks = benchmark.get("tasks")
    repeats = args.repeats if args.repeats is not None else benchmark.get("repeats", 3)
    max_llm_calls = benchmark.get("max_llm_calls", 4)
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("benchmark tasks must be a non-empty array")
    if not isinstance(repeats, int) or repeats < 1:
        raise ValueError("repeats must be a positive integer")

    system_prompt = read_text(resolve_from_file(benchmark["system_prompt_path"], benchmark_path)).strip()
    tools_schema = read_json(resolve_from_file(benchmark["tools_schema_path"], benchmark_path))
    required = _required_by_tool(tools_schema)
    variants = {}
    for injection in INJECTION_MODES:
        variant = outdir / "configs" / f"model_{injection}.yaml"
        _write_model_variant(model_path, injection, variant)
        variants[injection] = variant

    if not args.skip_warmup:
        for injection in INJECTION_MODES:
            print(f"warmup injection={injection}", flush=True)
            _run_case(
                tasks[0], injection, 0, variants[injection], tools_schema,
                system_prompt, required, max_llm_calls, args.llm_mode,
                outdir / "warmup" / injection,
            )

    rows = []
    for repeat in range(1, repeats + 1):
        for task in tasks:
            for injection in INJECTION_MODES:
                print(f"run injection={injection} task={task['id']} repeat={repeat}", flush=True)
                row = _run_case(
                    task, injection, repeat, variants[injection], tools_schema,
                    system_prompt, required, max_llm_calls, args.llm_mode,
                    outdir / "runs" / injection / task["id"] / f"repeat_{repeat:03d}",
                )
                rows.append(row)
                write_json(rows, outdir / "run_results.json")

    summary = _summarize(rows)
    write_json(summary, outdir / "summary.json")
    columns = list(rows[0])
    with (outdir / "run_results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    write_text(_report(summary, len(rows), repeats), outdir / "comparison_report.md")
    print(outdir / "comparison_report.md")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
