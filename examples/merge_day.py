import json
import sqlite3
from contextlib import closing

from lake.audit.history import HistoryNotFound, replay
from lake.book.queries import station_book
from lake.intake.batches import BatchInput
from lake.intake.freeze import freeze, initialize_freeze
from lake.intake.provenance import Provenance
from lake.intake.rows import SourceRow
from lake.mirror.acceptance import accept, initialize_mirror
from lake.release.formats import encode_document
from lake.release.issues import initialize_release, issue
from lake.release.withdrawals import initialize_withdrawals

STATION = "北闸"
DELIVERIES = {"东库": (("P", 0), ("P", 1)), "西库": (("P", 0), ("QB", 0))}


def prepare_source(db):
    initialize_freeze(db)
    initialize_release(db)
    initialize_withdrawals(db)
    originals = BatchInput(STATION, "日报-合并例", (
        SourceRow("A", 7, Provenance("北闸/原日报", "1")),
        SourceRow("B", 11, Provenance("北闸/原日报", "2")),
    ))
    review = BatchInput(STATION, "复核-合并例", (
        SourceRow("C", 13, Provenance("北闸/复核页", "8")),
    ))
    facts = {fact.row_id: fact for batch in (originals, review) for fact in freeze(db, batch).facts}
    publications = {}
    for name, labels in {"P": ("A", "B"), "QA": ("A",), "QB": ("B",), "R": ("C",)}.items():
        numbers = tuple(facts[label].fact_id for label in labels)
        publications[name] = issue(db, name, numbers, tuple((number,) for number in numbers))
    return facts, publications


def seed_mirror(db, publications, deliveries):
    initialize_mirror(db, (STATION,))
    for name, position in deliveries:
        part = publications[name].parts[position]
        accept(db, encode_document(part))


def preview():
    with closing(sqlite3.connect(":memory:")) as source:
        facts, publications = prepare_source(source)
    summary = {}
    for mirror, deliveries in DELIVERIES.items():
        with closing(sqlite3.connect(":memory:")) as db:
            seed_mirror(db, publications, deliveries)
            book = station_book(db, STATION)
            expected_ids = {facts[key].fact_id for key in ("A", "B")}
            actual_ids = {fact.fact_id for fact in book.facts}
            if (book.row_count, book.litres, actual_ids) != (2, 18, expected_ids):
                raise AssertionError(f"{mirror} should show both originals and 18 L")
            actual_p_positions = []
            for part in publications["P"].parts:
                try:
                    history = replay(db, part.part_id)
                except HistoryNotFound:
                    continue
                if history.receipt.receipt_id != part.part_id:
                    raise AssertionError(f"{mirror} returned an unrelated receipt")
                actual_p_positions.append(part.position)
            expected_p_positions = [0, 1] if mirror == "东库" else [0]
            if actual_p_positions != expected_p_positions:
                raise AssertionError(f"{mirror} has incorrect P coverage: {actual_p_positions}")
            summary[mirror] = {"litres": book.litres, "P_positions": actual_p_positions}
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    preview()
