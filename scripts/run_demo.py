"""
Run Bulwark demo scenarios against a local orchestrator and print the verdicts.

    python scripts/run_demo.py                       # regression, then fixed
    python scripts/run_demo.py llm-regression --comment
    python scripts/run_demo.py --api http://127.0.0.1:8000 regression

Needs the orchestrator running with BULWARK_DEMO_MODE=true (see README).
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx

TERMINAL = {
    "completed", "cancelled", "error", "timed_out",
    "build_failed", "build_timeout", "health_check_failed", "runtime_crash",
}


def run(api: str, scenario: str, show_comment: bool) -> dict:
    sim_id = httpx.post(f"{api}/api/demo/simulations", json={"scenario": scenario}, timeout=30).raise_for_status().json()["simulation_id"]
    print(f"\n▶ {scenario}: {sim_id}", flush=True)
    last = None
    while True:
        sim = httpx.get(f"{api}/api/simulation/{sim_id}", timeout=30).raise_for_status().json()
        if sim["status"] != last:
            print(f"  · {sim['status']}", flush=True)
            last = sim["status"]
        if sim["status"] in TERMINAL:
            break
        time.sleep(1)

    v = sim.get("verdict") or {}
    replay = (sim.get("dynatrace") or {}).get("replay_result") or {}
    base = (sim.get("dynatrace") or {}).get("baseline") or {}
    if v:
        print(f"  ⇒ {v['verdict']} {v['overall_score']}/100  (infra {v['infrastructure_score']}, "
              f"reliability {v['reliability_score']}, llm {v['llm_quality_score']}, model {v['gemini_model']})")
        print(f"    p95 {base.get('p95_latency_ms', 0):.0f}ms → {replay.get('actual_p95_ms', 0):.0f}ms "
              f"({replay.get('latency_delta_p95_ms', 0):+.0f}ms) in {sim['total_duration_ms'] / 1000:.0f}s")
    for line in sim.get("error_log") or []:
        print(f"    ! {line}")
    if show_comment and (sim.get("gitlab") or {}).get("comment_body"):
        print("\n" + sim["gitlab"]["comment_body"])
    return sim


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenarios", nargs="*", default=["regression", "fixed"],
                        help="regression | fixed | llm-regression (default: regression fixed)")
    parser.add_argument("--api", default="http://127.0.0.1:8000", help="orchestrator base URL")
    parser.add_argument("--comment", action="store_true", help="print the full MR comment")
    args = parser.parse_args()

    results = [run(args.api, s, args.comment) for s in args.scenarios]
    return 0 if all(r["status"] == "completed" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
