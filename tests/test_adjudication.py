"""Standard-library-only checks for app/adjudication.py.

Run with either:
    python3 tests/test_adjudication.py
    pytest tests/test_adjudication.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.adjudication import Scan, Segment, adjudicate_runner  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 10, 1, 8, 0, 0, tzinfo=UTC)
CUTOFF = T0 + timedelta(hours=1)


def segments(minutes):
    # minutes: list of (min, max) pairs per segment
    return [
        Segment(position=i + 1, min_seconds=lo * 60, max_seconds=hi * 60)
        for i, (lo, hi) in enumerate(minutes)
    ]


def sc(no, pos, minute, second=0):
    return Scan(
        scan_no=no,
        point_position=pos,
        occurred_at=T0 + timedelta(minutes=minute, seconds=second),
    )


def check(name, got, want):
    if got != want:
        raise AssertionError(f"{name}:\n  got={got!r}\n want={want!r}")


def test_happy_path_out_of_order_submission():
    segs = segments([(5, 30), (5, 30), (5, 30)])
    scans = [sc("30", 3, 30), sc("10", 1, 10), sc("20", 2, 20)]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("status", r.status, "finished")
    check("finish", r.finish_time, sc("30", 3, 30).occurred_at)
    check("total", r.total_seconds, 1800.0)
    check("used", [u.scan_no for u in r.used_scans], ["10", "20", "30"])


def test_earliest_finish_wins():
    segs = segments([(5, 40), (5, 40)])
    scans = [
        sc("a1", 1, 10), sc("a2", 2, 20),   # finishes 20
        sc("b1", 1, 15), sc("b2", 2, 25),   # finishes 25
    ]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("earliest", [u.scan_no for u in r.used_scans], ["a1", "a2"])

    # Cross paths: a1(10)->b2(15, only 5 min gap, valid) finishes 15 < 20
    scans = [
        sc("a1", 1, 10), sc("a2", 2, 30),
        sc("b1", 1, 20), sc("b2", 2, 15),
    ]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("cross", [u.scan_no for u in r.used_scans], ["a1", "b2"])


def test_lex_tiebreak_on_equal_finish():
    segs = segments([(5, 40), (5, 40), (5, 40)])
    scans = [
        sc("9", 1, 10), sc("10", 1, 10),
        sc("19", 2, 20), sc("20", 2, 20),
        sc("29", 3, 30), sc("30", 3, 30),
    ]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    # All eight combinations finish at the same time; the lex-min tuple of
    # scan numbers wins ("10" < "9" lexically, then "19" < "20", then "29" < "30").
    check("lex", [u.scan_no for u in r.used_scans], ["10", "19", "29"])
    check("finish", r.finish_time, sc("30", 3, 30).occurred_at)


def test_equal_timestamps_between_points_rejected():
    segs = segments([(5, 40), (5, 40)])
    scans = [sc("1", 1, 10), sc("2", 2, 10)]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("equal ts dnf", r.status, "did_not_finish")


def test_segment_outside_interval_dnf():
    segs = segments([(5, 9), (5, 30)])
    scans = [sc("1", 1, 10), sc("2", 2, 15)]  # first segment 10 > max 9
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("over max dnf", r.status, "did_not_finish")

    segs2 = segments([(11, 30), (5, 30)])
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs2, scans=scans)
    check("under min dnf", r.status, "did_not_finish")


def test_closed_interval_boundaries_inclusive():
    segs = segments([(5, 30), (5, 30)])
    scans = [sc("1", 1, 5), sc("2", 2, 30)]  # exactly min, then exactly max
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("boundary", [u.scan_no for u in r.used_scans], ["1", "2"])
    check("boundary secs", [u.segment_seconds for u in r.used_scans], [300.0, 1500.0])


def test_missing_finish_scan_dnf():
    segs = segments([(5, 30), (5, 30)])
    scans = [sc("1", 1, 10)]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("no finish", r.status, "did_not_finish")
    check("no finish used", r.used_scans, ())


def test_finish_after_cutoff_dnf():
    segs = segments([(5, 60), (5, 60)])
    scans = [sc("1", 1, 20), sc("2", 2, 61)]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("after cutoff", r.status, "did_not_finish")

    # exactly at cutoff is allowed ("not later than")
    scans = [sc("1", 1, 20), sc("2", 2, 60)]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("at cutoff", r.status, "finished")
    check("at cutoff time", r.finish_time, CUTOFF)


def test_irregular_scan_before_start_dnf():
    segs = segments([(5, 30), (5, 30)])
    scans = [sc("early", 1, -1), sc("late", 2, 10)]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("before start dnf", r.status, "did_not_finish")


def test_first_segment_zero_lower_bound_at_start_instant():
    segs = segments([(0, 30), (5, 30)])
    scans = [sc("1", 1, 0), sc("2", 2, 10)]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("zero min at start", r.status, "finished")
    check("zero min secs", r.used_scans[0].segment_seconds, 0.0)
    # even with a 0 lower bound, two scans at the same instant across points
    # violate strict increase between consecutive scans
    scans = [sc("1", 1, 5), sc("2", 2, 5)]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("equal ts still dnf", r.status, "did_not_finish")


def test_irrelevant_point_scans_ignored():
    segs = segments([(5, 30), (5, 30)])
    scans = [sc("1", 1, 10), sc("x", 5, 12), sc("2", 2, 20)]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("ignore", [u.scan_no for u in r.used_scans], ["1", "2"])


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok - {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
