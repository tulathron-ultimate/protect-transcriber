# Running protect-transcriber on Unraid

This is the deployment this project was written for: the container sits on the
same Unraid box as your Whisper instances, reaching the UDM/NVR over the LAN.

## 1. Build the image

There is no published image yet, so build it on the Unraid host. Open a terminal
(or SSH in):

```bash
mkdir -p /mnt/user/appdata/protect-transcriber
cd /boot/config/plugins/dockerMan
git clone https://github.com/tulathron-ultimate/protect-transcriber.git /mnt/user/appdata/protect-transcriber/src
cd /mnt/user/appdata/protect-transcriber/src
docker build -t protect-transcriber:latest .
```

Rebuild after a `git pull` with the same `docker build` line.

## 2. Find your Whisper URLs

From inside a Docker container, `localhost` means *that container*, so a Whisper
container on the same host is not at `http://localhost:9000`. Use one of:

- **The host's LAN IP** — `http://192.168.1.50:9000`. Simplest and works
  regardless of Docker networking. Recommended.
- **`host.docker.internal`** — `http://host.docker.internal:9000`, if you add
  `--add-host=host.docker.internal:host-gateway` to the container's extra
  parameters.
- **The container name** — `http://whisper-asr:9000`, only if both containers are
  on the same user-defined Docker network (Unraid's default `bridge` does not do
  name resolution).

Check which API each one speaks:

```bash
# faster-whisper-server / Speaches -> kind=openai
curl -s http://192.168.1.50:8000/v1/models | head -c 200

# openai-whisper-asr-webservice -> kind=asr_webservice
curl -s http://192.168.1.50:9000/openapi.json | grep -o '"/asr"'
```

Leaving `kind` off (or `auto`) makes the app probe for you — the Whisper pill in
the UI then shows what it found.

## 3. Add the container

Either use the template in this repo or fill the fields in by hand.

### Using the template

Copy [`docs/unraid-template.xml`](unraid-template.xml) to
`/boot/config/plugins/dockerMan/templates-user/my-protect-transcriber.xml`, then
in the Unraid GUI: **Docker → Add Container → Template → protect-transcriber**.
Fill in the Protect and Whisper fields and hit Apply.

### By hand

**Docker → Add Container**, toggle Advanced View:

| Field | Value |
| --- | --- |
| Name | `protect-transcriber` |
| Repository | `protect-transcriber:latest` |
| Network Type | `Bridge` |
| Port | `8099` → `8099` (TCP) |
| Path | `/data` → `/mnt/user/appdata/protect-transcriber` (rw) |
| Extra Parameters | `--add-host=host.docker.internal:host-gateway` (optional) |

Then add these variables:

| Variable | Example |
| --- | --- |
| `PROTECT_HOST` | `192.168.1.1` |
| `PROTECT_USERNAME` | `transcriber` |
| `PROTECT_PASSWORD` | your local Protect password |
| `PROTECT_VERIFY_SSL` | `false` |
| `WHISPER_INSTANCES` | `faster=http://192.168.1.50:8000\|openai\|Systran/faster-whisper-large-v3\|2,asr=http://192.168.1.50:9000\|asr_webservice` |
| `CHUNK_SECONDS` | `300` |
| `RETENTION_DAYS` | `30` |
| `APP_TOKEN` | *(optional)* a long random string |

> In the Unraid GUI the `|` characters in `WHISPER_INSTANCES` are fine as-is —
> the escaping above is only for this Markdown table.

Apply, then open `http://TOWER-IP:8099`.

## 4. Create the Protect account

In the UniFi console (not the Protect app):

1. **Settings → Admins & Users → Add Admin**
2. Choose **Local Access Only** — a Ubiquiti SSO account cannot be used here.
3. Username/password, **no 2FA**. The login API cannot answer a second factor.
4. Under Protect, give it at least **View** on the cameras you want to
   transcribe.

Clip export needs this session login. An API key alone covers listing cameras and
events on current firmware but not reliably the export endpoint, which is why
the app prefers the local account and falls back to the key.

## 5. Storage planning

Under `/mnt/user/appdata/protect-transcriber`:

| Directory | Contents | Rough size |
| --- | --- | --- |
| `clips/` | Exported mp4s | Whatever Protect sends, often 5–20 MB/min |
| `audio/` | Extracted 16 kHz mono wav | ~1.9 MB/min |
| `transcripts/<job>/` | txt, srt, vtt, json, log | A few KB |
| `protect-transcriber.db` | Job rows + search index | Small |

The clips dominate. `RETENTION_DAYS=30` sweeps finished jobs and their files
every six hours; set `KEEP_CLIPS=false` to discard the mp4 and wav as soon as a
transcript is written (you lose in-UI playback, and a retry then has to
re-export).

Because this is appdata rather than a cache-only share, point it at an array
share if you plan to keep a lot of clips.

## 6. Verify it end to end

1. The **Protect** pill should go green and show the NVR version. Red means
   credentials or host — the tooltip carries the error.
2. The **Whisper** pill should read `2/2`. Click it to re-probe; the sidebar lists
   each instance with the API it was detected as.
3. Use **Upload a clip instead** with any file that has speech in it. That
   exercises ffmpeg, chunking, and the pool without involving Protect — if this
   works and a camera job does not, the problem is on the Protect side.
4. Pick a doorbell camera (those have mics) and a 1-minute window where you know
   someone spoke.

## Notes for this setup

- **GPU.** If a Whisper container has a GPU, raise its `concurrency` to 2–3 and
  put it first in `WHISPER_INSTANCES`; the pool prefers whichever instance has
  the best measured realtime factor when load is otherwise equal.
- **Mixed speeds.** A slow CPU instance alongside a fast GPU one is fine — the
  pool measures throughput and biases toward the fast one rather than splitting
  evenly.
- **Home Assistant.** `POST /api/jobs` takes a camera id and an ISO range, so a
  `rest_command` can transcribe the minute around a doorbell press. `GET
  /api/jobs/{id}` polls for `status: completed` and carries the text.
- **Backups.** `protect-transcriber.db` plus `transcripts/` is everything that
  matters; clips are re-exportable while Protect still has the footage.
