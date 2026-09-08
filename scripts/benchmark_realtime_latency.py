"""Repeated real end-to-end latency acceptance; routing fallback is a failure."""
import argparse
import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import statistics

from verify_real_session import verify


async def run(output, repeats):
    output.mkdir(parents=True, exist_ok=False)
    runs = []
    for index in range(repeats):
        for scenario in ('greeting', 'memory'):
            directory = output / f'{scenario}-{index + 1}'
            with redirect_stdout(io.StringIO()):
                await verify(directory, 'qwen3.5-omni-flash-realtime', scenario)
            result = json.loads((directory / 'acceptance.json').read_text(encoding='utf-8'))
            statuses = result.get('routingStatuses', [])
            row = {k: result.get(k) for k in (
                'connectMs', 'firstTextMs', 'firstAudioMs', 'finishedMs', 'routingLatencyMs',
                'routingStatuses', 'memoryRetrieved', 'memoryAnswerCorrect', 'error')}
            row.update(scenario=scenario, source=str(directory / 'acceptance.json'),
                       passed=bool(result['passed'] and statuses and all(s == 'classified' for s in statuses)))
            runs.append(row)
            print(json.dumps(row), flush=True)
    summary = {'passed': all(row['passed'] for row in runs), 'mock': False,
               'input': 'text-control', 'microphoneAsrMeasured': False, 'runs': runs}
    for scenario in ('greeting', 'memory'):
        values = [row['firstAudioMs'] for row in runs
                  if row['scenario'] == scenario and row['firstAudioMs'] is not None]
        summary[scenario] = {'n': len(values), 'medianFirstAudioMs': statistics.median(values) if values else None,
                             'maxFirstAudioMs': max(values) if values else None}
    (output / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in summary.items() if k != 'runs'}), flush=True)
    return summary['passed']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error('--repeats must be positive')
    raise SystemExit(0 if asyncio.run(run(args.output, args.repeats)) else 1)
