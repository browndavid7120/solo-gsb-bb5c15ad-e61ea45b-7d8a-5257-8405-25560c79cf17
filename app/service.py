"""Business logic: races, scans, revocation, adjudication, sealing."""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .adjudication import Scan as DomainScan
from .adjudication import Segment as DomainSegment
from .adjudication import adjudicate_runner
from .database import (
    Point,
    PointClockOffset,
    Race,
    ResultSnapshot,
    Runner,
    Scan,
    SegmentRow,
    utcnow,
)
from .schemas import PointClockOffsetIn, RaceCreate, ScanCreate


class ApiError(Exception):
    """Error mapped to an HTTP response by the router."""

    def __init__(self, status_code: int, error: str, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.error = error
        self.detail = detail


def _json_safe(value: Any) -> Any:
    """Convert a result payload into JSONB-storable plain values."""

    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


# --------------------------------------------------------------------------
# Serialization helpers
# --------------------------------------------------------------------------


def _segment_out(s: SegmentRow) -> dict:
    return {
        "position": s.position,
        "from_point": s.from_point_code,
        "to_point": s.to_point_code,
        "min_seconds": float(s.min_seconds),
        "max_seconds": float(s.max_seconds),
    }


def serialize_race(race: Race) -> dict:
    return {
        "id": str(race.id),
        "name": race.name,
        "start_time": race.start_time,
        "cutoff_time": race.cutoff_time,
        "sealed": race.sealed,
        "created_at": race.created_at,
        "sealed_at": race.sealed_at,
        "points": [
            {
                "position": p.position,
                "code": p.code,
                "name": p.name,
                "is_finish": p.is_finish,
            }
            for p in sorted(race.points, key=lambda p: p.position)
        ],
        "segments": [
            _segment_out(s)
            for s in sorted(race.segments, key=lambda s: s.position)
        ],
        "runners": [
            {
                "id": str(r.id),
                "bib": r.bib,
                "name": r.name,
                "chip": r.chip_code,
            }
            for r in race.runners
        ],
    }


def serialize_scan(scan: Scan, *, replayed: bool = False) -> dict:
    return {
        "scan_no": scan.scan_no,
        "chip": scan.runner.chip_code,
        "point": scan.point.code,
        "occurred_at": scan.occurred_at,
        "revoked": scan.revoked,
        "replayed": replayed,
    }


def _valid_uuid(value: str, *, what: str = "identifier") -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise ApiError(404, "not_found", f"invalid {what}: {value!r}")


# --------------------------------------------------------------------------
# Race creation (configuration is fixed afterwards: no update endpoints)
# --------------------------------------------------------------------------


def create_race(db: Session, payload: RaceCreate) -> Race:
    # Extra defensive checks beyond pydantic (validation normally raises 422).
    if payload.cutoff_time <= payload.start_time:
        raise ApiError(422, "invalid_time_window",
                       "cutoff_time must be later than start_time")
    if any(s.min_seconds < 0 or s.max_seconds < 0
           for s in payload.segments):
        raise ApiError(422, "invalid_segment", "durations must be non-negative")
    if any(s.min_seconds > s.max_seconds for s in payload.segments):
        raise ApiError(422, "invalid_segment",
                       "min_seconds must be <= max_seconds")
    codes = [p.code for p in payload.points]
    if len(codes) != len(set(codes)):
        raise ApiError(422, "duplicate_point", "point codes must be unique")
    chips = [r.chip for r in payload.runners]
    if len(chips) != len(set(chips)):
        raise ApiError(422, "duplicate_chip", "chip bindings must be unique")

    race_id = str(uuid.uuid4())
    race = Race(
        id=race_id,
        name=payload.name,
        start_time=payload.start_time,
        cutoff_time=payload.cutoff_time,
        sealed=False,
    )
    db.add(race)

    points: list[Point] = []
    for i, p in enumerate(payload.points, start=1):
        point = Point(
            race_id=race_id,
            position=i,
            code=p.code,
            name=p.name,
            # Last ordered point is always the finish.
            is_finish=(i == len(payload.points)) or p.is_finish,
        )
        points.append(point)
        db.add(point)

    all_codes = ["START"] + codes
    for i, seg in enumerate(payload.segments, start=1):
        db.add(
            SegmentRow(
                race_id=race_id,
                position=i,
                from_point_code=all_codes[i - 1],
                to_point_code=all_codes[i],
                min_seconds=Decimal(str(seg.min_seconds)),
                max_seconds=Decimal(str(seg.max_seconds)),
            )
        )

    for r in payload.runners:
        db.add(
            Runner(
                id=str(uuid.uuid4()),
                race_id=race_id,
                bib=r.bib,
                name=r.name,
                chip_code=r.chip,
            )
        )

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "race_conflict", "race could not be created")

    db.refresh(race)
    return race


# --------------------------------------------------------------------------
# Loading helpers
# --------------------------------------------------------------------------


def get_race_locked(db: Session, race_id: str) -> Race:
    """Race row with SELECT ... FOR UPDATE (serialises scan/seal/withdraw)."""

    race_id = _valid_uuid(race_id, what="race_id")
    race = db.get(Race, race_id, with_for_update=True)
    if race is None:
        raise ApiError(404, "race_not_found", f"race {race_id} does not exist")
    return race


def get_race(db: Session, race_id: str) -> Race:
    race_id = _valid_uuid(race_id, what="race_id")
    race = db.get(
        Race,
        race_id,
        options=[
            selectinload(Race.points),
            selectinload(Race.segments),
            selectinload(Race.runners),
        ],
    )
    if race is None:
        raise ApiError(404, "race_not_found", f"race {race_id} does not exist")
    return race


# --------------------------------------------------------------------------
# Point clock offsets
# --------------------------------------------------------------------------


def _get_point_or_404(db: Session, race: Race, point_code: str) -> Point:
    point = db.scalars(
        select(Point).where(
            Point.race_id == race.id, Point.code == point_code
        )
    ).first()
    if point is None:
        raise ApiError(
            404, "unknown_point",
            f"point {point_code!r} does not exist in this race",
        )
    return point


def _offset_by_point_id(db: Session, race: Race) -> dict[int, PointClockOffset]:
    return {
        row.point_id: row
        for row in db.scalars(
            select(PointClockOffset).where(PointClockOffset.race_id == race.id)
        ).all()
    }


def _offsets_view(
    race: Race,
    points: list[Point],
    rows: dict[int, PointClockOffset] | list[PointClockOffset],
) -> list[dict]:
    """Serializable current offset version, ordered by point position.

    ``rows`` is either the mapping used during adjudication or the race's
    loaded relationship; points without a configured row read as zero but
    are omitted here (unset == zero, and the view only lists settings).
    """

    if isinstance(rows, dict):
        by_id = rows
    else:
        by_id = {r.point_id: r for r in rows}
    view: list[dict] = []
    for point in points:
        row = by_id.get(point.id)
        if row is None:
            continue
        view.append(
            {
                "position": point.position,
                "point": point.code,
                "offset_seconds": int(row.offset_seconds),
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
        )
    return view


def set_point_offset(
    db: Session, race_id: str, point_code: str, payload: PointClockOffsetIn
) -> dict:
    """Set or replace the signed integer offset of one point's clock.

    Serialised against scan submission/revocation/sealing by the race row
    lock; rejected after sealing. Provisional results are recomputed on the
    next query from the same committed scans plus this new offset version.
    """

    race = get_race_locked(db, race_id)
    if race.sealed:
        raise ApiError(409, "race_sealed",
                       "race is sealed; clock offsets cannot be changed")

    point = _get_point_or_404(db, race, point_code)

    row = db.scalars(
        select(PointClockOffset).where(
            PointClockOffset.race_id == race.id,
            PointClockOffset.point_id == point.id,
        )
    ).first()
    now = utcnow()
    replaced = row is not None
    if row is None:
        row = PointClockOffset(
            race_id=race.id,
            point_id=point.id,
            offset_seconds=payload.offset_seconds,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
    else:
        row.offset_seconds = payload.offset_seconds
        row.updated_at = now
    db.commit()
    db.refresh(row)
    return {
        "race_id": str(race.id),
        "point": point.code,
        "position": point.position,
        "offset_seconds": int(row.offset_seconds),
        "replaced": replaced,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def list_point_offsets(db: Session, race_id: str) -> dict:
    race = get_race(db, race_id)
    points = sorted(race.points, key=lambda p: p.position)
    return {
        "race_id": str(race.id),
        "sealed": race.sealed,
        "sealed_at": race.sealed_at,
        "offsets": _offsets_view(race, points, race.clock_offsets),
    }


def get_point_offset(db: Session, race_id: str, point_code: str) -> dict:
    race = get_race(db, race_id)
    point = _get_point_or_404(db, race, point_code)
    row = next(
        (r for r in race.clock_offsets if r.point_id == point.id), None
    )
    return {
        "race_id": str(race.id),
        "point": point.code,
        "position": point.position,
        # Unset offsets read as zero.
        "offset_seconds": int(row.offset_seconds) if row is not None else 0,
        "replaced": False,
        "created_at": row.created_at if row is not None else None,
        "updated_at": row.updated_at if row is not None else None,
    }


# --------------------------------------------------------------------------
# Scan ingestion
# --------------------------------------------------------------------------


def _resolve_scan_refs(
    db: Session, race: Race, payload: ScanCreate
) -> tuple[Runner, Point]:
    runner = db.scalars(
        select(Runner).where(
            Runner.race_id == race.id, Runner.chip_code == payload.chip
        )
    ).first()
    if runner is None:
        raise ApiError(404, "unknown_chip",
                       f"chip {payload.chip!r} is not bound to this race")

    point = db.scalars(
        select(Point).where(
            Point.race_id == race.id, Point.code == payload.point
        )
    ).first()
    if point is None:
        raise ApiError(404, "unknown_point",
                       f"point {payload.point!r} does not exist in this race")
    return runner, point


def _existing_replay_or_conflict(
    existing: Scan, runner: Runner, point: Point, occurred_at: datetime, scan_no: str
) -> Optional[Scan]:
    """Return the existing scan for an idempotent replay, else raise.

    A revoked scan keeps its unique number: a byte-identical retransmission
    replays as revoked; any other content is a conflict (use a new scan_no
    for a genuinely new observation).
    """

    same = (
        existing.runner_id == runner.id
        and existing.point_id == point.id
        and existing.occurred_at == occurred_at
    )
    if not same:
        raise ApiError(
            409,
            "scan_conflict",
            f"scan_no {scan_no!r} already exists with different "
            "chip/point/occurred_at",
        )
    return existing


def submit_scan(
    db: Session, race_id: str, payload: ScanCreate
) -> tuple[Scan, bool]:
    """Insert one scan. Returns (scan, replayed).

    Same scan_no + same (chip, point, occurred_at) is an idempotent replay.
    Same scan_no with different content is a 409 conflict.
    """

    race = get_race_locked(db, race_id)
    if race.sealed:
        raise ApiError(409, "race_sealed",
                       "race is sealed; no scans are accepted")

    runner, point = _resolve_scan_refs(db, race, payload)

    existing = db.scalars(
        select(Scan).where(Scan.race_id == race.id, Scan.scan_no == payload.scan_no)
    ).first()
    if existing is not None:
        return _existing_replay_or_conflict(
            existing, runner, point, payload.occurred_at, payload.scan_no
        ), True

    scan = Scan(
        race_id=race.id,
        scan_no=payload.scan_no,
        runner_id=runner.id,
        point_id=point.id,
        occurred_at=payload.occurred_at,
        revoked=False,
    )
    db.add(scan)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "scan_conflict",
                       f"scan_no {payload.scan_no!r} already exists")
    db.refresh(scan)
    return scan, False


def submit_scan_batch(
    db: Session, race_id: str, payloads: Iterable[ScanCreate]
) -> list[dict]:
    """Validate/insert many scans in one transaction, one result per item."""

    results: list[dict] = []
    try:
        race = get_race_locked(db, race_id)
        if race.sealed:
            raise ApiError(409, "race_sealed",
                           "race is sealed; no scans are accepted")

        seen_in_batch: Counter[str] = Counter()
        for payload in payloads:
            seen_in_batch[payload.scan_no] += 1
            if seen_in_batch[payload.scan_no] > 1:
                results.append(
                    {
                        "status_code": 409,
                        "error": "scan_conflict",
                        "detail": f"scan_no {payload.scan_no!r} repeated "
                        "inside the same batch",
                        "scan": None,
                    }
                )
                continue
            try:
                runner, point = _resolve_scan_refs(db, race, payload)
            except ApiError as exc:
                results.append(
                    {
                        "status_code": exc.status_code,
                        "error": exc.error,
                        "detail": exc.detail,
                        "scan": None,
                    }
                )
                continue

            existing = db.scalars(
                select(Scan).where(
                    Scan.race_id == race.id, Scan.scan_no == payload.scan_no
                )
            ).first()
            if existing is not None:
                try:
                    replayed = _existing_replay_or_conflict(
                        existing, runner, point,
                        payload.occurred_at, payload.scan_no,
                    )
                except ApiError as exc:
                    results.append(
                        {
                            "status_code": exc.status_code,
                            "error": exc.error,
                            "detail": exc.detail,
                            "scan": None,
                        }
                    )
                    continue
                results.append(
                    {
                        "status_code": 200,
                        "error": None,
                        "detail": None,
                        "scan": serialize_scan(replayed, replayed=True),
                    }
                )
                continue

            scan = Scan(
                race_id=race.id,
                scan_no=payload.scan_no,
                runner_id=runner.id,
                point_id=point.id,
                occurred_at=payload.occurred_at,
                revoked=False,
            )
            db.add(scan)
            try:
                db.flush()  # surface constraint errors within the loop
            except IntegrityError as exc:
                db.rollback()
                raise ApiError(409, "scan_conflict", str(exc.orig)) from exc
            results.append(
                {
                    "status_code": 201,
                    "error": None,
                    "detail": None,
                    "scan": serialize_scan(scan),
                }
            )

        db.commit()
    except ApiError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise

    return results


# --------------------------------------------------------------------------
# Revocation
# --------------------------------------------------------------------------


def revoke_scan(db: Session, race_id: str, scan_no: str) -> Scan:
    race = get_race_locked(db, race_id)
    if race.sealed:
        raise ApiError(409, "race_sealed",
                       "race is sealed; revocation is rejected")

    scan = db.scalars(
        select(Scan).where(Scan.race_id == race.id, Scan.scan_no == scan_no)
    ).first()
    if scan is None:
        raise ApiError(404, "scan_not_found",
                       f"scan {scan_no!r} does not exist")
    if scan.revoked:
        raise ApiError(409, "scan_already_revoked",
                       f"scan {scan_no!r} is already revoked")

    scan.revoked = True
    db.commit()
    db.refresh(scan)
    return scan


# --------------------------------------------------------------------------
# Adjudication
# --------------------------------------------------------------------------


def _runner_results(
    db: Session,
    race: Race,
    offset_rows: dict[int, PointClockOffset] | None = None,
) -> list[dict]:
    points = sorted(race.points, key=lambda p: p.position)
    point_by_id = {p.id: p for p in points}
    position_by_id = {p.id: p.position for p in points}
    segments = [
        DomainSegment(
            position=s.position,
            min_seconds=float(s.min_seconds),
            max_seconds=float(s.max_seconds),
        )
        for s in sorted(race.segments, key=lambda s: s.position)
    ]

    active_scans = db.scalars(
        select(Scan)
        .where(Scan.race_id == race.id, Scan.revoked.is_(False))
        .order_by(Scan.occurred_at, Scan.scan_no)
    ).all()

    # Current offset version for every point (unset points read zero).
    if offset_rows is None:
        offset_rows = _offset_by_point_id(db, race)

    by_runner: dict[str, list[Scan]] = {}
    for scan in active_scans:
        by_runner.setdefault(scan.runner_id, []).append(scan)

    results: list[dict] = []
    for runner in sorted(race.runners, key=lambda r: r.bib):
        runner_scans = by_runner.get(runner.id, [])
        point_by_scan_no = {s.scan_no: point_by_id[s.point_id] for s in runner_scans}
        domain_scans = [
            DomainScan(
                scan_no=s.scan_no,
                point_position=position_by_id[s.point_id],
                occurred_at=s.occurred_at,
                offset_seconds=int(
                    offset_rows[s.point_id].offset_seconds
                    if s.point_id in offset_rows
                    else 0
                ),
            )
            for s in runner_scans
        ]
        result = adjudicate_runner(
            race_start=race.start_time,
            cutoff=race.cutoff_time,
            segments=segments,
            scans=domain_scans,
        )
        used = [
            {
                "segment": u.segment_position,
                "point": point_by_scan_no[u.scan_no].code,
                "scan_no": u.scan_no,
                "occurred_at": u.occurred_at,
                "offset_seconds": u.offset_seconds,
                "calibrated_at": u.calibrated_at,
                "segment_seconds": u.segment_seconds,
            }
            for u in result.used_scans
        ]
        results.append(
            {
                "runner_id": str(runner.id),
                "bib": runner.bib,
                "name": runner.name,
                "chip": runner.chip_code,
                "status": result.status,
                "finish_time": result.finish_time,
                "total_seconds": result.total_seconds,
                "used_scans": used,
            }
        )

    # Finished first by earliest finish, then lex-min scan-no sequence;
    # DNF afterwards by bib.
    used_map = {
        r["runner_id"]: [u["scan_no"] for u in r["used_scans"]]
        for r in results
    }
    finished = sorted(
        (r for r in results if r["status"] == "finished"),
        key=lambda r: (r["finish_time"], used_map[r["runner_id"]], r["bib"]),
    )
    dnf = sorted(
        (r for r in results if r["status"] != "finished"),
        key=lambda r: r["bib"],
    )
    ordered: list[dict] = []
    for rank, r in enumerate(finished, start=1):
        r["rank"] = rank
        ordered.append(r)
    for r in dnf:
        r["rank"] = None
        ordered.append(r)
    return ordered


def _results_envelope(
    race: Race,
    points: list[Point],
    offset_rows: dict[int, PointClockOffset],
    *,
    sealed: bool,
    results: list[dict],
) -> dict:
    return {
        "race_id": str(race.id),
        "name": race.name,
        "sealed": sealed,
        "sealed_at": race.sealed_at if sealed else None,
        "clock_offsets": _offsets_view(race, points, offset_rows),
        "results": results,
    }


def live_results(db: Session, race_id: str) -> dict:
    race = get_race(db, race_id)
    if race.sealed:
        return sealed_results_payload(db, race)
    points = sorted(race.points, key=lambda p: p.position)
    offset_rows = _offset_by_point_id(db, race)
    return _results_envelope(
        race,
        points,
        offset_rows,
        sealed=False,
        results=_runner_results(db, race, offset_rows),
    )


def runner_result(db: Session, race_id: str, runner_id: str) -> dict:
    runner_id = _valid_uuid(runner_id, what="runner_id")
    race = get_race(db, race_id)
    if race.sealed:
        payload = sealed_results_payload(db, race)
    else:
        points = sorted(race.points, key=lambda p: p.position)
        offset_rows = _offset_by_point_id(db, race)
        payload = _results_envelope(
            race,
            points,
            offset_rows,
            sealed=False,
            results=_runner_results(db, race, offset_rows),
        )
    for item in payload["results"]:
        if item["runner_id"] == runner_id:
            return item
    raise ApiError(404, "runner_not_found",
                   f"runner {runner_id} does not exist in this race")


# --------------------------------------------------------------------------
# Sealing
# --------------------------------------------------------------------------


def seal_race(db: Session, race_id: str) -> dict:
    """Freeze one consistent snapshot; idempotent for an already-sealed race."""

    race = get_race_locked(db, race_id)
    if race.sealed:
        return sealed_results_payload(db, race)

    points = sorted(race.points, key=lambda p: p.position)
    offset_rows = _offset_by_point_id(db, race)
    results = _runner_results(db, race, offset_rows)
    sealed_at = utcnow()
    race.sealed = True
    race.sealed_at = sealed_at
    for item in results:
        db.add(
            ResultSnapshot(
                race_id=race.id,
                runner_id=item["runner_id"],
                payload=_json_safe(item),
            )
        )
    db.commit()
    return _json_safe(
        _results_envelope(
            race, points, offset_rows, sealed=True, results=results
        )
    )


def sealed_results_payload(db: Session, race: Race) -> dict:
    rows = db.scalars(
        select(ResultSnapshot)
        .where(ResultSnapshot.race_id == race.id)
        .order_by(ResultSnapshot.id)
    ).all()
    payloads = [row.payload for row in rows]
    points = sorted(race.points, key=lambda p: p.position)
    # After sealing no offset row can change; the committed rows describe the
    # exact version captured by the frozen per-runner snapshots.
    offset_rows = _offset_by_point_id(db, race)
    return _json_safe(
        _results_envelope(
            race, points, offset_rows, sealed=True, results=payloads
        )
    )
