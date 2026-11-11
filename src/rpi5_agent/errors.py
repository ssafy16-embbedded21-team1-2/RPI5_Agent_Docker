from rpi5_agent.models import LLMMetadata, LLMRouting


class SummaryError(ValueError):
    """A model call or response failed; only sanitized diagnostics are exposed."""

    def __init__(
        self,
        code: str,
        metadata: LLMMetadata | None = None,
        routing: LLMRouting | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.metadata = metadata
        self.routing = routing
