"""Offline tests for provider routing/failover and audit-response hardening (no network, no keys)."""
import pytest

from schemas.models import LLMAuditResult
from stages.llm_auditor import LLMAuditor
from stages.stage3_audit import run as run_stage3  # noqa: F401  (import check)
from utils.errors import LLMAuthError, LLMResponseError, PipelineError
from utils.llm_client import LLMClient, ProviderError, load_providers

GOOD = '{"verdict":"approve","policy_sections":["s2.1"],"rationale":"Specific and compliant.","confidence":0.9,"red_flags":[]}'


class FakeClient(LLMClient):
    """Replaces the HTTP layer with scripted per-provider behaviour."""

    def __init__(self, providers, script):
        super().__init__(providers, sleep=lambda s: None, clock=lambda: 0.0)
        self.script, self.calls = script, []

    def _request(self, p, st, system, user):
        self.calls.append(p.name)
        out = self.script[p.name]
        out = out.pop(0) if isinstance(out, list) else out
        if isinstance(out, Exception):
            raise out
        return out


def providers(*names):
    env = {f"{n.upper()}_API_KEY": "k" for n in names}
    return load_providers(env, order=",".join(names))


def test_load_providers_requires_a_key_and_validates_names():
    with pytest.raises(LLMAuthError):
        load_providers({})
    with pytest.raises(PipelineError):
        load_providers({"GROQ_API_KEY": "k"}, order="skynet")
    assert [p.name for p in load_providers({"GROQ_API_KEY": "k", "GEMINI_API_KEY": "k"})] == ["gemini", "groq"]


def test_failover_to_next_provider():
    c = FakeClient(providers("gemini", "groq"), {"gemini": ProviderError("rate limited", retry_after=30), "groq": GOOD})
    reply = c.complete("s", "u")
    assert reply.provider == "groq" and c.calls == ["gemini", "groq"]


def test_bad_credentials_disable_provider_for_rest_of_run():
    c = FakeClient(providers("gemini", "groq"), {"gemini": ProviderError("bad key", permanent=True), "groq": GOOD})
    c.complete("s", "u"), c.complete("s", "u")
    assert c.calls == ["gemini", "groq", "groq"]


def test_all_providers_failing_raises_recoverable_error():
    c = FakeClient(providers("gemini"), {"gemini": ProviderError("boom")})
    with pytest.raises(LLMResponseError, match="all LLM providers failed"):
        c.complete("s", "u")


def test_invalid_json_retries_on_a_different_provider(mock_item):
    c = FakeClient(providers("gemini", "groq"), {"gemini": "I think it's fine!", "groq": GOOD})
    result = LLMAuditor("policy", c).audit(mock_item, "SOFTWARE", [])
    assert result.verdict == "APPROVE" and result.passed and result.served_by.startswith("groq:")


def test_verdict_synonyms_and_passed_flag():
    r = LLMAuditResult.model_validate({"verdict": "Fail", "rationale": "Prohibited item.", "confidence": 0.8})
    assert r.verdict == "REJECT" and not r.passed


@pytest.fixture
def mock_item(tmp_path):
    from stages import stage1_ingest, stage2_justify
    from schemas.models import Stage2Payload
    from tests.test_pipeline import make_ctx
    from utils.artifacts import read_json_artifact
    ctx = make_ctx(tmp_path)
    ctx.run_dir.mkdir()
    stage1_ingest.run(ctx)
    stage2_justify.run(ctx)
    return read_json_artifact(ctx, 2, Stage2Payload)[1].items[0]
