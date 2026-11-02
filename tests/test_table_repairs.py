"""Regression coverage for interrupted long-running table conversion jobs."""
import io
import json
from pathlib import Path
import runpy
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/2_prepare_table_repairs.py'


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.module = runpy.run_path(str(SCRIPT))

    def test_transient_http_error_is_retried(self):
        error = urllib.error.HTTPError('http://local', 500, 'temporary', {}, None)
        with patch('urllib.request.urlopen', side_effect=[error, io.BytesIO(b'{"ok":true}')]) as call, patch('time.sleep'):
            self.assertEqual(self.module['request_completion'](object()), {'ok': True})
            self.assertEqual(call.call_count, 2)

    def test_auth_error_is_not_retried(self):
        error = urllib.error.HTTPError('http://local', 401, 'unauthorized', {}, None)
        with patch('urllib.request.urlopen', side_effect=error) as call, patch('time.sleep'):
            with self.assertRaises(urllib.error.HTTPError):
                self.module['request_completion'](object())
            self.assertEqual(call.call_count, 1)

    def test_failed_page_does_not_discard_later_pages_and_resume_retries_only_failure(self):
        with tempfile.TemporaryDirectory(prefix='table-repair-test-') as temp:
            root = Path(temp)
            (root / 'pdfs').mkdir()
            (root / 'pdfs/sample.pdf').write_bytes(b'PDF fixture: rendering mocked')
            baseline = root / 'output/md_output/sample'
            baseline.mkdir(parents=True)
            (baseline / 'sample_meta.json').write_text(json.dumps({'page_stats': [
                {'page_id': i, 'block_counts': [['Table', 1]]} for i in range(2)]}))
            main = self.module['main']
            main.__globals__['ROOT'] = root
            main.__globals__['OUT'] = root / 'output/table_repair'
            doc = MagicMock()
            doc.__getitem__.return_value.render.return_value.to_pil.return_value.save.side_effect = lambda path: path.write_bytes(b'image fixture')
            fake_pdfium = types.SimpleNamespace(PdfDocument=MagicMock(return_value=doc))
            response = {'output': [{'type': 'message', 'content': '<table><tr><td>Olivine</td><td>21.6</td></tr></table>'}], 'stats': {'total_output_tokens': 30}}
            error = urllib.error.HTTPError('http://local', 500, 'temporary', {}, None)
            with patch.dict(sys.modules, {'pypdfium2': fake_pdfium}), patch.object(sys, 'argv', ['repair', '--all']), patch('time.sleep'), patch('builtins.print'):
                with patch('urllib.request.urlopen', side_effect=[error, error, error, io.BytesIO(json.dumps(response).encode())]):
                    with self.assertRaises(SystemExit) as exc:
                        main()
                    self.assertEqual(exc.exception.code, 1)
                out = main.__globals__['OUT']
                summary = json.loads((out / 'run-summary.json').read_text())
                self.assertEqual(summary['complete_pages'], 1)
                self.assertEqual(summary['incomplete_pages'], [{'pdf': 'sample', 'page': 1}])
                self.assertTrue((out / 'sample/page-2/result.json').exists())
                with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(response).encode())) as call:
                    main()
                    self.assertEqual(call.call_count, 1)
                summary = json.loads((out / 'run-summary.json').read_text())
                self.assertEqual(summary['complete_pages'], 2)
                self.assertFalse((out / 'sample/page-1/error.json').exists())


if __name__ == '__main__':
    unittest.main()
