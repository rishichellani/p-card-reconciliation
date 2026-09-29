class PipelineError(Exception):
    """Fatal, run-level failure (missing input, integrity violation, bad config). Row-level problems never raise this."""


class ArtifactIntegrityError(PipelineError):
    """A persisted artifact no longer matches its recorded hash, or the chain of custody is broken."""


class LLMResponseError(Exception):
    """The LLM returned something unusable (refusal, truncation, non-JSON, schema violation) or the call failed."""


class LLMAuthError(PipelineError):
    """Credentials missing/invalid. Fatal: retrying 25 transactions would fail identically."""
