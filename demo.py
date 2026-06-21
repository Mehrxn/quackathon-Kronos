"""Demo driver: fire two incidents to show the repeat-incident fast path.

Run the server first (python main.py), then: python demo.py
The first incident takes the full retrieval+diagnosis path; the second,
with the same fingerprint, hits the Parcle pattern cache and resolves faster.
"""
from __future__ import annotations

import sys
import time

import httpx

BASE = "http://localhost:8020/api/v1"

INCIDENT = {
    "error_logs": [
        "level=error msg=processOrder: index out of range [5] with length 3",
        "goroutine 42: handlePayment: nil pointer dereference",
    ],
    "priority": "high",
    "service": "checkout-service",
}


def fire(label: str) -> str:
    r = httpx.post(f"{BASE}/quick-incident/", json=INCIDENT, timeout=30)
    r.raise_for_status()
    inc_id = r.json()["incident_id"]
    print(f"[{label}] created {inc_id}")
    return inc_id


def poll(inc_id: str, label: str) -> None:
    for _ in range(60):
        r = httpx.get(f"{BASE}/incidents/{inc_id}/", timeout=30)
        data = r.json()
        if data["status"] in ("resolved", "pr_opened", "issue_opened",
                              "ignored", "failed", "awaiting_approval"):
            print(f"[{label}] status={data['status']} "
                  f"cache={data['cache_result']} "
                  f"priority={data['resolved_priority']}")
            d = httpx.get(f"{BASE}/incidents/{inc_id}/diagnosis", timeout=10).json()
            print(f"[{label}] root_cause={d.get('root_cause')!r} "
                  f"from_cache={d.get('from_cache')}")
            return
        time.sleep(1)
    print(f"[{label}] timed out waiting")


if __name__ == "__main__":
    try:
        httpx.get(f"{BASE}/health", timeout=5)
    except httpx.HTTPError:
        print("Server not reachable at localhost:8000. Run `python main.py` first.")
        sys.exit(1)

    print("=== First occurrence (full path) ===")
    t0 = time.time()
    id1 = fire("first")
    poll(id1, "first")
    print(f"   elapsed {time.time()-t0:.1f}s\n")

    print("=== Second occurrence (cache fast path) ===")
    time.sleep(3)  # let Parcle index the rule
    t0 = time.time()
    id2 = fire("second")
    poll(id2, "second")
    print(f"   elapsed {time.time()-t0:.1f}s")
