# Architecture

## The pipeline

A job is one selected time range, walked through five stages. Each stage updates
the job row and publishes an event, which is what makes the progress bar move.

```
POST /api/jobs
     │
     ▼
  queued ──▶ exporting ──▶ extracting ──▶ chunking ──▶ transcribing ──▶ finalizing ──▶ completed
                 │              │             │              │              │
                 │              │             │              │              └─ merge segments,
                 │              │             │              │                 write txt/srt/vtt/json
                 │              │             │              └─ all chunks in flight across the pool
                 │              │             └─ split on silence near CHUNK_SECONDS
                 │              └─ ffmpeg -> mono 16 kHz pcm_s16le wav
                 └─ stream mp4 from Protect to disk (never buffered in memory)
```

Progress is stage-weighted (`_STAGE_FLOOR` in `app/jobs.py`) so the bar advances
smoothly: export occupies 2–30%, extraction 30–36%, chunking 36–40%,
transcription 40–95%, finalizing the rest. Within transcription it tracks
completed chunks; within export it estimates from bytes written, because Protect
sends no `Content-Length` for an export.

## Why chunk at all

Two reasons. Whisper's accuracy degrades over very long inputs, and HTTP
transcribe calls on a CPU container will hit a timeout well before an hour of
audio finishes. Chunking also buys parallelism: chunks are independent, so *n*
instances give roughly *n* times the throughput.

The cost is boundaries. A fixed cut every 300 s lands mid-word about as often as
not. So:

1. `ffmpeg silencedetect` finds the quiet spans in the whole wav.
2. `plan_cut_points()` walks forward in `CHUNK_SECONDS` steps and, for each
   target, snaps to the middle of the nearest silence within
   `SILENCE_SNAP_WINDOW` (default ±15 s), falling back to the exact target when
   there is none.
3. Each chunk after the first also reads `CHUNK_OVERLAP_SECONDS` early, so a word
   spanning a cut is heard in full at least once.
4. On merge, a segment whose midpoint falls inside the overlap is dropped — the
   previous chunk already covered that audio with more context around it — and a
   repeated phrase at the seam is trimmed by word-sequence comparison
   (`_strip_repeated_prefix`).

`plan_cut_points` and the merge are pure functions, so both are unit-tested
without touching ffmpeg or a Whisper server.

## The Whisper pool

`WhisperPool` holds an `InstanceState` per configured instance: an
`asyncio.Semaphore` sized to that instance's `concurrency`, plus health, in-flight
count, and cumulative audio/wall seconds.

**Detection.** With `kind=auto`, each instance is probed in order — `GET
/v1/models` (OpenAI-shaped), `GET /openapi.json` containing `/asr`
(asr-webservice), then `GET /inference` (whisper.cpp). The winner is remembered
for the process, so it costs one round trip at startup.

**Dispatch.** Every chunk of a job is launched at once; the semaphores do the
throttling. `_pick()` chooses the least-loaded healthy instance by in-flight
fraction, breaking ties toward the better measured realtime factor. So a GPU box
and a CPU box mixed together self-balance rather than splitting evenly.

**Failure.** A failed chunk re-probes that instance, then retries on whatever the
pool picks next, up to `WHISPER_MAX_ATTEMPTS`. With two instances a dead
container costs one chunk's latency, not the job.

## UniFi Protect

Protect's endpoint paths move between firmware versions, and there are two auth
models. Rather than pin one shape, `ProtectClient` tries an ordered list of
candidates and remembers what answered:

| Operation | Candidates |
| --- | --- |
| Cameras | `/proxy/protect/api/cameras`, `/proxy/protect/integration/v1/cameras` |
| Events | `/proxy/protect/api/events`, `/proxy/protect/integration/v1/events` |
| Export | `/proxy/protect/api/video/export?camera=…`, `/proxy/protect/api/video/export/{id}`, `/proxy/protect/integration/v1/cameras/{id}/video/export` |

Session auth (`POST /api/auth/login`) keeps the `TOKEN` cookie and tracks the
CSRF token, including the `X-Updated-CSRF-Token` rotation header, and
re-authenticates once on a 401. API-key auth sends `X-API-KEY` and skips login
entirely.

Export is streamed: `export_clip()` is an async generator, and `jobs.py` writes
each chunk straight to disk, so a multi-hundred-megabyte clip never lands in
memory. Cancelling mid-export deletes the partial file.

Event lookups are deliberately failure-tolerant — they only feed timeline
markers, so a 500 from Protect returns an empty list rather than breaking the
page.

## Storage

SQLite, with every call wrapped in `asyncio.to_thread` so the event loop never
blocks on disk. WAL mode, one `jobs` table, and an FTS5 mirror of the transcript
text kept in step on write — which is what makes cross-transcript search a single
query with server-generated `<mark>` highlighting.

`JobStore.init()` also fails any job left mid-flight by a container restart. Such
a job can never make progress, so leaving it "transcribing" forever would be a
lie; it is marked failed with a reason and can be retried with one click.

## Concurrency model

One asyncio event loop. `MAX_CONCURRENT_JOBS` workers pull from a queue; each job
runs as a named task in a registry so cancellation can reach it. Blocking work is
kept off the loop: ffmpeg runs as a subprocess via
`asyncio.create_subprocess_exec`, SQLite and chunk file reads go through
`asyncio.to_thread`.

Job state changes fan out through an `EventBus` to every SSE subscriber. A
subscriber that cannot keep up drops frames rather than applying backpressure —
the UI resyncs from `/api/jobs` on the next full update, so a slow tab can never
stall the pipeline.

## What is deliberately not here

- **No diarization.** "Who spoke" needs a separate model (pyannote et al.) and a
  lot more compute. `Segment.speaker` exists in the data model so it can be added
  without a migration.
- **No live/continuous transcription.** This is for going back and reading a
  window you chose. Always-on capture is a different tool with different storage
  and privacy consequences.
- **No user accounts.** `APP_TOKEN` is a single shared secret, which is the right
  weight for a LAN tool. Put a reverse proxy in front if you need more.
