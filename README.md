# protect-transcriber

Pick a camera and a time range from your UniFi Protect recordings, and get a
transcript back from the Whisper containers already running on your Unraid box.

Protect stores the audio but gives you no way to read it. This does that part:
it exports the clip for the window you select, extracts the audio, splits it
across every Whisper instance you have, and stitches the result back into one
timeline you can read, search, download, and click through against the video.

```
   browser UI            this container                   your Unraid host
┌────────────────┐   ┌──────────────────────┐        ┌──────────────────────┐
│ camera list    │   │ 1. export clip ──────┼───────▶│ UniFi Protect (NVR)  │
│ 24h timeline   │──▶│ 2. ffmpeg -> 16k wav │        └──────────────────────┘
│ drag a range   │   │ 3. split on silence  │        ┌──────────────────────┐
│ live progress  │◀──│ 4. fan out chunks ───┼───────▶│ faster-whisper :8000 │
│ transcript+vid │   │ 5. merge + write     │───────▶│ whisper-asr   :9000  │
└────────────────┘   └──────────────────────┘        └──────────────────────┘
```

## What it does

- **Timeline selection.** A 24-hour scrubber per camera with Protect's own event
  markers (motion, person, vehicle, ring) drawn on it. Drag to select, click a
  marker to snap to that event, or type exact timestamps.
- **Uses every Whisper instance at once.** Chunks are dispatched to whichever
  instance has a free slot, so two containers roughly halve the wall time. A
  chunk that fails is retried elsewhere.
- **Speaks three Whisper APIs.** `faster-whisper-server`/Speaches
  (OpenAI-compatible), `openai-whisper-asr-webservice`, and `whisper.cpp`'s
  server. Set `kind=auto` and each instance is probed and identified on startup.
- **Cuts on silence.** Chunk boundaries snap to a nearby silence instead of
  landing mid-word, with an overlap as backup and de-duplication on merge.
- **Readable output.** Segment list synced to an inline video player, plus
  `.txt`, `.srt`, `.vtt`, `.json`, and a wall-clock log for pasting into an
  incident note.
- **Full-text search** across every transcript you have made (SQLite FTS5).
- **Live progress** over server-sent events, with per-chunk counts.

## Quick start

```bash
git clone https://github.com/tulathron-ultimate/protect-transcriber.git
cd protect-transcriber
cp .env.example .env
$EDITOR .env          # Protect host + local account, and your Whisper URLs
docker compose up -d
```

Then open <http://localhost:8099>. The two pills in the header tell you whether
Protect and the Whisper pool are actually reachable; click either to re-probe.

### The two things you must set

**1. A local Protect account.** In the UniFi console: *Settings → Admins &
Users → Add Admin*, choose **Local Access Only**, and give it View access to the
cameras you care about. Do not enable 2FA on it — the login API cannot answer a
second factor. Put it in `PROTECT_USERNAME`/`PROTECT_PASSWORD`.

An API key (*Settings → Control Plane → Integrations*) works for listing
cameras and events, but clip export is only reliably available to a session
login, so configure the local account even if you also set `PROTECT_API_KEY`.

**2. Your Whisper instances.** Comma-separated,
`[name=]url[|kind][|model][|concurrency]`:

```bash
WHISPER_INSTANCES=faster=http://192.168.1.50:8000|openai|Systran/faster-whisper-large-v3|2,asr=http://192.168.1.50:9000|asr_webservice
```

| `kind` | Container | Endpoint |
| --- | --- | --- |
| `openai` | Speaches, faster-whisper-server, LocalAI | `POST /v1/audio/transcriptions` |
| `asr_webservice` | `onerahmet/openai-whisper-asr-webservice` | `POST /asr` |
| `whisper_cpp` | whisper.cpp `server` | `POST /inference` |
| `auto` (default) | — | probed in the order above |

`concurrency` is how many requests that instance handles at once — leave it at 1
for a CPU container, raise it for a GPU one with headroom. `model` only matters
for the OpenAI-shaped API, which requires a model name in the request.

See [docs/unraid-setup.md](docs/unraid-setup.md) for the Unraid-specific path
(Community Applications template, appdata paths, talking to containers on the
same host) and [docs/architecture.md](docs/architecture.md) for how the pipeline
fits together.

## Using it

1. Pick a camera. The `mic` / `no mic` badge is Protect's own answer for whether
   there is any audio to transcribe — a camera with `micVolume` at 0 is muted and
   will produce a silent clip.
2. Pick a window: drag on the timeline, click an event marker, use a `last 15
   min` preset, or type timestamps. The duration readout updates as you go.
3. Optionally set a language (blank auto-detects), translate-to-English mode, or
   a prompt hint — names and jargon in the hint measurably improve how Whisper
   spells them ("Tulathron", "UPS", a street name).
4. **Transcribe selection** (or ⌘/Ctrl+Enter). Progress streams live through
   export → extract → chunk *n/m* → done.
5. **Open** the finished job: click any line to jump the video there, search
   within it, or download it in any format.

No Protect access handy? **Upload a clip instead** runs a local file through the
same pipeline — the quickest way to confirm your Whisper pool works.

## Configuration

Every setting is an environment variable; `.env.example` documents all of them.
The ones worth knowing:

| Variable | Default | Notes |
| --- | --- | --- |
| `CHUNK_SECONDS` | `300` | Audio per Whisper request. Lower it if a container times out. |
| `CHUNK_OVERLAP_SECONDS` | `2` | Read-ahead across a cut; trimmed on merge. |
| `MAX_RANGE_SECONDS` | `14400` | Largest selectable window (4 h). |
| `KEEP_CLIPS` | `true` | Keep the mp4 so the UI can play it back. |
| `RETENTION_DAYS` | `30` | Sweep finished jobs and their files. `0` disables. |
| `APP_TOKEN` | *(empty)* | If set, the API requires it; open the UI as `/?token=…`. |
| `MAX_CONCURRENT_JOBS` | `1` | Chunk fan-out already saturates the pool. |
| `WHISPER_TIMEOUT` | `1800` | Per-request read timeout, in seconds. |

Everything lands under `DATA_DIR` (`/data`): `clips/`, `audio/`,
`transcripts/<job>/`, and `protect-transcriber.db`.

### A note on exposing this

It holds Protect credentials and can export footage, so keep it on the LAN. If
you put it behind a reverse proxy, set `APP_TOKEN`, and note that the SSE
progress stream needs buffering disabled (`proxy_buffering off;` in nginx).

## API

The UI is a client of the same REST API, browsable at `/docs`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Liveness + configuration summary (never gated by `APP_TOKEN`) |
| `GET` | `/api/cameras` | Cameras with audio capability |
| `GET` | `/api/cameras/{id}/events` | Event markers for a window |
| `GET` | `/api/whisper?refresh=true` | Instance health, load, throughput |
| `POST` | `/api/jobs` | Queue a job: `{cameraId, start, end, language?, task?, prompt?}` |
| `POST` | `/api/jobs/upload` | Queue a job from an uploaded file |
| `GET` | `/api/jobs` | List jobs with aggregate stats |
| `GET` | `/api/jobs/search?q=` | Full-text search across transcripts |
| `GET` | `/api/jobs/stream` | SSE stream of job state changes |
| `GET` | `/api/jobs/{id}` | One job, including segments |
| `POST` | `/api/jobs/{id}/cancel`<br>`/retry` | Control a job |
| `GET` | `/api/jobs/{id}/transcript.{txt,srt,vtt,json,log}` | Download |
| `GET` | `/api/jobs/{id}/clip` | The exported mp4 (supports range requests) |

Queue a job from a script:

```bash
curl -X POST http://localhost:8099/api/jobs \
  -H 'Content-Type: application/json' \
  -d '{"cameraId":"abc123","start":"2026-09-21T14:00:00Z","end":"2026-09-21T14:05:00Z"}'
```

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `rejected the credentials` | 2FA on the account, or it is a Ubiquiti SSO login rather than a local one. |
| `refused the clip export` | Usually API-key-only auth. Set `PROTECT_USERNAME`/`PROTECT_PASSWORD`. The error lists every endpoint it tried. |
| `returned an empty clip` | No footage for that window — the range may predate the oldest recording (the timeline shades that part). |
| `no audio track` | The camera has no mic, or `micVolume` is 0 in Protect. |
| `No Whisper instance is reachable` | Check the URLs. From inside a container, `localhost` is the container — use the host IP or `host.docker.internal`. |
| Transcript is empty but the job succeeded | There was genuinely no speech. `WHISPER_VAD_FILTER=true` drops silence. |
| Whisper times out on long clips | Lower `CHUNK_SECONDS`, or raise `WHISPER_TIMEOUT`. |

## Development

```bash
pip install -r requirements-dev.txt
pytest                      # 100 tests
ruff check app tests && ruff format --check app tests
uvicorn app.main:app --reload --port 8099
```

Protect and Whisper are stubbed in the tests, but the pipeline is not: the
end-to-end tests push a real ffmpeg-generated mp4 through export, extraction,
silence-aware chunking, merging, and file output. Tests needing ffmpeg skip
themselves if it is not installed.

| Module | Responsibility |
| --- | --- |
| `app/config.py` | Settings and `WHISPER_INSTANCES` parsing |
| `app/protect.py` | Protect auth, cameras, events, clip export |
| `app/whisper.py` | Backend adapters and the load-balancing pool |
| `app/media.py` | ffmpeg: probe, extract, silence detection, chunking |
| `app/transcript.py` | Segment merging and output formats |
| `app/store.py` | SQLite job store with FTS5 search |
| `app/jobs.py` | The pipeline and its event bus |
| `app/main.py` | FastAPI routes and the UI |

## License

MIT — see [LICENSE](LICENSE).
