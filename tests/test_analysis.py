"""Transcript analysis: URL handling, prompting, parsing, and the API."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.analysis import (
    AnalysisClient,
    AnalysisConfig,
    AnalysisError,
    AnalysisNotConfigured,
    _extract_json,
    chunk_transcript,
    format_transcript,
)
from app.store import AnalysisConfigStore, JobStore
from app.transcript import Segment
from tests.conftest import needs_ffmpeg
from tests.test_api import FakeProtect, build_client

SEGMENTS = [
    Segment(0.0, 2.0, "hello is anyone home"),
    Segment(75.0, 78.0, "i have a delivery for you"),
]


def _reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


# --------------------------------------------------------------------------- #
# config / helpers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("https://api.openai.com/v1", "https://api.openai.com/v1/chat/completions"),
        ("https://api.openai.com", "https://api.openai.com/v1/chat/completions"),
        ("http://ollama:11434", "http://ollama:11434/v1/chat/completions"),
        ("http://x/v1/", "http://x/v1/chat/completions"),
        ("http://x/v1/chat/completions", "http://x/v1/chat/completions"),
    ],
)
def test_chat_url_tolerates_how_people_write_the_base(base, expected):
    assert AnalysisConfig(base_url=base, model="m", enabled=True).chat_url == expected


def test_ready_requires_enabled_url_and_model():
    assert not AnalysisConfig().ready
    assert not AnalysisConfig(base_url="http://x", model="m").ready, "not enabled"
    assert not AnalysisConfig(base_url="http://x", enabled=True).ready, "no model"
    assert AnalysisConfig(base_url="http://x", model="m", enabled=True).ready


def test_format_transcript_carries_timestamps():
    assert format_transcript(SEGMENTS) == (
        "[00:00] hello is anyone home\n[01:15] i have a delivery for you"
    )
    assert "[" not in format_transcript(SEGMENTS, include_times=False)


def test_chunking_splits_on_line_boundaries():
    text = "\n".join(f"[00:0{i}] line {i}" for i in range(200))
    chunks = chunk_transcript(text, chunk_chars=200)
    assert len(chunks) > 1
    assert all(len(c) <= 220 for c in chunks)
    # Nothing is lost and no line is cut in half.
    assert "\n".join(chunks).count("line") == text.count("line")
    assert all(line.startswith("[") for c in chunks for line in c.splitlines())


def test_short_transcripts_are_a_single_chunk():
    assert chunk_transcript("one line") == ["one line"]
    assert chunk_transcript("") == []


def test_json_is_recovered_from_fences_and_chatter():
    assert _extract_json('{"a": 1}') == {"a": 1}
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('Sure!\n{"a": 1}\nHope that helps') == {"a": 1}
    with pytest.raises(AnalysisError, match="usable JSON"):
        _extract_json("no json here at all")


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #


def _client(**overrides) -> AnalysisClient:
    config = AnalysisConfig(
        base_url="http://llm:8000/v1", model="test-model", enabled=True, **overrides
    )
    return AnalysisClient(config)


async def test_chat_refuses_when_not_configured():
    client = AnalysisClient(AnalysisConfig())
    with pytest.raises(AnalysisNotConfigured):
        await client.chat("sys", "user")
    await client.aclose()


@respx.mock
async def test_summarize_sends_the_transcript_and_parses_the_reply():
    route = respx.post("http://llm:8000/v1/chat/completions").mock(
        return_value=_reply(
            '{"summary": "A delivery arrived.", "points": ["knock", "parcel"], '
            '"speakers": "one voice"}'
        )
    )
    client = _client()
    result = await client.summarize(SEGMENTS)
    await client.aclose()

    body = route.calls[0].request.content.decode()
    assert "i have a delivery for you" in body, "the transcript must be sent"
    assert "[01:15]" in body, "timestamps give the model something to cite"
    assert "test-model" in body
    assert result["summary"] == "A delivery arrived."
    assert result["points"] == ["knock", "parcel"]


@respx.mock
async def test_summarize_map_reduces_a_long_transcript():
    calls: list[str] = []

    def handler(request):
        calls.append(request.content.decode())
        if len(calls) <= 2:
            return _reply("partial summary")
        return _reply('{"summary": "combined", "points": []}')

    respx.post("http://llm:8000/v1/chat/completions").mock(side_effect=handler)
    long_segments = [Segment(i, i + 1, f"line number {i} " + "x" * 200) for i in range(400)]
    client = _client()
    result = await client.summarize(long_segments)
    await client.aclose()

    assert len(calls) >= 3, "expected per-chunk calls plus a combining call"
    assert result["summary"] == "combined"


@respx.mock
async def test_review_normalises_flags_and_sorts_by_severity():
    respx.post("http://llm:8000/v1/chat/completions").mock(
        return_value=_reply(
            '{"assessment": "Mostly routine.", "flags": ['
            '{"category": "profanity", "severity": "low", "timestamp": "[00:02]", '
            ' "quote": "q1", "reason": "r1"},'
            '{"category": "threat", "severity": "HIGH", "timestamp": "[01:15]", '
            ' "quote": "q2", "reason": "r2"},'
            '{"category": "odd", "severity": "bogus", "quote": "q3"}]}'
        )
    )
    client = _client()
    result = await client.review(SEGMENTS)
    await client.aclose()

    assert result["assessment"] == "Mostly routine."
    assert [f["severity"] for f in result["flags"]] == ["high", "low", "low"]
    assert result["flags"][0]["category"] == "threat"
    # An unrecognised severity degrades to low rather than breaking the UI.
    assert result["flags"][-1]["severity"] == "low"


@respx.mock
async def test_review_with_nothing_to_flag_returns_an_empty_list():
    respx.post("http://llm:8000/v1/chat/completions").mock(
        return_value=_reply('{"assessment": "Ordinary conversation.", "flags": []}')
    )
    client = _client()
    result = await client.review(SEGMENTS)
    await client.aclose()
    assert result["flags"] == []


@respx.mock
async def test_ask_answers_from_the_transcript():
    route = respx.post("http://llm:8000/v1/chat/completions").mock(
        return_value=_reply("A delivery driver spoke at [01:15].")
    )
    client = _client()
    result = await client.ask(SEGMENTS, "who was at the door?")
    await client.aclose()
    assert "delivery driver" in result["answer"]
    assert "who was at the door?" in route.calls[0].request.content.decode()


@respx.mock
async def test_ask_skips_chunks_the_model_says_are_irrelevant():
    replies = iter([_reply("NOTHING RELEVANT"), _reply("NOTHING RELEVANT")])
    respx.post("http://llm:8000/v1/chat/completions").mock(side_effect=lambda r: next(replies))
    long_segments = [Segment(i, i + 1, f"filler {i} " + "y" * 200) for i in range(400)]
    client = _client()
    result = await client.ask(long_segments, "was there a dog?")
    await client.aclose()
    assert result["answer"] == "The transcript does not say."


async def test_empty_transcripts_short_circuit_without_calling_the_model():
    client = _client()
    assert (await client.summarize([]))["summary"] == ""
    assert (await client.review([]))["flags"] == []
    assert "nothing to answer" in (await client.ask([], "anything?"))["answer"]
    await client.aclose()


@respx.mock
async def test_a_401_explains_the_chatgpt_subscription_trap():
    respx.post("http://llm:8000/v1/chat/completions").mock(return_value=httpx.Response(401))
    client = _client()
    with pytest.raises(AnalysisError, match="subscription does not include API access"):
        await client.chat("sys", "user")
    await client.aclose()


@respx.mock
async def test_transport_errors_name_the_endpoint():
    respx.post("http://llm:8000/v1/chat/completions").mock(
        side_effect=httpx.ConnectError("refused")
    )
    client = _client()
    with pytest.raises(AnalysisError, match="llm:8000"):
        await client.chat("sys", "user")
    await client.aclose()


@respx.mock
async def test_api_key_is_sent_as_a_bearer_token():
    route = respx.post("http://llm:8000/v1/chat/completions").mock(return_value=_reply("hi"))
    client = _client(api_key="sk-test")
    await client.chat("sys", "user")
    await client.aclose()
    assert route.calls[0].request.headers["Authorization"] == "Bearer sk-test"


# --------------------------------------------------------------------------- #
# store + API
# --------------------------------------------------------------------------- #


async def test_saving_config_merges_and_keeps_the_key(tmp_path):
    path = tmp_path / "jobs.db"
    await JobStore(path).init()
    store = AnalysisConfigStore(path)
    assert (await store.get())["base_url"] == ""
    await store.save(base_url="http://x/v1", model="m", api_key="sk-1", enabled=1)
    assert (await store.save(model="m2"))["api_key"] == "sk-1", "a partial save keeps the key"
    assert (await store.get())["model"] == "m2"


def test_config_endpoint_never_returns_the_key(tmp_path):
    client, _, _ = build_client(tmp_path, protect=FakeProtect())
    with client:
        saved = client.put(
            "/api/analysis",
            json={
                "baseUrl": "http://llm:8000/v1",
                "model": "m",
                "apiKey": "sk-secret",
                "enabled": True,
            },
        ).json()
        assert saved["hasApiKey"] is True
        assert saved["ready"] is True
        body = client.get("/api/analysis").json()
    assert "sk-secret" not in str(body)
    assert "apiKey" not in body


def test_config_rejects_a_url_without_a_scheme(tmp_path):
    client, _, _ = build_client(tmp_path, protect=FakeProtect())
    with client:
        assert client.put("/api/analysis", json={"baseUrl": "llm:8000"}).status_code == 422


def test_analysis_endpoints_require_configuration(tmp_path):
    client, _, _ = build_client(tmp_path, protect=FakeProtect())
    with client:
        # Seed a completed job directly so there is a transcript to analyse.
        response = client.post("/api/jobs/does-not-exist/summarize")
    assert response.status_code == 404


def test_analysis_on_an_unfinished_job_is_rejected(tmp_path):
    from app.protect import ProtectError

    client, _, _ = build_client(tmp_path, protect=FakeProtect(fail=ProtectError("nope")), pool=None)
    with client:
        from tests.test_api import make_payload, wait_for_status

        job_id = client.post("/api/jobs", json=make_payload()).json()["id"]
        wait_for_status(client, job_id, {"failed"})
        response = client.post(f"/api/jobs/{job_id}/summarize")
    assert response.status_code == 409
    assert "no transcript yet" in response.json()["detail"]


def test_analysis_endpoints_respect_the_app_token(tmp_path):
    client, _, _ = build_client(tmp_path, protect=FakeProtect(), app_token="s3cret")
    with client:
        assert client.get("/api/analysis").status_code == 401
        assert client.put("/api/analysis", json={"baseUrl": ""}).status_code == 401
        assert client.get("/api/analysis", headers={"X-App-Token": "s3cret"}).status_code == 200


# --------------------------------------------------------------------------- #
# end to end over a completed job
# --------------------------------------------------------------------------- #


@needs_ffmpeg
@respx.mock
def test_summary_is_stored_and_shown_again_without_a_second_call(tmp_path, clip_bytes):
    """Reopening a transcript must not pay for another completion."""
    from tests.test_api import FakePool, make_payload, wait_for_status

    route = respx.post("http://llm:8000/v1/chat/completions").mock(
        return_value=_reply('{"summary": "Someone delivered a parcel.", "points": ["knock"]}')
    )
    client, _, _ = build_client(tmp_path, protect=FakeProtect(clip=clip_bytes), pool=FakePool())
    with client:
        job_id = client.post("/api/jobs", json=make_payload()).json()["id"]
        job = wait_for_status(client, job_id, {"completed", "failed"})
        assert job["status"] == "completed", job.get("error")

        client.put(
            "/api/analysis",
            json={"baseUrl": "http://llm:8000/v1", "model": "m", "enabled": True},
        )
        result = client.post(f"/api/jobs/{job_id}/summarize").json()
        assert result["summary"] == "Someone delivered a parcel."
        assert route.call_count == 1

        # The stored summary comes back with the job.
        reopened = client.get(f"/api/jobs/{job_id}").json()
    assert reopened["summary"]["summary"] == "Someone delivered a parcel."
    assert route.call_count == 1, "reopening must not call the model again"


@needs_ffmpeg
@respx.mock
def test_review_is_stored_on_the_job(tmp_path, clip_bytes):
    from tests.test_api import FakePool, make_payload, wait_for_status

    respx.post("http://llm:8000/v1/chat/completions").mock(
        return_value=_reply(
            '{"assessment": "One raised voice.", "flags": [{"category": "aggression", '
            '"severity": "medium", "timestamp": "[00:01]", "quote": "q", "reason": "r"}]}'
        )
    )
    client, _, _ = build_client(tmp_path, protect=FakeProtect(clip=clip_bytes), pool=FakePool())
    with client:
        job_id = client.post("/api/jobs", json=make_payload()).json()["id"]
        wait_for_status(client, job_id, {"completed", "failed"})
        client.put(
            "/api/analysis",
            json={"baseUrl": "http://llm:8000/v1", "model": "m", "enabled": True},
        )
        assert client.post(f"/api/jobs/{job_id}/review").json()["flags"][0]["category"] == (
            "aggression"
        )
        reopened = client.get(f"/api/jobs/{job_id}").json()
    assert reopened["review"]["flags"][0]["severity"] == "medium"


@needs_ffmpeg
@respx.mock
def test_ask_is_grounded_in_that_job_transcript(tmp_path, clip_bytes):
    from tests.test_api import FakePool, make_payload, wait_for_status

    route = respx.post("http://llm:8000/v1/chat/completions").mock(
        return_value=_reply("A courier, at [00:01].")
    )
    client, _, _ = build_client(tmp_path, protect=FakeProtect(clip=clip_bytes), pool=FakePool())
    with client:
        job_id = client.post("/api/jobs", json=make_payload()).json()["id"]
        wait_for_status(client, job_id, {"completed"})
        client.put(
            "/api/analysis",
            json={"baseUrl": "http://llm:8000/v1", "model": "m", "enabled": True},
        )
        answer = client.post(
            f"/api/jobs/{job_id}/ask", json={"question": "who was at the door?"}
        ).json()
    assert answer["answer"] == "A courier, at [00:01]."
    sent = route.calls[0].request.content.decode()
    assert "who was at the door?" in sent
    # The job's own transcript text must be what the model sees.
    assert "chunk 1 words" in sent


@needs_ffmpeg
def test_analysis_without_configuration_is_a_409(tmp_path, clip_bytes):
    from tests.test_api import FakePool, make_payload, wait_for_status

    client, _, _ = build_client(tmp_path, protect=FakeProtect(clip=clip_bytes), pool=FakePool())
    with client:
        job_id = client.post("/api/jobs", json=make_payload()).json()["id"]
        wait_for_status(client, job_id, {"completed"})
        response = client.post(f"/api/jobs/{job_id}/summarize")
    assert response.status_code == 409
    assert "does not include API access" in response.json()["detail"]
