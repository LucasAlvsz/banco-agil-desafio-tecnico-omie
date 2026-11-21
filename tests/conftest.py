import socket

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field

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


class ScriptedChatModel(BaseChatModel):
    responses: list[Exception | AIMessage]
    calls: list[dict] = Field(default_factory=list)
    bindings: int = 0

    @property
    def _llm_type(self):
        return "scripted-offline"

    def bind_tools(self, tools, **kwargs):
        self.bindings += 1
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append({"messages": messages, "tools": kwargs.get("tools")})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return ChatResult(generations=[ChatGeneration(message=response)])


@pytest.fixture
def scripted_model():
    return ScriptedChatModel
