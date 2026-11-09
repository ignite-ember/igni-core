"""No RPC result carries a secret.

``get_model_registry`` answered with each model's ``api_key`` and
``api_key_cmd``, and ``get_hooks_details`` with an HTTP hook's headers
(an ``Authorization`` header is a credential) — to every window, every
program attached, and every extension granted ``models.registry`` or
``hooks.details``. A result says there is a key; never what it is.
"""

from __future__ import annotations

import json

from igni_core.backend.rpc_result import RpcResult
from igni_core.backend.schemas_panels import HookEntryView
from igni_core.backend.schemas_rpc import ModelRegistryResult
from igni_core.core.config.schemas.models import ModelsConfig
from igni_core.core.hooks.schemas import HookDefinition
from igni_core.protocol.redaction import url_without_credentials

SECRET = "sk-very-secret"


def _wire(value: object) -> str:
    return json.dumps(RpcResult.serialize(value))


def test_the_model_registry_says_a_model_has_a_key_and_never_what_it_is() -> None:
    models = ModelsConfig(
        default="claude",
        registry={
            "claude": {"model_id": "claude-x", "provider": "anthropic", "api_key": SECRET},
            "local": {
                "model_id": "llama",
                "url": f"https://user:{SECRET}@llm.example.com/v1?key={SECRET}",
                "api_key_cmd": f"echo {SECRET}",
                "source": "cloud",
                "token": SECRET,
            },
            "keyless": {"model_id": "m"},
        },
    )

    result = ModelRegistryResult.from_settings(models)
    wire = _wire(result)

    assert SECRET not in wire
    assert result.registry["claude"].has_api_key
    assert result.registry["local"].has_api_key
    assert not result.registry["keyless"].has_api_key
    assert result.registry["local"].url == "https://llm.example.com/v1"
    assert result.registry["local"].source == "cloud"
    assert sorted(result.registry) == ["claude", "keyless", "local"]


def test_a_hook_s_headers_are_named_and_never_valued() -> None:
    hook = HookDefinition(
        type="http",
        url=f"https://bot:{SECRET}@hooks.example.com/run?token={SECRET}",
        headers={"Authorization": f"Bearer {SECRET}", "X-Team": "core"},
    )

    view = HookEntryView.from_hook(hook, "PreToolUse")

    assert SECRET not in _wire(view)
    assert view.header_names == ["Authorization", "X-Team"]
    assert view.url == "https://hooks.example.com/run"


def test_a_url_without_credentials_keeps_what_is_not_one() -> None:
    assert url_without_credentials("https://a.example.com:8443/p") == "https://a.example.com:8443/p"
    assert url_without_credentials("") == ""
    assert url_without_credentials("not a url") == "not a url"
