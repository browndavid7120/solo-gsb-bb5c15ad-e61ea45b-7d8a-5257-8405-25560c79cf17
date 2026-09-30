#!/usr/bin/env python3
"""End-to-end calling example for the split timing adjudication API.

Only uses the Python standard library. By default it talks to the
docker-compose service:

    python3 examples/demo.py
    BASE_URL=http://127.0.0.1:8000 python3 examples/demo.py

The script walks through every requirement:
  1. create a race (start/cutoff, ordered points + finish, closed duration
     intervals per segment, chip bindings)
  2. rejected configuration: invalid time window, bad interval, dup chip
  3. submit scans out of order: idempotent replay, conflicting replay,
     unknown chip / unknown point rejection
  4. provisional results: earliest valid finish, lex-min scan-no tiebreak,
     did-not-finish cases
  5. point clock offsets: query unset (zero), set/replace signed integer
     seconds, non-integer rejection, instant provisional recomputation on
     the same submitted scans
  6. revoke a scan before sealing -> provisional result recomputed
  7. seal the race -> frozen snapshot; afterwards submit/revoke/offset
     changes are rejected
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")

# Fixed UTC instant so the demo is reproducible.
T0 = datetime(2026, 10, 1, 8, 0, 0, tzinfo=timezone.utc)
CUTOFF = T0 + timedelta(hours=1)


def at(minute: int, second: int = 0) -> str:
    return (T0 + timedelta(minutes=minute, seconds=second)).isoformat()


def wire(minute: int, second: int = 0) -> str:
    # Wire format of response timestamps: FastAPI renders UTC with a "Z".
    return (T0 + timedelta(minutes=minute, seconds=second)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def request(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            payload = resp.read()
            return resp.status, json.loads(payload) if payload else None
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        return exc.code, json.loads(payload) if payload else None


def step(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(status: int, body) -> None:
    print(f"HTTP {status}")
    print(json.dumps(body, indent=2, ensure_ascii=False, default=str))


def expect(status: int, actual: int, label: str) -> None:
    if status != actual:
        print(f"!! expected {status} for {label}, got {actual}")
        sys.exit(1)
    print(f"ok [{actual}] {label}")


def main() -> None:
    # ------------------------------------------------------------------ #
    step("1. Create the race")
    # ------------------------------------------------------------------ #
    race_body = {
        "name": "City 10K 2026",
        "start_time": at(0),
        "cutoff_time": at(60),
        # Ordered points; the last one is the finish (FIN).
        "points": [
            {"code": "C1", "name": "Checkpoint 1"},
            {"code": "C2", "name": "Checkpoint 2"},
            {"code": "FIN", "name": "Finish", "is_finish": True},
        ],
        # start->C1, C1->C2, C2->FIN: closed non-negative intervals (sec)
        "segments": [
            {"min_seconds": 300, "max_seconds": 1800},
            {"min_seconds": 300, "max_seconds": 1800},
            {"min_seconds": 300, "max_seconds": 1800},
        ],
        "runners": [
            {"bib": "A01", "name": "Alice", "chip": "CHIP-A"},
            {"bib": "B02", "name": "Bob", "chip": "CHIP-B"},
            {"bib": "C03", "name": "Carol", "chip": "CHIP-C"},
            {"bib": "D04", "name": "Dan", "chip": "CHIP-D"},
        ],
    }
    status, body = request("POST", "/races", race_body)
    show(status, body)
    expect(201, status, "race created")
    race_id = body["id"]
    rp = f"/races/{race_id}"
    runner_id = {r["bib"]: r["id"] for r in body["runners"]}

    # ------------------------------------------------------------------ #
    step("2. Invalid configurations are rejected (422)")
    # ------------------------------------------------------------------ #
    bad = dict(race_body)
    bad["name"] = "bad window"
    bad["cutoff_time"] = at(-1)
    status, body = request("POST", "/races", bad)
    show(status, body)
    expect(422, status, "cutoff before start rejected")

    bad = json.loads(json.dumps(race_body))
    bad["name"] = "bad interval"
    bad["segments"][0] = {"min_seconds": 900, "max_seconds": 300}
    status, body = request("POST", "/races", bad)
    expect(422, status, "min > max interval rejected")

    bad = json.loads(json.dumps(race_body))
    bad["name"] = "dup chip"
    bad["runners"][1]["chip"] = "CHIP-A"
    status, body = request("POST", "/races", bad)
    expect(422, status, "duplicate chip rejected")

    bad = json.loads(json.dumps(race_body))
    bad["name"] = "segment count mismatch"
    bad["segments"].pop()
    status, body = request("POST", "/races", bad)
    expect(422, status, "segment/point count mismatch rejected")

    # ------------------------------------------------------------------ #
    step("3. Submit scans out of order")
    # ------------------------------------------------------------------ #
    def scan(scan_no, chip, point, minute, second=0):
        return request(
            "POST",
            f"{rp}/scans",
            {
                "scan_no": scan_no,
                "chip": chip,
                "point": point,
                "occurred_at": at(minute, second),
            },
        )

    # Alice: clean 10/20/30 path, plus an out-of-order early FIN scan that
    # creates a slower alternative path (25 -> 30 only 5 minutes < 300s
    # minimum, so it is unusable as C2->FIN, leaving the canonical path).
    status, _ = scan("A-030", "CHIP-A", "FIN", 30)
    expect(201, status, "Alice FIN (submitted first, out of order)")
    status, _ = scan("A-010", "CHIP-A", "C1", 10)
    expect(201, status, "Alice C1")
    status, _ = scan("A-020", "CHIP-A", "C2", 20)
    expect(201, status, "Alice C2")

    # Bob: never crosses the finish -> DNF.
    status, _ = scan("B-010", "CHIP-B", "C1", 11)
    expect(201, status, "Bob C1")
    status, _ = scan("B-020", "CHIP-B", "C2", 22)
    expect(201, status, "Bob C2 (no FIN scan)")

    # Carol: C2->FIN gap too short (2 minutes) -> no valid path -> DNF.
    status, _ = scan("C-010", "CHIP-C", "C1", 10)
    expect(201, status, "Carol C1")
    status, _ = scan("C-020", "CHIP-C", "C2", 20)
    expect(201, status, "Carol C2")
    status, _ = scan("C-022", "CHIP-C", "FIN", 22)
    expect(201, status, "Carol FIN too early (still accepted as a scan)")

    # Dan: two complete paths with identical finish time (30 min).
    #   path X: "DA01","DB01","DC01"
    #   path Y: "DA02","DB02","DC02"
    # Both finish at the same instant, so the lex-min tuple wins:
    # ("DA01","DB01","DC01") < ("DA02","DB02","DC02").
    status, _ = scan("DA02", "CHIP-D", "C1", 10)
    expect(201, status, "Dan C1 scan DA02")
    status, _ = scan("DA01", "CHIP-D", "C1", 10)
    expect(201, status, "Dan C1 scan DA01")
    status, _ = scan("DB02", "CHIP-D", "C2", 20)
    expect(201, status, "Dan C2 scan DB02")
    status, _ = scan("DB01", "CHIP-D", "C2", 20)
    expect(201, status, "Dan C2 scan DB01")
    status, _ = scan("DC02", "CHIP-D", "FIN", 30)
    expect(201, status, "Dan FIN scan DC02")
    status, _ = scan("DC01", "CHIP-D", "FIN", 30)
    expect(201, status, "Dan FIN scan DC01")

    # --- idempotency & conflict --------------------------------------- #
    status, body = scan("A-010", "CHIP-A", "C1", 10)
    show(status, body)
    expect(200, status, "same scan_no + same content is idempotent replay")
    assert body["replayed"] is True

    status, body = scan("A-010", "CHIP-A", "C1", 11)
    show(status, body)
    expect(409, status, "same scan_no + different content conflicts")

    status, body = scan("X-001", "CHIP-UNKNOWN", "C1", 10)
    show(status, body)
    expect(404, status, "unknown chip rejected")

    status, body = scan("X-002", "CHIP-A", "NEVER", 10)
    show(status, body)
    expect(404, status, "unknown point rejected")

    # --- batch endpoint ------------------------------------------------- #
    status, body = request(
        "POST",
        f"{rp}/scans/batch",
        {
            "scans": [
                # unknown chip
                {"scan_no": "Z1", "chip": "NOPE", "point": "C1",
                 "occurred_at": at(10)},
                # valid new
                {"scan_no": "BATCH-1", "chip": "CHIP-A", "point": "C1",
                 "occurred_at": at(12)},
                # idempotent replay
                {"scan_no": "A-010", "chip": "CHIP-A", "point": "C1",
                 "occurred_at": at(10)},
            ]
        },
    )
    show(status, body)
    expect(200, status, "batch accepted with per-item statuses")
    assert [item["status_code"] for item in body] == [404, 201, 200]

    # BATCH-1 gives Alice a C1 at minute 12; earliest finish still uses
    # A-010(10) -> A-020(20) -> A-030(30).

    # ------------------------------------------------------------------ #
    step("4. Provisional results (before sealing)")
    # ------------------------------------------------------------------ #
    status, results = request("GET", f"{rp}/results")
    show(status, results)
    expect(200, status, "provisional results")
    by_bib = {r["bib"]: r for r in results["results"]}

    assert by_bib["A01"]["status"] == "finished"
    assert [u["scan_no"] for u in by_bib["A01"]["used_scans"]] == [
        "A-010", "A-020", "A-030"
    ]
    assert by_bib["A01"]["rank"] == 1
    print("ok Alice fastest, rank 1, scans A-010/A-020/A-030")

    assert by_bib["B02"]["status"] == "did_not_finish"
    print("ok Bob DNF (no finish scan)")

    assert by_bib["C03"]["status"] == "did_not_finish"
    print("ok Carol DNF (segment outside allowed interval)")

    assert by_bib["D04"]["status"] == "finished"
    assert [u["scan_no"] for u in by_bib["D04"]["used_scans"]] == [
        "DA01", "DB01", "DC01"
    ]
    assert by_bib["D04"]["finish_time"] == by_bib["A01"]["finish_time"]
    assert by_bib["D04"]["rank"] == 2
    print("ok Dan tied finish time -> lex-min scan sequence DA01/DB01/DC01")

    # single-runner view
    status, body = request(
        "GET", f"{rp}/runners/{runner_id['A01']}/result"
    )
    expect(200, status, "single runner result")
    assert body["bib"] == "A01"

    # ------------------------------------------------------------------ #
    step("5. Point clock offsets: set/replace, query, recompute")
    # ------------------------------------------------------------------ #
    # Unset offsets read as zero.
    status, body = request("GET", f"{rp}/points/C1/offset")
    show(status, body)
    expect(200, status, "unset offset reads as zero")
    assert body["offset_seconds"] == 0 and body["created_at"] is None

    status, body = request("GET", f"{rp}/offsets")
    expect(200, status, "list all offsets")
    assert body["offsets"] == []
    print("ok no offsets configured yet")

    # Unknown race / point are explicitly rejected.
    status, body = request(
        "GET", f"/races/{runner_id['A01']}/points/C1/offset"
    )  # a runner UUID is not a race id
    expect(404, status, "unknown race for offset rejected")

    status, body = request("GET", f"{rp}/points/NOPE/offset")
    show(status, body)
    expect(404, status, "unknown point for offset rejected")

    # Non-integer offsets are explicitly rejected (422), including 2.0.
    status, body = request(
        "PUT", f"{rp}/points/C1/offset", {"offset_seconds": 2.5}
    )
    show(status, body)
    expect(422, status, "fractional offset rejected")

    status, body = request(
        "PUT", f"{rp}/points/C1/offset", {"offset_seconds": 2.0}
    )
    show(status, body)
    expect(422, status, "float-looking-like-integer offset rejected")

    # Carol DNFs because raw C1(10) -> C2(20) -> FIN(22) has a 120-second
    # last leg, below the 300s minimum. The C2 device was found running
    # 180 seconds fast: correct it with -180. Her calibrated instants become
    # C1 10:00, C2 19:57, FIN 22:00 — strictly increasing, with the last leg
    # exactly 300 seconds. Adjudication reruns on the SAME committed scans.
    status, body = request(
        "PUT", f"{rp}/points/C2/offset", {"offset_seconds": -180}
    )
    show(status, body)
    expect(200, status, "C2 offset -180s set")
    assert body["replaced"] is False

    status, body = request("GET", f"{rp}/offsets")
    show(status, body)
    expect(200, status, "list configured offsets")
    listed = {o["point"]: o["offset_seconds"] for o in body["offsets"]}
    assert listed == {"C2": -180}

    status, results = request("GET", f"{rp}/results")
    show(status, results)
    by_bib = {r["bib"]: r for r in results["results"]}
    # Results carry the offset version they were computed with.
    assert {o["point"]: o["offset_seconds"]
            for o in results["clock_offsets"]} == {"C2": -180}
    carol = by_bib["C03"]
    assert carol["status"] == "finished"
    c2_used = next(u for u in carol["used_scans"] if u["point"] == "C2")
    fin_used = next(u for u in carol["used_scans"] if u["point"] == "FIN")
    # Original scan times are preserved ...
    assert c2_used["occurred_at"] == wire(20)
    assert fin_used["occurred_at"] == wire(22)
    # ... while the calibrated times and segment seconds use the offset.
    assert c2_used["offset_seconds"] == -180
    assert c2_used["calibrated_at"] == wire(17)
    assert [u["segment_seconds"] for u in carol["used_scans"]] == [
        600.0, 420.0, 300.0
    ]
    # FIN carries no offset of its own; calibrated == raw there.
    assert fin_used["offset_seconds"] == 0
    assert fin_used["calibrated_at"] == fin_used["occurred_at"]
    # Carol's calibrated finish 22:00 now beats Alice/Dan at 30:00.
    assert carol["finish_time"] == wire(22)
    assert carol["rank"] == 1
    print("ok Carol now finishes (rank 1) via C2 calibration; raw times kept")

    # Alice and Dan still finish at 30:00; their tie still resolves by the
    # lexicographically smallest scan-number sequence.
    assert by_bib["A01"]["status"] == "finished"
    assert by_bib["A01"]["rank"] == 2
    assert by_bib["D04"]["rank"] == 3
    assert [u["scan_no"] for u in by_bib["D04"]["used_scans"]] == [
        "DA01", "DB01", "DC01"
    ]
    print("ok Alice/Dan re-adjudicated on the same scans, ranking 2/3")

    # Take the correction back: provisional results immediately return to the
    # previous version on the same scans.
    status, _ = request(
        "PUT", f"{rp}/points/C2/offset", {"offset_seconds": 0}
    )
    expect(200, status, "C2 offset replaced back to 0")
    status, results = request("GET", f"{rp}/results")
    by_bib = {r["bib"]: r for r in results["results"]}
    assert by_bib["C03"]["status"] == "did_not_finish"
    assert by_bib["A01"]["rank"] == 1
    print("ok reverting the offset instantly restores the step-4 results")

    # ------------------------------------------------------------------ #
    step("6. Revoke a scan before sealing -> results recompute")
    # ------------------------------------------------------------------ #
    # Revoke Alice's FIN scan. She drops to DNF provisionally.
    status, body = request("DELETE", f"{rp}/scans/A-030")
    show(status, body)
    expect(200, status, "scan revoked")

    status, results = request("GET", f"{rp}/results")
    by_bib = {r["bib"]: r for r in results["results"]}
    assert by_bib["A01"]["status"] == "did_not_finish"
    print("ok Alice provisionally DNF after her FIN scan was revoked")

    # double revoke rejected
    status, body = request("DELETE", f"{rp}/scans/A-030")
    show(status, body)
    expect(409, status, "revoking an already-revoked scan rejected")

    # Revoking an unknown scan 404s
    status, body = request("DELETE", f"{rp}/scans/NO-SUCH-SCAN")
    expect(404, status, "unknown scan revocation 404")

    # ------------------------------------------------------------------ #
    step("7. Seal the race and verify the frozen snapshot")
    # ------------------------------------------------------------------ #
    status, sealed = request("POST", f"{rp}/seal")
    show(status, sealed)
    expect(200, status, "race sealed")
    assert sealed["sealed"] is True
    # The frozen snapshot remembers the offset version it was built with
    # (C2 was set then replaced back to 0; the row still exists).
    assert [o["point"] for o in sealed["clock_offsets"]] == ["C2"]
    assert sealed["clock_offsets"][0]["offset_seconds"] == 0

    # Sealing is idempotent and returns the same snapshot.
    status, sealed_again = request("POST", f"{rp}/seal")
    expect(200, status, "re-seal is idempotent")
    assert sealed_again["results"] == sealed["results"]
    print("ok repeated seal returns the same frozen snapshot")

    # GET results now comes from the snapshot (Alice stays DNF).
    status, body = request("GET", f"{rp}/results")
    by_bib = {r["bib"]: r for r in body["results"]}
    assert by_bib["A01"]["status"] == "did_not_finish"
    print("ok snapshot serves results after sealing")

    # After sealing: submit and revoke are rejected.
    status, body = scan("LATE", "CHIP-A", "FIN", 35)
    show(status, body)
    expect(409, status, "scan submission after sealing rejected")

    status, body = request("DELETE", f"{rp}/scans/BATCH-1")
    show(status, body)
    expect(409, status, "revocation after sealing rejected")

    # Offset changes are serialised with scans/seal and rejected afterwards.
    status, body = request(
        "PUT", f"{rp}/points/C1/offset", {"offset_seconds": -60}
    )
    show(status, body)
    expect(409, status, "offset change after sealing rejected")

    # The frozen snapshot is unaffected by later requests and still serves
    # the same payload.
    status, body = request("GET", f"{rp}/results")
    assert body["results"] == sealed["results"]
    assert [o["point"] for o in body["clock_offsets"]] == ["C2"]
    print("ok sealed snapshot unaffected by later offset/submit requests")

    print("\nALL DEMO STEPS PASSED")


if __name__ == "__main__":
    main()
