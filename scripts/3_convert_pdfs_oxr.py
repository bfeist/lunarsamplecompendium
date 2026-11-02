"""Convert pages with local OXR, saving candidates separately for comparison.

Page numbers on the CLI and in output paths are one-based. No corpus files are
replaced. Run setup_oxr.ps1 first; this client never uses SoMark's hosted API.
"""
import argparse
import base64
from contextlib import closing
import hashlib
import html
import io
import json
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
OXR_REVISION = 'c1a1dcb312ee9f691404f127785510453c0d9350'
MODEL_REVISION = 'c708407f9d2b4df658f2543d9132b62bb7221df4'
ARTIFACTS = ('source.png', 'response.json', 'page.md', 'page.json', 'review.html')


def write_json(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2), encoding='utf-8')
    tmp.replace(path)


def parse_pages(value):
    pages = set()
    try:
        for part in value.split(','):
            bounds = [int(n) for n in part.split('-')]
            if len(bounds) == 1:
                bounds *= 2
            if len(bounds) != 2 or bounds[0] < 1 or bounds[1] < bounds[0]:
                raise ValueError()
            pages.update(range(bounds[0], bounds[1] + 1))
    except ValueError:
        raise argparse.ArgumentTypeError('Use one-based page numbers, e.g. 1,3-5')
    return sorted(pages)


def local_url(value):
    url = urllib.parse.urlsplit(value)
    if (url.scheme != 'http' or url.hostname not in {'localhost', '127.0.0.1', '::1'}
            or url.username or url.password or url.query or url.fragment):
        raise argparse.ArgumentTypeError('Use a local HTTP OXR URL, e.g. http://localhost:8010/v1')
    return value.rstrip('/')


def make_request(image, base_url, keep_header_footer=True):
    boundary = uuid.uuid4().hex
    fields = [('api_key', 'dummy'), ('output_formats', 'markdown'), ('output_formats', 'json'),
              ('feature_config', json.dumps({'keep_header_footer': keep_header_footer})),
              ('element_formats', json.dumps({'image': 'base64', 'table': 'html'})),
              ('extra', json.dumps({'temperature': 0}))]
    parts = []
    for key, value in fields:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    parts.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="page.png"\r\nContent-Type: image/png\r\n\r\n'.encode(),
                  image, f'\r\n--{boundary}--\r\n'.encode()])
    return urllib.request.Request(base_url + '/parse/sync', data=b''.join(parts),
                                  headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})


def request_parse(request, attempts=3):
    # Do not follow redirects or inherit proxies: scans stay on the local service.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    for attempt in range(attempts):
        try:
            with opener.open(request, timeout=900) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError) as exc:
            retryable = isinstance(exc, TimeoutError) or (isinstance(exc, urllib.error.HTTPError)
                         and exc.code in {408, 429, 500, 502, 503, 504})
            if not retryable or attempt + 1 == attempts:
                raise
            time.sleep(2 ** (attempt + 1))


def unpack_response(response):
    if response.get('code') != 0 or not isinstance(response.get('data'), dict):
        raise ValueError(f'OXR error {response.get("code")}: {response.get("message")}')
    data = response['data']
    if data.get('error'):
        raise ValueError(f'OXR error: {data["error"]}')
    outputs = data.get('result', {}).get('outputs', {})
    markdown, structured = outputs.get('markdown'), outputs.get('json')
    if not isinstance(markdown, str) or not isinstance(structured, dict):
        raise ValueError('OXR omitted Markdown or structured JSON')
    pages = structured.get('pages')
    if not isinstance(pages, list) or len(pages) != 1 or not isinstance(pages[0].get('blocks'), list):
        raise ValueError('Expected exactly one parsed page with blocks')
    if not markdown.strip() and not pages[0]['blocks']:
        raise ValueError('OXR returned an empty page; check the source before accepting it')
    return markdown, structured


def embed_images(markdown, structured, source):
    """Preserve figure crops even when upstream ignores image=base64.

    The pinned OXR API returns container filesystem paths for Picture blocks.
    Its JSON boxes are pixel coordinates in page_size. Crop the source scan
    using those boxes and embed PNGs in both exports; keep response.json raw.
    """
    from PIL import Image
    with Image.open(source) as image:
        page = structured['pages'][0]
        size = page.get('page_size', {'w': image.width, 'h': image.height})
        sx, sy = image.width / size['w'], image.height / size['h']
        for block in page['blocks']:
            content = block.get('content', '').strip()
            match = re.fullmatch(r'!\[[^\]]*\]\(([^)]+)\)', content)
            if not match or match[1].startswith('data:image/'):
                continue
            x1, y1, x2, y2 = block['bbox']
            box = (max(0, round(x1 * sx)), max(0, round(y1 * sy)),
                   min(image.width, round(x2 * sx)), min(image.height, round(y2 * sy)))
            if box[2] <= box[0] or box[3] <= box[1]:
                raise ValueError('OXR returned an invalid image bounding box')
            buffer = io.BytesIO()
            with image.crop(box) as crop:
                crop.save(buffer, format='PNG')
            embedded = 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')
            replacement = content.replace(match[1], embedded)
            markdown = markdown.replace(content, replacement)
            block['content'] = replacement
    for match in re.finditer(r'!\[[^\]]*\]\(([^)]+)\)', markdown):
        if not match[1].startswith('data:image/'):
            raise ValueError(f'Unpreserved image reference: {match[1]}')
    return markdown, structured


def safe_tables(markup):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(markup, 'html.parser')
    tables = soup.find_all('table')
    allowed = {'table', 'tr', 'td', 'th', 'thead', 'tbody', 'tfoot', 'caption',
               'p', 'br', 'sub', 'sup', 'b', 'i', 'strong', 'em'}
    for table in tables:
        for tag in [table, *table.find_all(True)]:
            if tag.name not in allowed:
                tag.unwrap()
                continue
            tag.attrs = {key: str(value) for key, value in tag.attrs.items()
                         if key in {'rowspan', 'colspan'} and str(value).isdigit()
                         and 1 <= int(value) <= 500}
    return ''.join(str(t) for t in tables)


def write_review(folder, name, page, markdown, structured, warnings, root):
    blocks = structured['pages'][0]['blocks']
    tables = [b for b in blocks if b.get('type') == 'Table']
    oxr_tables = safe_tables(''.join(b.get('content', '') for b in tables))
    chandra = root / 'output/table_repair' / name / f'page-{page}'
    prior = ''.join(p.read_text(encoding='utf-8') for p in sorted(chandra.glob('table-*.html')))
    title = html.escape(f'{name}, page {page}')
    review = ('<!doctype html><meta charset="utf-8">'
              '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src \'self\' data:; style-src \'unsafe-inline\'">'
              f'<title>{title} — OXR comparison</title>'
              '<style>body{font:15px system-ui;margin:24px}main{display:grid;grid-template-columns:4fr 3fr 3fr;gap:16px}'
              'section{overflow:auto}img{width:100%}table{border-collapse:collapse;font-size:12px;margin:16px 0}'
              'td,th{border:1px solid #aaa;padding:4px;white-space:nowrap}pre{white-space:pre-wrap}</style>'
              f'<h1>{title}</h1><p>Candidate: verify values, row/column attribution, references and methods against the scan.</p>'
              f'<p>{html.escape("; ".join(warnings))}</p><main><section><h2>Source</h2><img src="source.png"></section>'
              f'<section><h2>OXR tables</h2>{oxr_tables or "No tables returned"}</section>'
              f'<section><h2>Chandra tables</h2>{safe_tables(prior) or "No saved candidates"}</section></main>'
              f'<details><summary>Full OXR Markdown</summary><pre>{html.escape(markdown)}</pre></details>')
    (folder / 'review.html').write_text(review, encoding='utf-8')
    return len(tables)


def completed(folder, provenance):
    path = folder / 'result.json'
    if not path.exists():
        return False
    result = json.loads(path.read_text(encoding='utf-8'))
    if result['provenance'] != provenance:
        raise ValueError(f'{folder}: source/settings changed; choose a fresh --output directory')
    return all((folder / name).is_file() for name in ARTIFACTS)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('pdfs', nargs='*', help='PDF basenames without .pdf')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--tables-only', action='store_true', help='Select the same Marker-detected table pages as Chandra')
    ap.add_argument('--pages', type=parse_pages, help='Select one-based pages, e.g. 1,3-5 (one PDF only)')
    ap.add_argument('--limit', type=int)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--base-url', type=local_url, default='http://localhost:8010/v1')
    ap.add_argument('--output', type=Path, default=ROOT / 'output/oxr')
    ap.add_argument('--dpi', type=int, default=180)
    ap.add_argument('--drop-header-footer', action='store_true', help='Use upstream filtering; can remove sample titles')
    args = ap.parse_args(argv)
    if args.all == bool(args.pdfs):
        ap.error('Specify PDF basenames or --all')
    if args.pages and (args.all or len(args.pdfs) != 1 or args.tables_only):
        ap.error('--pages requires one PDF and cannot be combined with --tables-only')
    if args.limit is not None and args.limit < 1:
        ap.error('--limit must be positive')
    if not 72 <= args.dpi <= 300:
        ap.error('--dpi must be between 72 and 300')
    out = args.output.resolve()
    for protected in (ROOT / 'pdfs', ROOT / 'output/md_output', ROOT / 'output/table_repair'):
        protected = protected.resolve()
        if out == protected or out in protected.parents or protected in out.parents:
            ap.error('--output must be separate from input PDFs and existing corpus/repairs')
    import pypdfium2 as pdfium
    names = sorted(p.stem for p in (ROOT / 'pdfs').glob('*.pdf')) if args.all else sorted(set(args.pdfs))
    jobs, page_counts, hashes = [], {}, {}
    for name in names:
        if name in {'.', '..'} or Path(name).name != name or '/' in name or '\\' in name:
            ap.error('Use PDF basenames without paths')
        source = ROOT / 'pdfs' / (name + '.pdf')
        with closing(pdfium.PdfDocument(source)) as doc:
            page_counts[name] = len(doc)
        if args.tables_only:
            meta = ROOT / 'output/md_output' / name / (name + '_meta.json')
            data = json.loads(meta.read_text(encoding='utf-8'))
            selected = [(int(p['page_id']) + 1, dict(p['block_counts']).get('Table', 0))
                        for p in data['page_stats'] if dict(p['block_counts']).get('Table', 0)]
        else:
            selected = [(p, None) for p in (args.pages or range(1, page_counts[name] + 1))]
        for page, expected in selected:
            if not 1 <= page <= page_counts[name]:
                ap.error(f'{name}: page {page} outside 1-{page_counts[name]}')
            jobs.append((name, page, expected))
    if args.limit:
        jobs = jobs[:args.limit]
    print(f'Selected {len(jobs)} pages from {len(set(j[0] for j in jobs))} PDFs.', flush=True)
    if args.dry_run:
        for name, page, _ in jobs[:10]:
            print(f'  {name} p{page}')
        return
    config = {'engine': 'OXR-1.0', 'oxr_revision': OXR_REVISION, 'model_revision': MODEL_REVISION,
              'client_version': 3, 'base_url': args.base_url, 'dpi': args.dpi,
              'keep_header_footer': not args.drop_header_footer,
              'pypdfium2_version': str(pdfium.PYPDFIUM_INFO), 'pdfium_version': str(pdfium.PDFIUM_INFO),
              'temperature': 0, 'table_format': 'html', 'image_format': 'source_crop_base64'}
    states = []
    # Validate the whole selection before sending requests or modifying saved runs.
    for name, page, expected in jobs:
        if name not in hashes:
            hashes[name] = hashlib.sha256((ROOT / 'pdfs' / (name + '.pdf')).read_bytes()).hexdigest()
        provenance = {**config, 'source_sha256': hashes[name], 'page': page}
        folder = out / name / f'page-{page}'
        states.append((name, page, expected, folder, provenance, completed(folder, provenance)))
    failures = consecutive = 0
    for i, (name, page, expected, folder, provenance, done) in enumerate(states, 1):
        if done:
            print(f'[{i}/{len(states)}] {name} p{page}: already complete', flush=True)
            continue
        folder.mkdir(parents=True, exist_ok=True)
        # A result is the completion marker; interrupted reruns must not expose an old one.
        (folder / 'result.json').unlink(missing_ok=True)
        try:
            start = time.perf_counter()
            with closing(pdfium.PdfDocument(ROOT / 'pdfs' / (name + '.pdf'))) as doc:
                with closing(doc[page - 1]) as pdf_page:
                    bitmap = pdf_page.render(scale=args.dpi / 72)
                    try:
                        bitmap.to_pil().save(folder / 'source.png')
                    finally:
                        bitmap.close()
            response = request_parse(make_request((folder / 'source.png').read_bytes(), args.base_url,
                                                 keep_header_footer=not args.drop_header_footer))
            write_json(folder / 'response.json', response)
            markdown, structured = unpack_response(response)
            markdown, structured = embed_images(markdown, structured, folder / 'source.png')
            (folder / 'page.md').write_text(markdown, encoding='utf-8')
            write_json(folder / 'page.json', structured)
            warnings = list(response.get('warnings', []))
            tables = sum(b.get('type') == 'Table' for b in structured['pages'][0]['blocks'])
            if expected is not None and tables != expected:
                warnings.append(f'Marker found {expected} tables; OXR returned {tables}')
            write_review(folder, name, page, markdown, structured, warnings, ROOT)
            write_json(folder / 'result.json', {'provenance': provenance, 'status': 'needs_review',
                       'tables': tables, 'warnings': warnings, 'seconds': time.perf_counter() - start,
                       'server_metadata': response['data'].get('metadata', {})})
            (folder / 'error.json').unlink(missing_ok=True)
            consecutive = 0
            print(f'[{i}/{len(states)}] {name} p{page}: {tables} tables, {time.perf_counter() - start:.1f}s', flush=True)
        except Exception as exc:
            failures += 1
            consecutive += 1
            write_json(folder / 'error.json', {'error': str(exc), 'provenance': provenance})
            print(f'[{i}/{len(states)}] {name} p{page}: ERROR {exc}', flush=True)
            unavailable = isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError)
            config_error = isinstance(exc, urllib.error.HTTPError) and exc.code in {301, 302, 307, 308, 401, 403, 404}
            if unavailable or config_error or consecutive >= 3:
                break
    out.mkdir(parents=True, exist_ok=True)
    incomplete = [{'pdf': name, 'page': page} for name, page, _, folder, provenance, _ in states
                  if not completed(folder, provenance)]
    # Publish a combined document only when every source page completed in this selection.
    for name in names:
        selected = [s for s in states if s[0] == name]
        if ({s[1] for s in selected} == set(range(1, page_counts[name] + 1))
                and all(completed(s[3], s[4]) for s in selected)):
            text = '\n\n'.join(f'<!-- Source page {s[1]} -->\n' + (s[3] / 'page.md').read_text(encoding='utf-8')
                                for s in sorted(selected, key=lambda s: s[1]))
            (out / name / (name + '.md')).write_text(text, encoding='utf-8')
    links = []
    for path in sorted(out.glob('*/page-*/result.json')):
        if all((path.parent / f).is_file() for f in ARTIFACTS):
            relative = path.parent.relative_to(out).as_posix()
            links.append(f'<li><a href="{urllib.parse.quote(relative)}/review.html">{html.escape(relative)}</a></li>')
    (out / 'index.html').write_text('<!doctype html><meta charset="utf-8"><h1>OXR candidates</h1><ul>' + ''.join(links) + '</ul>', encoding='utf-8')
    write_json(out / 'run-summary.json', {'selected_pages': len(states), 'complete_pages': len(states) - len(incomplete),
                                        'failed_this_run': failures, 'incomplete_pages': incomplete})
    print(f'Completed {len(states) - len(incomplete)}/{len(states)} selected pages. Review: {out / "index.html"}', flush=True)
    if incomplete:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
