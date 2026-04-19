#!/usr/bin/env python3
import argparse
import base64
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from openai import OpenAI

TOOL_VERSION = "0.1.0"
ALLOWED_EXTENSIONS = {".m4a", ".mp3", ".wav"}
API_UPLOAD_LIMIT_BYTES = 25 * 1024 * 1024


class ToolError(Exception):
    pass


@dataclass
class ChunkInfo:
    index: int
    source_start: float
    source_end: float
    duration: float
    path: Path


def run_cmd(cmd: list[str], error_message: str, verbose: bool = False) -> subprocess.CompletedProcess:
    if verbose:
        print("[cmd]", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ToolError(f"{error_message}\nCommand: {' '.join(cmd)}\n{detail}")
    return proc


def ensure_binary(name: str) -> None:
    if shutil.which(name) is None:
        raise ToolError(f"Required tool '{name}' was not found in PATH. Please install ffmpeg/ffprobe.")


def probe_audio(path: Path, verbose: bool = False) -> dict[str, Any]:
    proc = run_cmd(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        "Failed to inspect input audio with ffprobe.",
        verbose=verbose,
    )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ToolError(f"Unable to parse ffprobe output: {exc}") from exc

    audio_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    fmt = data.get("format", {})
    return {
        "duration_seconds": float(fmt.get("duration", 0.0) or 0.0),
        "size_bytes": int(fmt.get("size", path.stat().st_size)),
        "codec": (audio_stream or {}).get("codec_name", "unknown"),
        "sample_rate": int((audio_stream or {}).get("sample_rate", 0) or 0),
        "channels": int((audio_stream or {}).get("channels", 0) or 0),
        "format_name": fmt.get("format_name", "unknown"),
    }


def normalize_audio(input_path: Path, output_path: Path, verbose: bool = False) -> str:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    aac_cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        str(output_path),
    ]
    proc = subprocess.run(aac_cmd, capture_output=True, text=True)
    if proc.returncode == 0:
        return "m4a"

    fallback = output_path.with_suffix(".mp3")
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "64k",
            str(fallback),
        ],
        "Failed to normalize audio with ffmpeg.",
        verbose=verbose,
    )
    return "mp3"


def create_chunks(audio_path: Path, duration: float, chunk_minutes: float, overlap_seconds: float, temp_dir: Path, verbose: bool = False) -> list[ChunkInfo]:
    chunk_len = max(1.0, chunk_minutes * 60.0)
    overlap = max(0.0, overlap_seconds)
    chunks: list[ChunkInfo] = []
    start = 0.0
    idx = 0

    while start < duration:
        end = min(duration, start + chunk_len)
        chunk_file = temp_dir / f"chunk_{idx:03d}{audio_path.suffix}"
        run_cmd(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{start:.3f}",
                "-i",
                str(audio_path),
                "-t",
                f"{(end - start):.3f}",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "aac" if audio_path.suffix == ".m4a" else "libmp3lame",
                str(chunk_file),
            ],
            f"Failed to extract chunk {idx}.",
            verbose=verbose,
        )
        chunks.append(ChunkInfo(index=idx, source_start=start, source_end=end, duration=end - start, path=chunk_file))
        if end >= duration:
            break
        start = max(0.0, end - overlap)
        idx += 1

    return chunks


def obj_to_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "__dict__"):
        return dict(obj.__dict__)
    raise ToolError("Could not convert API response to dictionary.")


def data_url_for_file(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "audio/wav"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def parse_segments(resp_dict: dict[str, Any]) -> list[dict[str, Any]]:
    segments = resp_dict.get("segments") or []
    normalized = []
    for seg in segments:
        try:
            s = float(seg.get("start", 0.0))
            e = float(seg.get("end", s))
        except (TypeError, ValueError):
            continue
        speaker = str(seg.get("speaker") or "unknown")
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        normalized.append({"start": s, "end": e, "speaker": speaker, "text": text})
    return normalized


def pick_bootstrap_segments(segments: list[dict[str, Any]], max_speakers: int, first_chunk_duration: float) -> dict[str, dict[str, Any]]:
    by_speaker: dict[str, list[dict[str, Any]]] = {}
    for seg in segments:
        by_speaker.setdefault(seg["speaker"], []).append(seg)

    chosen: dict[str, dict[str, Any]] = {}
    for raw_speaker in sorted(by_speaker):
        if len(chosen) >= max_speakers:
            break
        candidates = sorted(by_speaker[raw_speaker], key=lambda x: abs((x["end"] - x["start"]) - 5.0))
        selected = None
        for c in candidates:
            dur = c["end"] - c["start"]
            if dur < 2.0 or dur > 10.0:
                continue
            if c["start"] <= 0.25 or c["end"] >= first_chunk_duration - 0.25:
                continue
            selected = c
            break
        if selected:
            chosen[raw_speaker] = selected
    return chosen


def export_reference_clip(chunk_path: Path, start: float, end: float, output_path: Path, verbose: bool = False) -> None:
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-i",
            str(chunk_path),
            "-t",
            f"{max(0.2, end - start):.3f}",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ],
        "Failed to export speaker reference clip.",
        verbose=verbose,
    )


def normalize_text(s: str) -> str:
    return " ".join("".join(ch.lower() if ch.isalnum() or ch.isspace() else " " for ch in s).split())


def looks_like_duplicate(candidate: dict[str, Any], recent: list[dict[str, Any]]) -> bool:
    ctext = normalize_text(candidate["text"])
    if not ctext:
        return False
    for r in recent:
        if candidate["end"] < r["start"] or candidate["start"] > r["end"]:
            continue
        rtext = normalize_text(r["text"])
        if not rtext:
            continue
        ratio = SequenceMatcher(None, ctext, rtext).ratio()
        if ctext == rtext or ratio >= 0.92:
            return True
    return False


def transcribe_chunk(
    client: OpenAI,
    chunk_path: Path,
    model: str,
    language: str,
    known_names: list[str] | None,
    known_refs: list[str] | None,
    max_retries: int = 3,
) -> dict[str, Any]:
    for attempt in range(1, max_retries + 1):
        try:
            with chunk_path.open("rb") as fh:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "file": fh,
                    "response_format": "diarized_json",
                    "language": language,
                    "chunking_strategy": "auto",
                }
                if known_names and known_refs:
                    kwargs["known_speaker_names"] = known_names
                    kwargs["known_speaker_references"] = known_refs
                try:
                    resp = client.audio.transcriptions.create(**kwargs)
                except TypeError:
                    extra_body = {}
                    if known_names and known_refs:
                        extra_body = {
                            "known_speaker_names": known_names,
                            "known_speaker_references": known_refs,
                        }
                    kwargs.pop("known_speaker_names", None)
                    kwargs.pop("known_speaker_references", None)
                    if extra_body:
                        kwargs["extra_body"] = extra_body
                    resp = client.audio.transcriptions.create(**kwargs)
            return obj_to_dict(resp)
        except Exception as exc:  # noqa: BLE001
            if attempt == max_retries:
                raise ToolError(f"Transcription API failed for {chunk_path.name}: {exc}") from exc
            time.sleep(1.5 * attempt)
    raise ToolError("Unreachable")


def format_display_speaker(raw: str) -> str:
    tail = raw.split("_")[-1]
    return f"Speaker {tail}" if tail.isdigit() else raw.replace("_", " ").title()


def main() -> int:
    parser = argparse.ArgumentParser(description="Transcribe meeting audio to diarized JSON.")
    parser.add_argument("input", help="Path to .m4a, .mp3, or .wav file")
    parser.add_argument("--output", help="Output JSON path (default: <input>.json)")
    parser.add_argument("--language", default="sl")
    parser.add_argument("--model", default="gpt-4o-transcribe-diarize")
    parser.add_argument("--chunk-minutes", type=float, default=10.0)
    parser.add_argument("--overlap-seconds", type=float, default=4.0)
    parser.add_argument("--keep-temp", action="store_true")
    parser.add_argument("--temp-dir")
    parser.add_argument("--no-bootstrap-speakers", action="store_true")
    parser.add_argument("--max-bootstrap-speakers", type=int, default=4)
    parser.add_argument("--normalize", dest="normalize", action="store_true", default=True)
    parser.add_argument("--no-normalize", dest="normalize", action="store_false")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    try:
        input_path = Path(args.input).resolve()
        if input_path.suffix.lower() not in ALLOWED_EXTENSIONS:
            raise ToolError("Unsupported input extension. Use one of: .m4a, .mp3, .wav")
        if not input_path.exists() or not input_path.is_file():
            raise ToolError(f"Input file is not readable: {input_path}")
        if "OPENAI_API_KEY" not in os.environ:
            raise ToolError("OPENAI_API_KEY is missing from environment.")

        ensure_binary("ffmpeg")
        ensure_binary("ffprobe")

        input_meta = probe_audio(input_path, verbose=args.verbose)

        output_json = Path(args.output) if args.output else input_path.with_suffix(".json")

        cleanup_temp = False
        if args.temp_dir:
            temp_root = Path(args.temp_dir).resolve()
            temp_root.mkdir(parents=True, exist_ok=True)
        else:
            temp_root = Path(tempfile.mkdtemp(prefix="meeting2json_"))
            cleanup_temp = not args.keep_temp

        work_audio = input_path
        normalized_format = input_path.suffix.lstrip(".").lower()

        if args.normalize:
            normalized_path = temp_root / "normalized.m4a"
            normalized_format = normalize_audio(input_path, normalized_path, verbose=args.verbose)
            work_audio = normalized_path if normalized_format == "m4a" else normalized_path.with_suffix(".mp3")

        work_meta = probe_audio(work_audio, verbose=args.verbose)
        chunks = create_chunks(
            audio_path=work_audio,
            duration=work_meta["duration_seconds"],
            chunk_minutes=args.chunk_minutes,
            overlap_seconds=args.overlap_seconds,
            temp_dir=temp_root,
            verbose=args.verbose,
        )

        # Ensure each chunk stays below upload limit by splitting oversized chunks.
        final_chunks: list[ChunkInfo] = []
        reindex = 0
        for ch in chunks:
            if ch.path.stat().st_size <= API_UPLOAD_LIMIT_BYTES:
                final_chunks.append(ChunkInfo(reindex, ch.source_start, ch.source_end, ch.duration, ch.path))
                reindex += 1
                continue
            half = max(30.0, ch.duration / 2)
            sub = create_chunks(ch.path, ch.duration, half / 60.0, 0.0, temp_root, verbose=args.verbose)
            for subc in sub:
                final_chunks.append(
                    ChunkInfo(
                        index=reindex,
                        source_start=ch.source_start + subc.source_start,
                        source_end=ch.source_start + subc.source_end,
                        duration=subc.duration,
                        path=subc.path,
                    )
                )
                reindex += 1
        chunks = final_chunks

        client = OpenAI()

        bootstrap_enabled = not args.no_bootstrap_speakers
        known_names: list[str] = []
        known_refs: list[str] = []
        bootstrap_reference_speakers: list[str] = []

        merged_segments: list[dict[str, Any]] = []

        for chunk in chunks:
            if args.verbose:
                print(f"Transcribing chunk {chunk.index + 1}/{len(chunks)}: {chunk.path.name}")
            response_dict = transcribe_chunk(
                client=client,
                chunk_path=chunk.path,
                model=args.model,
                language=args.language,
                known_names=known_names if chunk.index > 0 else None,
                known_refs=known_refs if chunk.index > 0 else None,
            )
            local_segments = parse_segments(response_dict)

            if chunk.index == 0 and bootstrap_enabled:
                try:
                    chosen = pick_bootstrap_segments(local_segments, max(1, min(args.max_bootstrap_speakers, 4)), chunk.duration)
                    for i, (raw_speaker, seg) in enumerate(chosen.items(), start=1):
                        speaker_name = f"speaker_{i}"
                        ref_path = temp_root / f"bootstrap_{speaker_name}.wav"
                        export_reference_clip(chunk.path, seg["start"], seg["end"], ref_path, verbose=args.verbose)
                        known_names.append(speaker_name)
                        known_refs.append(data_url_for_file(ref_path))
                        bootstrap_reference_speakers.append(speaker_name)
                except Exception:
                    known_names = []
                    known_refs = []
                    bootstrap_reference_speakers = []

            boundary_start = chunk.source_start
            recent = [s for s in merged_segments if s["end"] >= boundary_start - args.overlap_seconds - 0.25]

            for i, seg in enumerate(local_segments):
                global_seg = {
                    "id": f"chunk{chunk.index}_seg{i}",
                    "chunk_index": chunk.index,
                    "start": round(chunk.source_start + seg["start"], 3),
                    "end": round(chunk.source_start + seg["end"], 3),
                    "speaker": seg["speaker"],
                    "raw_speaker": seg["speaker"],
                    "display_speaker": format_display_speaker(seg["speaker"]),
                    "text": seg["text"],
                }
                in_overlap_lead = chunk.index > 0 and global_seg["start"] <= chunk.source_start + args.overlap_seconds + 0.25
                if in_overlap_lead and looks_like_duplicate(global_seg, recent):
                    continue
                merged_segments.append(global_seg)

        merged_segments.sort(key=lambda x: (x["start"], x["end"]))

        speaker_keys = sorted({seg["speaker"] for seg in merged_segments})
        speakers = [{"raw_speaker": s, "display_name": format_display_speaker(s)} for s in speaker_keys]

        transcript_text = "\n".join(seg["text"] for seg in merged_segments)

        payload = {
            "tool": {"name": "meeting2json", "version": TOOL_VERSION},
            "input": {
                "path": str(input_path),
                "filename": input_path.name,
                "format": input_path.suffix.lstrip(".").lower(),
                "duration_seconds": input_meta["duration_seconds"],
                "size_bytes": input_meta["size_bytes"],
                "codec": input_meta["codec"],
                "sample_rate": input_meta["sample_rate"],
                "channels": input_meta["channels"],
            },
            "settings": {
                "model": args.model,
                "language": args.language,
                "response_format": "diarized_json",
                "chunk_minutes": args.chunk_minutes,
                "overlap_seconds": args.overlap_seconds,
                "normalize": args.normalize,
                "bootstrap_speakers": bootstrap_enabled,
                "chunking_strategy": "auto",
            },
            "processing": {
                "normalized_format": normalized_format,
                "num_chunks": len(chunks),
                "bootstrap_reference_speakers": bootstrap_reference_speakers,
                "temp_dir": str(temp_root) if args.keep_temp else None,
            },
            "chunks": [
                {
                    "index": ch.index,
                    "source_start": round(ch.source_start, 3),
                    "source_end": round(ch.source_end, 3),
                    "duration": round(ch.duration, 3),
                    "path": str(ch.path),
                }
                for ch in chunks
            ],
            "speakers": speakers,
            "segments": merged_segments,
            "text": transcript_text,
        }

        try:
            output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"Failed to write JSON output: {exc}") from exc

        if cleanup_temp:
            shutil.rmtree(temp_root, ignore_errors=True)

        print(f"Wrote JSON transcript: {output_json}")
        return 0

    except ToolError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
