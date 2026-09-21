from __future__ import annotations

import pytest

from app.config import Settings, _parse_instances


def test_shorthand_instances_parse_name_kind_model_and_concurrency():
    instances = _parse_instances(
        "faster=http://10.0.0.5:8000|openai|large-v3|3, asr=http://10.0.0.5:9000|asr_webservice"
    )
    assert [i.name for i in instances] == ["faster", "asr"]
    assert instances[0].kind == "openai"
    assert instances[0].model == "large-v3"
    assert instances[0].concurrency == 3
    assert instances[1].kind == "asr_webservice"
    # Unspecified concurrency defaults to one in-flight request.
    assert instances[1].concurrency == 1


def test_bare_url_defaults_to_auto_detection():
    (instance,) = _parse_instances("http://10.0.0.9:9000/")
    assert instance.kind == "auto"
    assert instance.url == "http://10.0.0.9:9000", "trailing slash should be trimmed"
    assert instance.name == "whisper1"


def test_json_instances_parse():
    instances = _parse_instances(
        '[{"name":"gpu","url":"http://x:8000","kind":"openai","concurrency":2,"api_key":"k"}]'
    )
    assert instances[0].name == "gpu"
    assert instances[0].concurrency == 2
    assert instances[0].api_key == "k"


def test_json_instances_require_a_url():
    with pytest.raises(ValueError, match="missing 'url'"):
        _parse_instances('[{"name":"oops"}]')


def test_empty_instances_is_not_an_error():
    assert _parse_instances("") == []
    assert _parse_instances("   ,  ") == []


def test_concurrency_is_clamped_to_at_least_one():
    (instance,) = _parse_instances("http://x:8000|openai||0")
    assert instance.concurrency == 1


def test_host_scheme_is_stripped_and_base_url_built():
    settings = Settings(protect_host="https://192.168.1.1/", protect_port=8443, _env_file=None)
    assert settings.protect_host == "192.168.1.1"
    assert settings.protect_base_url == "https://192.168.1.1:8443"


def test_configured_requires_host_credentials_and_an_instance():
    base = {"protect_host": "nvr", "_env_file": None}
    assert not Settings(**base).configured(), "no credentials, no instances"
    assert not Settings(**base, protect_api_key="k").configured(), "no instances"
    assert Settings(**base, protect_api_key="k", whisper_instances="http://w:9000").configured()
    assert Settings(
        **base, protect_username="u", protect_password="p", whisper_instances="http://w:9000"
    ).configured()


def test_data_dir_layout(tmp_path):
    settings = Settings(data_dir=tmp_path / "d", _env_file=None)
    settings.ensure_dirs()
    assert settings.clips_dir.is_dir()
    assert settings.audio_dir.is_dir()
    assert settings.transcripts_dir.is_dir()
    assert settings.db_path.parent == tmp_path / "d"
