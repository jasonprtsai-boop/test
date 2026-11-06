"""Read-only timing summary. Missing measurements never count as zero."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from pathlib import Path


FIELDS = {
    'capture_history': ('workflow_seconds', 'capture_seconds', 'processing_seconds', 'stationary_confirmation_seconds'),
    'engine_history': ('duration_seconds',),
    'motion_history': ('duration_seconds', 'standby_return_seconds'),
}


def summarize(payloads):
    result = {}
    for history, fields in FIELDS.items():
        rows = [r for p in payloads for r in p.get(history, []) if isinstance(r, dict)]
        for field in fields:
            groups = {}
            for row in rows:
                # Keep failed, incomplete and successful attempts separate.
                group = str(row.get('operation') or row.get('purpose') or 'motion') + '/' + str(row.get('status', 'unknown'))
                groups.setdefault(group, []).append(row.get(field))
            for group, raw in groups.items():
                values = sorted(float(v) for v in raw if type(v) in (int, float) and math.isfinite(v) and v >= 0)
                result[f'{history}/{group}/{field}'] = {
                    'attempts': len(raw), 'measured': len(values), 'missing_or_invalid': len(raw) - len(values),
                    'median_seconds': statistics.median(values) if values else None,
                    'p95_seconds': values[math.ceil(len(values) * .95) - 1] if values else None,
                }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--games-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    payloads, errors, sources = [], [], []
    files = sorted(args.games_root.glob('*/game.json'))
    for path in files:
        try:
            raw = path.read_bytes()
            sources.append({'path': str(path.resolve()), 'sha256': hashlib.sha256(raw).hexdigest()})
            payload = json.loads(raw.decode('utf-8'))
            if not isinstance(payload, dict) or any(not isinstance(payload.get(k, []), list) for k in FIELDS):
                raise ValueError('Invalid history structure')
            payloads.append(payload)
        except (OSError, ValueError) as exc:
            errors.append({'source': str(path), 'error': str(exc)})
    report = {'files_found': len(files), 'files_read': len(payloads), 'errors': errors,
              'method': 'P95 nearest rank; seconds; grouped by operation and status; segments are not battles; no accuracy claims',
              'sources': sources, 'timings': summarize(payloads)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite a source snapshot or a previous evidence report.
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
    return 1 if errors or not files else 0


if __name__ == '__main__':
    raise SystemExit(main())
