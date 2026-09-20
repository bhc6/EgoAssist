"""Reference coverage, deliberately separate from answer correctness."""
import re


def gold_references(values):
    result = set()
    for value in values:
        for part in re.split(r'[;,]', value):
            part = part.strip()
            if not re.fullmatch(r'D\d+:\d+', part):
                raise ValueError('invalid benchmark evidence reference: ' + repr(part))
            result.add(part)
    return result


def score_packet(qa, packet, receipts, *, conversation, index):
    mapping = {row['sourceId']: row['id'] for row in receipts}
    gold = gold_references(qa.get('evidence', []))
    if gold - set(mapping.values()):
        raise ValueError('gold references absent from imported sources')
    candidates = {mapping[row['sourceId']] for row in packet['audit']}
    anchors = {mapping[row['sourceId']] for row in packet['audit'] if row['selected']}
    selected = {row['externalId'] for row in packet['sources']}
    context = selected | {row['externalId'] for row in packet['conversation']}
    return {'conversation': conversation, 'index': index, 'category': int(qa['category']),
        'gold': sorted(gold), 'goldCount': len(gold),
        'candidateHits': len(gold & candidates), 'anchorHits': len(gold & anchors),
        'finalHits': len(gold & selected), 'answerContextHits': len(gold & context),
        'missingFromFinal': sorted(gold - selected),
        'candidateButNotFinal': sorted((gold & candidates) - selected),
        'absentFromCandidates': sorted(gold - candidates),
        'absentFromCandidatesAndFinal': sorted(gold - candidates - selected),
        'snapshotId': packet['snapshotId'], 'coverage': packet['coverage']}


def aggregate(rows):
    denominator = sum(row['goldCount'] for row in rows)
    hits = {key: sum(row[key] for row in rows)
            for key in ('candidateHits', 'anchorHits', 'finalHits', 'answerContextHits')}
    labeled = [row for row in rows if row['goldCount']]
    return {'questions': len(rows), 'questionsWithGold': len(labeled), 'goldReferences': denominator,
        **hits, 'recall': {key: count / denominator if denominator else None for key, count in hits.items()},
        'completeEvidenceQuestions': sum(row['finalHits'] == row['goldCount'] for row in labeled),
        'zeroFinalEvidenceQuestions': sum(row['finalHits'] == 0 for row in labeled),
        'candidateButNotFinalReferences': sum(len(row['candidateButNotFinal']) for row in rows),
        'absentFromCandidatesReferences': sum(len(row['absentFromCandidates']) for row in rows),
        'absentFromCandidatesAndFinalReferences': sum(len(row['absentFromCandidatesAndFinal']) for row in rows)}


def retrieval_metrics(rows):
    return {'answerable': aggregate([row for row in rows if row['category'] in {1, 2, 3, 4}]),
        'adversarialContextOnly': aggregate([row for row in rows if row['category'] == 5]),
        'allContextOnly': aggregate(rows),
        'byCategory': {str(category): aggregate([row for row in rows if row['category'] == category])
                       for category in range(1, 6)},
        'interpretation': 'Reference coverage is not answer accuracy. Category 5 measures referenced context only.'}


def acceptance(answers, metrics):
    operational = bool(answers) and all(row['httpStatus'] == 200 for row in answers)
    recall = metrics['answerable']['recall']['finalHits']
    recall_met = recall >= .8 if recall is not None else None
    return {'operationalChecksPassed': operational, 'answerQualityEvaluated': False,
        'answerAccuracy': None, 'answerableFalseRefusalRate': None,
        'finalEvidenceRecallTargetMet': recall_met,
        'qualityTargetsMet': False if recall_met is False else None,
        'passed': False if not operational or recall_met is False else None}
