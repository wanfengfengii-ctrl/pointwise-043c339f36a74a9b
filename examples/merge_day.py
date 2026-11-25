import json
import sqlite3
from contextlib import ExitStack, closing
from pathlib import Path
from tempfile import TemporaryDirectory

from lake.audit.history import HistoryNotFound, replay
from lake.book.queries import station_book
from lake.intake.batches import BatchInput
from lake.intake.freeze import freeze, initialize_freeze
from lake.intake.provenance import Provenance
from lake.intake.rows import SourceRow
from lake.mirror.acceptance import accept, apply_merge, initialize_mirror
from lake.release.formats import encode_document
from lake.release.issues import initialize_release, issue, issue_merge
from lake.release.withdrawals import initialize_withdrawals, withdraw_issue

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


def _book(db):
    book = station_book(db, STATION)
    return {"rows": book.row_count, "litres": book.litres}


def _settlement(adjustment):
    return {
        "added": [fact.provenance.line for fact in adjustment.added],
        "removed": [fact.provenance.line for fact in adjustment.removed],
        "row_delta": adjustment.row_delta,
        "litres_delta": adjustment.litres_delta,
        "by_station": {delta.station: [delta.row_delta, delta.litres_delta] for delta in adjustment.by_station},
    }


def _check(label, actual, expected):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected}, got {actual}")
    return actual


def _pending_history_missing(db, number):
    try:
        replay(db, number)
    except HistoryNotFound:
        return True
    return False


def merge_scenarios(directory):
    directory = Path(directory)
    with closing(sqlite3.connect(directory / "source.sqlite")) as source:
        facts, publications = prepare_source(source)
        correction = issue_merge(
            source, "合并-P", publications["P"].publication_id,
            (facts["A"].fact_id, facts["B"].fact_id), facts["C"].fact_id,
        )
        _check("源库勘正原件", replay(source, correction.number).documents, (correction,))
        notices = {
            name: withdraw_issue(source, f"撤-{name}", publications[name].publication_id).notice
            for name in ("P", "QA")
        }
    wire = encode_document(correction)
    notice_p = encode_document(notices["P"])
    summary = {}
    merge_results, settle_results = {}, {}
    with ExitStack() as stack:
        mirrors = {}
        for name, deliveries in DELIVERIES.items():
            db = stack.enter_context(closing(sqlite3.connect(directory / f"{name}.sqlite")))
            seed_mirror(db, publications, deliveries)
            mirrors[name] = db
        east, west = mirrors["东库"], mirrors["西库"]
        summary["合并前"] = _check("合并前两库同为18L", {name: _book(db) for name, db in mirrors.items()},
                                   {"东库": {"rows": 2, "litres": 18}, "西库": {"rows": 2, "litres": 18}})
        east_merge = apply_merge(east, wire)
        west_first = apply_merge(west, wire)
        summary["首办"] = _check("首办状态", (east_merge.status, west_first.status), ("merged", "pending"))
        _check("待件无回执", (west_first.receipt, west_first.adjustment), (None, None))
        _check("西库无勘正首执", _pending_history_missing(west, correction.number), True)
        _check("西库待件仍18L", _book(west), {"rows": 2, "litres": 18})
        summary["东库合并调整"] = _check("东库合并调整", _settlement(east_merge.adjustment), {
            "added": ["8"], "removed": ["1", "2"], "row_delta": -1, "litres_delta": -5,
            "by_station": {STATION: [-1, -5]},
        })
        _check("东库合并后13L", _book(east), {"rows": 1, "litres": 13})
        _check("西库补收P/B", accept(west, encode_document(publications["P"].parts[1])).status, "received")
        west_merge = apply_merge(west, wire)
        _check("西库再投生效", west_merge.status, "merged")
        _check("西库合并调整", _settlement(west_merge.adjustment), summary["东库合并调整"])
        summary["补件后"] = _check("补件后两库13L", {name: _book(db) for name, db in mirrors.items()},
                                   {"东库": {"rows": 1, "litres": 13}, "西库": {"rows": 1, "litres": 13}})
        qa = accept(east, encode_document(publications["QA"].parts[0]))
        _check("合并期QA实收", qa.status, "received")
        summary["合并期QA新增清单"] = _check("QA新增清单为空", qa.receipt.details["added_fact_ids"], [])
        _check("收QA后东库仍13L", _book(east), {"rows": 1, "litres": 13})
        east_settle = accept(east, notice_p)
        west_settle = accept(west, notice_p)
        _check("两库撤P状态", (east_settle.status, west_settle.status), ("withdrawn", "withdrawn"))
        summary["撤P"] = {
            "东库": _check("东库撤P调整", _settlement(east_settle.adjustment), {
                "added": ["1"], "removed": ["8"], "row_delta": 0, "litres_delta": -6,
                "by_station": {STATION: [0, -6]},
            }),
            "西库": _check("西库撤P调整", _settlement(west_settle.adjustment), {
                "added": ["2"], "removed": ["8"], "row_delta": 0, "litres_delta": -2,
                "by_station": {STATION: [0, -2]},
            }),
        }
        summary["撤P后"] = _check("撤P后两库", {name: _book(db) for name, db in mirrors.items()},
                                  {"东库": {"rows": 1, "litres": 7}, "西库": {"rows": 1, "litres": 11}})
        merge_results = {"东库": east_merge, "西库": west_merge}
        settle_results = {"东库": east_settle, "西库": west_settle}
    reopened = {}
    for name in DELIVERIES:
        with closing(sqlite3.connect(directory / f"{name}.sqlite")) as db:
            merge_history = replay(db, correction.number)
            _check(f"{name}重开勘正回执", merge_history.receipt, merge_results[name].receipt)
            _check(f"{name}重开勘正调整", merge_history.adjustment, merge_results[name].adjustment)
            settle_history = replay(db, notices["P"].notice_id)
            _check(f"{name}重开撤P调整", settle_history.adjustment, settle_results[name].adjustment)
            reopened[name] = _book(db)
    summary["重开复核"] = _check("重开账不变", reopened,
                                 {"东库": {"rows": 1, "litres": 7}, "西库": {"rows": 1, "litres": 11}})
    with closing(sqlite3.connect(directory / "先撤QA.sqlite")) as db:
        seed_mirror(db, publications, DELIVERIES["东库"])
        apply_merge(db, wire)
        accept(db, encode_document(publications["QA"].parts[0]))
        accept(db, encode_document(notices["QA"]))
        settled = accept(db, notice_p)
        summary["先撤QA再撤P"] = _check("先撤QA再撤P", {**_settlement(settled.adjustment), "after": _book(db)}, {
            "added": [], "removed": ["8"], "row_delta": -1, "litres_delta": -13,
            "by_station": {STATION: [-1, -13]}, "after": {"rows": 0, "litres": 0},
        })
    with closing(sqlite3.connect(directory / "留QA收R.sqlite")) as db:
        seed_mirror(db, publications, DELIVERIES["东库"])
        apply_merge(db, wire)
        accept(db, encode_document(publications["QA"].parts[0]))
        r_receipt = accept(db, encode_document(publications["R"].parts[0]))
        _check("合并期R实收新增为空", r_receipt.receipt.details["added_fact_ids"], [])
        settled = accept(db, notice_p)
        summary["留QA收R再撤P"] = _check("留QA收R再撤P", {**_settlement(settled.adjustment), "after": _book(db)}, {
            "added": ["1"], "removed": [], "row_delta": 1, "litres_delta": 7,
            "by_station": {STATION: [1, 7]}, "after": {"rows": 2, "litres": 20},
        })
    return summary


def main():
    preview()
    with TemporaryDirectory(prefix="binti-merge-") as directory:
        summary = merge_scenarios(directory)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
