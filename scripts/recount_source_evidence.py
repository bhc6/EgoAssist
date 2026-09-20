"""Recount archived packets without rerunning retrieval or modifying original logs."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from egoassist.eval.source_evidence import score_packet, retrieval_metrics, acceptance


def recount(folder, dataset, output):
    hashes = {}
    def read(path):
        data = path.read_bytes()
        hashes[str(path.resolve())] = hashlib.sha256(data).hexdigest()
        return json.loads(data)
    protocol = read(folder / 'protocol.json')
    samples = read(dataset)
    if hashes[str(dataset.resolve())] != protocol['datasetSha256']:
        raise ValueError('dataset differs from frozen protocol')
    original = read(folder / 'verification.json')
    rows = []
    for name in protocol['developmentConversations']:
        sample = next(row for row in samples if row['sample_id'] == name)
        packets = read(folder / (name + '-retrieval-packets.json'))
        receipts = read(folder / (name + '-imports.json'))
        if len(sample['qa']) != len(packets):
            raise ValueError('question/packet count mismatch')
        for index, (qa, packet) in enumerate(zip(sample['qa'], packets)):
            if qa['question'] != packet['question']:
                raise ValueError('question/packet mismatch')
            rows.append(score_packet(qa, packet, receipts, conversation=name, index=index))
    metrics = retrieval_metrics(rows)
    report = {'schema': 'egoassist.evidence-evaluation.v2', 'sourceHashes': hashes,
        'method': 'Archived packet recount; no new retrieval, model calls, or answer scoring.',
        'corrections': ['Split compound references', 'Separate adversarial context from answerable recall',
                        'Separate HTTP success from quality acceptance'],
        'originalReportedMetrics': original['metrics'], 'metrics': metrics,
        **acceptance(original['answers'], metrics), 'retrieval': rows}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({key: value for key, value in report.items() if key not in {'retrieval', 'sourceHashes'}}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--dataset', type=Path, default=Path('D:/DEMO/locomo-evaluation-20260917/upstream/data/locomo10.json'))
    args = parser.parse_args()
    recount(args.input, args.dataset, args.output)
