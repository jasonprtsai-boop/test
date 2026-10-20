"""Read-only history audit; optional Excel copies in a new output directory."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tmchess.config import CONFIG
from tmchess.record_audit import audit_history
from tmchess.records import GameRecordWriter
from tmchess.session import GAME_ID_PATTERN


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--games-root', type=Path, default=CONFIG.capture_root)
    parser.add_argument('--game-id')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--export-dir', type=Path, help='New directory for Excel copies; must not exist.')
    args = parser.parse_args()
    if args.game_id and not GAME_ID_PATTERN.fullmatch(args.game_id):
        parser.error('Invalid game id')
    if args.export_dir and args.export_dir.exists():
        parser.error('Export directory must be new; existing records are never overwritten.')
    files = sorted(args.games_root.glob(f'{args.game_id or "*"}/game.json'))
    rows, payloads = [], []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
            frames, issues = audit_history(payload)
            rows.append({'game_id': path.parent.name, 'reconstructed_positions': len(frames), 'issues': issues})
            payloads.append(payload)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            rows.append({'game_id': path.parent.name, 'issues': [str(exc)]})
    report = {'games': len(rows), 'with_issues': sum(bool(row['issues']) for row in rows), 'results': rows}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.export_dir and payloads:
        writer = GameRecordWriter(args.export_dir)
        try:
            for payload in payloads:
                writer.schedule(payload)
            if not writer.flush(timeout=30):
                raise RuntimeError('Excel export did not complete; inspect logs and file permissions.')
            errors = [writer.status_for(p['game_id']).get('last_error') for p in payloads]
            if any(errors):
                raise RuntimeError(str(errors))
        finally:
            writer.close()
    print(json.dumps({'games': report['games'], 'with_issues': report['with_issues']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
