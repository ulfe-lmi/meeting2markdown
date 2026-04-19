# meeting2markdown

Small CLI tools for transcribing meeting audio into diarized JSON and rendering that JSON into readable Markdown.

## What this project does

1. `meeting2json.py`
   - Accepts `.m4a`, `.mp3`, `.wav`
   - Uses `ffprobe` + `imageio-ffmpeg` for inspection/preprocessing/chunking
   - Calls OpenAI's transcription API with diarization (`gpt-4o-transcribe-diarize`)
   - Produces one consolidated JSON transcript
2. `json2markdown.py`
   - Reads that JSON file
   - Writes a clean Markdown transcript with metadata, timestamps, and speakers

## Prerequisites

- Python 3.10+
- `ffprobe` available on `PATH`
- `OPENAI_API_KEY` in the environment

`meeting2json.py` resolves `ffmpeg` from `imageio-ffmpeg` automatically (no system `ffmpeg` binary required).

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Set API key:

```bash
export OPENAI_API_KEY="your_api_key_here"
```

## Usage

### JSON transcript

```bash
python meeting2json.py input.m4a
python meeting2json.py input.m4a --output transcript.json
python meeting2json.py input.mp3 --language sl
```

### Markdown transcript

```bash
python json2markdown.py transcript.json
python json2markdown.py transcript.json --output transcript.md
```

### Useful options

```bash
# Keep temp normalized/chunk/reference files
python meeting2json.py input.m4a --keep-temp

# Smaller chunk size + overlap tuning
python meeting2json.py input.wav --chunk-minutes 10 --overlap-seconds 4

# Disable bootstrap speaker references
python meeting2json.py input.m4a --no-bootstrap-speakers
```

## `meeting2json.py` options

- positional `input`
- `--output`
- `--language` (default: `sl`)
- `--model` (default: `gpt-4o-transcribe-diarize`)
- `--chunk-minutes` (default: `10`, fractional allowed)
- `--overlap-seconds` (default: `4`)
- `--keep-temp`
- `--temp-dir`
- `--no-bootstrap-speakers`
- `--max-bootstrap-speakers` (default: `4`)
- `--normalize` / `--no-normalize` (default: normalize on)
- `--verbose`

## JSON output shape

`meeting2json.py` writes one consolidated JSON document with sections like:

- `tool`: tool metadata/version
- `input`: source file and audio metadata from `ffprobe`
- `settings`: model/language/chunking settings used
- `processing`: normalization format, number of chunks, bootstrap info
- `chunks`: chunk boundaries and file paths
- `speakers`: speaker mapping (`raw_speaker` + `display_name`)
- `segments`: global timeline diarized transcript segments
- `text`: concatenated transcript text

## Markdown output shape

`json2markdown.py` writes:

- title
- metadata bullets (source, model, language, duration, chunk count)
- transcript section with per-segment lines:
  - `[start - end] speaker`
  - text body

Optional `--compact` merges nearby consecutive segments by the same speaker.

## Notes and limitations

- OpenAI transcription uploads are limited to **25 MB** per request.
- Local chunking is used so longer meetings can be processed safely.
- Overlap (default 4s) helps avoid text loss at chunk boundaries.
- Overlap improves continuity, but does **not** guarantee perfect cross-chunk speaker identity.
- Speaker identity is usually reliable within one API response and best-effort across local chunks.
- Optional speaker bootstrap extracts short reference clips from chunk 0 and sends them to later chunk requests.
- Known speaker references are limited to up to 4 speakers.
- Reference clips are kept in the recommended 2–10 second range.

## Troubleshooting

- **`ffprobe` not found**
  - Install ffprobe (typically from your OS ffmpeg package) and ensure it is on `PATH`.
- **Authentication/API errors**
  - Confirm `OPENAI_API_KEY` is set and valid.
- **Odd or drifting speaker labels**
  - This can happen across local chunk boundaries. Keep bootstrap enabled and tune chunk size/overlap.
- **Duplicate lines near boundaries**
  - The tool does conservative overlap deduplication and may intentionally keep a small duplicate rather than remove real speech.

## API behavior used

Each transcription request uses the official OpenAI Python SDK transcription endpoint:

- `client.audio.transcriptions.create(...)`
- `model="gpt-4o-transcribe-diarize"`
- `response_format="diarized_json"`
- `language="sl"` by default
- `chunking_strategy="auto"`

No chat/completions API is used for transcription.
