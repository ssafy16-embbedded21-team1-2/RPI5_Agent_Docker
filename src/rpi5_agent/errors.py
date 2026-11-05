from typing import Literal

from rpi5_agent.models import LLMMetadata


class SummaryError(ValueError):
    """A model response was received but cannot be used as a complete explanation."""

    def __init__(
        self,
        code: Literal["output_truncated", "invalid_model_output", "unexpected_finish_reason"],
        metadata: LLMMetadata,
    ):
        super().__init__(code)
        self.code = code
        self.metadata = metadata
