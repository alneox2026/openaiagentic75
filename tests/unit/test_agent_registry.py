import pytest

from services.agent_gateway_v3.app.core.config import get_settings
from services.agent_gateway_v3.app.core.errors import ApiError
from services.agent_gateway_v3.app.services.agent_registry import get_agent_config, load_registry


@pytest.fixture(autouse=True)
def clear_registry_cache():
    get_settings.cache_clear()
    load_registry.cache_clear()
    yield
    get_settings.cache_clear()
    load_registry.cache_clear()


def test_get_agent_config_returns_openai_saved_agent() -> None:
    config = get_agent_config("openai_managed_agent")
    assert config.agent_id == "openai_managed_agent"
    assert config.backend == "openai_managed"
    assert config.remote_agent_id == "agent_bc86941978354b21a682cde68398daca57998fc854d74ce3a1"
    assert config.model == "gpt-6-luna"
    assert config.streaming_enabled is True

def test_get_agent_config_rejects_unknown_agent() -> None:
    with pytest.raises(ApiError) as exc_info:
        get_agent_config("unknown-agent")
    assert exc_info.value.code == "unknown_agent"


def test_get_agent_config_applies_openai_agent_env_overrides(tmp_path, monkeypatch) -> None:
    registry_path = tmp_path / "agents.yaml"
    registry_path.write_text(
        """
agents:
  openai_test_agent:
    agent_id: openai_test_agent
    backend: openai_managed
    remote_agent_id: agent_old123
    model: gpt-6-luna
    persistence_enabled: true
""",
        encoding="utf-8",
    )

    monkeypatch.setenv("AGENT_REGISTRY_PATH", str(registry_path))
    monkeypatch.setenv(
        "AGENT_OPENAI_TEST_AGENT_REMOTE_AGENT_ID",
        "agent_new123",
    )
    monkeypatch.setenv("AGENT_OPENAI_TEST_AGENT_MODEL", "gpt-6-luna")

    agent_config = get_agent_config("openai_test_agent")

    assert agent_config.remote_agent_id == "agent_new123"
    assert agent_config.model == "gpt-6-luna"
