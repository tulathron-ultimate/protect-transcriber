"""Guard the Unraid template against drift.

The template is what most people will actually install with, so a variable name
that no longer matches a setting, or a port that disagrees with the Dockerfile,
is a real bug -- and an invisible one until someone's container will not start.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from app.config import Settings

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "unraid" / "protect-transcriber.xml"
PROFILE = REPO / "ca_profile.xml"
RAW_PREFIX = "https://raw.githubusercontent.com/tulathron-ultimate/protect-transcriber/main/"


@pytest.fixture(scope="module")
def template() -> ET.Element:
    return ET.parse(TEMPLATE).getroot()


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return (REPO / "Dockerfile").read_text()


def _configs(root: ET.Element, kind: str) -> list[ET.Element]:
    return [c for c in root.findall("Config") if c.get("Type") == kind]


def test_template_and_profile_are_well_formed(template: ET.Element):
    assert template.tag == "Container"
    assert ET.parse(PROFILE).getroot().tag == "Maintainer"


def test_every_variable_maps_to_a_real_setting(template: ET.Element):
    """A typo'd variable name would be silently ignored by the app."""
    known = {name.upper() for name in Settings.model_fields}
    unknown = [
        config.get("Target")
        for config in _configs(template, "Variable")
        if config.get("Target", "").upper() not in known
    ]
    assert not unknown, f"template sets variables the app does not read: {unknown}"


def test_required_settings_are_present_and_marked_required(template: ET.Element):
    variables = {c.get("Target"): c for c in _configs(template, "Variable")}
    # Protect credentials have no in-app equivalent, so they must be set up front.
    for name in ("PROTECT_HOST", "PROTECT_USERNAME", "PROTECT_PASSWORD"):
        assert name in variables, f"{name} must be in the template; the app cannot work without it"
        assert variables[name].get("Required") == "true", f"{name} should be marked required"


def test_whisper_instances_is_optional_because_the_ui_manages_them(template: ET.Element):
    """Instances are editable at runtime, so the env var is only a seed."""
    variables = {c.get("Target"): c for c in _configs(template, "Variable")}
    assert "WHISPER_INSTANCES" in variables
    config = variables["WHISPER_INSTANCES"]
    assert config.get("Required") == "false"
    assert "WebUI" in (config.get("Description") or "")


def test_secrets_are_masked(template: ET.Element):
    """Unraid shows unmasked values in plain text on the container page."""
    for name in ("PROTECT_PASSWORD", "PROTECT_API_KEY", "APP_TOKEN"):
        config = next(c for c in _configs(template, "Variable") if c.get("Target") == name)
        assert config.get("Mask") == "true", f"{name} must be masked in the UI"


def test_variable_defaults_are_valid_for_the_app(template: ET.Element):
    """Defaults shipped in the template must actually parse into Settings."""
    defaults = {
        config.get("Target").lower(): (config.text or "").strip()
        for config in _configs(template, "Variable")
        if (config.text or "").strip()
    }
    # Should not raise -- this is the same validation the container does at boot.
    settings = Settings(_env_file=None, **defaults)
    assert settings.chunk_seconds > 0
    assert settings.max_range_seconds > 0


def test_port_matches_the_dockerfile_and_webui(template: ET.Element, dockerfile: str):
    port = next(c for c in _configs(template, "Port") if c.get("Target") == "8099")
    assert port.get("Default") == "8099"
    assert "EXPOSE 8099" in dockerfile
    assert "--port" in dockerfile and "8099" in dockerfile
    assert "[PORT:8099]" in template.findtext("WebUI", "")


def test_data_path_matches_the_container_default(template: ET.Element, dockerfile: str):
    path = next(c for c in _configs(template, "Path") if c.get("Target") == "/data")
    assert path.get("Mode") == "rw", "clips and the database need write access"
    assert "DATA_DIR=/data" in dockerfile
    assert Settings(_env_file=None).data_dir.as_posix() == "/data"


def test_host_path_follows_the_unraid_appdata_convention(template: ET.Element):
    """Persistent data belongs at /mnt/user/appdata/<container-name>."""
    name = template.findtext("Name", "")
    expected = f"/mnt/user/appdata/{name}"
    path = next(c for c in _configs(template, "Path") if c.get("Target") == "/data")
    assert path.get("Default") == expected
    assert (path.text or "").strip() == expected, "the element value is what Unraid pre-fills"


def test_image_is_pullable_rather_than_built_locally(template: ET.Element):
    repository = template.findtext("Repository", "")
    assert repository.startswith("ghcr.io/"), "the template must reference a published image"
    assert ":" in repository.rsplit("/", 1)[-1], "pin an explicit tag"


def test_repo_relative_urls_point_at_files_that_exist(template: ET.Element):
    """A broken icon or TemplateURL only shows up after publishing, so check here."""
    urls = [template.findtext("Icon", ""), template.findtext("TemplateURL", "")]
    urls.append(ET.parse(PROFILE).getroot().findtext("Icon", ""))
    for url in urls:
        assert url.startswith(RAW_PREFIX), f"{url} should be a raw URL on the default branch"
        relative = url[len(RAW_PREFIX) :]
        assert (REPO / relative).exists(), f"{relative} is referenced but missing from the repo"


def test_template_url_points_at_this_template(template: ET.Element):
    assert template.findtext("TemplateURL", "").endswith("unraid/protect-transcriber.xml")


def test_documented_links_are_on_the_default_branch(template: ET.Element):
    """Links baked into the template must not point at a feature branch."""
    text = TEMPLATE.read_text()
    branches = re.findall(r"githubusercontent\.com/[^/]+/[^/]+/([^/]+)/", text)
    assert set(branches) <= {"main"}, f"template links reference non-main branches: {set(branches)}"
