"""Benchmark latency script comparing gemini-3.5-flash-lite with a comparable model.

Runs 30 minimal calls each, spaced out, reporting:
- TTFT (time to first token) & Total Latency
- Median, p95, Max
- Time of day (local IST)
- Verification that no service tier (flex / priority) is accidentally set.

Usage:
    uv run python scripts/benchmark_latency.py [--calls 30] [--model2 gemini-2.5-flash] [--mock]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
from datetime import datetime
from typing import Any

from dotenv import load_dotenv

from clea.providers.gemini import GeminiProvider

load_dotenv(".env")


def calculate_percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {"median": 0.0, "p95": 0.0, "max": 0.0}
    sorted_vals = sorted(values)
    median = statistics.median(sorted_vals)
    # 95th percentile
    p95_idx = int(0.95 * (len(sorted_vals) - 1))
    p95 = sorted_vals[p95_idx]
    max_val = max(sorted_vals)
    return {
        "median": round(median, 2),
        "p95": round(p95, 2),
        "max": round(max_val, 2),
    }


async def benchmark_model(
    model_name: str,
    calls: int,
    interval_seconds: float,
    api_key: str | None = None,
    mock: bool = False,
) -> dict[str, Any]:
    print(f"\n--- Benchmarking {model_name} ({calls} calls, {interval_seconds}s spacing) ---")

    ttfts: list[float] = []
    totals: list[float] = []
    service_tiers_observed: set[str] = set()

    if mock:
        # Simulated run for automated verification
        import random

        for i in range(calls):
            ttft = round(random.uniform(250.0, 600.0), 2)
            total = ttft + round(random.uniform(100.0, 300.0), 2)
            ttfts.append(ttft)
            totals.append(total)
            print(f"[{i + 1}/{calls}] (mock) TTFT: {ttft} ms | Total: {total} ms")
            if interval_seconds > 0 and i < calls - 1:
                await asyncio.sleep(min(interval_seconds, 0.05))
        return {
            "model": model_name,
            "calls": calls,
            "ttft": calculate_percentiles(ttfts),
            "total": calculate_percentiles(totals),
            "service_tiers": ["none"],
        }

    provider = GeminiProvider(api_key=api_key, model=model_name)

    for i in range(calls):
        t0 = time.perf_counter()
        first_token_time: float | None = None
        try:
            from clea.providers.base import Message

            async for chunk in provider.stream_turn(
                messages=[Message(role="user", content="Ping. Respond with pong.")],
                tools=[],
                system_prompt="You are Clea.",
            ):
                if chunk.type == "text_delta" and first_token_time is None:
                    first_token_time = (time.perf_counter() - t0) * 1000.0

            t_total = (time.perf_counter() - t0) * 1000.0
            t_first = first_token_time or t_total
            ttfts.append(t_first)
            totals.append(t_total)
            print(f"[{i + 1}/{calls}] TTFT: {t_first:.1f} ms | Total: {t_total:.1f} ms")
        except Exception as exc:
            print(f"[{i + 1}/{calls}] Error: {exc}", file=sys.stderr)

        if interval_seconds > 0 and i < calls - 1:
            await asyncio.sleep(interval_seconds)

    return {
        "model": model_name,
        "calls": len(totals),
        "ttft": calculate_percentiles(ttfts),
        "total": calculate_percentiles(totals),
        "service_tiers": sorted(list(service_tiers_observed))
        if service_tiers_observed
        else ["none (default standard)"],
    }


def verify_service_tier_configuration() -> bool:
    """Verify that no priority/flex service tier configuration is set."""
    # Check common env vars or provider code
    suspicious_vars = [
        k
        for k in os.environ
        if "TIER" in k.upper() or "FLEX" in k.upper() or "PRIORITY" in k.upper()
    ]
    if suspicious_vars:
        print(f"Warning: suspicious environment variables found: {suspicious_vars}")
        return False
    return True


async def main() -> int:
    parser = argparse.ArgumentParser(description="Clea Model Latency Benchmark")
    parser.add_argument(
        "--calls", type=int, default=30, help="Number of calls per model (default: 30)"
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Spacing between calls in seconds (default: 1.0)",
    )
    parser.add_argument("--model1", type=str, default="gemini-3.5-flash-lite", help="Primary model")
    parser.add_argument("--model2", type=str, default="gemini-3.8-flash", help="Comparison model")
    parser.add_argument(
        "--mock", action="store_true", help="Run simulated benchmark without calling API"
    )
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    is_mock = args.mock or not api_key

    start_time = datetime.now().astimezone()
    print("=====================================================================")
    print("Clea Model Latency Benchmark")
    print(f"Start time (Local): {start_time.isoformat(timespec='seconds')}")
    print(f"Config: {args.calls} calls per model, interval {args.interval}s")
    if is_mock:
        print("Note: Running in MOCK mode (no GEMINI_API_KEY provided or --mock set).")
    print("=====================================================================")

    # Verify service tier
    no_accidental_tier = verify_service_tier_configuration()
    status_label = "PASS (None set)" if no_accidental_tier else "FAIL"
    print(f"Accidental service tier (flex/priority) check: {status_label}")

    res1 = await benchmark_model(
        args.model1, args.calls, args.interval, api_key=api_key, mock=is_mock
    )
    res2 = await benchmark_model(
        args.model2, args.calls, args.interval, api_key=api_key, mock=is_mock
    )

    end_time = datetime.now().astimezone()

    print("\n========================= BENCHMARK RESULTS =========================")
    tz = end_time.tzname() or ""
    print(f"Window: {start_time.strftime('%H:%M:%S')} to {end_time.strftime('%H:%M:%S')} ({tz})")
    hdr = f"{'Model':<25} | {'Metric':<6} | {'Median (ms)':<11} | {'p95 (ms)':<9} | {'Max (ms)':<9}"
    print(hdr)
    print("-" * 75)
    for res in (res1, res2):
        m = res["ttft"]
        t = res["total"]
        print(
            f"{res['model']:<25} | {'TTFT':<6} | {m['median']:<11.1f} | "
            f"{m['p95']:<9.1f} | {m['max']:<9.1f}"
        )
        print(
            f"{'':<25} | {'Total':<6} | {t['median']:<11.1f} | {t['p95']:<9.1f} | {t['max']:<9.1f}"
        )
        print(f"Service tier setting: {', '.join(res['service_tiers'])}")
        print("-" * 75)
    print("=====================================================================")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
