from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
import atexit
import threading
from pathlib import Path
from time import perf_counter
from typing import Any

from common.errors import SkillError
from common.io_utils import read_json, read_yaml, write_json
from common.path_utils import PROJECT_ROOT


TTS_CONFIG_PATH = PROJECT_ROOT / "configs" / "tts.yaml"
TTS_WORKER_PATH = Path(__file__).resolve().with_name("tts_worker.py")
SUPPORTED_LANGUAGES = {"auto", "zh", "en"}
TTS_EVENT_PREFIX = "__TTS_WORKER_EVENT__:"
_WORKER_LOCK = threading.Lock()
_WORKER_PROCESS: subprocess.Popen[str] | None = None
_WORKER_KEY: tuple[str, str] | None = None


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise SkillError(code, message)


def _nonempty_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _resolve_setting(
    configured: Any,
    environment_name: str,
    default: str | None = None,
) -> str | None:
    return (
        _nonempty_string(os.environ.get(environment_name))
        or _nonempty_string(configured)
        or default
    )


def _resolve_config_path(value: str, *, executable: bool = False) -> str:
    """Resolve configured file paths relative to configs/tts.yaml.

    A bare executable name such as ``python`` is intentionally left untouched so
    subprocess can resolve it from PATH.
    """
    candidate = Path(value).expanduser()
    if executable and candidate.parent == Path("."):
        return value
    if not candidate.is_absolute():
        candidate = TTS_CONFIG_PATH.parent / candidate
    return str(candidate.resolve())


def _load_tts_settings() -> dict[str, Any]:
    _require(
        TTS_CONFIG_PATH.is_file(),
        "TTS-1003",
        f"TTS config file not found: {TTS_CONFIG_PATH}",
    )
    config = read_yaml(TTS_CONFIG_PATH)
    _require(isinstance(config, dict), "TTS-1003", "tts.yaml must contain an object")

    runtime = config.get("runtime") or {}
    voice = config.get("voice") or {}
    _require(isinstance(runtime, dict), "TTS-1003", "tts.yaml.runtime must be an object")
    _require(isinstance(voice, dict), "TTS-1003", "tts.yaml.voice must be an object")

    python_executable = _resolve_setting(
        runtime.get("python_executable"),
        "COSYVOICE_PYTHON",
        sys.executable,
    )
    repo = _resolve_setting(voice.get("cosyvoice_repo"), "COSYVOICE_REPO")
    model_dir = _resolve_setting(voice.get("model_dir"), "COSYVOICE_MODEL")
    prompt_wav = _resolve_setting(voice.get("prompt_wav"), "COSYVOICE_PROMPT_WAV")
    prompt_text = _resolve_setting(
        voice.get("prompt_text"),
        "COSYVOICE_PROMPT_TEXT",
        "希望你以后能够做的比我还好呦。",
    )

    _require(
        python_executable is not None,
        "TTS-1003",
        "missing COSYVOICE_PYTHON / runtime.python_executable",
    )
    _require(
        repo is not None,
        "TTS-1003",
        "missing COSYVOICE_REPO / voice.cosyvoice_repo",
    )
    _require(
        model_dir is not None,
        "TTS-1003",
        "missing COSYVOICE_MODEL / voice.model_dir",
    )

    repo_path = Path(_resolve_config_path(repo))
    model_path = Path(_resolve_config_path(model_dir))
    if prompt_wav:
        prompt_path = Path(_resolve_config_path(prompt_wav))
    else:
        prompt_path = repo_path / "asset" / "zero_shot_prompt.wav"

    _require(repo_path.is_dir(), "TTS-1003", f"CosyVoice repo not found: {repo_path}")
    _require(model_path.is_dir(), "TTS-1003", f"CosyVoice model not found: {model_path}")
    _require(prompt_path.is_file(), "TTS-1003", f"CosyVoice prompt WAV not found: {prompt_path}")

    timeout_sec = runtime.get("timeout_sec", 600)
    max_total_chars = runtime.get("max_total_chars", 1200)
    min_chunk_chars = runtime.get("min_chunk_chars", 20)
    max_chunk_chars = runtime.get("max_chunk_chars", 60)
    hard_max_chunk_chars = runtime.get("hard_max_chunk_chars", 80)
    silence_sec = runtime.get("inter_chunk_silence_sec", 0.18)
    persistent_worker = runtime.get("persistent_worker", False)
    persistent_override = _nonempty_string(os.environ.get("COSYVOICE_PERSISTENT_WORKER"))
    if persistent_override is not None:
        normalized_override = persistent_override.lower()
        _require(
            normalized_override in {"1", "true", "yes", "on", "0", "false", "no", "off"},
            "TTS-1003",
            "COSYVOICE_PERSISTENT_WORKER must be a boolean value",
        )
        persistent_worker = normalized_override in {"1", "true", "yes", "on"}
    load_jit = runtime.get("load_jit", False)
    fp16 = runtime.get("fp16", False)
    disable_text_frontend = runtime.get("disable_text_frontend", False)

    _require(
        isinstance(timeout_sec, int) and not isinstance(timeout_sec, bool) and 30 <= timeout_sec <= 3600,
        "TTS-1003",
        "tts.yaml.runtime.timeout_sec must be an integer from 30 to 3600",
    )
    _require(
        isinstance(max_total_chars, int) and not isinstance(max_total_chars, bool) and 1 <= max_total_chars <= 10000,
        "TTS-1003",
        "tts.yaml.runtime.max_total_chars must be an integer from 1 to 10000",
    )
    _require(
        isinstance(max_chunk_chars, int) and not isinstance(max_chunk_chars, bool) and 20 <= max_chunk_chars <= 1000,
        "TTS-1003",
        "tts.yaml.runtime.max_chunk_chars must be an integer from 20 to 1000",
    )
    _require(
        isinstance(min_chunk_chars, int) and not isinstance(min_chunk_chars, bool) and 1 <= min_chunk_chars <= max_chunk_chars,
        "TTS-1003",
        "tts.yaml.runtime.min_chunk_chars must be an integer from 1 to max_chunk_chars",
    )
    _require(
        isinstance(hard_max_chunk_chars, int)
        and not isinstance(hard_max_chunk_chars, bool)
        and max_chunk_chars <= hard_max_chunk_chars <= 1000,
        "TTS-1003",
        "tts.yaml.runtime.hard_max_chunk_chars must be an integer from max_chunk_chars to 1000",
    )
    _require(
        isinstance(silence_sec, (int, float)) and not isinstance(silence_sec, bool) and 0 <= float(silence_sec) <= 3,
        "TTS-1003",
        "tts.yaml.runtime.inter_chunk_silence_sec must be between 0 and 3",
    )
    _require(isinstance(persistent_worker, bool), "TTS-1003", "tts.yaml.runtime.persistent_worker must be boolean")
    _require(isinstance(load_jit, bool), "TTS-1003", "tts.yaml.runtime.load_jit must be boolean")
    _require(isinstance(fp16, bool), "TTS-1003", "tts.yaml.runtime.fp16 must be boolean")
    _require(
        isinstance(disable_text_frontend, bool),
        "TTS-1003",
        "tts.yaml.runtime.disable_text_frontend must be boolean",
    )

    return {
        "python_executable": _resolve_config_path(python_executable, executable=True),
        "cosyvoice_repo": str(repo_path),
        "model_dir": str(model_path),
        "prompt_wav": str(prompt_path),
        "prompt_text": prompt_text,
        "timeout_sec": timeout_sec,
        "max_total_chars": max_total_chars,
        "min_chunk_chars": min_chunk_chars,
        "max_chunk_chars": max_chunk_chars,
        "hard_max_chunk_chars": hard_max_chunk_chars,
        "inter_chunk_silence_sec": float(silence_sec),
        "persistent_worker": persistent_worker,
        "load_jit": load_jit,
        "fp16": fp16,
        "disable_text_frontend": disable_text_frontend,
    }


def shutdown_tts_worker() -> None:
    global _WORKER_PROCESS, _WORKER_KEY
    with _WORKER_LOCK:
        process, _WORKER_PROCESS, _WORKER_KEY = _WORKER_PROCESS, None, None
        if process is not None and process.poll() is None:
            try:
                process.stdin.write('{"command":"shutdown","id":"shutdown"}\n')
                process.stdin.flush()
                process.wait(timeout=5)
            except Exception:
                process.terminate()


atexit.register(shutdown_tts_worker)


def _get_persistent_worker(settings: dict[str, Any], env: dict[str, str]) -> subprocess.Popen[str]:
    global _WORKER_PROCESS, _WORKER_KEY
    key = (settings["python_executable"], settings["model_dir"])
    if _WORKER_PROCESS is not None and _WORKER_PROCESS.poll() is None and _WORKER_KEY == key:
        return _WORKER_PROCESS
    if _WORKER_PROCESS is not None and _WORKER_PROCESS.poll() is None:
        _WORKER_PROCESS.terminate()
    process = subprocess.Popen(
        [settings["python_executable"], str(TTS_WORKER_PATH), "--serve"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
        text=True, bufsize=1, env=env,
    )
    while True:
        line = process.stdout.readline()
        if not line:
            raise SkillError("TTS-1007", "persistent CosyVoice worker exited during startup")
        if line.strip() == "__TTS_WORKER_READY__":
            break
    _WORKER_PROCESS, _WORKER_KEY = process, key
    return process


def _run_persistent(settings: dict[str, Any], env: dict[str, str], request_path: Path, response_path: Path) -> int:
    with _WORKER_LOCK:
        process = _get_persistent_worker(settings, env)
        request_id = uuid.uuid4().hex
        process.stdin.write(json.dumps({"id": request_id, "request": str(request_path), "response": str(response_path)}) + "\n")
        process.stdin.flush()
        marker = f"__TTS_WORKER_DONE__:{request_id}:"
        while True:
            line = process.stdout.readline()
            if not line:
                raise SkillError("TTS-1007", "persistent CosyVoice worker exited during synthesis")
            if line.strip().startswith(marker):
                return int(line.strip().rsplit(":", 1)[1])


def _run_persistent_stream(
    settings: dict[str, Any],
    env: dict[str, str],
    request_path: Path,
    response_path: Path,
):
    with _WORKER_LOCK:
        process = _get_persistent_worker(settings, env)
        request_id = uuid.uuid4().hex
        process.stdin.write(json.dumps({"id": request_id, "request": str(request_path), "response": str(response_path)}) + "\n")
        process.stdin.flush()
        event_marker = f"{TTS_EVENT_PREFIX}{request_id}:"
        done_marker = f"__TTS_WORKER_DONE__:{request_id}:"
        while True:
            line = process.stdout.readline()
            if not line:
                raise SkillError("TTS-1007", "persistent CosyVoice worker exited during stream synthesis")
            stripped = line.strip()
            if stripped.startswith(event_marker):
                yield json.loads(stripped[len(event_marker):])
            elif stripped.startswith(done_marker):
                returncode = int(stripped.rsplit(":", 1)[1])
                if returncode != 0:
                    response = read_json(response_path) if response_path.is_file() else {}
                    raise SkillError("TTS-1006", _worker_error_message(response, None))
                return


def _detect_language(text: str) -> str:
    # 对最终回答使用保守检测：只要出现中文字符，就选中文 zero-shot 音色合成。
    return "zh" if re.search(r"[\u4e00-\u9fff]", text) else "en"


def _validate_tts_input(text: str, language: str) -> tuple[dict[str, Any], str, str, list[str]]:
    _require(isinstance(text, str) and text.strip(), "TTS-1001", "text must be a non-empty string")
    _require(isinstance(language, str), "TTS-1002", "language must be auto, zh, or en")

    normalized_language = language.strip().lower()
    _require(
        normalized_language in SUPPORTED_LANGUAGES,
        "TTS-1002",
        "language must be auto, zh, or en",
    )

    settings = _load_tts_settings()
    normalized_text = re.sub(r"\s+", " ", text).strip()
    _require(
        len(normalized_text) <= settings["max_total_chars"],
        "TTS-1001",
        f"text exceeds configured max_total_chars={settings['max_total_chars']}",
    )

    resolved_language = _detect_language(normalized_text) if normalized_language == "auto" else normalized_language
    chunks = _split_text(
        normalized_text,
        settings["max_chunk_chars"],
        settings["min_chunk_chars"],
        settings["hard_max_chunk_chars"],
    )
    _require(bool(chunks), "TTS-1001", "text contains no speakable content")
    return settings, normalized_text, resolved_language, chunks


def _worker_settings(settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "cosyvoice_repo": settings["cosyvoice_repo"],
        "model_dir": settings["model_dir"],
        "prompt_wav": settings["prompt_wav"],
        "prompt_text": settings["prompt_text"],
        "inter_chunk_silence_sec": settings["inter_chunk_silence_sec"],
        "load_jit": settings["load_jit"],
        "fp16": settings["fp16"],
        "disable_text_frontend": settings["disable_text_frontend"],
    }


def _worker_env(settings: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "COSYVOICE_REPO": settings["cosyvoice_repo"],
            "COSYVOICE_MODEL": settings["model_dir"],
            "COSYVOICE_PROMPT_WAV": settings["prompt_wav"],
        }
    )
    return env


def _split_text_legacy_unused(
    text: str,
    max_chunk_chars: int,
    min_chunk_chars: int = 20,
    hard_max_chunk_chars: int | None = None,
) -> list[str]:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) <= max_chunk_chars:
        return [normalized]
    hard_limit = hard_max_chunk_chars or max_chunk_chars

    sentences = [
        piece.strip()
        for piece in re.split(r"(?<=[。！？!?；;])\s*|\n+", normalized)
        if piece.strip()
    ]
    soft_breaks = re.compile(r"(?<=[，,、：:])\s*")
    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current:
            chunks.append(current)
            current = ""

    for sentence in sentences:
        pieces = [sentence]
        if len(sentence) > max_chunk_chars:
            pieces = [piece for piece in soft_breaks.split(sentence) if piece]

        expanded: list[str] = []
        for piece in pieces:
            if len(piece) <= hard_limit:
                expanded.append(piece)
            else:
                expanded.extend(piece[start : start + hard_limit] for start in range(0, len(piece), hard_limit))

        for piece in expanded:
            candidate = piece if not current else f"{current} {piece}"
            if len(candidate) <= max_chunk_chars or len(current) < min_chunk_chars:
                current = candidate
            else:
                flush()
                current = piece

    flush()
    return chunks


def _split_text(
    text: str,
    max_chunk_chars: int,
    min_chunk_chars: int = 20,
    hard_max_chunk_chars: int | None = None,
) -> list[str]:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return []

    hard_limit = hard_max_chunk_chars or max_chunk_chars
    sentence_breaks = r"(?<=[\u3002\uff01\uff1f!?；;])\s*|\n+"
    soft_breaks = re.compile(r"(?<=[\uff0c,\u3001\uff1a:])\s*")
    sentences = [piece.strip() for piece in re.split(sentence_breaks, normalized) if piece.strip()]
    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current:
            chunks.append(current)
            current = ""

    for sentence in sentences:
        pieces = [sentence]
        if len(sentence) > max_chunk_chars:
            pieces = [piece.strip() for piece in soft_breaks.split(sentence) if piece.strip()]

        expanded: list[str] = []
        for piece in pieces:
            if len(piece) <= hard_limit:
                expanded.append(piece)
            else:
                expanded.extend(piece[start : start + hard_limit] for start in range(0, len(piece), hard_limit))

        for piece in expanded:
            candidate = piece if not current else f"{current} {piece}"
            if len(candidate) <= max_chunk_chars and len(current) < min_chunk_chars:
                current = candidate
                continue
            if current and len(current) >= min_chunk_chars:
                flush()
                current = piece
            elif len(candidate) <= hard_limit:
                current = candidate
            else:
                flush()
                current = piece

    flush()
    return chunks


def _safe_output_path(output_dir: str | None, output_filename: str | None) -> Path:
    base_dir = Path(output_dir).resolve() if output_dir else (PROJECT_ROOT / "outputs" / "B2_skills").resolve()
    target_dir = base_dir / "text_to_speech"
    raw_name = _nonempty_string(output_filename) or "final_answer.wav"
    safe_name = Path(raw_name).name
    stem = Path(safe_name).stem or "final_answer"
    candidate = target_dir / f"{stem}.wav"
    index = 1
    while candidate.exists():
        candidate = target_dir / f"{stem}({index}).wav"
        index += 1
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


def _worker_error_message(result: dict[str, Any], completed: subprocess.CompletedProcess[str] | None) -> str:
    worker_error = result.get("error") if isinstance(result, dict) else None
    if isinstance(worker_error, dict):
        detail = worker_error.get("message")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
    stderr = (completed.stderr or "").strip() if completed is not None else ""
    return stderr[-2000:] if stderr else "CosyVoice worker failed without an error message"


def text_to_speech(
    text: str,
    language: str = "auto",
    output_filename: str | None = None,
    *,
    output_dir: str | None = None,
) -> dict:
    """Run CosyVoice in its own configured Python environment and return one WAV artifact."""
    settings, _normalized_text, resolved_language, chunks = _validate_tts_input(text, language)
    output_path = _safe_output_path(output_dir, output_filename)
    request_path = output_path.with_name(f".{output_path.stem}_{uuid.uuid4().hex}_request.json")
    response_path = output_path.with_name(f".{output_path.stem}_{uuid.uuid4().hex}_response.json")

    request = {
        "chunks": chunks,
        "language": resolved_language,
        "output_path": str(output_path),
        "settings": _worker_settings(settings),
    }

    write_json(request, request_path)
    env = _worker_env(settings)

    started = perf_counter()
    try:
        try:
            if settings["persistent_worker"]:
                completed = None
                returncode = _run_persistent(settings, env, request_path, response_path)
            else:
                completed = subprocess.run(
                    [settings["python_executable"], str(TTS_WORKER_PATH), "--request", str(request_path), "--response", str(response_path)],
                    capture_output=True, text=True, env=env,
                    timeout=settings["timeout_sec"], check=False,
                )
                returncode = completed.returncode
        except FileNotFoundError as exc:
            raise SkillError(
                "TTS-1004",
                f"CosyVoice Python executable was not found: {settings['python_executable']}",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise SkillError(
                "TTS-1005",
                f"CosyVoice synthesis timed out after {settings['timeout_sec']} seconds",
            ) from exc

        _require(
            response_path.is_file(),
            "TTS-1007",
            "CosyVoice worker did not write a response JSON file",
        )
        response = read_json(response_path)
        _require(isinstance(response, dict), "TTS-1007", "CosyVoice worker response must be an object")

        if returncode != 0 or response.get("status") != "success":
            raise SkillError("TTS-1006", _worker_error_message(response, completed))

        output = response.get("output")
        _require(isinstance(output, dict), "TTS-1007", "CosyVoice worker response has no output object")
        generated_path = output.get("audio_path")
        _require(
            isinstance(generated_path, str) and Path(generated_path).is_file(),
            "TTS-1008",
            "CosyVoice worker reported success but the WAV file does not exist",
        )

        return {
            **output,
            "language": resolved_language,
            "chunk_count": len(chunks),
            "controller_latency_ms": round((perf_counter() - started) * 1000, 3),
            "worker_python": settings["python_executable"],
        }
    finally:
        for temporary in (request_path, response_path):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def stream_text_to_speech(
    text: str,
    language: str = "auto",
    output_filename: str | None = None,
    *,
    output_dir: str | None = None,
):
    """Yield one event per synthesized chunk while keeping the CosyVoice worker loaded."""
    settings, _normalized_text, resolved_language, chunks = _validate_tts_input(text, language)
    output_path = _safe_output_path(output_dir, output_filename)
    request_path = output_path.with_name(f".{output_path.stem}_{uuid.uuid4().hex}_stream_request.json")
    response_path = output_path.with_name(f".{output_path.stem}_{uuid.uuid4().hex}_stream_response.json")
    chunk_dir = output_path.with_name(f"{output_path.stem}_chunks_{uuid.uuid4().hex[:8]}")

    request = {
        "stream": True,
        "chunks": chunks,
        "language": resolved_language,
        "output_path": str(output_path),
        "chunk_dir": str(chunk_dir),
        "settings": _worker_settings(settings),
    }
    write_json(request, request_path)
    env = _worker_env(settings)

    started = perf_counter()
    try:
        if settings["persistent_worker"]:
            event_source = _run_persistent_stream(settings, env, request_path, response_path)
        else:
            process = subprocess.Popen(
                [settings["python_executable"], str(TTS_WORKER_PATH), "--request", str(request_path), "--response", str(response_path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, bufsize=1,
            )

            def one_shot_events():
                assert process.stdout is not None
                request_marker = f"{TTS_EVENT_PREFIX}oneshot:"
                for line in process.stdout:
                    stripped = line.strip()
                    if stripped.startswith(request_marker):
                        payload_text = stripped[len(request_marker):]
                        yield json.loads(payload_text)
                _, stderr = process.communicate(timeout=5)
                if process.returncode != 0:
                    response = read_json(response_path) if response_path.is_file() else {}
                    message = _worker_error_message(response, None) or stderr
                    raise SkillError("TTS-1006", message)
            event_source = one_shot_events()

        for event in event_source:
            event["language"] = resolved_language
            event["controller_latency_ms"] = round((perf_counter() - started) * 1000, 3)
            event["worker_python"] = settings["python_executable"]
            yield event
    finally:
        for temporary in (request_path, response_path):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
