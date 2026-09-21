from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import Settings

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(
    not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe are not installed"
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        protect_host="nvr.example.com",
        protect_username="svc",
        protect_password="secret",
        whisper_instances="a=http://whisper-a:8000|openai",
        data_dir=tmp_path / "data",
        chunk_seconds=10,
        chunk_overlap_seconds=1.0,
        retention_days=0,
        _env_file=None,
    )


def _make_media(
    path: Path, *, seconds: float, with_audio: bool = True, video: bool = False
) -> Path:
    """Build a small synthetic clip: a beep-silence-beep pattern with tone gaps."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    if with_audio:
        # Alternating tone and silence so silencedetect has something to find.
        expr = "sin(440*2*PI*t)*lt(mod(t\\,4)\\,2)"
        cmd += ["-f", "lavfi", "-i", f"aevalsrc={expr}:s=16000:d={seconds}"]
    if video:
        cmd += ["-f", "lavfi", "-i", f"testsrc=size=160x120:rate=10:duration={seconds}"]
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", str(seconds)]
    if with_audio:
        cmd += ["-c:a", "aac" if path.suffix == ".mp4" else "pcm_s16le"]
    cmd += ["-t", str(seconds), str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


@pytest.fixture
def make_media():
    if not FFMPEG:
        pytest.skip("ffmpeg is not installed")
    return _make_media


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
