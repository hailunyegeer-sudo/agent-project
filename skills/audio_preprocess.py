from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

from skills import resolve_data_path


SUPPORTED_AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aac", ".webm"}
SUPPORTED_EXPORT_FORMATS = {"wav", "mp3", "flac"}
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "outputs" / "audio_preprocess"


class AudioPreprocessError(RuntimeError):
    """Structured audio-skill error. B2's error wrapper may read ``code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise AudioPreprocessError(code, message)


def _safe_output_dir(output_dir: str | None) -> Path:
    root = Path(output_dir).resolve() if output_dir else DEFAULT_OUTPUT_DIR.resolve()
    target = root / "audio_preprocess"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _output_stem(source: Path) -> str:
    digest = hashlib.sha1(str(source.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"{source.stem}_{digest}"


def _audio_info(path: Path) -> dict:
    try:
        info = sf.info(str(path))
    except Exception as exc:
        raise AudioPreprocessError("AUDIO_DECODE_FAILED", f"cannot inspect audio file: {exc}") from exc
    return {
        "sample_rate": int(info.samplerate),
        "channels": int(info.channels),
        "duration_sec": round(float(info.duration), 3),
        "frames": int(info.frames),
        "format": str(info.format),
        "subtype": str(info.subtype),
    }


def _export_with_ffmpeg(source_wav: Path, target: Path) -> None:
    if shutil.which("ffmpeg") is None:
        raise AudioPreprocessError("AUDIO_EXPORT_FAILED", "ffmpeg is not available on PATH")
    completed = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source_wav), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or "ffmpeg conversion failed").strip()
        raise AudioPreprocessError("AUDIO_EXPORT_FAILED", detail)


def _save_diagnostics(waveform: np.ndarray, sample_rate: int, target: Path, title: str) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import librosa.display
    except Exception as exc:
        raise AudioPreprocessError("AUDIO_DIAGNOSTIC_FAILED", f"cannot import plotting dependencies: {exc}") from exc

    if waveform.size == 0:
        raise AudioPreprocessError("AUDIO_EMPTY_AFTER_TRIM", "audio contains no samples after preprocessing")

    stft_db = librosa.amplitude_to_db(
        np.abs(librosa.stft(waveform, n_fft=1024, hop_length=256)), ref=np.max
    )
    mel = librosa.feature.melspectrogram(
        y=waveform, sr=sample_rate, n_fft=1024, hop_length=256, n_mels=80
    )
    mel_db = librosa.power_to_db(mel, ref=np.max)
    mfcc = librosa.feature.mfcc(
        y=waveform, sr=sample_rate, n_fft=1024, hop_length=256, n_mfcc=13
    )

    fig, axes = plt.subplots(4, 1, figsize=(11, 13))
    t = np.arange(waveform.size) / sample_rate
    axes[0].plot(t, waveform, linewidth=0.6)
    axes[0].set_title(f"{title}: waveform")
    axes[0].set_xlabel("Time (s)")
    axes[0].set_ylabel("Amplitude")

    image = librosa.display.specshow(
        stft_db, sr=sample_rate, hop_length=256, x_axis="time", y_axis="hz", ax=axes[1]
    )
    axes[1].set_title("Linear Spectrogram (STFT, dB)")
    fig.colorbar(image, ax=axes[1], format="%+2.0f dB")

    image = librosa.display.specshow(
        mel_db, sr=sample_rate, hop_length=256, x_axis="time", y_axis="mel", ax=axes[2]
    )
    axes[2].set_title("Mel-Spectrogram / FBank (80 mel)")
    fig.colorbar(image, ax=axes[2], format="%+2.0f dB")

    image = librosa.display.specshow(mfcc, sr=sample_rate, hop_length=256, x_axis="time", ax=axes[3])
    axes[3].set_title("MFCC (13 coefficients)")
    fig.colorbar(image, ax=axes[3])

    fig.tight_layout()
    fig.savefig(target, dpi=130)
    plt.close(fig)


def audio_preprocess(
    audio_path: str,
    target_sample_rate: int = 16000,
    trim_silence: bool = True,
    trim_top_db: float = 30.0,
    normalize: bool = True,
    export_formats: list[str] | None = None,
    generate_diagnostics: bool = False,
    max_duration_sec: float = 300.0,
    *,
    data_root: str | None = None,
    output_dir: str | None = None,
) -> dict:
    """Normalize one local audio file into ASR-ready 16kHz mono audio.

    The input path is constrained to B2's configured data root. Outputs are written
    under ``output_dir/audio_preprocess`` so a Skill cannot overwrite source files.
    """
    _require(isinstance(audio_path, str) and audio_path.strip(), "AUDIO_INVALID_PATH", "audio_path must be a non-empty string")
    _require(
        isinstance(target_sample_rate, int) and not isinstance(target_sample_rate, bool) and 8000 <= target_sample_rate <= 48000,
        "AUDIO_INVALID_SAMPLE_RATE",
        "target_sample_rate must be an integer between 8000 and 48000",
    )
    _require(isinstance(trim_silence, bool), "AUDIO_INVALID_ARGUMENT", "trim_silence must be boolean")
    _require(isinstance(normalize, bool), "AUDIO_INVALID_ARGUMENT", "normalize must be boolean")
    _require(
        isinstance(trim_top_db, (int, float)) and not isinstance(trim_top_db, bool) and 1.0 <= float(trim_top_db) <= 100.0,
        "AUDIO_INVALID_ARGUMENT",
        "trim_top_db must be between 1 and 100",
    )
    _require(
        isinstance(max_duration_sec, (int, float)) and not isinstance(max_duration_sec, bool) and 0 < float(max_duration_sec) <= 3600,
        "AUDIO_INVALID_ARGUMENT",
        "max_duration_sec must be in (0, 3600]",
    )
    _require(isinstance(generate_diagnostics, bool), "AUDIO_INVALID_ARGUMENT", "generate_diagnostics must be boolean")

    source, data_root_path = resolve_data_path(audio_path, data_root)
    if not source.is_file():
        raise AudioPreprocessError("AUDIO_FILE_NOT_FOUND", f"audio file not found: {audio_path}")
    if source.suffix.lower() not in SUPPORTED_AUDIO_SUFFIXES:
        raise AudioPreprocessError(
            "AUDIO_UNSUPPORTED_FORMAT",
            f"unsupported audio suffix: {source.suffix}; supported: {sorted(SUPPORTED_AUDIO_SUFFIXES)}",
        )

    original_info = _audio_info(source)
    if original_info["duration_sec"] > float(max_duration_sec):
        raise AudioPreprocessError(
            "AUDIO_DURATION_LIMIT_EXCEEDED",
            f"audio duration {original_info['duration_sec']:.3f}s exceeds limit {float(max_duration_sec):.3f}s",
        )

    try:
        original_waveform, original_sr = librosa.load(str(source), sr=None, mono=False)
    except Exception as exc:
        raise AudioPreprocessError("AUDIO_DECODE_FAILED", f"cannot decode audio: {exc}") from exc

    if original_waveform.size == 0:
        raise AudioPreprocessError("AUDIO_EMPTY_INPUT", "audio file contains no samples")

    original_channels = 1 if original_waveform.ndim == 1 else int(original_waveform.shape[0])
    waveform = librosa.to_mono(original_waveform) if original_waveform.ndim > 1 else original_waveform
    if int(original_sr) != target_sample_rate:
        waveform = librosa.resample(waveform, orig_sr=int(original_sr), target_sr=target_sample_rate)

    samples_before_trim = int(waveform.size)
    if trim_silence:
        waveform, _ = librosa.effects.trim(waveform, top_db=float(trim_top_db))
    if waveform.size == 0:
        raise AudioPreprocessError("AUDIO_EMPTY_AFTER_TRIM", "audio contains no remaining samples after silence trimming")

    peak_before_normalize = float(np.max(np.abs(waveform)))
    if normalize:
        waveform = waveform / (peak_before_normalize + 1e-9)
    waveform = np.asarray(waveform, dtype=np.float32)

    target_dir = _safe_output_dir(output_dir)
    stem = _output_stem(source)
    wav_path = target_dir / f"{stem}_{target_sample_rate}hz_mono.wav"
    try:
        sf.write(str(wav_path), waveform, target_sample_rate, subtype="PCM_16")
    except Exception as exc:
        raise AudioPreprocessError("AUDIO_EXPORT_FAILED", f"cannot write processed wav: {exc}") from exc

    requested_formats = export_formats if export_formats is not None else ["wav"]
    _require(isinstance(requested_formats, list) and requested_formats, "AUDIO_INVALID_ARGUMENT", "export_formats must be a non-empty array")
    normalized_formats: list[str] = []
    for raw_format in requested_formats:
        _require(isinstance(raw_format, str), "AUDIO_INVALID_ARGUMENT", "each export format must be a string")
        fmt = raw_format.strip().lower().lstrip(".")
        _require(fmt in SUPPORTED_EXPORT_FORMATS, "AUDIO_UNSUPPORTED_EXPORT_FORMAT", f"unsupported export format: {raw_format}")
        if fmt not in normalized_formats:
            normalized_formats.append(fmt)

    generated_files: dict[str, str] = {"wav": str(wav_path)}
    for fmt in normalized_formats:
        if fmt == "wav":
            continue
        target_path = target_dir / f"{stem}_{target_sample_rate}hz_mono.{fmt}"
        _export_with_ffmpeg(wav_path, target_path)
        generated_files[fmt] = str(target_path)

    diagnostics_path: str | None = None
    if generate_diagnostics:
        diagnostics = target_dir / f"{stem}_overview.png"
        _save_diagnostics(waveform, target_sample_rate, diagnostics, source.name)
        diagnostics_path = str(diagnostics)

    processed_duration = round(float(waveform.size) / target_sample_rate, 3)
    return {
        "source": source.relative_to(data_root_path).as_posix(),
        "processed_audio_path": str(wav_path),
        "generated_files": generated_files,
        "diagnostics_path": diagnostics_path,
        "original": original_info,
        "processed": {
            "sample_rate": target_sample_rate,
            "channels": 1,
            "duration_sec": processed_duration,
            "frames": int(waveform.size),
            "samples_before_trim": samples_before_trim,
            "samples_after_trim": int(waveform.size),
            "trimmed": bool(trim_silence),
            "normalized": bool(normalize),
            "peak_before_normalize": round(peak_before_normalize, 6),
            "peak_after_normalize": round(float(np.max(np.abs(waveform))), 6),
        },
    }
