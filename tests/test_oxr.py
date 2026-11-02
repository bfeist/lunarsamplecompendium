"""OXR contract, local transport and resumable conversion regressions."""
import argparse
import base64
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import io
from pathlib import Path
import runpy
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/3_convert_pdfs_oxr.py'


def response():
    return {'code': 0, 'warnings': [], 'data': {'error': None, 'metadata': {}, 'result': {
        'outputs': {'markdown': '<table><tr><td>21.6</td></tr></table>', 'json': {'pages': [
            {'page_num': 0, 'blocks': [{'type': 'Table', 'content': '<table><tr><td>21.6</td></tr></table>'}]}]}}}}}


class OXRTests(unittest.TestCase):
    def setUp(self):
        self.module = runpy.run_path(str(SCRIPT))

    def test_http_200_application_error_and_empty_output_are_failures(self):
        for value in ({'code': 1137, 'message': 'Model failed', 'data': None},
                      {'code': 0, 'data': {'result': {'outputs': {}}}}):
            with self.assertRaises(ValueError):
                self.module['unpack_response'](value)
        self.assertIn('21.6', self.module['unpack_response'](response())[0])

    def test_page_selection_and_local_only_url(self):
        self.assertEqual(self.module['parse_pages']('1,3-5,3'), [1, 3, 4, 5])
        for invalid in ('0', '4-2', '1-2-3', 'foo'):
            with self.assertRaises(argparse.ArgumentTypeError):
                self.module['parse_pages'](invalid)
        for invalid in ('https://free-oxr.somark.ai/api/v1', 'http://example.com', 'http://user@localhost'):
            with self.assertRaises(argparse.ArgumentTypeError):
                self.module['local_url'](invalid)

    def test_review_removes_active_markup_and_retains_spans(self):
        markup = '<table onclick="alert(1)"><tr><td colspan="2"><img src="https://example.com"><script>alert(1)</script><b>21.6</b></td></tr></table>'
        safe = self.module['safe_tables'](markup)
        self.assertNotIn('onclick', safe)
        self.assertNotIn('<script', safe)
        self.assertNotIn('<img', safe)
        self.assertIn('colspan="2"', safe)
        self.assertIn('<b>21.6</b>', safe)

    def test_container_image_paths_become_embedded_source_crops(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'source.png'
            Image.new('RGB', (20, 30), 'red').save(source)
            content = '![](outputs/page/imgs/0_3.png)'
            structured = {'pages': [{'page_size': {'w': 20, 'h': 30}, 'blocks': [
                {'type': 'Picture', 'content': content, 'bbox': [2, 3, 12, 23]}]}]}
            markdown, result = self.module['embed_images'](content, structured, source)
            self.assertNotIn('outputs/', markdown)
            encoded = markdown.split('base64,')[1].removesuffix(')')
            with Image.open(io.BytesIO(base64.b64decode(encoded))) as crop:
                self.assertEqual(crop.size, (10, 20))
                self.assertEqual(crop.getpixel((0, 0)), (255, 0, 0))
            self.assertEqual(result['pages'][0]['blocks'][0]['content'], markdown)
            with self.assertRaisesRegex(ValueError, 'Unpreserved image'):
                self.module['embed_images']('![](missing.png)', {'pages': [{'blocks': []}]}, source)

    def test_multipart_contract_and_redirect_is_not_followed(self):
        captured = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                captured.append(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path == '/redirect/parse/sync':
                    self.send_response(302)
                    self.send_header('Location', '/v1/parse/sync')
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(json.dumps(response()).encode())
            def log_message(self, *args):
                pass
        with HTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f'http://127.0.0.1:{server.server_port}'
                request = self.module['make_request'](b'PNG fixture', url + '/v1')
                result = self.module['request_parse'](request)
                self.assertEqual(result['code'], 0)
                self.assertEqual(captured[0].count(b'name="output_formats"'), 2)
                self.assertIn(b'PNG fixture', captured[0])
                self.assertIn(b'base64', captured[0])
                self.assertIn(b'"keep_header_footer": true', captured[0])
                with self.assertRaises(urllib.error.HTTPError):
                    self.module['request_parse'](self.module['make_request'](b'PNG', url + '/redirect'))
                self.assertEqual(len(captured), 2)
            finally:
                server.shutdown()
                thread.join()

    def test_resume_retries_failed_page_and_refuses_changed_settings(self):
        import pypdfium2 as pdfium
        with tempfile.TemporaryDirectory(prefix='lsc-oxr-test-') as temp:
            root = Path(temp)
            (root / 'pdfs').mkdir()
            with closing(pdfium.PdfDocument.new()) as doc:
                for _ in range(2):
                    doc.new_page(72, 72).close()
                doc.save(root / 'pdfs/sample.pdf')
            baseline = root / 'output/md_output/sample'
            baseline.mkdir(parents=True)
            original = b'Existing Marker output'
            (baseline / 'sample.md').write_bytes(original)
            main = self.module['main']
            main.__globals__['ROOT'] = root
            out = root / 'output/oxr'
            args = ['sample', '--output', str(out)]
            with patch.dict(main.__globals__, request_parse=unittest.mock.Mock(side_effect=[ValueError('model failed'), response()])), patch('builtins.print'):
                with self.assertRaises(SystemExit):
                    main(args)
            self.assertFalse((out / 'sample/sample.md').exists())
            self.assertEqual(json.loads((out / 'run-summary.json').read_text())['complete_pages'], 1)
            with patch.dict(main.__globals__, request_parse=unittest.mock.Mock(return_value=response())) as scope, patch('builtins.print'):
                main(args)
                self.assertEqual(scope['request_parse'].call_count, 1)
                scope['request_parse'].reset_mock()
                main(args)
                scope['request_parse'].assert_not_called()
                with self.assertRaisesRegex(ValueError, 'settings changed'):
                    main(args + ['--dpi', '200'])
                scope['request_parse'].assert_not_called()
            self.assertEqual((baseline / 'sample.md').read_bytes(), original)
            self.assertIn('Source page 2', (out / 'sample/sample.md').read_text())
            self.assertFalse((out / 'sample/page-1/error.json').exists())
            # Missing an artifact makes that page eligible for repair on resume.
            (out / 'sample/page-1/page.json').unlink()
            with patch.dict(main.__globals__, request_parse=unittest.mock.Mock(return_value=response())) as scope, patch('builtins.print'):
                main(args)
                self.assertEqual(scope['request_parse'].call_count, 1)


if __name__ == '__main__':
    unittest.main()
