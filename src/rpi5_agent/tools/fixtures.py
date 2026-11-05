from rpi5_agent.models import Scenario, ToolResult


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
            "gateway_timeout_ms": 500,
            "bms_age_ms": 2500 if scenario == Scenario.BMS_TIMEOUT else 50,
            "bms_valid": True,
            "bms_timeout_ms": 1000,
        },
        error="No fresh gateway evidence available" if bus_off else None,
    )
    return [bus, gateway]
