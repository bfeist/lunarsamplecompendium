"""End-to-end contract for the OXR-only whole-document pipeline."""
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/convert_pdfs.py'


def response(stem):
    picture = f'![](outputs/{stem}/imgs/1_3.png)'
    return {'code': 0, 'warnings': [], 'data': {'metadata': {'page_num': 2}, 'result': {'outputs': {
        'markdown': '# Sample\n\nText and $x^2$.\n\n<table><tr><td>21.6</td></tr></table>\n\n' + picture,
        'json': {'pages': [
            {'page_num': 0, 'page_size': {'w': 100, 'h': 200}, 'blocks': [
                {'type': 'Title', 'content': 'Sample'}, {'type': 'Table', 'content': '<table><tr><td>21.6</td></tr></table>'}]},
            {'page_num': 1, 'page_size': {'w': 100, 'h': 200}, 'blocks': [{'type': 'Picture', 'content': picture}]}]}}}}}


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.module = runpy.run_path(str(SCRIPT))

    def test_native_pdf_upload_and_full_document_contract(self):
        request = self.module['make_request'](b'%PDF whole document', 'lsc_fixture.pdf', 'http://localhost:8010/v1', True)
        self.assertIn(b'filename="lsc_fixture.pdf"', request.data)
        self.assertIn(b'Content-Type: application/pdf', request.data)
        self.assertIn(b'%PDF whole document', request.data)
        self.assertIn(b'"keep_header_footer": true', request.data)
        markdown, structured = self.module['unpack_response'](response('fixture'))
        self.assertEqual(len(structured['pages']), 2)
        self.assertIn('$x^2$', markdown)
        invalid = response('fixture')
        invalid['data']['metadata']['page_num'] = 3
        with self.assertRaisesRegex(ValueError, 'page counts disagree'):
            self.module['unpack_response'](invalid)

    def test_full_conversion_needs_no_marker_or_chandra_and_resume_repairs_missing_image(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs, assets, out = root / 'pdfs', root / 'assets', root / 'documents'
            inputs.mkdir()
            inputs.joinpath('sample.pdf').write_bytes(b'%PDF fixture: server mocked')
            def parse(request, timeout):
                stem = request.data.split(b'filename="')[1].split(b'.pdf"')[0].decode()
                crop = assets / stem / 'imgs/1_3.png'
                crop.parent.mkdir(parents=True, exist_ok=True)
                crop.write_bytes(b'exact OXR image fixture')
                return response(stem)
            main = self.module['main']
            main.__globals__['ROOT'] = root
            args = ['sample', '--input', str(inputs), '--output', str(out), '--asset-dir', str(assets)]
            with patch.dict(main.__globals__, request_parse=Mock(side_effect=parse)) as scope, patch('builtins.print'):
                main(args)
                scope['request_parse'].assert_called_once()
                folder = out / 'sample'
                markdown = (folder / 'sample.md').read_text(encoding='utf-8')
                self.assertIn('images/1_3.png', markdown)
                self.assertNotIn('outputs/', markdown)
                self.assertEqual((folder / 'images/1_3.png').read_bytes(), b'exact OXR image fixture')
                layout = json.loads((folder / 'sample.json').read_text())
                self.assertIn('images/1_3.png', layout['pages'][1]['blocks'][0]['content'])
                metadata = json.loads((folder / 'sample_meta.json').read_text())
                self.assertEqual(metadata['page_stats'][1]['page_id'], 1)
                self.assertEqual(dict(metadata['page_stats'][0]['block_counts'])['Table'], 1)
                scope['request_parse'].reset_mock()
                main(args)
                scope['request_parse'].assert_not_called()
                (folder / 'images/1_3.png').unlink()
                main(args)
                scope['request_parse'].assert_called_once()
                scope['request_parse'].reset_mock()
                with self.assertRaisesRegex(ValueError, 'settings changed'):
                    main(args + ['--drop-header-footer'])
                scope['request_parse'].assert_not_called()

    def test_failed_document_stays_incomplete_and_later_document_is_saved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs = root / 'pdfs'
            inputs.mkdir()
            for name in ('a', 'b'):
                (inputs / (name + '.pdf')).write_bytes(name.encode())
            valid = response('fixture')
            valid['data']['result']['outputs']['markdown'] = '# No figures'
            valid['data']['result']['outputs']['json']['pages'][1]['blocks'] = []
            main = self.module['main']
            main.__globals__['ROOT'] = root
            out = root / 'documents'
            args = ['--all', '--input', str(inputs), '--output', str(out), '--asset-dir', str(root / 'assets')]
            with patch.dict(main.__globals__, request_parse=Mock(side_effect=[ValueError('OCR failed'), valid])), patch('builtins.print'):
                with self.assertRaises(SystemExit) as exc:
                    main(args)
                self.assertEqual(exc.exception.code, 1)
            self.assertFalse((out / 'a/result.json').exists())
            self.assertTrue((out / 'b/result.json').exists())
            summary = json.loads((out / 'run-summary.json').read_text())
            self.assertEqual(summary['incomplete_documents'], ['a'])
            self.assertEqual(summary['complete_pages'], 2)

    def test_missing_or_escaping_crop_is_never_marked_successful(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / 'doc'
            folder.mkdir()
            preserve = self.module['preserve_assets']
            with self.assertRaises(FileNotFoundError):
                preserve('![](outputs/fixture/imgs/0_1.png)', {}, root / 'assets', folder, 'fixture')
            with self.assertRaises(ValueError):
                preserve('![](outputs/fixture/imgs/../../secret.png)', {}, root / 'assets', folder, 'fixture')


if __name__ == '__main__':
    unittest.main()
