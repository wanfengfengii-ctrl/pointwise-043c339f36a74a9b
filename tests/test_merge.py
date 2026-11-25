import json
from dataclasses import replace

from lake.audit.history import HistoryNotFound, replay
from lake.audit.requests import RequestConflict
from lake.book.queries import station_book, withdrawal_adjustment
from lake.intake.batches import BatchInput
from lake.intake.freeze import freeze
from lake.intake.provenance import Provenance
from lake.intake.rows import SourceRow
from lake.mirror.acceptance import accept, apply_merge
from lake.mirror.conflicts import DocumentConflict
from lake.mirror.transactions import CommitInterrupted
from lake.release.formats import MergeCorrection, decode_document, encode_document
from lake.release.issues import issue, issue_merge
from lake.release.membership import MemberSelectionError
from lake.release.withdrawals import withdraw_issue

from tests import LakeTestCase


class MergeCase(LakeTestCase):
    def prepare(self, source):
        originals = freeze(source, BatchInput('北闸', '日报-合并', (
            SourceRow('A', 7, Provenance('北闸/原日报', '1')),
            SourceRow('B', 11, Provenance('北闸/原日报', '2')),
        )))
        review = freeze(source, BatchInput('北闸', '复核-合并', (
            SourceRow('C', 13, Provenance('北闸/复核页', '8')),
        )))
        facts = {fact.row_id: fact for fact in originals.facts + review.facts}
        publications = {}
        for name, labels in {'P': ('A', 'B'), 'QA': ('A',), 'QB': ('B',), 'R': ('C',)}.items():
            numbers = tuple(facts[label].fact_id for label in labels)
            publications[name] = issue(source, name, numbers, tuple((number,) for number in numbers))
        return facts, publications

    def correction(self, source, facts, publications, request='合并-P'):
        return issue_merge(source, request, publications['P'].publication_id,
                           (facts['A'].fact_id, facts['B'].fact_id), facts['C'].fact_id)

    def seed(self, mirror, publications, *deliveries):
        for name, position in deliveries:
            accept(mirror, publications[name].parts[position])


class MergeIssueTests(MergeCase):
    def test_issue_merge_binds_identities_and_replays(self):
        source = self.source()
        facts, publications = self.prepare(source)
        document = self.correction(source, facts, publications)
        self.assertIsInstance(document, MergeCorrection)
        self.assertEqual(document.publication_id, publications['P'].publication_id)
        self.assertEqual(document.old_ids, (facts['A'].fact_id, facts['B'].fact_id))
        replacement = document.replacement
        self.assertEqual((replacement.fact_id, replacement.station, replacement.litres),
                         (facts['C'].fact_id, '北闸', 13))
        self.assertEqual(replacement.provenance, Provenance('北闸/复核页', '8'))
        history = replay(source, document.number)
        self.assertEqual(history.documents, (document,))
        self.assertEqual(history.receipt.kind, 'merge_issue')
        self.assertEqual(history.receipt.details['publication_id'], publications['P'].publication_id)
        self.assertEqual(history.receipt.details['old_ids'], [facts['A'].fact_id, facts['B'].fact_id])
        self.assertEqual(history.origins[facts['A'].fact_id], {'record': '北闸/原日报', 'line': '1'})
        self.assertEqual(history.origins[facts['C'].fact_id], {'record': '北闸/复核页', 'line': '8'})
        self.assertEqual(self.correction(source, facts, publications), document)
        self.assertEqual(decode_document(encode_document(document)), document)
        source.close()
        source = self.source()
        self.assertEqual(replay(source, document.number).documents, (document,))
        self.assertEqual(self.correction(source, facts, publications), document)

    def test_issue_merge_rebind_conflict(self):
        source = self.source()
        facts, publications = self.prepare(source)
        document = self.correction(source, facts, publications)
        with self.assertRaises(RequestConflict):
            issue_merge(source, '合并-P', publications['P'].publication_id,
                        (facts['B'].fact_id, facts['A'].fact_id), facts['C'].fact_id)
        with self.assertRaises(RequestConflict):
            issue_merge(source, '合并-P', publications['QA'].publication_id,
                        (facts['A'].fact_id, facts['B'].fact_id), facts['C'].fact_id)
        with self.assertRaises(MemberSelectionError):
            issue_merge(source, '合并-P', publications['P'].publication_id,
                        (facts['A'].fact_id, facts['B'].fact_id), facts['B'].fact_id)
        self.assertEqual(replay(source, document.number).documents, (document,))

    def test_issue_merge_selection_rules(self):
        source = self.source()
        facts, publications = self.prepare(source)
        p = publications['P'].publication_id
        a, b, c = (facts[key].fact_id for key in 'ABC')
        for bad_old_ids in ((a,), (a, b, c), (a, a), ('未冻结', b), (a, b, None), a):
            with self.subTest(bad_old_ids=bad_old_ids):
                with self.assertRaises(MemberSelectionError):
                    issue_merge(source, f'选件-{bad_old_ids!r}', p, bad_old_ids, c)
        with self.assertRaises(MemberSelectionError):
            issue_merge(source, '复核未知', p, (a, b), '未冻结')
        with self.assertRaises(MemberSelectionError):
            issue_merge(source, '复核即原行', p, (a, b), a)
        with self.assertRaises(MemberSelectionError):
            issue_merge(source, '原行不在发布', p, (a, c), b)
        with self.assertRaises(MemberSelectionError):
            issue_merge(source, '未知发布', '未知发布号', (a, b), c)
        other = freeze(source, BatchInput('南堤', '日报-他站', (
            SourceRow('D', 13, Provenance('南堤/原日报', '1')),
        ))).facts[0]
        with self.assertRaises(MemberSelectionError):
            issue_merge(source, '跨站复核', p, (a, b), other.fact_id)
        with self.assertRaises(HistoryNotFound):
            replay(source, 'merge:未知')

    def test_one_correction_per_publication_and_fact(self):
        source = self.source()
        facts, publications = self.prepare(source)
        a, b, c = (facts[key].fact_id for key in 'ABC')
        document = self.correction(source, facts, publications)
        with self.assertRaises(MemberSelectionError):
            issue_merge(source, '再办', publications['P'].publication_id, (a, b), c)
        extra = freeze(source, BatchInput('北闸', '日报-补充', (
            SourceRow('D', 5, Provenance('北闸/补充页', '1')),
            SourceRow('E', 6, Provenance('北闸/补充页', '2')),
        )))
        d, e = (fact.fact_id for fact in extra.facts)
        s = issue(source, 'S', (a, d), ((a,), (d,)))
        t = issue(source, 'T', (d, e), ((d,), (e,)))
        with self.assertRaises(MemberSelectionError):
            issue_merge(source, '原行再参加', s.publication_id, (a, d), e)
        with self.assertRaises(MemberSelectionError):
            issue_merge(source, '复核再参加', t.publication_id, (d, e), c)
        with self.assertRaises(MemberSelectionError):
            issue_merge(source, '原行作复核', t.publication_id, (d, e), b)
        self.assertEqual(replay(source, document.number).documents, (document,))


class MergeAcceptanceTests(MergeCase):
    def test_merge_takes_effect_and_replays(self):
        source = self.source()
        facts, publications = self.prepare(source)
        document = self.correction(source, facts, publications)
        notice = withdraw_issue(source, '撤-P', publications['P'].publication_id).notice
        source.close()
        east = self.mirror('east')
        self.seed(east, publications, ('P', 0), ('P', 1))
        self.assertEqual(station_book(east, '北闸').litres, 18)
        result = apply_merge(east, document)
        self.assertEqual(result.status, 'merged')
        adjustment = result.adjustment
        self.assertEqual([f.fact_id for f in adjustment.removed],
                         [facts['A'].fact_id, facts['B'].fact_id])
        self.assertEqual([f.fact_id for f in adjustment.added], [facts['C'].fact_id])
        self.assertEqual((adjustment.row_delta, adjustment.litres_delta), (-1, -5))
        self.assertEqual([(d.station, d.row_delta, d.litres_delta) for d in adjustment.by_station],
                         [('北闸', -1, -5)])
        book = station_book(east, '北闸')
        self.assertEqual((book.row_count, book.litres), (1, 13))
        self.assertEqual([f.fact_id for f in book.facts], [facts['C'].fact_id])
        self.assertEqual(apply_merge(east, document), result)
        self.assertEqual(apply_merge(east, encode_document(document)), result)
        self.assertEqual(apply_merge(east, encode_document(document).encode()), result)
        accept(east, notice)
        self.assertEqual(apply_merge(east, document), result)
        history = replay(east, document.number)
        self.assertEqual(history.receipt, result.receipt)
        self.assertEqual(history.adjustment, adjustment)
        self.assertEqual(history.origins[facts['A'].fact_id], {'record': '北闸/原日报', 'line': '1'})
        self.assertEqual(history.origins[facts['B'].fact_id], {'record': '北闸/原日报', 'line': '2'})
        self.assertEqual(history.origins[facts['C'].fact_id], {'record': '北闸/复核页', 'line': '8'})

    def test_pending_until_carrier_parts_complete(self):
        source = self.source()
        facts, publications = self.prepare(source)
        document = self.correction(source, facts, publications)
        source.close()
        west = self.mirror('west')
        self.seed(west, publications, ('P', 0), ('QB', 0))
        self.assertEqual(station_book(west, '北闸').litres, 18)
        pending = apply_merge(west, document)
        self.assertEqual((pending.status, pending.receipt, pending.adjustment), ('pending', None, None))
        with self.assertRaises(HistoryNotFound):
            replay(west, document.number)
        self.assertEqual(station_book(west, '北闸').litres, 18)
        self.assertEqual(apply_merge(west, document).status, 'pending')
        accept(west, publications['P'].parts[1])
        merged = apply_merge(west, document)
        self.assertEqual(merged.status, 'merged')
        self.assertEqual((merged.adjustment.row_delta, merged.adjustment.litres_delta), (-1, -5))
        self.assertEqual(station_book(west, '北闸').litres, 13)
        self.assertEqual(replay(west, document.number).adjustment, merged.adjustment)

    def test_late_when_publication_already_withdrawn(self):
        source = self.source()
        facts, publications = self.prepare(source)
        document = self.correction(source, facts, publications)
        notice = withdraw_issue(source, '撤-P', publications['P'].publication_id).notice
        source.close()
        mirror = self.mirror('east')
        accept(mirror, notice)
        late = apply_merge(mirror, document)
        self.assertEqual((late.status, late.receipt, late.adjustment), ('late', None, None))
        with self.assertRaises(HistoryNotFound):
            replay(mirror, document.number)
        self.assertEqual(apply_merge(mirror, document).status, 'late')
        self.assertEqual(station_book(mirror, '北闸').litres, 0)

    def test_merge_document_conflicts(self):
        source = self.source()
        facts, publications = self.prepare(source)
        document = self.correction(source, facts, publications)
        source.close()
        mirror = self.mirror('east')
        self.seed(mirror, publications, ('P', 0), ('P', 1))
        result = apply_merge(mirror, document)
        forged = replace(document, old_ids=(document.old_ids[1], document.old_ids[0]))
        with self.assertRaises(DocumentConflict):
            apply_merge(mirror, forged)
        wrong = replace(document, replacement=replace(document.replacement, litres=14))
        with self.assertRaises(DocumentConflict):
            apply_merge(mirror, wrong)
        self.assertEqual(apply_merge(mirror, document), result)
        self.assertEqual(station_book(mirror, '北闸').litres, 13)

    def test_replacement_must_match_known_fact(self):
        source = self.source()
        facts, publications = self.prepare(source)
        document = self.correction(source, facts, publications)
        source.close()
        mirror = self.mirror('east')
        self.seed(mirror, publications, ('P', 0), ('P', 1), ('R', 0))
        wrong = replace(document, replacement=replace(document.replacement, litres=14))
        with self.assertRaises(DocumentConflict):
            apply_merge(mirror, wrong)
        wrong_origin = replace(
            document, replacement=replace(document.replacement, provenance=Provenance('错页', '9')))
        with self.assertRaises(DocumentConflict):
            apply_merge(mirror, wrong_origin)
        self.assertEqual(apply_merge(mirror, document).status, 'merged')

    def test_replacement_consistency_outlives_account(self):
        source = self.source()
        facts, publications = self.prepare(source)
        document = self.correction(source, facts, publications)
        notice = withdraw_issue(source, '撤-P', publications['P'].publication_id).notice
        source.close()
        mirror = self.mirror('east')
        self.seed(mirror, publications, ('P', 0), ('P', 1))
        apply_merge(mirror, document)
        accept(mirror, notice)
        self.assertEqual(station_book(mirror, '北闸').litres, 0)
        forged_part = replace(
            publications['R'].parts[0],
            facts=(replace(publications['R'].parts[0].facts[0], litres=14),))
        with self.assertRaises(DocumentConflict):
            accept(mirror, forged_part)
        genuine = accept(mirror, publications['R'].parts[0])
        self.assertEqual(genuine.status, 'received')
        self.assertEqual(genuine.receipt.details['added_fact_ids'], [facts['C'].fact_id])
        self.assertEqual(station_book(mirror, '北闸').litres, 13)

    def test_ordinary_receipt_during_merge_and_restore_on_withdrawal(self):
        source = self.source()
        facts, publications = self.prepare(source)
        document = self.correction(source, facts, publications)
        notice = withdraw_issue(source, '撤-P', publications['P'].publication_id).notice
        source.close()
        east = self.mirror('east')
        self.seed(east, publications, ('P', 0), ('P', 1))
        apply_merge(east, document)
        qa = accept(east, publications['QA'].parts[0])
        self.assertEqual(qa.status, 'received')
        self.assertEqual(qa.receipt.details['added_fact_ids'], [])
        self.assertEqual(qa.receipt.details['facts'][0]['fact_id'], facts['A'].fact_id)
        self.assertEqual(station_book(east, '北闸').litres, 13)
        settled = accept(east, notice)
        self.assertEqual(settled.status, 'withdrawn')
        adjustment = settled.adjustment
        self.assertEqual([f.fact_id for f in adjustment.added], [facts['A'].fact_id])
        self.assertEqual([f.fact_id for f in adjustment.removed], [facts['C'].fact_id])
        self.assertEqual((adjustment.row_delta, adjustment.litres_delta), (0, -6))
        self.assertEqual([(d.station, d.row_delta, d.litres_delta) for d in adjustment.by_station],
                         [('北闸', 0, -6)])
        book = station_book(east, '北闸')
        self.assertEqual((book.row_count, book.litres), (1, 7))
        self.assertEqual([f.provenance.line for f in book.facts], ['1'])
        self.assertEqual(withdrawal_adjustment(east, notice.notice_id), adjustment)
        history = replay(east, notice.notice_id)
        self.assertEqual(history.adjustment, adjustment)
        self.assertEqual(set(history.origins), {facts['A'].fact_id, facts['C'].fact_id})

    def test_withdraw_qa_before_p_leaves_zero(self):
        source = self.source()
        facts, publications = self.prepare(source)
        document = self.correction(source, facts, publications)
        notice_p = withdraw_issue(source, '撤-P', publications['P'].publication_id).notice
        notice_qa = withdraw_issue(source, '撤-QA', publications['QA'].publication_id).notice
        source.close()
        east = self.mirror('east')
        self.seed(east, publications, ('P', 0), ('P', 1))
        apply_merge(east, document)
        accept(east, publications['QA'].parts[0])
        settled_qa = accept(east, notice_qa)
        self.assertEqual((settled_qa.adjustment.added, settled_qa.adjustment.removed), ((), ()))
        self.assertEqual((settled_qa.adjustment.row_delta, settled_qa.adjustment.litres_delta), (0, 0))
        settled = accept(east, notice_p)
        self.assertEqual([f.fact_id for f in settled.adjustment.removed], [facts['C'].fact_id])
        self.assertEqual(settled.adjustment.added, ())
        self.assertEqual((settled.adjustment.row_delta, settled.adjustment.litres_delta), (-1, -13))
        book = station_book(east, '北闸')
        self.assertEqual((book.row_count, book.litres), (0, 0))

    def test_keep_qa_and_receive_r_restores_to_twenty(self):
        source = self.source()
        facts, publications = self.prepare(source)
        document = self.correction(source, facts, publications)
        notice = withdraw_issue(source, '撤-P', publications['P'].publication_id).notice
        source.close()
        east = self.mirror('east')
        self.seed(east, publications, ('P', 0), ('P', 1))
        apply_merge(east, document)
        self.assertEqual(accept(east, publications['QA'].parts[0]).receipt.details['added_fact_ids'], [])
        r_receipt = accept(east, publications['R'].parts[0])
        self.assertEqual(r_receipt.receipt.details['added_fact_ids'], [])
        self.assertEqual(station_book(east, '北闸').litres, 13)
        settled = accept(east, notice)
        self.assertEqual([f.fact_id for f in settled.adjustment.added], [facts['A'].fact_id])
        self.assertEqual(settled.adjustment.removed, ())
        self.assertEqual((settled.adjustment.row_delta, settled.adjustment.litres_delta), (1, 7))
        book = station_book(east, '北闸')
        self.assertEqual((book.row_count, book.litres), (2, 20))
        self.assertEqual([f.fact_id for f in book.facts], [facts['A'].fact_id, facts['C'].fact_id])

    def test_fail_before_commit_preserves_account_and_qualification(self):
        source = self.source()
        facts, publications = self.prepare(source)
        document = self.correction(source, facts, publications)
        source.close()
        mirror = self.mirror('west')
        self.seed(mirror, publications, ('P', 0), ('P', 1))
        before = station_book(mirror, '北闸')
        with self.assertRaises(CommitInterrupted):
            apply_merge(mirror, document, fail_before_commit=True)
        reader = self.connect('west')
        self.assertEqual(station_book(reader, '北闸'), before)
        with self.assertRaises(HistoryNotFound):
            replay(reader, document.number)
        mirror.close()
        mirror = self.mirror('west')
        merged = apply_merge(mirror, document)
        self.assertEqual(merged.status, 'merged')
        self.assertEqual(station_book(mirror, '北闸').litres, 13)
        self.assertEqual(apply_merge(mirror, document), merged)

    def test_accept_and_apply_merge_reject_each_others_documents(self):
        source = self.source()
        facts, publications = self.prepare(source)
        document = self.correction(source, facts, publications)
        source.close()
        mirror = self.mirror('east')
        with self.assertRaises(ValueError):
            accept(mirror, document)
        with self.assertRaises(ValueError):
            accept(mirror, encode_document(document))
        with self.assertRaises(ValueError):
            apply_merge(mirror, publications['P'].parts[0])
        with self.assertRaises(ValueError):
            apply_merge(mirror, encode_document(publications['P'].parts[0]))
        self.assertEqual(station_book(mirror, '北闸').litres, 0)
        self.seed(mirror, publications, ('P', 0), ('P', 1))
        self.assertEqual(apply_merge(mirror, document).status, 'merged')

    def test_invalid_merge_wire_is_rejected(self):
        source = self.source()
        facts, publications = self.prepare(source)
        document = self.correction(source, facts, publications)
        source.close()
        mirror = self.mirror('east')
        fields = json.loads(encode_document(document))
        fields['old_ids'] = [fields['old_ids'][0]]
        with self.assertRaises(ValueError):
            apply_merge(mirror, json.dumps(fields, ensure_ascii=False))
        fields = json.loads(encode_document(document))
        fields['replacement']['litres'] = '13.0'
        with self.assertRaises(ValueError):
            apply_merge(mirror, json.dumps(fields, ensure_ascii=False))
        fields = json.loads(encode_document(document))
        fields['old_ids'] = fields['old_ids'][0]
        with self.assertRaises(ValueError):
            apply_merge(mirror, json.dumps(fields, ensure_ascii=False))
        with self.assertRaises(HistoryNotFound):
            replay(mirror, document.number)
        self.assertEqual(station_book(mirror, '北闸').litres, 0)

    def test_plain_withdrawal_history_has_empty_added(self):
        source = self.source()
        facts, publications = self.prepare(source)
        notice = withdraw_issue(source, '撤-QA', publications['QA'].publication_id).notice
        source.close()
        mirror = self.mirror('east')
        self.seed(mirror, publications, ('QA', 0))
        settled = accept(mirror, notice)
        self.assertEqual(settled.adjustment.added, ())
        self.assertEqual([f.fact_id for f in settled.adjustment.removed], [facts['A'].fact_id])
        self.assertEqual(replay(mirror, notice.notice_id).adjustment.added, ())
