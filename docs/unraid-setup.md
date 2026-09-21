# Running protect-transcriber on Unraid

This is the deployment the project was written for: the container sits on the
same Unraid box as your Whisper instances and reaches the UDM/NVR over the LAN.

The image is published to GitHub Container Registry, so there is nothing to
build on the host. Adding the template repository once puts the app in Unraid's
own template list, and from then on it installs and updates like any other
container.

---

## 1. Add the template repository

In the Unraid webUI:

1. Go to the **Docker** tab.
2. Scroll to the bottom — below the container list — to **Template
   Repositories**.
3. Paste this into the empty box:

   ```
   https://github.com/tulathron-ultimate/protect-transcriber
   ```

4. Click **Save**.

Unraid clones the repo and reads the template from `unraid/`. You only do this
once; afterwards `git`-side changes to the template flow through on their own.

### If there is no Template Repositories box

Some builds hide it, and Community Applications changed where it lives more than
once. The direct route always works — SSH in (or open the Unraid terminal) and
drop the template on the boot drive:

```bash
mkdir -p /boot/config/plugins/dockerMan/templates-user
curl -fsSL -o /boot/config/plugins/dockerMan/templates-user/my-protect-transcriber.xml \
  https://raw.githubusercontent.com/tulathron-ultimate/protect-transcriber/main/unraid/protect-transcriber.xml
```

That is exactly what the Template Repositories field does for you. The template
carries a `TemplateURL`, so Unraid can still refresh it from GitHub later.

---

## 2. Add the container

**Docker → Add Container**, then choose **protect-transcriber** from the
**Template** dropdown (under *User templates*).

Everything is pre-filled — port, appdata path, and a description on every
field. Four values are yours to supply:

| Field | Example | Notes |
| --- | --- | --- |
| `PROTECT_HOST` | `192.168.1.1` | UDM / Cloud Key / NVR. IP or hostname, **no** `https://` |
| `PROTECT_USERNAME` | `transcriber` | A local Protect account — see step 4 |
| `PROTECT_PASSWORD` | … | Masked in the UI |
| `WHISPER_INSTANCES` | see step 3 | Your Whisper container URLs |

Click **Apply**, wait for the pull, then click the container icon → **WebUI**
(or browse to `http://TOWER-IP:8099`).

Everything else has a working default. The advanced fields — language, chunk
size, retention, `APP_TOKEN` — are described inline and in
[`.env.example`](../.env.example).

---

## 3. Work out your Whisper URLs

This is where most setups go wrong, so it is worth two minutes.

**Inside a container, `localhost` means that container** — not your Unraid host.
A Whisper container on the same box is *not* at `http://localhost:9000`. Use one
of these instead:

- **The host's LAN IP** — `http://192.168.1.50:9000`. Works regardless of Docker
  networking. Recommended.
- **`host.docker.internal`** — the template already passes
  `--add-host=host.docker.internal:host-gateway`, so `http://host.docker.internal:9000`
  resolves to the host.
- **The container name** — `http://whisper-asr:9000`, but only if both containers
  are on the same user-defined Docker network. Unraid's default `bridge` does not
  resolve container names.

Check which API each instance speaks:

```bash
# faster-whisper-server / Speaches  -> kind=openai
curl -s http://192.168.1.50:8000/v1/models | head -c 200

# openai-whisper-asr-webservice     -> kind=asr_webservice
curl -s http://192.168.1.50:9000/openapi.json | grep -o '"/asr"'
```

Then build the value. Format is `[name=]url[|kind][|model][|concurrency]`,
comma-separated:

```
faster=http://192.168.1.50:8000|openai|Systran/faster-whisper-large-v3|2,asr=http://192.168.1.50:9000|asr_webservice
```

You can leave the `kind` off entirely — with `auto`, each instance is probed on
startup and the Whisper pill in the UI shows what was detected. Naming them is
optional too, but names make the sidebar and the per-job "via …" line readable.

`concurrency` is how many requests that instance takes at once: 1 for a CPU
container, 2–3 for a GPU one with headroom.

---

## 4. Create the Protect account

In the **UniFi console** (not the Protect app):

1. **Settings → Admins & Users → Add Admin**
2. Choose **Local Access Only**. A Ubiquiti SSO account will not work here.
3. Set a username and password, and **do not enable 2FA** — the login API cannot
   answer a second factor.
4. Under Protect, give it at least **View** on the cameras you want to
   transcribe.

Clip export needs this session login. An API key covers listing cameras and
events on current firmware, but not reliably the export endpoint, which is why
the app prefers the local account and treats the key as a supplement.

---

## 5. Verify it end to end

In the UI header:

1. The **Protect** pill should go green and show the NVR version. Red means host
   or credentials — hover for the actual error.
2. The **Whisper** pill should read `2/2`. Click it to re-probe; the sidebar
   lists each instance with the API it was detected as and its measured speed.
3. Use **Upload a clip instead** with any file containing speech. That exercises
   ffmpeg, chunking, and the pool *without* involving Protect — if this works and
   a camera job does not, the problem is on the Protect side.
4. Now pick a camera with a `mic` badge (doorbells have them) and a one-minute
   window where you know someone spoke.

---

## Storage

Everything lives under `/mnt/user/appdata/protect-transcriber`:

| Directory | Contents | Rough size |
| --- | --- | --- |
| `clips/` | Exported mp4s | 5–20 MB per minute |
| `audio/` | Extracted 16 kHz mono wav | ~1.9 MB per minute |
| `transcripts/<job>/` | txt, srt, vtt, json, log | a few KB |
| `protect-transcriber.db` | Job rows and the search index | small |

Clips dominate. `RETENTION_DAYS=30` sweeps finished jobs and their files every
six hours. Set `KEEP_CLIPS=false` to discard the mp4 and wav as soon as the
transcript is written — you lose in-UI playback, and a retry has to re-export.

If you plan to keep a lot of clips, point the `/data` mapping at an array share
rather than a cache-only one.

---

## Updating

The template tracks the `latest` tag, so **Docker → check for updates → apply
update** picks up new builds. To pin a version instead, change the repository
field on the container to a tag such as
`ghcr.io/tulathron-ultimate/protect-transcriber:v0.1.0`.

---

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Template does not appear in the dropdown | The repository was added but not saved, or the fetch failed. Use the `curl` fallback in step 1. |
| `manifest unknown` / `denied` on pull | The GHCR package is private. The owner makes it public once under the repo's **Packages → protect-transcriber → Package settings → Change visibility**. |
| Protect pill red, `rejected the credentials` | 2FA is on, or the account is a Ubiquiti SSO login rather than a local one. |
| Protect pill red, `refused the clip export` | Usually API-key-only auth. Set `PROTECT_USERNAME`/`PROTECT_PASSWORD`. The message lists every endpoint that was tried. |
| Whisper pill `0/2` | URLs point at `localhost`, or the containers are down. See step 3. |
| Job fails with `no audio track` | The camera has no mic, or `micVolume` is 0 in Protect. |
| Job fails with `returned an empty clip` | No footage for that window — it may predate the oldest recording. The timeline shades that region. |
| Whisper times out on long clips | Lower `CHUNK_SECONDS`, or raise `WHISPER_TIMEOUT`. |

---

## Notes for this setup

- **GPU.** Give a GPU instance `concurrency` 2–3. The pool measures throughput
  and biases toward the faster instance, so mixing a GPU and a CPU container
  works better than splitting evenly.
- **Home Assistant.** `POST /api/jobs` takes `{cameraId, start, end}`, so a
  `rest_command` can transcribe the minute around a doorbell press; poll
  `GET /api/jobs/{id}` for `status: completed` and read `text`.
- **Exposure.** This holds Protect credentials and can export footage. Keep it on
  the LAN. Behind a reverse proxy, set `APP_TOKEN` and disable buffering so the
  progress stream works (`proxy_buffering off;` in nginx).
- **Backups.** `protect-transcriber.db` plus `transcripts/` is everything that
  matters; clips are re-exportable while Protect still holds the footage.
