"""Independent fixture acceptance cases; expected labels never go into model input."""

from collections import Counter
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel

from rpi5_agent.models import Diagnosis, Scenario
from rpi5_agent.specification import FIXTURE_SPECIFICATION

CASE_CATALOG_ID = "fixture-acceptance-v1"

# Enumerated expectations, not computed by the fixture generator or the assessor.
EXPECTED_CODES = MappingProxyType(
    {
        Scenario.HEALTHY: frozenset({"GATEWAY_FRESH", "BMS_FRESH"}),
        Scenario.BMS_TIMEOUT: frozenset({"GATEWAY_FRESH", "BMS_STALE"}),
        Scenario.BUS_OFF: frozenset({"LOCAL_ADAPTER_BUS_OFF", "REMOTE_STATE_UNKNOWN"}),
        Scenario.ECU_UNRESPONSIVE: frozenset({"GATEWAY_EVIDENCE_UNAVAILABLE"}),
        Scenario.ECU_REBOOTED: frozenset({"GATEWAY_FRESH", "BMS_FRESH", "ECU_BOOT_CHANGED"}),
        Scenario.CRASH_RECORDED: frozenset(
            {"GATEWAY_FRESH", "BMS_FRESH", "CRASH_RECORD_PRESENT", "CRASH_SOURCE_LOCATION_UNKNOWN"}
        ),
        Scenario.CRASH_RECORD_ABSENT: frozenset(
            {"GATEWAY_FRESH", "BMS_FRESH", "CRASH_RECORD_ABSENT"}
        ),
        Scenario.CRASH_RECORD_UNSUPPORTED: frozenset(
            {"GATEWAY_FRESH", "BMS_FRESH", "CRASH_RECORD_UNSUPPORTED"}
        ),
        Scenario.CRASH_RECORD_TIMEOUT: frozenset(
            {"GATEWAY_FRESH", "BMS_FRESH", "CRASH_RECORD_TIMEOUT"}
        ),
        Scenario.CRASH_RECORD_INVALID: frozenset(
            {"GATEWAY_FRESH", "BMS_FRESH", "CRASH_RECORD_INVALID"}
        ),
        Scenario.SYMBOLS_MISSING: frozenset(
            {"GATEWAY_FRESH", "BMS_FRESH", "CRASH_RECORD_PRESENT", "SYMBOLS_MISSING"}
        ),
        Scenario.SYMBOL_BUILD_MISMATCH: frozenset(
            {"GATEWAY_FRESH", "BMS_FRESH", "CRASH_RECORD_PRESENT", "SYMBOL_BUILD_MISMATCH"}
        ),
        Scenario.DEBUG_ACCESS_UNAVAILABLE: frozenset(
            {"GATEWAY_FRESH", "BMS_FRESH", "DEBUG_ACCESS_UNAVAILABLE"}
        ),
    }
)


class FixtureVerification(BaseModel):
    scope: Literal["fixture_findings"] = "fixture_findings"
    case_catalog_id: str = CASE_CATALOG_ID
    case_id: Scenario
    verdict: Literal["passed", "failed", "not_evaluated"]
    expected_finding_codes: list[str]
    actual_finding_codes: list[str]
    missing_finding_codes: list[str]
    unexpected_finding_codes: list[str]
    duplicate_finding_codes: list[str]
    physical_validation: Literal[False] = False
    llm_semantics: Literal["not_evaluated"] = "not_evaluated"
    diagnosis: Diagnosis


def verify_fixture(diagnosis: Diagnosis) -> FixtureVerification:
    expected = EXPECTED_CODES[diagnosis.scenario]
    actual = Counter(item.code for item in diagnosis.findings)
    missing = sorted(expected - actual.keys())
    unexpected = sorted(actual.keys() - expected)
    duplicates = sorted(code for code, count in actual.items() if count > 1)
    if diagnosis.assessment_specification_id != FIXTURE_SPECIFICATION.id:
        verdict = "not_evaluated"
    else:
        verdict = "failed" if missing or unexpected or duplicates else "passed"
    return FixtureVerification(
        case_id=diagnosis.scenario,
        verdict=verdict,
        expected_finding_codes=sorted(expected),
        actual_finding_codes=sorted(actual),
        missing_finding_codes=missing,
        unexpected_finding_codes=unexpected,
        duplicate_finding_codes=duplicates,
        diagnosis=diagnosis,
    )
