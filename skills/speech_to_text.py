from __future__ import annotations

from pathlib import Path
from time import perf_counter
from typing import Any

import soundfile as sf


# 当前文件：
# assignment_B/agent/skills/speech_to_text.py
#
# 自动定位到：
# assignment_B/faster-whisper-large-v3
SKILLS_DIR = Path(__file__).resolve().parent
AGENT_ROOT = SKILLS_DIR.parent
ASSIGNMENT_ROOT = AGENT_ROOT.parent

DEFAULT_OUTPUTS_ROOT = AGENT_ROOT / "outputs"
DEFAULT_ASR_MODEL_PATH = ASSIGNMENT_ROOT / "faster-whisper-large-v3"

SUPPORTED_AUDIO_SUFFIXES = {
    ".wav",
    ".mp3",
    ".flac",
    ".m4a",
    ".ogg",
    ".aac",
    ".webm",
}

_MODEL_CACHE: dict[tuple[str, str, str, str | None, bool], Any] = {}


class SpeechToTextError(RuntimeError):
    """结构化 ASR 错误，B2 包装层可读取 code。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise SpeechToTextError(code, message)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_audio_path(
    audio_path: str,
    data_root: str | None,
    output_dir: str | None,
) -> tuple[Path, str]:
    """
    允许读取：
    1. agent/data/ 下原始音频；
    2. agent/outputs/ 下 audio_preprocess 生成的处理后音频。
    """
    _require(
        isinstance(audio_path, str) and audio_path.strip(),
        "ASR_INVALID_PATH",
        "audio_path must be a non-empty string",
    )

    data_root_path = (
        Path(data_root).resolve()
        if data_root
        else (AGENT_ROOT / "data").resolve()
    )
    outputs_root = DEFAULT_OUTPUTS_ROOT.resolve()
    run_output_dir = Path(output_dir).resolve() if output_dir else None

    raw = Path(audio_path).expanduser()
    candidate = raw.resolve() if raw.is_absolute() else (data_root_path / raw).resolve()

    permitted_roots = [data_root_path, outputs_root]
    if run_output_dir is not None:
        permitted_roots.append(run_output_dir)

    _require(
        any(_is_within(candidate, root) for root in permitted_roots),
        "ASR_PATH_OUTSIDE_ALLOWED_ROOT",
        "audio_path must be under the configured data directory or this Agent project's outputs directory",
    )

    _require(
        candidate.is_file(),
        "ASR_AUDIO_FILE_NOT_FOUND",
        f"audio file not found: {audio_path}",
    )

    _require(
        candidate.suffix.lower() in SUPPORTED_AUDIO_SUFFIXES,
        "ASR_UNSUPPORTED_FORMAT",
        f"unsupported audio suffix: {candidate.suffix}; "
        f"supported: {sorted(SUPPORTED_AUDIO_SUFFIXES)}",
    )

    if _is_within(candidate, data_root_path):
        return candidate, candidate.relative_to(data_root_path).as_posix()

    return candidate, str(candidate)


def _resolve_local_model_path(model_size_or_path: str) -> str:
    """
    解析本地 faster-whisper CTranslate2 模型目录。

    支持两种相对路径：
    1. 相对当前终端工作目录；
    2. 相对 assignment_B 根目录。
    """
    _require(
        isinstance(model_size_or_path, str) and model_size_or_path.strip(),
        "ASR_INVALID_MODEL",
        "model_size_or_path must be a non-empty local model directory",
    )

    raw = Path(model_size_or_path).expanduser()

    if raw.is_absolute():
        candidates = [raw.resolve()]
    else:
        candidates = [
            raw.resolve(),
            (ASSIGNMENT_ROOT / raw).resolve(),
        ]

    model_dir = next(
        (path for path in candidates if path.exists()),
        candidates[0],
    )

    _require(
        model_dir.is_dir(),
        "ASR_LOCAL_MODEL_NOT_FOUND",
        f"local faster-whisper model directory not found: {model_dir}",
    )

    required_files = [
        "model.bin",
        "config.json",
        "tokenizer.json",
    ]

    missing_files = [
        name for name in required_files
        if not (model_dir / name).is_file()
    ]

    _require(
        not missing_files,
        "ASR_LOCAL_MODEL_INVALID",
        "local model directory is not a valid faster-whisper CTranslate2 model; "
        f"missing: {', '.join(missing_files)}. "
        "Expected files include model.bin, config.json, tokenizer.json.",
    )

    return str(model_dir)


def _choose_runtime(device: str, compute_type: str) -> tuple[str, str]:
    _require(
        device in {"auto", "cuda", "cpu"},
        "ASR_INVALID_DEVICE",
        "device must be auto, cuda, or cpu",
    )

    _require(
        isinstance(compute_type, str) and compute_type.strip(),
        "ASR_INVALID_COMPUTE_TYPE",
        "compute_type must be a non-empty string",
    )

    try:
        import ctranslate2

        cuda_available = ctranslate2.get_cuda_device_count() > 0
    except Exception:
        cuda_available = False

    if device == "auto":
        resolved_device = "cuda" if cuda_available else "cpu"
    else:
        resolved_device = device

    if resolved_device == "cuda" and not cuda_available:
        raise SpeechToTextError(
            "ASR_GPU_UNAVAILABLE",
            "CUDA was requested but CTranslate2 cannot detect a compatible CUDA device. "
            "Use device='cpu' or repair the ASR environment.",
        )

    if compute_type == "auto":
        resolved_compute_type = (
            "int8_float16"
            if resolved_device == "cuda"
            else "int8"
        )
    else:
        resolved_compute_type = compute_type

    return resolved_device, resolved_compute_type


def _load_model(
    model_size_or_path: str,
    device: str,
    compute_type: str,
    download_root: str | None,
    local_files_only: bool,
) -> tuple[Any, bool, float, str]:
    """
    加载模型并做进程内缓存。
    当前默认 local_files_only=True，因此不会联网下载。
    """
    if local_files_only:
        resolved_model_path = _resolve_local_model_path(model_size_or_path)
    else:
        _require(
            isinstance(model_size_or_path, str) and model_size_or_path.strip(),
            "ASR_INVALID_MODEL",
            "model_size_or_path must be a non-empty model name or local directory",
        )
        resolved_model_path = model_size_or_path.strip()

    cache_key = (
        resolved_model_path,
        device,
        compute_type,
        download_root,
        local_files_only,
    )

    if cache_key in _MODEL_CACHE:
        return _MODEL_CACHE[cache_key], True, 0.0, resolved_model_path

    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        raise SpeechToTextError(
            "ASR_DEPENDENCY_MISSING",
            f"cannot import faster_whisper: {exc}. "
            "Install it in the active Agent environment.",
        ) from exc

    started = perf_counter()

    try:
        model = WhisperModel(
            resolved_model_path,
            device=device,
            compute_type=compute_type,
            download_root=download_root,
            local_files_only=local_files_only,
        )
    except Exception as exc:
        if local_files_only:
            hint = (
                " Check that the local path is correct and that it is a "
                "CTranslate2 faster-whisper model directory containing "
                "model.bin, config.json, and tokenizer.json."
            )
        else:
            hint = " The first run may require model download."

        raise SpeechToTextError(
            "ASR_MODEL_LOAD_FAILED",
            f"cannot load ASR model '{resolved_model_path}': {exc}.{hint}",
        ) from exc

    elapsed_ms = round((perf_counter() - started) * 1000, 3)

    _MODEL_CACHE[cache_key] = model

    return model, False, elapsed_ms, resolved_model_path


def _duration_seconds(audio_file: Path) -> float | None:
    try:
        return round(float(sf.info(str(audio_file)).duration), 3)
    except Exception:
        return None


def speech_to_text(
    audio_path: str,
    language: str | None = None,
    task: str = "transcribe",
    model_size_or_path: str = str(DEFAULT_ASR_MODEL_PATH),
    device: str = "auto",
    compute_type: str = "auto",
    beam_size: int = 5,
    vad_filter: bool = False,
    max_duration_sec: float = 300.0,
    include_segments: bool = True,
    max_segments: int = 200,
    local_files_only: bool = True,
    download_root: str | None = None,
    *,
    data_root: str | None = None,
    output_dir: str | None = None,
) -> dict:
    """
    使用本地 faster-whisper 模型识别单条音频。

    默认模型目录：
    assignment_B/faster-whisper-large-v3

    audio_path 可指向：
    - agent/data/ 下原始音频；
    - audio_preprocess 输出的绝对路径。

    模型只从本地读取，且同一 Agent 进程内会进行缓存。
    """
    _require(
        task in {"transcribe", "translate"},
        "ASR_INVALID_TASK",
        "task must be transcribe or translate",
    )

    _require(
        isinstance(beam_size, int)
        and not isinstance(beam_size, bool)
        and 1 <= beam_size <= 10,
        "ASR_INVALID_ARGUMENT",
        "beam_size must be an integer from 1 to 10",
    )

    _require(
        isinstance(vad_filter, bool),
        "ASR_INVALID_ARGUMENT",
        "vad_filter must be boolean",
    )

    _require(
        isinstance(include_segments, bool),
        "ASR_INVALID_ARGUMENT",
        "include_segments must be boolean",
    )

    _require(
        isinstance(max_segments, int)
        and not isinstance(max_segments, bool)
        and 1 <= max_segments <= 1000,
        "ASR_INVALID_ARGUMENT",
        "max_segments must be an integer from 1 to 1000",
    )

    _require(
        isinstance(local_files_only, bool),
        "ASR_INVALID_ARGUMENT",
        "local_files_only must be boolean",
    )

    _require(
        isinstance(max_duration_sec, (int, float))
        and not isinstance(max_duration_sec, bool)
        and 0 < float(max_duration_sec) <= 3600,
        "ASR_INVALID_ARGUMENT",
        "max_duration_sec must be in (0, 3600]",
    )

    if language is not None:
        _require(
            isinstance(language, str) and language.strip(),
            "ASR_INVALID_LANGUAGE",
            "language must be a non-empty language code when provided",
        )

    if download_root is not None:
        _require(
            isinstance(download_root, str) and download_root.strip(),
            "ASR_INVALID_ARGUMENT",
            "download_root must be a non-empty path when provided",
        )

    source, source_display = _resolve_audio_path(
        audio_path,
        data_root,
        output_dir,
    )

    duration_sec = _duration_seconds(source)

    if duration_sec is not None and duration_sec > float(max_duration_sec):
        raise SpeechToTextError(
            "ASR_DURATION_LIMIT_EXCEEDED",
            f"audio duration {duration_sec:.3f}s exceeds limit "
            f"{float(max_duration_sec):.3f}s",
        )

    resolved_device, resolved_compute_type = _choose_runtime(
        device,
        compute_type,
    )

    model, model_cache_hit, model_load_ms, resolved_model_path = _load_model(
        model_size_or_path=model_size_or_path,
        device=resolved_device,
        compute_type=resolved_compute_type,
        download_root=download_root,
        local_files_only=local_files_only,
    )

    transcribe_kwargs: dict[str, Any] = {
        "beam_size": beam_size,
        "task": task,
        "vad_filter": vad_filter,
        "condition_on_previous_text": False,
    }

    if language is not None:
        transcribe_kwargs["language"] = language.strip()

    started = perf_counter()

    try:
        segment_iterator, info = model.transcribe(
            str(source),
            **transcribe_kwargs,
        )

        segments = []
        transcript_parts = []
        total_segments = 0
        truncated_segments = False

        for segment in segment_iterator:
            total_segments += 1

            text = (segment.text or "").strip()

            if text:
                transcript_parts.append(text)

            if include_segments and len(segments) < max_segments:
                segments.append(
                    {
                        "start_sec": round(float(segment.start), 3),
                        "end_sec": round(float(segment.end), 3),
                        "text": text,
                    }
                )
            elif include_segments:
                truncated_segments = True

    except SpeechToTextError:
        raise

    except Exception as exc:
        raise SpeechToTextError(
            "ASR_INFERENCE_FAILED",
            f"ASR inference failed: {exc}",
        ) from exc

    inference_ms = round((perf_counter() - started) * 1000, 3)

    transcript = " ".join(
        part for part in transcript_parts if part
    ).strip()

    if not transcript:
        raise SpeechToTextError(
            "ASR_EMPTY_TRANSCRIPT",
            "ASR returned an empty transcript",
        )

    resolved_language = getattr(info, "language", None)
    language_probability = getattr(info, "language_probability", None)

    rtf = None
    if duration_sec and duration_sec > 0:
        rtf = round((inference_ms / 1000) / duration_sec, 4)

    return {
        "source": source_display,
        "text": transcript,
        "language": resolved_language,
        "language_probability": (
            round(float(language_probability), 6)
            if language_probability is not None
            else None
        ),
        "task": task,
        "audio_duration_sec": duration_sec,
        "segments": segments if include_segments else [],
        "segment_count": total_segments,
        "segments_truncated": truncated_segments,
        "model": {
            "model_size_or_path": resolved_model_path,
            "device": resolved_device,
            "compute_type": resolved_compute_type,
            "local_files_only": local_files_only,
            "cache_hit": model_cache_hit,
            "load_latency_ms": model_load_ms,
        },
        "inference": {
            "latency_ms": inference_ms,
            "rtf": rtf,
            "beam_size": beam_size,
            "vad_filter": vad_filter,
        },
    }