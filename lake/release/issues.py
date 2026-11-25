import sqlite3
from dataclasses import dataclass

from lake.audit.receipts import HistoryNotFound, Receipt, prepare_receipt_store, require_receipt, save_receipt
from lake.audit.requests import bind_request, canonical_content, check_request, fixed_number, prepare_request_store, write_request
from lake.audit.trace import snapshot_origins
from lake.intake.provenance import require_identifier
from lake.release.formats import MergeCorrection, PartFact, PublicationPart, decode_document, encode_document
from lake.release.membership import MemberSelectionError, arrange_members, select_members
from lake.release.parts import assemble_parts, prepare_part_store, read_original_parts, store_original_parts


@dataclass(frozen=True)
class IssuedPublication:
    publication_id: str
    parts: tuple[PublicationPart, ...]
    receipt: Receipt


def initialize_release(db: sqlite3.Connection) -> None:
    with write_request(db):
        db.execute('''
            CREATE TABLE IF NOT EXISTS publications (
                publication_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE
            )
        ''')
        prepare_part_store(db)
        prepare_merge_store(db)
        prepare_request_store(db)
        prepare_receipt_store(db)


def prepare_merge_store(db: sqlite3.Connection) -> None:
    db.execute('''
        CREATE TABLE IF NOT EXISTS merge_corrections (
            correction_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
            publication_id TEXT NOT NULL UNIQUE REFERENCES publications(publication_id),
            document TEXT NOT NULL
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS merge_members (
            fact_id TEXT PRIMARY KEY,
            correction_id TEXT NOT NULL REFERENCES merge_corrections(correction_id)
        )
    ''')


def find_publication(db: sqlite3.Connection, publication_id: str) -> IssuedPublication | None:
    row = db.execute('SELECT 1 FROM publications WHERE publication_id = ?', (publication_id,)).fetchone()
    if row is None:
        return None
    return IssuedPublication(
        publication_id, read_original_parts(db, publication_id), require_receipt(db, publication_id),
    )


def issue(db: sqlite3.Connection, request_id: str, fact_ids: object, parts: object) -> IssuedPublication:
    request_id = require_identifier(request_id, 'issuance request')
    with write_request(db):
        members = select_members(db, fact_ids)
        arranged = arrange_members(members, parts)
        selection = [fact.fact_id for fact in members]
        layout = [[fact.fact_id for fact in part] for part in arranged]
        content = canonical_content({'selection': selection, 'parts': layout})
        previous = check_request(db, 'issue', request_id, content)
        if previous is not None:
            original = find_publication(db, previous)
            if original is None:
                raise sqlite3.DatabaseError('issuance binding lacks its publication')
            return original

        publication_id = fixed_number('publication', [request_id, selection, layout])
        fixed_parts = assemble_parts(publication_id, arranged)
        db.execute('INSERT INTO publications VALUES (?, ?)', (publication_id, request_id))
        store_original_parts(db, fixed_parts)
        receipt = Receipt(
            publication_id, 'issue',
            {'publication_id': publication_id, 'fact_ids': selection,
             'part_ids': [part.part_id for part in fixed_parts]},
            snapshot_origins(members),
        )
        save_receipt(db, receipt)
        bind_request(db, 'issue', request_id, content, receipt.receipt_id)
        return IssuedPublication(publication_id, fixed_parts, receipt)


def _merge_old_numbers(old_ids: object) -> tuple[str, str]:
    if not isinstance(old_ids, (tuple, list)) or len(old_ids) != 2:
        raise MemberSelectionError('expected exactly two ordered original fact numbers')
    try:
        numbers = tuple(require_identifier(number, 'fact number') for number in old_ids)
    except ValueError as exc:
        raise MemberSelectionError(str(exc)) from exc
    if numbers[0] == numbers[1]:
        raise MemberSelectionError('duplicate original fact number')
    return numbers


def read_original_merge(db: sqlite3.Connection, correction_id: str) -> MergeCorrection:
    row = db.execute(
        'SELECT document FROM merge_corrections WHERE correction_id = ?', (correction_id,),
    ).fetchone()
    if row is None:
        raise HistoryNotFound(correction_id)
    document = decode_document(row[0])
    if not isinstance(document, MergeCorrection):
        raise ValueError('stored correction has the wrong document kind')
    return document


def issue_merge(
    db: sqlite3.Connection, request_id: str, publication_id: str, old_ids: object, replacement_id: str,
) -> MergeCorrection:
    request_id = require_identifier(request_id, 'merge request')
    publication_id = require_identifier(publication_id, 'publication number')
    old_ids = _merge_old_numbers(old_ids)
    try:
        replacement_id = require_identifier(replacement_id, 'fact number')
    except ValueError as exc:
        raise MemberSelectionError(str(exc)) from exc
    if replacement_id in old_ids:
        raise MemberSelectionError('the review row repeats an original fact')
    content = canonical_content({
        'publication_id': publication_id, 'old_ids': list(old_ids), 'replacement_id': replacement_id,
    })
    with write_request(db):
        previous = check_request(db, 'merge_issue', request_id, content)
        if previous is not None:
            return read_original_merge(db, previous)
        publication = find_publication(db, publication_id)
        if publication is None:
            raise MemberSelectionError(f'unknown publication {publication_id}')
        if db.execute(
            'SELECT 1 FROM merge_corrections WHERE publication_id = ?', (publication_id,),
        ).fetchone() is not None:
            raise MemberSelectionError(f'publication {publication_id} already has a correction')
        members = select_members(db, (*old_ids, replacement_id))
        replacement = members[2]
        if len({fact.station for fact in members}) != 1:
            raise MemberSelectionError('merged facts must belong to one station')
        member_ids = {fact.fact_id for part in publication.parts for fact in part.facts}
        for number in old_ids:
            if number not in member_ids:
                raise MemberSelectionError(f'fact {number} is not in publication {publication_id}')
        for number in (*old_ids, replacement_id):
            if db.execute(
                'SELECT 1 FROM merge_members WHERE fact_id = ?', (number,),
            ).fetchone() is not None:
                raise MemberSelectionError(f'fact {number} already participates in a correction')
        correction_id = fixed_number('merge', [request_id, publication_id, list(old_ids), replacement_id])
        document = MergeCorrection(
            correction_id, publication_id, old_ids,
            PartFact(replacement.fact_id, replacement.station, replacement.litres, replacement.provenance),
        )
        db.execute(
            'INSERT INTO merge_corrections VALUES (?, ?, ?, ?)',
            (correction_id, request_id, publication_id, encode_document(document)),
        )
        db.executemany(
            'INSERT INTO merge_members VALUES (?, ?)',
            ((number, correction_id) for number in (*old_ids, replacement_id)),
        )
        receipt = Receipt(
            correction_id, 'merge_issue',
            {'correction_id': correction_id, 'publication_id': publication_id,
             'old_ids': list(old_ids), 'replacement_id': replacement_id},
            snapshot_origins(members),
        )
        save_receipt(db, receipt)
        bind_request(db, 'merge_issue', request_id, content, receipt.receipt_id)
        return document
