"""Trusted, synchronous provider boundary; no physical provider is installed yet."""

from typing import Protocol

from rpi5_agent.models import DiagnosticProfile, Scenario, ToolCall, ToolError, ToolResult

PRODUCT_READ_TOOLS = frozenset(
    {
        "can.bus_state",
        "gateway.snapshot",
        "can.get_identity",
        "can.check_dtc",
        "can.read_boot_record",
        "can.read_crash_record",
        "analyze.symbols",
    }
)
BENCH_READ_TOOLS = frozenset({"swd.read_fault", "swd.read_sfr"})
MAX_TOOL_CALLS = 16


class EvidenceProvider(Protocol):
    """Implementations must bound their own I/O time and return sanitized errors."""

    def plan(self, scenario: Scenario) -> list[ToolCall]: ...

    def read(self, call: ToolCall, scenario: Scenario) -> ToolResult: ...


def allowed_tools(profile: DiagnosticProfile) -> frozenset[str]:
    return PRODUCT_READ_TOOLS | (BENCH_READ_TOOLS if profile == DiagnosticProfile.BENCH else set())


def collect_evidence(
    provider: EvidenceProvider, scenario: Scenario, profile: DiagnosticProfile
) -> list[ToolResult]:
    # Check the whole plan before any read. A provider is trusted application code,
    # not an LLM tool, subprocess command, or a remotely selected plugin.
    calls = provider.plan(scenario)
    if len(calls) > MAX_TOOL_CALLS:
        raise ValueError("Evidence plan exceeds the tool call limit")
    keys = [(call.tool, call.target) for call in calls]
    if len(keys) != len(set(keys)):
        raise ValueError("Evidence plan contains duplicate tool/target pairs")
    evidence = []
    for call in calls:
        if call.tool not in allowed_tools(profile):
            evidence.append(
                ToolResult(
                    **call.model_dump(),
                    status="unavailable",
                    transport="bench_swd" if call.tool.startswith("swd.") else None,
                    error=ToolError(
                        code="debug_access_unavailable"
                        if call.tool.startswith("swd.")
                        else "tool_not_allowed",
                        reason="profile_disabled",
                    ),
                )
            )
            continue
        try:
            result = provider.read(call, scenario)
            # Revalidate mutable models so bad/NaN data cannot break JSON response
            # serialization after assessment has already preserved other findings.
            result = ToolResult.model_validate(result.model_dump())
            if (result.tool, result.target) != (call.tool, call.target):
                raise ValueError("Provider returned a different tool/target")
        except Exception as exc:
            # No retries or fallback debug/reset/flash calls. Preserve other evidence.
            result = ToolResult(
                **call.model_dump(),
                status="timeout" if isinstance(exc, TimeoutError) else "error",
                error=ToolError(
                    code="tool_timeout" if isinstance(exc, TimeoutError) else "collection_failed"
                ),
            )
        evidence.append(result)
    return evidence
