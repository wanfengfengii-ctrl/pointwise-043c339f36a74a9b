import sqlite3
from dataclasses import dataclass

from lake.intake.provenance import Provenance
from lake.intake.rows import litres_text, parse_litres
from lake.release.formats import MergeCorrection, PartFact, PublicationPart


@dataclass(frozen=True)
class PartEvidence:
    document: PublicationPart
    receipt_id: str


def prepare_evidence_store(db: sqlite3.Connection) -> None:
    db.execute('''
        CREATE TABLE IF NOT EXISTS received_parts (
            part_id TEXT PRIMARY KEY, publication_id TEXT NOT NULL,
            position INTEGER NOT NULL, receipt_id TEXT NOT NULL REFERENCES receipts(receipt_id),
            UNIQUE(publication_id, position)
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS received_facts (
            fact_id TEXT PRIMARY KEY, station TEXT NOT NULL, litres TEXT NOT NULL,
            origin_record TEXT NOT NULL, origin_line TEXT NOT NULL
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS part_facts (
            part_id TEXT NOT NULL REFERENCES received_parts(part_id),
            ordinal INTEGER NOT NULL, fact_id TEXT NOT NULL REFERENCES received_facts(fact_id),
            PRIMARY KEY(part_id, ordinal), UNIQUE(part_id, fact_id)
        )
    ''')
    db.execute('CREATE INDEX IF NOT EXISTS part_facts_by_fact ON part_facts(fact_id)')


def prepare_merge_store(db: sqlite3.Connection) -> None:
    db.execute('''
        CREATE TABLE IF NOT EXISTS applied_merges (
            correction_id TEXT PRIMARY KEY, publication_id TEXT NOT NULL,
            old_fact_1 TEXT NOT NULL, old_fact_2 TEXT NOT NULL, replacement_id TEXT NOT NULL,
            receipt_id TEXT NOT NULL REFERENCES receipts(receipt_id)
        )
    ''')


def record_merge(db: sqlite3.Connection, correction: MergeCorrection, receipt_id: str) -> None:
    db.execute(
        'INSERT INTO applied_merges VALUES (?, ?, ?, ?, ?, ?)',
        (correction.correction_id, correction.publication_id, correction.old_ids[0],
         correction.old_ids[1], correction.replacement.fact_id, receipt_id),
    )
    fact = correction.replacement
    db.execute(
        'INSERT INTO received_facts VALUES (?, ?, ?, ?, ?) ON CONFLICT(fact_id) DO NOTHING',
        (fact.fact_id, fact.station, litres_text(fact.litres), fact.provenance.record, fact.provenance.line),
    )


def has_received_fact_from(db: sqlite3.Connection, publication_id: str, fact_id: str) -> bool:
    return db.execute('''
        SELECT 1 FROM received_parts AS p JOIN part_facts AS pf USING (part_id)
        WHERE p.publication_id = ? AND pf.fact_id = ?
    ''', (publication_id, fact_id)).fetchone() is not None


def record_received_part(db: sqlite3.Connection, part: PublicationPart, receipt_id: str) -> None:
    db.execute('INSERT INTO received_parts VALUES (?, ?, ?, ?)',
               (part.part_id, part.publication_id, part.position, receipt_id))
    db.executemany(
        'INSERT INTO received_facts VALUES (?, ?, ?, ?, ?) ON CONFLICT(fact_id) DO NOTHING',
        ((fact.fact_id, fact.station, litres_text(fact.litres), fact.provenance.record, fact.provenance.line)
         for fact in part.facts),
    )
    db.executemany('INSERT INTO part_facts VALUES (?, ?, ?)',
                   ((part.part_id, ordinal, fact.fact_id) for ordinal, fact in enumerate(part.facts)))


def _part_fact(row) -> PartFact:
    return PartFact(row[0], row[1], parse_litres(row[2]), Provenance(row[3], row[4]))


def read_known_fact(db: sqlite3.Connection, fact_id: str) -> PartFact | None:
    row = db.execute('SELECT * FROM received_facts WHERE fact_id = ?', (fact_id,)).fetchone()
    return None if row is None else _part_fact(row)


def read_received_part(db: sqlite3.Connection, part_id: str) -> PartEvidence | None:
    row = db.execute('''
        SELECT publication_id, position, receipt_id FROM received_parts WHERE part_id = ?
    ''', (part_id,)).fetchone()
    if row is None:
        return None
    publication_id, position, receipt_id = row
    facts = db.execute('''
        SELECT f.* FROM part_facts AS pf JOIN received_facts AS f USING (fact_id)
        WHERE pf.part_id = ? ORDER BY pf.ordinal
    ''', (part_id,))
    part = PublicationPart(part_id, publication_id, position, tuple(_part_fact(fact) for fact in facts))
    return PartEvidence(part, receipt_id)


def _effective_merges(db: sqlite3.Connection):
    return db.execute('''
        SELECT m.old_fact_1, m.old_fact_2, m.replacement_id FROM applied_merges AS m
        LEFT JOIN withdrawn_publications AS w ON w.publication_id = m.publication_id
        WHERE w.publication_id IS NULL
        ORDER BY m.correction_id
    ''')


def list_paused_fact_ids(db: sqlite3.Connection) -> set[str]:
    return {number for row in _effective_merges(db) for number in row[:2]}


def list_active_facts(db: sqlite3.Connection) -> tuple[PartFact, ...]:
    rows = db.execute('''
        SELECT f.* FROM received_facts AS f
        WHERE EXISTS (
            SELECT 1 FROM part_facts AS pf JOIN received_parts AS p USING (part_id)
            LEFT JOIN withdrawn_publications AS w ON w.publication_id = p.publication_id
            WHERE pf.fact_id = f.fact_id AND w.publication_id IS NULL
        )
        ORDER BY f.fact_id
    ''')
    active = {fact.fact_id: fact for fact in (_part_fact(row) for row in rows)}
    for old_1, old_2, replacement_id in _effective_merges(db):
        replacement = read_known_fact(db, replacement_id)
        if replacement is None:
            raise sqlite3.DatabaseError('applied merge lacks its review row')
        active[replacement_id] = replacement
        active.pop(old_1, None)
        active.pop(old_2, None)
    return tuple(active[number] for number in sorted(active))
