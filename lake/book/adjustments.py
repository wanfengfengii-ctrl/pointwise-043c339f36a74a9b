import json
import sqlite3
from dataclasses import dataclass

from lake.audit.requests import canonical_content
from lake.book.facts import CurrentFact
from lake.book.station_totals import StationTotal, sum_station_facts, totals_by_station
from lake.intake.provenance import Provenance
from lake.intake.rows import litres_text, parse_litres


@dataclass(frozen=True)
class StationDelta:
    station: str
    row_delta: int
    litres_delta: int


@dataclass(frozen=True)
class WithdrawalAdjustment:
    notice_id: str
    publication_id: str
    removed: tuple[CurrentFact, ...]
    row_delta: int
    litres_delta: int
    by_station: tuple[StationDelta, ...]
    added: tuple[CurrentFact, ...] = ()


def prepare_adjustment_store(db: sqlite3.Connection) -> None:
    db.execute('''
        CREATE TABLE IF NOT EXISTS withdrawal_adjustments (
            notice_id TEXT PRIMARY KEY, publication_id TEXT NOT NULL,
            removed TEXT NOT NULL, row_delta INTEGER NOT NULL, litres_delta TEXT NOT NULL,
            by_station TEXT NOT NULL, added TEXT NOT NULL DEFAULT '[]'
        )
    ''')
    columns = {row[1] for row in db.execute('PRAGMA table_info(withdrawal_adjustments)')}
    if 'added' not in columns:
        db.execute("ALTER TABLE withdrawal_adjustments ADD COLUMN added TEXT NOT NULL DEFAULT '[]'")


def compute_adjustment(
    notice_id: str, publication_id: str, before: tuple[CurrentFact, ...], after: tuple[CurrentFact, ...],
) -> WithdrawalAdjustment:
    original = {fact.fact_id: fact for fact in before}
    remaining = {fact.fact_id: fact for fact in after}
    if any(original[number] != remaining[number] for number in original.keys() & remaining.keys()):
        raise ValueError('a settlement cannot modify current facts')
    added = tuple(remaining[number] for number in sorted(remaining.keys() - original.keys()))
    removed = tuple(original[number] for number in sorted(original.keys() - remaining.keys()))
    gained = sum_station_facts(added)
    lost = sum_station_facts(removed)
    before_totals = totals_by_station(before)
    after_totals = totals_by_station(after)
    by_station = []
    for station in sorted(set(before_totals) | set(after_totals)):
        earlier = before_totals.get(station, StationTotal(0, 0))
        later = after_totals.get(station, StationTotal(0, 0))
        delta = StationDelta(station, later.row_count - earlier.row_count, later.litres - earlier.litres)
        if delta.row_delta or delta.litres_delta:
            by_station.append(delta)
    return WithdrawalAdjustment(
        notice_id, publication_id, removed,
        gained.row_count - lost.row_count, gained.litres - lost.litres,
        tuple(by_station), added,
    )


def _fact_row(fact: CurrentFact) -> list:
    return [fact.fact_id, fact.station, litres_text(fact.litres), fact.provenance.record, fact.provenance.line]


def save_adjustment(db: sqlite3.Connection, adjustment: WithdrawalAdjustment) -> None:
    removed = canonical_content([_fact_row(fact) for fact in adjustment.removed])
    added = canonical_content([_fact_row(fact) for fact in adjustment.added])
    by_station = canonical_content([
        [delta.station, delta.row_delta, litres_text(delta.litres_delta)] for delta in adjustment.by_station
    ])
    db.execute('INSERT INTO withdrawal_adjustments VALUES (?, ?, ?, ?, ?, ?, ?)',
               (adjustment.notice_id, adjustment.publication_id, removed,
                adjustment.row_delta, litres_text(adjustment.litres_delta), by_station, added))


def read_adjustment(db: sqlite3.Connection, notice_id: str) -> WithdrawalAdjustment | None:
    row = db.execute('''
        SELECT publication_id, removed, row_delta, litres_delta, by_station, added
        FROM withdrawal_adjustments WHERE notice_id = ?
    ''', (notice_id,)).fetchone()
    if row is None:
        return None
    publication_id, removed, row_delta, litres_delta, by_station, added = row
    return WithdrawalAdjustment(
        notice_id, publication_id,
        tuple(CurrentFact(number, station, parse_litres(litres), Provenance(record, line))
              for number, station, litres, record, line in json.loads(removed)),
        row_delta, parse_litres(litres_delta),
        tuple(StationDelta(station, count, parse_litres(litres))
              for station, count, litres in json.loads(by_station)),
        tuple(CurrentFact(number, station, parse_litres(litres), Provenance(record, line))
              for number, station, litres, record, line in json.loads(added)),
    )
