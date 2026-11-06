import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from tools.summarize_performance import main
from tools.summarize_performance import summarize


class PerformanceSummaryTests(unittest.TestCase):
    def test_missing_invalid_and_failure_measurements_do_not_bias_success(self):
        report = summarize([{'motion_history': [
            {'status': 'completed', 'duration_seconds': value}
            for value in [1, 2, 3, None, -1, float('nan'), True]
        ] + [{'status': 'failed', 'duration_seconds': 100}]}])
        success = report['motion_history/motion/completed/duration_seconds']
        self.assertEqual(success['measured'], 3)
        self.assertEqual(success['missing_or_invalid'], 4)
        self.assertEqual(success['median_seconds'], 2)
        self.assertEqual(success['p95_seconds'], 3)
        self.assertEqual(report['motion_history/motion/failed/duration_seconds']['median_seconds'], 100)

    def test_empty_measurements_remain_unknown(self):
        report = summarize([{'engine_history': [{'operation': 'suggest', 'status': 'failed'}]}])
        self.assertIsNone(report['engine_history/suggest/failed/duration_seconds']['p95_seconds'])
        self.assertEqual(summarize([]), {})

    def test_cli_preserves_source_and_existing_report_and_reports_corruption(self):
        # Use the same writable runtime root as the rest of this Windows suite.
        runtime = Path(__file__).resolve().parents[1] / 'runtime'
        runtime.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runtime) as directory:
            root = Path(directory)
            game = root / 'game'
            game.mkdir()
            source = game / 'game.json'
            source.write_text('{broken', encoding='utf-8')
            output = root / 'report.json'
            args = ['summary', '--games-root', str(root), '--output', str(output)]
            with patch('sys.argv', args):
                self.assertEqual(main(), 1)
            report = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(report['files_read'], 0)
            self.assertEqual(len(report['errors']), 1)
            self.assertEqual(source.read_text(encoding='utf-8'), '{broken')
            before = output.read_bytes()
            with patch('sys.argv', args), self.assertRaises(FileExistsError):
                main()
            self.assertEqual(output.read_bytes(), before)
