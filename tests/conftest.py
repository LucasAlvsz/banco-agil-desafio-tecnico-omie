import socket

import pytest

from banco_agil.config import Settings


def pytest_configure(config):
    # Bloqueia rede também na coleta/importação, antes de carregar os SDKs.
    patch = pytest.MonkeyPatch()
    config.add_cleanup(patch.undo)
    patch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    patch.setenv("LITELLM_TELEMETRY", "False")
    patch.setenv("LANGCHAIN_TRACING_V2", "false")
    patch.setenv("LANGSMITH_TRACING", "false")

    def no_network(*args, **kwargs):
        raise AssertionError("Rede proibida no pytest")

    for name in ("connect", "connect_ex", "sendto"):
        patch.setattr(socket.socket, name, no_network)
    patch.setattr(socket, "getaddrinfo", no_network)
    patch.setattr(socket, "create_connection", no_network)


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
