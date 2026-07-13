from __future__ import annotations

import argparse
import json
import sys
import traceback
import uuid
from pathlib import Path
from time import perf_counter
from typing import Any

TTS_EVENT_PREFIX = "__TTS_WORKER_EVENT__:"


def _write_response(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _require(value: bool, message: str) -> None:
    if not value:
        raise ValueError(message)


_MODEL_CACHE: dict[tuple[str, bool, bool, bool], Any] = {}


def _get_model(repo: Path, model_dir: Path, settings: dict[str, Any]) -> tuple[Any, float, bool]:
    key = (
        str(model_dir),
        bool(settings.get("load_jit", False)),
        bool(settings.get("fp16", False)),
        bool(settings.get("disable_text_frontend", False)),
    )
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key], 0.0, True
    repo_text = str(repo)
    matcha_text = str(repo / "third_party" / "Matcha-TTS")
    if repo_text not in sys.path:
        sys.path.insert(0, repo_text)
    if matcha_text not in sys.path:
        sys.path.insert(0, matcha_text)
    # WeText lazily downloads its normalizer model from ModelScope.  On an
    # offline inference server, make the optional import fail so CosyVoice
    # falls back to its built-in lightweight normalization instead of waiting
    # through repeated 60-second network timeouts.
    if settings.get("disable_text_frontend", False):
        sys.modules["wetext"] = None
    from cosyvoice.cli.cosyvoice import AutoModel
    started = perf_counter()
    model = AutoModel(model_dir=str(model_dir), load_jit=key[1], fp16=key[2])
    elapsed = round((perf_counter() - started) * 1000, 3)
    _MODEL_CACHE[key] = model
    return model, elapsed, False


def _synthesize_one(cosyvoice: Any, language: str, text: str, prompt_text: str, prompt_wav: Path):
    if language == "zh":
        return cosyvoice.inference_zero_shot(
            text,
            prompt_text,
            str(prompt_wav),
            stream=False,
        )
    return cosyvoice.inference_cross_lingual(
        f"<|en|>{text}",
        str(prompt_wav),
        stream=False,
    )


def _synthesize(request: dict[str, Any], event_callback=None) -> dict[str, Any]:
    settings = request.get("settings")
    _require(isinstance(settings, dict), "request.settings must be an object")
    chunks = request.get("chunks")
    _require(isinstance(chunks, list) and chunks and all(isinstance(item, str) and item.strip() for item in chunks), "request.chunks must be a non-empty string array")

    language = request.get("language")
    _require(language in {"zh", "en"}, "request.language must be zh or en")
    output_path = Path(request.get("output_path", "")).expanduser().resolve()
    _require(output_path.suffix.lower() == ".wav", "request.output_path must end with .wav")

    repo = Path(str(settings.get("cosyvoice_repo", ""))).expanduser().resolve()
    model_dir = Path(str(settings.get("model_dir", ""))).expanduser().resolve()
    prompt_wav = Path(str(settings.get("prompt_wav", ""))).expanduser().resolve()
    prompt_text = settings.get("prompt_text")
    silence_sec = settings.get("inter_chunk_silence_sec", 0.18)

    _require(repo.is_dir(), f"CosyVoice repo not found: {repo}")
    _require(model_dir.is_dir(), f"CosyVoice model not found: {model_dir}")
    _require(prompt_wav.is_file(), f"CosyVoice prompt WAV not found: {prompt_wav}")
    _require(isinstance(prompt_text, str) and prompt_text.strip(), "prompt_text must be a non-empty string")
    _require(isinstance(silence_sec, (int, float)) and 0 <= float(silence_sec) <= 3, "inter_chunk_silence_sec must be between 0 and 3")

    import torch
    import torchaudio
    cosyvoice, model_load_latency_ms, model_cache_hit = _get_model(repo, model_dir, settings)
    sample_rate = int(cosyvoice.sample_rate)

    synth_started = perf_counter()
    pieces = []
    chunk_outputs = []
    stream = bool(request.get("stream"))
    chunk_dir = Path(request.get("chunk_dir") or output_path.with_name(f"{output_path.stem}_chunks")).expanduser().resolve()
    if stream:
        chunk_dir.mkdir(parents=True, exist_ok=True)

    for index, text in enumerate(chunks):
        chunk_started = perf_counter()
        generator = _synthesize_one(cosyvoice, language, text, prompt_text, prompt_wav)
        chunk_audio = [item["tts_speech"] for item in generator]
        _require(bool(chunk_audio), f"CosyVoice returned no audio for chunk {index + 1}")
        audio = torch.cat(chunk_audio, dim=1)
        pieces.append(audio)
        chunk_duration_sec = round(float(audio.shape[1]) / sample_rate, 3)
        chunk_latency_ms = round((perf_counter() - chunk_started) * 1000, 3)

        if stream:
            chunk_path = chunk_dir / f"{output_path.stem}_chunk_{index + 1:03d}.wav"
            torchaudio.save(str(chunk_path), audio.detach().cpu(), sample_rate)
            event = {
                "type": "audio_chunk",
                "index": index + 1,
                "chunk_count": len(chunks),
                "text": text,
                "audio_path": str(chunk_path),
                "sample_rate": sample_rate,
                "duration_sec": chunk_duration_sec,
                "latency_ms": chunk_latency_ms,
                "model_cache_hit": model_cache_hit,
            }
            chunk_outputs.append(event)
            if event_callback is not None:
                event_callback(event)

        if index < len(chunks) - 1 and float(silence_sec) > 0:
            silence_samples = int(round(sample_rate * float(silence_sec)))
            pieces.append(torch.zeros((audio.shape[0], silence_samples), dtype=audio.dtype, device=audio.device))

    merged = torch.cat(pieces, dim=1).detach().cpu()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not stream:
        torchaudio.save(str(output_path), merged, sample_rate)
    synthesis_latency_ms = round((perf_counter() - synth_started) * 1000, 3)
    duration_sec = round(float(merged.shape[1]) / sample_rate, 3)

    return {
        "audio_path": None if stream else str(output_path),
        "chunk_dir": str(chunk_dir) if stream else None,
        "sample_rate": sample_rate,
        "duration_sec": duration_sec,
        "chunk_count": len(chunks),
        "chunks": chunk_outputs if stream else None,
        "model": {
            "name": "CosyVoice2-0.5B",
            "model_dir": str(model_dir),
            "model_load_latency_ms": model_load_latency_ms,
            "model_cache_hit": model_cache_hit,
            "synthesis_latency_ms": synthesis_latency_ms,
        },
    }


def _run_request(request_path: Path, response_path: Path) -> int:
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        if not isinstance(request, dict):
            raise ValueError("request JSON must be an object")
        event_callback = None
        if request.get("stream"):
            def emit(event: dict[str, Any]) -> None:
                print(
                    TTS_EVENT_PREFIX + "oneshot:" + json.dumps(event, ensure_ascii=False),
                    flush=True,
                )
            event_callback = emit
        output = _synthesize(request, event_callback=event_callback)
        _write_response(response_path, {"status": "success", "output": output, "error": None})
        return 0
    except Exception as exc:
        _write_response(response_path, {"status": "error", "output": None, "error": {
            "type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc(limit=5)}})
        return 1


def _serve() -> int:
    print("__TTS_WORKER_READY__", flush=True)
    for line in sys.stdin:
        request_id = "unknown"
        status = 1
        try:
            command = json.loads(line)
            request_id = str(command.get("id") or uuid.uuid4().hex)
            if command.get("command") == "shutdown":
                status = 0
                return 0
            request_path = Path(command["request"]).resolve()
            response_path = Path(command["response"]).resolve()
            request = json.loads(request_path.read_text(encoding="utf-8"))
            if isinstance(request, dict) and request.get("stream"):
                def emit(event: dict[str, Any]) -> None:
                    print(
                        TTS_EVENT_PREFIX + request_id + ":" + json.dumps(event, ensure_ascii=False),
                        flush=True,
                    )

                try:
                    output = _synthesize(request, event_callback=emit)
                    _write_response(response_path, {"status": "success", "output": output, "error": None})
                    status = 0
                except Exception as exc:
                    _write_response(response_path, {"status": "error", "output": None, "error": {
                        "type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc(limit=5)}})
                    status = 1
            else:
                status = _run_request(request_path, response_path)
        finally:
            print(f"__TTS_WORKER_DONE__:{request_id}:{status}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CosyVoice subprocess worker for the B2 TTS Skill.")
    parser.add_argument("--request")
    parser.add_argument("--response")
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args(argv)

    if args.serve:
        return _serve()
    if not args.request or not args.response:
        parser.error("--request and --response are required unless --serve is used")

    request_path = Path(args.request).resolve()
    response_path = Path(args.response).resolve()
    return _run_request(request_path, response_path)


if __name__ == "__main__":
    raise SystemExit(main())
