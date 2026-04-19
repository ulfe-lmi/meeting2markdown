# AGENTS.md

## Project goal

Build a small, production-usable command-line tool for transcribing meeting audio into structured JSON and then rendering that JSON into nicely formatted Markdown. Support an optional second-stage, context-aware repair pass in `json2markdown.py` driven by a raw text context file.

The repository must ship with these top-level files:

- `meeting2json.py`
- `json2markdown.py`
- `requirements.txt`
- `README.md`

Also include `prompts/repair-prompt.txt` in the repository and load it from `json2markdown.py` when `--context` is used. Do not hardcode the repair prompt in Python.

Keep the project small. Prefer the Python standard library plus the official OpenAI Python SDK. Use `ffmpeg` and `ffprobe` via `subprocess` rather than adding heavy audio libraries unless there is a strong reason.

The user will initialize the repo, provide `OPENAI_API_KEY` in the environment, allow internet access, and place at least one short audio file in the repo so the tool can be smoke-tested end to end.

## Core use case

Primary target: a Zoom meeting of about one hour, in Slovenian, producing:

- diarized transcript
- JSON output
- Markdown output
- numbered speaker labels are acceptable
- input formats: `.m4a`, `.mp3`, `.wav`

This is an offline file-processing tool, not a realtime tool.

## Required API usage

Use the official OpenAI Python SDK and the speech-to-text transcription API through:

- `OpenAI()`
- `client.audio.transcriptions.create(...)`

Use the diarization model:

- `model="gpt-4o-transcribe-diarize"`

Use:

- `response_format="diarized_json"`

For diarization on inputs longer than 30 seconds, set:

- `chunking_strategy="auto"`

Set the language explicitly by default:

- `language="sl"`

Do **not** use Chat Completions or any fake wrapper for transcription. Use the proper audio transcription API for `meeting2json.py`.

Do **not** pass unsupported diarization options such as:
- `prompt`
- `logprobs`
- `timestamp_granularities`

For the optional context-aware repair step in `json2markdown.py`, use the official OpenAI Python SDK Responses API through:

- `client.responses.create(...)`
- `model="gpt-5.4-mini"`

When `--context` is provided:
- assemble the whole meeting transcript from the consolidated JSON
- read the raw context text file and pass it as text input
- load instructions from `prompts/repair-prompt.txt`
- send the full meeting transcript plus the full raw context text in one self-contained request
- do **not** repair each 10-minute transcript chunk independently
- do **not** rely on preserved conversation state such as `previous_response_id`
- prefer structured JSON output for the repair response, then render Markdown locally

## Important API truths to honor

Design the tool around these current constraints and features:

1. File uploads are limited to 25 MB.
2. Supported input formats include `mp3`, `mp4`, `mpeg`, `mpga`, `m4a`, `wav`, and `webm`. This project only needs to accept `m4a`, `mp3`, and `wav`.
3. `gpt-4o-transcribe-diarize` supports `json`, `text`, and `diarized_json`. Use `diarized_json`.
4. Known speaker references are supported for up to 4 speakers.
5. Known speaker reference clips must be 2–10 seconds long and must be sent as data URLs.
6. Supplying `language="sl"` should improve accuracy and latency for Slovenian audio.
7. The diarization model is available on the transcriptions endpoint, not the Realtime API.
8. The `prompt` field is not supported for `gpt-4o-transcribe-diarize`.
9. `gpt-5.4-mini` is suitable for the second-stage context-aware repair pass and has a large enough context window for a one-hour meeting transcript plus reasonable meeting context.
10. `previous_response_id` exists in the Responses API, but instructions from a previous response do not automatically carry over. For this project, use one self-contained repair call instead of relying on preserved state.

## Scope and philosophy

Keep the code clean and straightforward.

Good:
- one clear path that works
- helpful error messages
- conservative defaults
- readable JSON and Markdown
- a small amount of overlap handling
- a basic, best-effort speaker bootstrap feature
- an optional whole-meeting context-aware repair step in `json2markdown.py`

Avoid:
- overengineering
- unnecessary frameworks
- complicated packaging
- speculative “AI summarization” features
- database storage
- web UI
- pretending cross-chunk speaker identity is perfect
- chunk-by-chunk semantic repair when `--context` is available
- relying on preserved conversation state for batch transcript repair

## Required deliverables

### 1) `meeting2json.py`

A CLI that:

- accepts `.m4a`, `.mp3`, or `.wav`
- preprocesses and/or converts audio when needed
- chunks long audio locally so each upload is safely below the API limit
- transcribes each chunk with diarization
- merges results into one JSON document
- optionally bootstraps speaker references from the first chunk
- writes one consolidated JSON file

### 2) `json2markdown.py`

A CLI that:

- reads the JSON generated by `meeting2json.py`
- optionally accepts `--context path/to/context.txt`
- if `--context` is provided, loads `prompts/repair-prompt.txt`, assembles the whole meeting transcript, calls the Responses API once, and uses the repaired text for final Markdown
- writes a nicely formatted Markdown transcript

### 3) `requirements.txt`

Keep it minimal. Prefer:
- `openai`

Optional only if genuinely needed:
- `rich`

Try to avoid adding anything else unless it materially improves the tool.

### 4) `README.md`

Must include:
- prerequisites
- installation
- environment variable setup
- ffmpeg requirement
- example commands
- explanation of JSON output
- explanation of Markdown output
- note about the 25 MB upload limit
- note about chunking and overlap
- note that speaker identity across chunks is best-effort
- optional speaker bootstrap explanation
- a small troubleshooting section

### 5) `prompts/repair-prompt.txt`

A checked-in prompt template used by `json2markdown.py` when `--context` is supplied.

Requirements:
- do not hardcode the repair prompt in Python
- load this file from disk
- treat it as the stable developer instruction set for the context-aware repair step

## CLI requirements

Keep the entry points as simple scripts at repository root.

### `meeting2json.py` expected CLI

At minimum:

```bash
python meeting2json.py input.m4a
python meeting2json.py input.m4a --output transcript.json
python meeting2json.py input.mp3 --language sl
python meeting2json.py input.wav --chunk-minutes 10 --overlap-seconds 4
python meeting2json.py input.m4a --keep-temp
python meeting2json.py input.m4a --no-bootstrap-speakers
```

Recommended options:

- positional `input`
- `--output`
- `--language` default `sl`
- `--model` default `gpt-4o-transcribe-diarize`
- `--chunk-minutes` default `10`
- `--overlap-seconds` default `4`
- `--keep-temp`
- `--temp-dir`
- `--no-bootstrap-speakers`
- `--max-bootstrap-speakers` default `4`
- `--normalize` default on
- `--verbose`

Allow fractional `--chunk-minutes` so a short test file can exercise local chunking if needed.

### `json2markdown.py` expected CLI

At minimum:

```bash
python json2markdown.py transcript.json
python json2markdown.py transcript.json --output transcript.md
python json2markdown.py transcript.json --context meeting_context.txt
python json2markdown.py transcript.json --output transcript.md --context meeting_context.txt
```

Optional:
- `--title`
- `--compact`
- `--context`

Keep it simple.

## Audio preprocessing requirements

Use `ffprobe` to inspect the file and gather:

- duration
- size
- codec
- sample rate
- channels

Use `ffmpeg` for conversion and chunk extraction.

### Preprocessing behavior

Implement a practical, conservative workflow:

1. Validate input extension.
2. Confirm `ffmpeg` and `ffprobe` are installed.
3. Inspect the source audio.
4. If local chunking is needed or normalization is enabled, convert to a speech-friendly normalized format in a temp directory:
   - mono
   - 16 kHz
   - compressed audio suitable for speech
5. Chunk locally by time with overlap.

A good default normalization target is:
- mono
- 16 kHz
- AAC in `.m4a` or MP3 if AAC is unavailable

This project does not need lossless archival audio.

## Local chunking strategy

Local chunking is required for long meetings because of the upload-size limit.

Use time-based chunking with overlap.

Recommended defaults:
- chunk length: 10 minutes
- overlap: 4 seconds

Example:
- chunk 0: `00:00` to `10:00`
- chunk 1: `09:56` to `19:56`
- chunk 2: `19:52` to `29:52`

Store chunk metadata:
- chunk index
- local start in original timeline
- duration
- chunk file path

### Why overlap exists

Overlap helps avoid losing words at boundaries when a sentence spans a chunk edge.

It does **not** guarantee perfect speaker continuity across external chunks.

## OpenAI transcription call requirements

For each chunk, call the official SDK:

- `model="gpt-4o-transcribe-diarize"`
- `response_format="diarized_json"`
- `language="sl"` by default
- `chunking_strategy="auto"`

### Known speaker references

Implement a best-effort bootstrap feature.

Behavior:
1. Transcribe the first local chunk without known speaker references.
2. Inspect diarized segments.
3. Select clean segments for up to 4 speakers.
4. Export short reference WAV clips for those speakers.
5. Encode those clips as data URLs.
6. For later chunks, pass:
   - `known_speaker_names`
   - `known_speaker_references`

Important:
- reference clips must be 2–10 seconds
- up to 4 speakers only
- if bootstrap fails, continue without it
- this is a best-effort consistency improvement, not a guarantee

### How to pass known speaker references

Use the official SDK call. If the installed SDK version supports these parameters directly, use direct parameters. If not, use `extra_body` with the official field names. The repository should contain code that works with the current official API shape, not a fake or guessed payload.

## Speaker bootstrap heuristic

Keep the heuristic simple and conservative.

From the first local chunk:
- group diarized segments by speaker label
- prefer segments with duration between about 3 and 8 seconds
- avoid extremely short segments
- avoid obvious overlap or clipped boundaries if detectable
- choose the first sufficiently clean example per speaker
- export one reference clip per selected speaker

Name bootstrap speakers deterministically:
- `speaker_1`
- `speaker_2`
- `speaker_3`
- `speaker_4`

These are acceptable because numbered users are fine.

Save extracted reference clips in temp if `--keep-temp` is set.

## Merge behavior

After transcribing each chunk:

1. Shift each segment’s local timestamps into the global timeline.
2. Preserve the chunk index and raw speaker label.
3. Merge all chunk segments into a single ordered list.
4. Deduplicate overlap regions conservatively.

### Deduplication

Implement simple, conservative overlap deduplication:
- focus only on segments that fall inside the leading overlap of later chunks
- compare against recent kept segments near the previous boundary
- normalize text for comparison
- if text is identical or very similar and timestamps overlap, keep only one

Prefer retaining a small duplicate rather than deleting real speech.

Use the Python standard library for similarity if possible.

## Speaker identity policy

Be honest in code and docs:

- Speaker identity is reliable within a single model response.
- Across multiple local chunks, identity can drift.
- Bootstrap speaker references should improve consistency for later chunks.
- Do not claim perfect global speaker tracking.

In the consolidated JSON, preserve:
- raw model speaker label
- rendered display speaker label

If bootstrap references are used successfully, later chunks should usually align to those names. If not, keep the labels best-effort and document this clearly.

## JSON output requirements

Write one consolidated JSON file.

Recommended high-level structure:

```json
{
  "tool": {
    "name": "meeting2json",
    "version": "0.1.0"
  },
  "input": {
    "path": "meeting.m4a",
    "filename": "meeting.m4a",
    "format": "m4a",
    "duration_seconds": 3725.4,
    "size_bytes": 68439210
  },
  "settings": {
    "model": "gpt-4o-transcribe-diarize",
    "language": "sl",
    "response_format": "diarized_json",
    "chunk_minutes": 10,
    "overlap_seconds": 4,
    "normalize": true,
    "bootstrap_speakers": true
  },
  "processing": {
    "normalized_format": "m4a",
    "num_chunks": 7,
    "bootstrap_reference_speakers": [
      "speaker_1",
      "speaker_2"
    ]
  },
  "chunks": [
    {
      "index": 0,
      "source_start": 0.0,
      "source_end": 600.0,
      "path": "temp/chunk_000.m4a"
    }
  ],
  "speakers": [
    {
      "raw_speaker": "speaker_1",
      "display_name": "Speaker 1"
    }
  ],
  "segments": [
    {
      "id": "chunk0_seg0",
      "chunk_index": 0,
      "start": 0.0,
      "end": 4.7,
      "speaker": "speaker_1",
      "display_speaker": "Speaker 1",
      "text": "Dober dan vsem skupaj."
    }
  ],
  "text": "Full transcript text..."
}
```

Exact field names may vary, but keep the shape clean, documented, and stable.

## Context-aware repair requirements

When `--context` is provided to `json2markdown.py`:

1. Read the context file as raw UTF-8 text.
2. Assemble the whole meeting transcript from the consolidated JSON. Do **not** repair the original 10-minute chunks independently.
3. Load developer instructions from `prompts/repair-prompt.txt`.
4. Call the Responses API once with `model="gpt-5.4-mini"`.
5. Ask for structured JSON output aligned to the existing transcript. Preserve segment ids and segment order.
6. Use the model-repaired text to render final Markdown locally.

Recommended repair response shape:

```json
{
  "speaker_name_suggestions": [
    {
      "speaker": "Speaker 1",
      "name": "Janez Demšar",
      "confidence": "high"
    }
  ],
  "segments": [
    {
      "id": "chunk0_seg0",
      "corrected_text": "Dober dan vsem skupaj."
    }
  ]
}
```

Important behavior:
- preserve timestamps and segment ordering
- do not summarize, paraphrase, or translate
- use meeting context to correct likely ASR mistakes in names, acronyms, project terminology, and English technical terms embedded in Slovenian speech
- only rename speakers when the context provides strong evidence
- if uncertain, keep the original wording and the original speaker label
- if `--context` is not supplied, render Markdown locally without a model call

## Markdown output requirements

`json2markdown.py` should render a readable transcript.

Recommended format:

```md
# Meeting transcript

- Source file: meeting.m4a
- Model: gpt-4o-transcribe-diarize
- Language: sl
- Duration: 01:02:05
- Chunks: 7
- Generated by: meeting2json.py

## Transcript

**[00:00:03 - 00:00:11] Speaker 1**  
Dober dan vsem skupaj.

**[00:00:12 - 00:00:18] Speaker 2**  
Pozdravljeni, začnimo.
```

Requirements:
- include metadata header
- include timestamps
- include speaker labels
- preserve ordering
- use clean Markdown
- support UTF-8 correctly
- if context-aware repair produced strong speaker name suggestions, use them; otherwise keep the existing speaker labels

Optional compact mode may group consecutive segments by the same speaker if that is easy and does not obscure timestamps.

## Error handling requirements

Handle these cases clearly:

- missing `OPENAI_API_KEY`
- missing `ffmpeg`
- missing `ffprobe`
- unsupported input extension
- unreadable input file
- failed normalization
- failed chunk extraction
- API failure
- JSON write failure
- Markdown write failure
- missing context file when `--context` is used
- missing `prompts/repair-prompt.txt`
- context-repair API failure or invalid repair response

Use actionable error messages.

For API failures:
- retry a small number of times on transient failures
- fail clearly if the error persists

## Testing requirements

The user will provide a short sample audio file and internet access so the agent can test the real API.

The coding agent should:
1. install dependencies
2. run the tool on the provided short audio
3. confirm that JSON output is produced
4. run `json2markdown.py` on that JSON
5. confirm Markdown output is produced
6. create or use a small context text file
7. run `json2markdown.py` with `--context`
8. confirm Markdown output is produced for the context-aware path
9. fix issues until the happy path works

If the short sample is under the local chunk threshold, that is fine for the smoke test.
If practical, also run a second test with a very small `--chunk-minutes` value to exercise local chunk merging on the short sample.

## README requirements

The README must be written for a human user who just cloned the repo.

It must include:

### Prerequisites
- Python 3.10+
- ffmpeg / ffprobe installed
- `OPENAI_API_KEY` in environment

### Install
- create virtualenv
- install `requirements.txt`

### Usage
Examples for:
- JSON only
- JSON to Markdown
- JSON to Markdown with `--context`
- keeping temp files
- changing chunk size
- disabling speaker bootstrap

### Notes
- 25 MB upload limit
- why local chunking exists
- why overlap exists
- why overlap helps text continuity
- why overlap does not fully guarantee cross-chunk speaker identity
- how bootstrap speaker references help
- up to 4 known speakers supported by the API
- reference clips are 2–10 seconds
- `json2markdown.py --context` uses one whole-meeting Responses API call, not one repair call per 10-minute chunk
- the default repair model is `gpt-5.4-mini`
- the repair instructions are loaded from `prompts/repair-prompt.txt`
- the diarization model itself cannot be given semantic meeting context via `prompt`, so semantic context is applied in the second stage

### Troubleshooting
Examples:
- ffmpeg not found
- auth errors
- empty or strange speaker assignments
- duplicate lines near chunk boundaries

## Acceptance criteria

The project is done when all of the following are true:

1. `meeting2json.py` runs from the command line on a provided sample audio file.
2. It calls the official OpenAI audio transcription API correctly.
3. It writes a valid consolidated JSON transcript.
4. `json2markdown.py` reads that JSON and writes a Markdown transcript.
5. `json2markdown.py --context context.txt` uses the Responses API once for whole-meeting repair and then writes a Markdown transcript.
6. `prompts/repair-prompt.txt` is present and loaded by the code.
7. `README.md` accurately documents setup and usage.
8. `requirements.txt` is present and sufficient.
9. The code is small, readable, and not overengineered.
10. The smoke test was actually run against the provided sample audio if the environment allows it.

## Implementation guidance

A simple internal structure is enough. The repo does not need packaging polish.

Possible approach:
- keep `meeting2json.py` as the main script
- place helper functions inside the same file if the code stays readable
- or create one small helper module only if necessary

Do not create a large framework.

## Final delivery expectations for the coding agent

When done:
- ensure files are written in repo root
- ensure `prompts/repair-prompt.txt` is included and referenced by the code
- keep commands in README copy-pasteable
- keep defaults sensible for a one-hour Zoom meeting
- mention any limitations honestly
- do not invent unsupported API behavior
