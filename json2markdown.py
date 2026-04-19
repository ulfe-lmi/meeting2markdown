#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from openai import OpenAI


class ToolError(Exception):
    pass


BLOCK_START = "SEGMENT_START"
BLOCK_END = "SEGMENT_END"


def fmt_ts(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def render_compact(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not segments:
        return []
    merged = [dict(segments[0])]
    for seg in segments[1:]:
        last = merged[-1]
        if seg.get("display_speaker") == last.get("display_speaker") and seg.get("start", 0) <= last.get("end", 0) + 1.0:
            last["end"] = max(last.get("end", 0), seg.get("end", 0))
            last["text"] = (last.get("text", "") + " " + seg.get("text", "")).strip()
        else:
            merged.append(dict(seg))
    return merged


def _safe_line(value: Any) -> str:
    return str(value or "").replace("\n", " ").strip()


def build_segment_contract(segments: list[dict[str, Any]]) -> str:
    out: list[str] = []
    for idx, seg in enumerate(segments):
        seg_id = _safe_line(seg.get("id") or f"seg_{idx}")
        speaker = _safe_line(seg.get("display_speaker") or seg.get("speaker") or "Speaker")
        start = fmt_ts(float(seg.get("start", 0)))
        end = fmt_ts(float(seg.get("end", seg.get("start", 0))))
        text = _safe_line(seg.get("text", ""))
        out.extend(
            [
                BLOCK_START,
                f"id: {seg_id}",
                f"speaker: {speaker}",
                f"start: {start}",
                f"end: {end}",
                f"text: {text}",
                BLOCK_END,
                "",
            ]
        )
    return "\n".join(out).strip() + "\n"


def extract_output_text(response: Any) -> str:
    text = getattr(response, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text

    output = getattr(response, "output", None)
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            content = getattr(item, "content", None)
            if not isinstance(content, list):
                continue
            for entry in content:
                entry_text = getattr(entry, "text", None)
                if isinstance(entry_text, str):
                    parts.append(entry_text)
        if parts:
            return "\n".join(parts)

    return ""


def parse_repaired_segments(text: str) -> dict[str, str]:
    pattern = re.compile(r"SEGMENT_START\s*(.*?)\s*SEGMENT_END", flags=re.DOTALL)
    parsed: dict[str, str] = {}
    for block in pattern.findall(text):
        seg_id = None
        seg_text = None
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if line.startswith("id:"):
                seg_id = line[3:].strip()
            elif line.startswith("text:"):
                seg_text = line[5:].strip()
        if seg_id and seg_text is not None:
            parsed[seg_id] = seg_text
    return parsed


def apply_repairs(segments: list[dict[str, Any]], repaired_by_id: dict[str, str]) -> list[dict[str, Any]]:
    patched: list[dict[str, Any]] = []
    for idx, seg in enumerate(segments):
        out_seg = dict(seg)
        seg_id = str(seg.get("id") or f"seg_{idx}")
        if seg_id in repaired_by_id:
            out_seg["text"] = repaired_by_id[seg_id]
        patched.append(out_seg)
    return patched


def repair_segments_with_context(segments: list[dict[str, Any]], context_path: Path, prompt_path: Path) -> list[dict[str, Any]]:
    if not context_path.exists():
        raise ToolError(f"Context file not found: {context_path}")
    if not prompt_path.exists():
        raise ToolError(f"Repair prompt file not found: {prompt_path}")
    if not os.environ.get("OPENAI_API_KEY"):
        raise ToolError("OPENAI_API_KEY is not set, required for --context repair")

    context_text = context_path.read_text(encoding="utf-8")
    repair_prompt = prompt_path.read_text(encoding="utf-8")
    transcript_text = build_segment_contract(segments)

    caller_instructions = (
        "Return only segment blocks in this exact format:\n"
        "SEGMENT_START\n"
        "id: <same id>\n"
        "speaker: <same speaker>\n"
        "start: <same hh:mm:ss>\n"
        "end: <same hh:mm:ss>\n"
        "text: <corrected text>\n"
        "SEGMENT_END\n"
        "Do not omit or reorder segments. Change only the text field conservatively."
    )

    client = OpenAI()
    response = client.responses.create(
        model="gpt-5.4-mini",
        instructions=repair_prompt,
        input=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": f"CONTEXT_TEXT:\n{context_text}\n\nTRANSCRIPT:\n{transcript_text}\n\n{caller_instructions}",
                    }
                ],
            }
        ],
    )

    repaired_text = extract_output_text(response).strip()
    if not repaired_text:
        raise ToolError("Repair API returned empty output")

    repaired_by_id = parse_repaired_segments(repaired_text)
    if not repaired_by_id:
        raise ToolError("Repair output could not be parsed as segment blocks")

    covered = 0
    for idx, seg in enumerate(segments):
        seg_id = str(seg.get("id") or f"seg_{idx}")
        if seg_id in repaired_by_id:
            covered += 1

    if covered == 0:
        raise ToolError("Repair output did not match any segment IDs")

    return apply_repairs(segments, repaired_by_id)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render meeting2json output into Markdown transcript.")
    parser.add_argument("input", help="Path to transcript JSON")
    parser.add_argument("--output", help="Output markdown path (default: <input>.md)")
    parser.add_argument("--title", default="Meeting transcript")
    parser.add_argument("--compact", action="store_true", help="Merge nearby consecutive segments by same speaker")
    parser.add_argument("--context", help="Path to raw text context file for optional whole-meeting repair")
    args = parser.parse_args()

    try:
        in_path = Path(args.input)
        if not in_path.exists():
            raise ToolError(f"Input JSON not found: {in_path}")

        try:
            data = json.loads(in_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"Failed to read JSON: {exc}") from exc

        segments = data.get("segments") or []

        repair_context_used = False
        repair_context_path: str | None = None
        repair_model: str | None = None

        if args.context:
            try:
                segments = repair_segments_with_context(
                    segments=segments,
                    context_path=Path(args.context),
                    prompt_path=Path("prompts/repair-prompt.txt"),
                )
                repair_context_used = True
                repair_context_path = str(Path(args.context))
                repair_model = "gpt-5.4-mini"
            except ToolError as exc:
                print(f"Warning: context repair failed, rendering original transcript: {exc}", file=sys.stderr)

        if args.compact:
            segments = render_compact(segments)

        source_name = (data.get("input") or {}).get("filename", "unknown")
        model = (data.get("settings") or {}).get("model", "unknown")
        language = (data.get("settings") or {}).get("language", "unknown")
        duration = (data.get("input") or {}).get("duration_seconds", 0)
        num_chunks = (data.get("processing") or {}).get("num_chunks", "?")
        generator = (data.get("tool") or {}).get("name", "meeting2json.py")

        lines: list[str] = []
        lines.append(f"# {args.title}")
        lines.append("")
        lines.append(f"- Source file: {source_name}")
        lines.append(f"- Model: {model}")
        lines.append(f"- Language: {language}")
        lines.append(f"- Duration: {fmt_ts(float(duration or 0))}")
        lines.append(f"- Chunks: {num_chunks}")
        if repair_context_used and repair_context_path and repair_model:
            lines.append(f"- Context file: {repair_context_path}")
            lines.append(f"- Repair model: {repair_model}")
        lines.append(f"- Generated by: {generator}.py")
        lines.append("")
        lines.append("## Transcript")
        lines.append("")

        for seg in segments:
            start = fmt_ts(float(seg.get("start", 0)))
            end = fmt_ts(float(seg.get("end", seg.get("start", 0))))
            speaker = seg.get("display_speaker") or seg.get("speaker") or "Speaker"
            text = str(seg.get("text", "")).strip()
            if not text:
                continue
            lines.append(f"**[{start} - {end}] {speaker}**  ")
            lines.append(text)
            lines.append("")

        output_path = Path(args.output) if args.output else in_path.with_suffix(".md")
        try:
            output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"Failed to write Markdown output: {exc}") from exc

        print(f"Wrote Markdown transcript: {output_path}")
        return 0

    except ToolError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
