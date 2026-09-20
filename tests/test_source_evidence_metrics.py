import pytest

from egoassist.eval.source_evidence import gold_references, score_packet, retrieval_metrics, acceptance


def test_compound_references_are_independent_and_unknown_ids_fail():
    assert gold_references(['D8:6; D9:17', 'D8:6']) == {'D8:6', 'D9:17'}
    with pytest.raises(ValueError):
        gold_references(['D8:6 unknown'])
    with pytest.raises(ValueError, match='absent'):
        score_packet({'category': 1, 'evidence': ['D8:6']}, {}, [], conversation='c', index=0)


def test_adversarial_coverage_and_http_success_cannot_pass_quality():
    packet = {'audit': [{'sourceId': 's', 'selected': True}],
        'sources': [{'externalId': 'D1:1'}], 'conversation': [], 'snapshotId': 'p', 'coverage': {}}
    receipts = [{'sourceId': 's', 'id': 'D1:1'}, {'sourceId': 't', 'id': 'D1:2'}]
    rows = [score_packet({'category': category, 'evidence': refs}, packet, receipts,
                        conversation='c', index=index)
            for index, (category, refs) in enumerate([(1, ['D1:1; D1:2']), (5, ['D1:1']), (3, [])])]
    metrics = retrieval_metrics(rows)
    assert metrics['answerable']['goldReferences'] == 2
    assert metrics['answerable']['questionsWithGold'] == 1
    assert metrics['answerable']['completeEvidenceQuestions'] == 0
    assert metrics['answerable']['recall']['finalHits'] == .5
    assert metrics['adversarialContextOnly']['recall']['finalHits'] == 1
    result = acceptance([{'httpStatus': 200}], metrics)
    assert result['operationalChecksPassed'] is True
    assert result['passed'] is False and result['qualityTargetsMet'] is False
    assert result['answerAccuracy'] is None and result['answerableFalseRefusalRate'] is None
    assert acceptance([], metrics)['operationalChecksPassed'] is False
    metrics['answerable']['recall']['finalHits'] = 1
    assert acceptance([{'httpStatus': 200}], metrics)['passed'] is None
