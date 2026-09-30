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


def sc(no, pos, minute, second=0, offset=0):
    return Scan(
        scan_no=no,
        point_position=pos,
        occurred_at=T0 + timedelta(minutes=minute, seconds=second),
        offset_seconds=offset,
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


# --------------------------------------------------------------------------
# Point clock offsets: adjudication runs on occurred_at + offset_seconds
# --------------------------------------------------------------------------


def test_positive_offset_pushes_scan_later():
    # Raw C1 at 4 min is below the 5-minute minimum; +120s calibrates it to
    # 6 minutes, inside [5,30]; FIN at 20 raw -> calibrated 22, still valid.
    segs = segments([(5, 30), (5, 30)])
    scans = [sc("1", 1, 4, offset=120), sc("2", 2, 20, offset=120)]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("offset finish", r.status, "finished")
    check("offset finish time", r.finish_time,
          T0 + timedelta(minutes=20, seconds=120))
    check("offset total", r.total_seconds, 20 * 60 + 120)
    check("segment secs calibrated",
          [u.segment_seconds for u in r.used_scans], [360.0, 960.0])
    # Original stamps are preserved alongside the calibrated ones.
    check("original kept", r.used_scans[0].occurred_at,
          T0 + timedelta(minutes=4))
    check("calibrated kept", r.used_scans[0].calibrated_at,
          T0 + timedelta(minutes=6))
    check("offset echoed", [u.offset_seconds for u in r.used_scans], [120, 120])


def test_negative_offset_pulls_scan_earlier():
    # Raw C1 at 6 min passes the minimum; -120s calibrates it to 4 minutes,
    # below the 5-minute lower bound -> DNF.
    segs = segments([(5, 30), (5, 30)])
    scans = [sc("1", 1, 6, offset=-120), sc("2", 2, 20, offset=-120)]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("negative offset dnf", r.status, "did_not_finish")


def test_strict_increase_uses_calibrated_times():
    # Both scans are stamped at the same raw minute 10, but different point
    # offsets make calibrated C1 = 10:00 and calibrated C2 = 10:30, which is
    # a strict increase; with zero offsets the equal stamps would be DNF.
    segs = segments([(5, 40), (0, 40)])
    scans = [sc("1", 1, 10, offset=0), sc("2", 2, 10, offset=30)]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("cross-point calibrated increase", r.status, "finished")
    check("cross-point gap", r.used_scans[1].segment_seconds, 30.0)

    # Reverse: raw C1(10) -> raw C2(12) looks ordered, but offsets calibrate
    # C1 to 11:00 and C2 to 11:30? no — calibrate C1 to 12:30 (+150s) while
    # C2 stays at 12:00, so the calibrated C2 precedes C1 -> DNF.
    scans = [sc("1", 1, 10, offset=150), sc("2", 2, 12, offset=0)]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("calibrated inversion dnf", r.status, "did_not_finish")


def test_cutoff_checked_on_calibrated_finish():
    # Raw FIN at 58 minutes passes the 60-minute cutoff, but +300s pushes the
    # calibrated instant to 1:03 -> after cutoff -> DNF.
    segs = segments([(5, 60), (5, 60)])
    scans = [sc("1", 1, 20), sc("2", 2, 58, offset=300)]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("positive offset past cutoff dnf", r.status, "did_not_finish")

    # Raw FIN at 61 minutes is too late, but -120s calibrates it to 60:00
    # exactly -> "not later than cutoff" -> finished at the calibrated time.
    scans = [sc("1", 1, 20), sc("2", 2, 61, offset=-60)]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("negative offset to cutoff finishes", r.status, "finished")
    check("calibrated finish at cutoff", r.finish_time, CUTOFF)
    check("raw finish preserved", r.used_scans[1].occurred_at,
          T0 + timedelta(minutes=61))


def test_offset_changes_earliest_finish_choice():
    # Both FIN scans share the finish-point offset of -420s at one moment:
    #   a1(C1 raw 10) -> b2(FIN raw 25 -> cal 18): 8-minute gap, valid
    #   b1(C1 raw 15) -> b2(FIN cal 18): 3-minute gap, below the 5-min min
    # so the earliest calibrated finish is the cross path a1->b2 at 18.
    segs = segments([(5, 40), (5, 40)])
    scans = [
        sc("a1", 1, 10), sc("a2", 2, 30, offset=-420),
        sc("b1", 1, 15), sc("b2", 2, 25, offset=-420),
    ]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("offset cross path earliest", [u.scan_no for u in r.used_scans],
          ["a1", "b2"])
    check("offset cross finish", r.finish_time, T0 + timedelta(minutes=18))

    # After the finish clock is replaced (b2 now observed with offset 0),
    # a1->a2 (cal 23) beats a1->b2 (raw 25).
    scans = [
        sc("a1", 1, 10), sc("a2", 2, 30, offset=-420),
        sc("b1", 1, 15), sc("b2", 2, 25, offset=0),
    ]
    r = adjudicate_runner(race_start=T0, cutoff=CUTOFF, segments=segs, scans=scans)
    check("offset reorders winner", [u.scan_no for u in r.used_scans],
          ["a1", "a2"])
    check("calibrated winner finish", r.finish_time,
          T0 + timedelta(minutes=23))


def test_unset_offset_defaults_to_zero():
    segs = segments([(5, 30), (5, 30)])
    scan1 = Scan("1", 1, T0 + timedelta(minutes=10))
    scan2 = Scan("2", 2, T0 + timedelta(minutes=20))
    r = adjudicate_runner(
        race_start=T0, cutoff=CUTOFF, segments=segs, scans=[scan1, scan2]
    )
    check("default zero finish", r.status, "finished")
    check("default zero echoed",
          [u.offset_seconds for u in r.used_scans], [0, 0])
    check("default calibrated equals raw",
          r.used_scans[0].calibrated_at, scan1.occurred_at)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok - {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
