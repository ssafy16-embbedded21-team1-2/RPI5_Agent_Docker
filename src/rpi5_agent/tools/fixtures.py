from rpi5_agent.models import Scenario, ToolCall, ToolError, ToolResult


def collect_fixture(scenario: Scenario) -> list[ToolResult]:
    """Return synthetic decoded observations; CAN payload layout is not assumed."""
    bus_off = scenario == Scenario.BUS_OFF
    bus = ToolResult(
        tool="can.bus_state",
        target="rpi_adapter",
        status="ok",
        data={"state": "bus_off" if bus_off else "active", "tec": None, "rec": None},
    )
    gateway = ToolResult(
        tool="gateway.snapshot",
        target="cell1",
        status="unavailable" if bus_off else "ok",
        data={}
        if bus_off
        else {
            "gateway_age_ms": 20,
            "bms_age_ms": 2500 if scenario == Scenario.BMS_TIMEOUT else 50,
            "bms_valid": True,
        },
        error="No fresh gateway evidence available" if bus_off else None,
    )
    bus.validity = "valid"
    gateway.origin_ecu = "cell1"
    gateway.transport = "gateway_cache"
    gateway.validity = "unknown" if bus_off else "valid"
    gateway.freshness = "unknown" if bus_off else "fresh"
    if scenario == Scenario.ECU_UNRESPONSIVE:
        gateway.status = "timeout"
        gateway.data = {}
        gateway.error = ToolError(code="tool_timeout")
        gateway.freshness = "unknown"
    evidence = [bus, gateway]
    if scenario == Scenario.ECU_REBOOTED:
        evidence.append(
            ToolResult(
                tool="can.read_boot_record",
                target="cell2",
                status="ok",
                origin_ecu="cell2",
                transport="can_record",
                boot_id="boot-current",
                schema_version="fixture-v1",
                validity="valid",
                data={"previous_boot_id": "boot-previous", "current_boot_id": "boot-current"},
            )
        )
    crash_scenarios = {
        Scenario.CRASH_RECORDED,
        Scenario.CRASH_RECORD_ABSENT,
        Scenario.CRASH_RECORD_UNSUPPORTED,
        Scenario.CRASH_RECORD_TIMEOUT,
        Scenario.CRASH_RECORD_INVALID,
        Scenario.SYMBOLS_MISSING,
        Scenario.SYMBOL_BUILD_MISMATCH,
    }
    if scenario in crash_scenarios:
        status = {
            Scenario.CRASH_RECORD_UNSUPPORTED: "unsupported",
            Scenario.CRASH_RECORD_TIMEOUT: "timeout",
            Scenario.CRASH_RECORD_INVALID: "invalid",
        }.get(scenario, "ok")
        present = scenario != Scenario.CRASH_RECORD_ABSENT
        record = ToolResult(
            tool="can.read_crash_record",
            target="cell2",
            status=status,
            origin_ecu="cell2",
            transport="can_record",
            boot_id="boot-current",
            schema_version="fixture-v1",
            running_build_id="firmware-current",
            record_id="record-1" if present and status == "ok" else None,
            capture_boot_id="boot-previous" if present and status == "ok" else None,
            capture_uptime_ms=12500 if present and status == "ok" else None,
            record_build_id="firmware-at-fault" if present and status == "ok" else None,
            validity="valid" if status == "ok" else "unknown",
            # A valid historical record does not assert a current fault or fresh heartbeat.
            freshness="unknown",
            field_validity={"stacked_pc": True} if present and status == "ok" else {},
            data={
                "record_present": present,
                "fault_kind": "hard_fault",
                "stacked_pc": 134222388,
                "frame_valid": True,
            }
            if present and status == "ok"
            else {"record_present": False}
            if not present
            else {},
            error=ToolError(
                code={
                    "unsupported": "service_unsupported",
                    "timeout": "tool_timeout",
                    "invalid": "record_invalid",
                }[status]
            )
            if status != "ok"
            else None,
        )
        evidence.append(record)
        if scenario in {Scenario.SYMBOLS_MISSING, Scenario.SYMBOL_BUILD_MISMATCH}:
            evidence.append(
                ToolResult(
                    tool="analyze.symbols",
                    target="cell2",
                    status="unavailable",
                    origin_ecu="cell2",
                    record_id=record.record_id,
                    record_build_id=record.record_build_id,
                    error=ToolError(
                        code="symbols_missing"
                        if scenario == Scenario.SYMBOLS_MISSING
                        else "symbol_build_mismatch"
                    ),
                )
            )
    if scenario == Scenario.DEBUG_ACCESS_UNAVAILABLE:
        evidence.append(
            ToolResult(
                tool="swd.read_fault",
                target="cell2",
                status="unavailable",
                transport="bench_swd",
                error=ToolError(code="debug_access_unavailable", reason="probe_missing"),
            )
        )
    return evidence


class FixtureProvider:
    """Synthetic observations, including synthetic access denial; never uses USB/CAN."""

    def plan(self, scenario: Scenario) -> list[ToolCall]:
        return [ToolCall(tool=item.tool, target=item.target) for item in collect_fixture(scenario)]

    def read(self, call: ToolCall, scenario: Scenario) -> ToolResult:
        return next(
            item
            for item in collect_fixture(scenario)
            if (item.tool, item.target) == (call.tool, call.target)
        )
