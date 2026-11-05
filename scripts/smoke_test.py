"""Check the running agent with Python's standard library; no model or CAN needed."""

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")
    try:
        with urlopen(f"{base_url}/health", timeout=10) as response:
            health = json.load(response)
        if health.get("mode") != "fixture" or health.get("status") != "ok":
            raise ValueError("Unexpected health response")
        for scenario, expected in (
            ("healthy", "BMS_FRESH"),
            ("bms_timeout", "BMS_STALE"),
            ("bus_off", "REMOTE_STATE_UNKNOWN"),
        ):
            request = Request(
                f"{base_url}/diagnose",
                data=json.dumps({"scenario": scenario, "use_llm": False}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=10) as response:
                result = json.load(response)
            if result.get("mode") != "fixture":
                raise ValueError("Synthetic diagnostics must be marked as fixture")
            if expected not in {item["code"] for item in result["findings"]}:
                raise ValueError(f"{scenario}: expected {expected}")
            print(f"PASS {scenario}: {expected}")
    except (HTTPError, URLError, TimeoutError, ValueError, KeyError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
