from __future__ import annotations

from acp.opencode_client import OpenCodeACP


def test_build_prompt_params_includes_max_tokens_by_default():
    acp = OpenCodeACP(max_tokens=500)
    params = acp._build_prompt_params("sid-1", [{"type": "text", "text": "hi"}])
    assert params["sessionId"] == "sid-1"
    assert params["maxTokens"] == 500


def test_build_prompt_params_omits_max_tokens_when_disabled():
    acp = OpenCodeACP(max_tokens=0)
    params = acp._build_prompt_params("sid-2", [{"type": "text", "text": "hi"}])
    assert params["sessionId"] == "sid-2"
    assert "maxTokens" not in params
