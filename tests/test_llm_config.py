import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from rpi5_agent.api import create_app
from rpi5_agent.config import Settings


def config(**kwargs):
    return Settings(_env_file=None, **kwargs)


def test_external_is_opt_in_and_explicit_request_is_rejected_when_disabled():
    settings = config(llm_enabled=True, external_llm_enabled=False)
    with TestClient(create_app(settings)) as client:
        response = client.post("/diagnose", json={"use_llm": True, "llm_target": "external"})
        assert response.status_code == 409
        assert client.post("/diagnose", json={"llm_target": "external"}).status_code == 422
        assert (
            client.post("/diagnose", json={"use_llm": True, "llm_target": "unknown"}).status_code
            == 422
        )


@pytest.mark.parametrize("key", ["", " ", "not-required", "test-key\n"])
def test_external_enabled_requires_a_real_header_safe_key(key):
    with pytest.raises(ValidationError):
        config(external_llm_enabled=True, external_llm_api_key=key)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://llm.pamout.com/v1",
        "http://llm.pamout.com/v1",
        "https://user:password@llm.pamout.com/v1",
        "https://llm.pamout.com/v1?key=secret",
        "https://llm.pamout.com/v1#secret",
    ],
)
def test_external_url_disallows_insecure_remote_transport_and_embedded_credentials(url):
    with pytest.raises(ValidationError):
        config(external_llm_base_url=url)


@pytest.mark.parametrize("user_agent", ["", " ", "app\n1", "에이전트"])
def test_pamout_user_agent_is_header_safe(user_agent):
    with pytest.raises(ValidationError):
        config(external_llm_user_agent=user_agent)


def test_configuration_errors_and_representation_do_not_expose_key():
    key = "private-config-secret"
    settings = config(external_llm_enabled=True, external_llm_api_key=key)
    assert key not in repr(settings)
    assert key not in settings.model_dump_json()
    with pytest.raises(ValidationError) as exc:
        config(external_llm_api_key=key, external_llm_timeout_seconds=0)
    assert key not in str(exc.value)
