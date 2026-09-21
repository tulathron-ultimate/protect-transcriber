from __future__ import annotations

import httpx
import pytest
import respx

from app.config import Settings, WhisperInstance
from app.whisper import (
    AsrWebserviceBackend,
    NoHealthyInstances,
    OpenAIBackend,
    WhisperCppBackend,
    WhisperError,
    WhisperPool,
    build_backend,
)

VERBOSE_JSON = {
    "text": "someone is at the door",
    "language": "en",
    "duration": 4.2,
    "segments": [
        {"start": 0.0, "end": 2.0, "text": " someone is at", "avg_logprob": -0.25},
        {"start": 2.0, "end": 4.2, "text": " the door", "avg_logprob": -0.4},
    ],
}


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "chunk.wav"
    path.write_bytes(b"RIFF....WAVEfake")
    return path


async def _client():
    return httpx.AsyncClient(timeout=5.0)


# --------------------------------------------------------------------------- #
# individual backends
# --------------------------------------------------------------------------- #


@respx.mock
async def test_openai_backend_posts_to_transcriptions_and_parses_segments(audio):
    route = respx.post("http://w:8000/v1/audio/transcriptions").mock(
        return_value=httpx.Response(200, json=VERBOSE_JSON)
    )
    backend = OpenAIBackend(WhisperInstance(name="w", url="http://w:8000", model="large-v3"))
    async with httpx.AsyncClient() as client:
        result = await backend.transcribe(
            client, audio, language="en", task="transcribe", vad_filter=True
        )
    assert route.called
    body = route.calls[0].request.content
    assert b"large-v3" in body, "the configured model must be sent"
    assert b"verbose_json" in body, "segments require verbose_json"
    assert result.text == "someone is at the door"
    assert [s.text for s in result.segments] == ["someone is at", "the door"]
    assert result.language == "en"
    # avg_logprob is mapped into a rough 0..1 confidence for the UI.
    assert 0.0 <= result.segments[0].confidence <= 1.0


@respx.mock
async def test_openai_backend_uses_the_translations_route_for_translate(audio):
    route = respx.post("http://w:8000/v1/audio/translations").mock(
        return_value=httpx.Response(200, json=VERBOSE_JSON)
    )
    backend = OpenAIBackend(WhisperInstance(name="w", url="http://w:8000"))
    async with httpx.AsyncClient() as client:
        await backend.transcribe(client, audio, language=None, task="translate", vad_filter=False)
    assert route.called


@respx.mock
async def test_openai_backend_sends_the_api_key_when_configured(audio):
    route = respx.post("http://w:8000/v1/audio/transcriptions").mock(
        return_value=httpx.Response(200, json=VERBOSE_JSON)
    )
    backend = OpenAIBackend(WhisperInstance(name="w", url="http://w:8000", api_key="sk-local"))
    async with httpx.AsyncClient() as client:
        await backend.transcribe(client, audio, language=None, task="transcribe", vad_filter=False)
    assert route.calls[0].request.headers["Authorization"] == "Bearer sk-local"


@respx.mock
async def test_asr_webservice_backend_uses_query_params_and_audio_file_field(audio):
    route = respx.post("http://w:9000/asr").mock(
        return_value=httpx.Response(200, json=VERBOSE_JSON)
    )
    backend = AsrWebserviceBackend(WhisperInstance(name="w", url="http://w:9000"))
    async with httpx.AsyncClient() as client:
        result = await backend.transcribe(
            client, audio, language="en", task="transcribe", vad_filter=True, prompt="UPS"
        )
    request = route.calls[0].request
    assert request.url.params["output"] == "json"
    assert request.url.params["task"] == "transcribe"
    assert request.url.params["vad_filter"] == "true"
    assert request.url.params["initial_prompt"] == "UPS"
    assert b'name="audio_file"' in request.content
    assert result.text == "someone is at the door"


@respx.mock
async def test_asr_webservice_backend_accepts_a_plain_text_response(audio):
    respx.post("http://w:9000/asr").mock(
        return_value=httpx.Response(
            200, text="just the words", headers={"content-type": "text/plain"}
        )
    )
    backend = AsrWebserviceBackend(WhisperInstance(name="w", url="http://w:9000"))
    async with httpx.AsyncClient() as client:
        result = await backend.transcribe(
            client, audio, language=None, task="transcribe", vad_filter=False
        )
    assert result.text == "just the words"
    assert result.segments == []


@respx.mock
async def test_whisper_cpp_backend_posts_to_inference(audio):
    route = respx.post("http://w:8080/inference").mock(
        return_value=httpx.Response(200, json=VERBOSE_JSON)
    )
    backend = WhisperCppBackend(WhisperInstance(name="w", url="http://w:8080"))
    async with httpx.AsyncClient() as client:
        result = await backend.transcribe(
            client, audio, language="en", task="translate", vad_filter=False
        )
    assert route.called
    assert b"translate" in route.calls[0].request.content
    assert result.text == "someone is at the door"


@respx.mock
async def test_backend_error_body_is_surfaced(audio):
    respx.post("http://w:8000/v1/audio/transcriptions").mock(
        return_value=httpx.Response(422, text="model not loaded")
    )
    backend = OpenAIBackend(WhisperInstance(name="gpu", url="http://w:8000"))
    async with httpx.AsyncClient() as client:
        with pytest.raises(WhisperError, match="model not loaded"):
            await backend.transcribe(
                client, audio, language=None, task="transcribe", vad_filter=False
            )


def test_build_backend_refuses_an_unresolved_auto_instance():
    with pytest.raises(WhisperError, match="not resolved"):
        build_backend(WhisperInstance(name="w", url="http://w:8000", kind="auto"))


# --------------------------------------------------------------------------- #
# pool
# --------------------------------------------------------------------------- #


def _settings(instances: str, **kwargs) -> Settings:
    return Settings(whisper_instances=instances, _env_file=None, **kwargs)


@respx.mock
async def test_auto_detection_identifies_an_openai_server(audio):
    respx.get("http://w:8000/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "large-v3"}]})
    )
    pool = WhisperPool(_settings("w=http://w:8000"))
    states = await pool.probe_all(force=True)
    assert states[0].healthy
    assert states[0].instance.resolved_kind == "openai"
    await pool.aclose()


@respx.mock
async def test_auto_detection_falls_through_to_asr_webservice(audio):
    respx.get("http://w:9000/v1/models").mock(return_value=httpx.Response(404))
    respx.get("http://w:9000/openapi.json").mock(
        return_value=httpx.Response(200, json={"paths": {"/asr": {}}})
    )
    pool = WhisperPool(_settings("w=http://w:9000"))
    states = await pool.probe_all(force=True)
    assert states[0].instance.resolved_kind == "asr_webservice"
    await pool.aclose()


@respx.mock
async def test_unreachable_instance_is_marked_unhealthy_with_a_reason():
    respx.get("http://down:9000/v1/models").mock(side_effect=httpx.ConnectError("refused"))
    respx.get("http://down:9000/openapi.json").mock(side_effect=httpx.ConnectError("refused"))
    respx.get("http://down:9000/inference").mock(side_effect=httpx.ConnectError("refused"))
    pool = WhisperPool(_settings("down=http://down:9000"))
    states = await pool.probe_all(force=True)
    assert not states[0].healthy
    assert "unreachable" in states[0].detail
    await pool.aclose()


@respx.mock
async def test_pool_raises_a_helpful_error_when_nothing_is_reachable(audio):
    respx.get("http://down:9000/v1/models").mock(side_effect=httpx.ConnectError("refused"))
    respx.get("http://down:9000/openapi.json").mock(side_effect=httpx.ConnectError("refused"))
    respx.get("http://down:9000/inference").mock(side_effect=httpx.ConnectError("refused"))
    pool = WhisperPool(_settings("down=http://down:9000"))
    with pytest.raises(NoHealthyInstances, match="No Whisper instance is reachable"):
        await pool.transcribe_file(audio)
    await pool.aclose()


@respx.mock
async def test_pool_retries_a_failed_chunk_on_another_instance(audio):
    respx.get("http://a:8000/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "m"}]})
    )
    respx.get("http://b:8000/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "m"}]})
    )
    bad = respx.post("http://a:8000/v1/audio/transcriptions").mock(
        return_value=httpx.Response(500, text="CUDA out of memory")
    )
    good = respx.post("http://b:8000/v1/audio/transcriptions").mock(
        return_value=httpx.Response(200, json=VERBOSE_JSON)
    )
    pool = WhisperPool(_settings("a=http://a:8000|openai, b=http://b:8000|openai"))
    result = await pool.transcribe_file(audio, audio_seconds=4.2)

    # Which instance is tried first is deliberately not fixed (least-loaded, ties
    # broken at random), so assert on the outcome: the work lands on the healthy
    # one either way, and exactly one call succeeds.
    assert result.text == "someone is at the door"
    assert result.instance == "b"
    assert good.called
    by_name = {state.instance.name: state for state in pool.states}
    assert by_name["b"].completed == 1
    assert by_name["a"].completed == 0
    if bad.called:
        assert by_name["a"].failed == 1, "a failure must be recorded against the instance"
    await pool.aclose()


@respx.mock
async def test_pool_retries_on_the_same_instance_when_it_is_the_only_one(audio):
    respx.get("http://a:8000/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "m"}]})
    )
    respx.post("http://a:8000/v1/audio/transcriptions").mock(
        side_effect=[
            httpx.Response(503, text="model still loading"),
            httpx.Response(200, json=VERBOSE_JSON),
        ]
    )
    pool = WhisperPool(_settings("a=http://a:8000|openai"))
    result = await pool.transcribe_file(audio, audio_seconds=4.2)
    assert result.text == "someone is at the door"
    assert pool.states[0].failed == 1
    assert pool.states[0].completed == 1
    await pool.aclose()


@respx.mock
async def test_pool_gives_up_after_max_attempts_and_names_the_instances(audio):
    respx.get("http://a:8000/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "m"}]})
    )
    respx.post("http://a:8000/v1/audio/transcriptions").mock(
        return_value=httpx.Response(500, text="boom")
    )
    pool = WhisperPool(_settings("a=http://a:8000|openai", whisper_max_attempts=2))
    with pytest.raises(WhisperError, match="failed after 2 attempt"):
        await pool.transcribe_file(audio)
    assert pool.states[0].failed == 2
    await pool.aclose()


@respx.mock
async def test_pool_records_throughput_and_capacity(audio):
    respx.get("http://a:8000/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "m"}]})
    )
    respx.post("http://a:8000/v1/audio/transcriptions").mock(
        return_value=httpx.Response(200, json=VERBOSE_JSON)
    )
    pool = WhisperPool(_settings("a=http://a:8000|openai|m|3"))
    await pool.transcribe_file(audio, audio_seconds=60.0)
    state = pool.states[0]
    assert state.completed == 1
    assert state.total_audio_seconds == 60.0
    assert state.speed and state.speed > 0
    assert pool.total_capacity == 3
    assert state.as_dict()["kind"] == "openai"
    await pool.aclose()
