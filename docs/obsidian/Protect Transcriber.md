---
title: Protect Transcriber
type: project
status: active
created: 2026-09-21
tags:
  - project/protect-transcriber
  - unraid
  - unifi-protect
  - whisper
  - self-hosted
  - homelab
repo: https://github.com/tulathron-ultimate/protect-transcriber
host: unraid
port: 8099
---

# Protect Transcriber

> Select a camera + time range from UniFi Protect, get a transcript from the
> self-hosted Whisper instances on [[Unraid]].

Repo: `tulathron-ultimate/protect-transcriber` · Branch built on:
`claude/unifi-protect-transcription-cstmnh`

## Why

Protect records audio but offers no way to read it. Scrubbing a doorbell clip to
work out what someone said is slow, and there is no search across footage. This
exports the window you pick, runs it through Whisper, and gives back a searchable
transcript synced to the video.

## Stack

| Layer | Choice | Note |
| --- | --- | --- |
| Service | Python 3.11+ / FastAPI / uvicorn | Async throughout; Docker on Unraid |
| Media | ffmpeg + ffprobe | Extract, silence-detect, chunk |
| ASR | faster-whisper-server *and* whisper-asr-webservice | Adapter per API, `auto` probing |
| Store | SQLite + FTS5 | Job rows + cross-transcript search |
| UI | Vanilla JS + canvas timeline | No build step, served by the app |

## Runbook

Install via the template repo — no local build:

1. Docker tab → **Template Repositories** → add
   `https://github.com/tulathron-ultimate/protect-transcriber` → Save.
2. Add Container → Template → **protect-transcriber**.
3. WebUI at `http://TOWER:8099`.

Image: `ghcr.io/tulathron-ultimate/protect-transcriber:latest`, built and pushed
by `.github/workflows/publish.yml` on every push to `main`.

Key env vars (full list in `.env.example`):

```
PROTECT_HOST=192.168.1.1
PROTECT_USERNAME=transcriber        # LOCAL account, no 2FA
PROTECT_PASSWORD=...
CHUNK_SECONDS=300
RETENTION_DAYS=30
```

Whisper instances are **not** env config any more — they live in the database and
are edited in the UI (sidebar → Whisper → Configure) with no restart.
`WHISPER_INSTANCES` seeds the table once on first start, then is ignored.

Appdata: `/mnt/user/appdata/protect-transcriber` (the template default).

## Findings worth keeping

These are the things that cost time to work out.

### UniFi Protect

- **Clip export needs a session login, not an API key.** `POST /api/auth/login`
  with a local account, keep the `TOKEN` cookie plus the `X-CSRF-Token` header.
  The API key (`X-API-KEY`) covers the integration API for cameras and events but
  not reliably `/video/export`.
- **2FA breaks it.** The login API cannot answer a second factor, so the service
  account must be **Local Access Only** without 2FA.
- **Export endpoint path varies by firmware.** Three shapes seen in the wild:
  `/proxy/protect/api/video/export?camera=<id>&start=<ms>&end=<ms>`,
  `/proxy/protect/api/video/export/<id>?start=&end=`, and
  `/proxy/protect/integration/v1/cameras/<id>/video/export`. The client tries all
  three and remembers the winner.
- **All timestamps are epoch milliseconds, UTC.**
- **`micVolume: 0` means muted**, so `featureFlags.hasMic` alone is not enough to
  know whether a transcript is possible. Both are checked.
- **Exports carry no `Content-Length`**, so export progress has to be estimated
  from bytes written.
- `stats.video.recordingStart` gives the oldest available footage — the UI shades
  the timeline before it.

### Whisper containers

- **The two popular containers disagree on everything.**
  `onerahmet/openai-whisper-asr-webservice` is `POST /asr` with query params and a
  multipart field named `audio_file`; faster-whisper-server/Speaches is `POST
  /v1/audio/transcriptions` with form fields and a field named `file`, and needs
  `response_format=verbose_json` before it will return segments at all.
- **Detection without configuration:** `GET /v1/models` returning `{"data": …}`
  means OpenAI-shaped; `GET /openapi.json` containing `/asr` means
  asr-webservice. One round trip each at startup.
- **`avg_logprob`** is faster-whisper's per-segment confidence signal; roughly
  `1 + logprob/5` clamped to 0..1 gives something displayable.
- **Chunk boundaries matter more than expected.** Cutting every N seconds slices
  words. Fix: `ffmpeg silencedetect` to find quiet spans, snap each cut to the
  nearest silence midpoint within ±15 s, overlap 2 s, then drop any segment whose
  midpoint lands in the overlap and trim repeated phrases by word-sequence match.
- **Prompt hints work.** Passing names/jargon (`initial_prompt` / `prompt`)
  measurably improves spelling of proper nouns.
- **Pre-resampling to mono 16 kHz PCM** in this service rather than letting each
  container decode saves a step and keeps chunk offsets exact.

### Runtime config vs env config

- **Anything a user will tune belongs in the database, not the environment.** On
  Unraid, changing an env var means editing the container template and
  restarting it — far too heavy for "the Whisper box moved to a new port".
- **Seed-once pattern** keeps both worlds working: read the env var only when
  the table is empty, then treat the database as the source of truth. A restart
  cannot silently undo a UI edit, and existing installs migrate with no action.
  Log the seeding so it is visible in the container log.
- **Hot-reload needs care with in-flight work.** Rebuild pool state only when
  connection settings actually changed; otherwise keep the existing object so
  health, throughput history and the semaphore survive. A running task holds its
  own reference to the state it picked, so a removed instance still finishes.
- **Concurrency changes must rebuild** the state, because `asyncio.Semaphore` is
  sized at construction and cannot be resized.
- **Never return a stored secret to the browser.** Send `hasApiKey: true`
  instead, and treat an omitted key on PATCH as "keep", an explicit `""` as
  "clear" (`model_dump(exclude_unset=True)` in pydantic v2 makes this exact).

### Unraid templates and GHCR

- **Unraid reads template repos natively.** Docker tab → **Template
  Repositories** → paste a GitHub repo URL. No Community Applications
  submission or moderation needed. CA's own feed is a separate, curated thing.
- **Repo layout:** one XML per app (the established convention is a folder per
  app, or a `templates/`-style dir) plus `ca_profile.xml` at the root, whose
  root element is `<Maintainer>` with `Icon`/`Profile`/`WebPage`.
- **Boot-drive fallback** always works and is what the field automates:
  drop the XML in `/boot/config/plugins/dockerMan/templates-user/`. It then
  shows up under *User templates* in Add Container.
- **`TemplateURL`** should point at the raw XML on the default branch so Unraid
  can refresh it; **`Repository`** must be a pullable image or the template is
  useless without a local build.
- **GHCR packages default to private.** The first publish succeeds but Unraid
  cannot pull anonymously until the package visibility is flipped to public,
  once, in the repo's Packages settings. Easy to lose an hour to.
- `GITHUB_TOKEN` needs `permissions: packages: write` in the workflow.

### Docker on Unraid

- `localhost` inside a container is *that* container. Use the host LAN IP for the
  Whisper URLs, or add `--add-host=host.docker.internal:host-gateway`.
- Unraid's default `bridge` network does not resolve container names; that needs
  a user-defined network.
- Clips dominate storage (~5–20 MB/min); extracted wav is ~1.9 MB/min.

### FastAPI gotcha

`Annotated[None, Depends(f)]` as a parameter annotation is **not** picked up as a
dependency — FastAPI reads it as a required query parameter and every request
fails with a 422 about a missing field `_`. Use
`@app.get(..., dependencies=[Depends(f)])` for a guard with no return value.

## Ideas / not done

- Speaker diarization (`Segment.speaker` is already in the schema).
- [[Home Assistant]] `rest_command` to auto-transcribe the minute around a
  doorbell press, then a notification with the text. `POST /api/jobs` takes
  `{cameraId, start, end}`; poll `GET /api/jobs/{id}` for `completed`.
- Push finished transcripts into this vault as dated notes.
- Submit to Community Applications proper, so it is searchable in the Apps tab
  rather than needing the template repo added by hand.

## Related

- [[Unraid]]
- [[UniFi Protect]]
- [[Whisper]]
- [[Home Assistant]]
