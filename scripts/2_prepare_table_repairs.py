"""Generate reviewable table replacements with local LM Studio; never edit md_output.

Requires pypdfium2 and beautifulsoup4. Prompt source:
https://github.com/datalab-to/chandra/blob/master/chandra/prompts.py
Snapshot used by the September 2026 audit. This is candidate generation, not
automatic scientific validation or application to the saved Markdown corpus.
"""
import argparse
import base64
import hashlib
import html
import json
from pathlib import Path
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output/table_repair'


def write_json(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2), encoding='utf-8')
    tmp.replace(path)


def request_completion(request, attempts=3):
    """Retry transient server/network errors without changing model settings."""
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=900) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError) as exc:
            retryable = not isinstance(exc, urllib.error.HTTPError) or exc.code in {408, 429, 500, 502, 503, 504}
            if not retryable or attempt + 1 == attempts:
                raise
            delay = 2 ** (attempt + 1)
            print(f'  Temporary request failure ({exc}); retry {attempt + 2}/{attempts} in {delay}s', flush=True)
            time.sleep(delay)


def is_complete(folder, result):
    files = ['source.png', 'review.html', 'response.json', 'page.html']
    files.extend(f'table-{i}.html' for i in range(1, result['tables'] + 1))
    return all((folder / name).is_file() for name in files)


def table_checks(table):
    """Detect inconsistent table grids, accounting for row and column spans."""
    occupied = set()
    widths = []
    for row_index, row in enumerate(table.find_all('tr')):
        col = 0
        for cell in row.find_all(['td', 'th'], recursive=False):
            while (row_index, col) in occupied:
                col += 1
            rs, cs = int(cell.get('rowspan', 1)), int(cell.get('colspan', 1))
            if not (1 <= rs <= 500 and 1 <= cs <= 100):
                return ['Invalid cell span']
            for r in range(row_index, row_index + rs):
                for c in range(col, col + cs):
                    if (r, c) in occupied:
                        return ['Overlapping cells']
                    occupied.add((r, c))
            col += cs
        widths.append(max((c + 1 for r, c in occupied if r == row_index), default=0))
    return ['Inconsistent row widths'] if len(set(widths)) > 1 else []


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('pdfs', nargs='*', help='PDF basenames, e.g. 12012 Luna16B1')
    ap.add_argument('--all', action='store_true', help='All pages marked Table in saved metadata')
    ap.add_argument('--limit', type=int, help='Maximum selected pages')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--model', default='lsc-table-repair')
    args = ap.parse_args()
    if args.all == bool(args.pdfs):
        ap.error('Specify PDF basenames or --all')
    if args.limit is not None and args.limit < 1:
        ap.error('--limit must be positive')
    names = sorted(p.stem for p in (ROOT / 'pdfs').glob('*.pdf')) if args.all else args.pdfs
    jobs = []
    for name in names:
        if Path(name).name != name or '/' in name or '\\' in name:
            ap.error('Use PDF basenames, without paths')
        meta = ROOT / 'output/md_output' / name / (name + '_meta.json')
        if not meta.exists():
            raise FileNotFoundError(meta)
        data = json.loads(meta.read_text(encoding='utf-8'))
        for page in data['page_stats']:
            count = dict(page['block_counts']).get('Table', 0)
            if count:
                jobs.append((name, int(page['page_id']), count))
    if args.limit:
        jobs = jobs[:args.limit]
    print(f'Selected {len(jobs)} pages from {len(set(j[0] for j in jobs))} PDFs.', flush=True)
    if args.dry_run:
        return
    import pypdfium2 as pdfium
    from bs4 import BeautifulSoup
    prompt = Path(__file__).with_name('chandra_ocr_layout_prompt.txt').read_text(encoding='utf-8')
    config = {'model': args.model, 'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
              'dpi': 180, 'temperature': 0, 'reasoning': 'off', 'max_output_tokens': 14000}
    OUT.mkdir(parents=True, exist_ok=True)
    failures = 0
    consecutive_failures = 0
    for i, (name, page, expected) in enumerate(jobs, 1):
        folder = OUT / name / f'page-{page + 1}'
        folder.mkdir(parents=True, exist_ok=True)
        pdf = ROOT / 'pdfs' / (name + '.pdf')
        provenance = {**config, 'source_sha256': hashlib.sha256(pdf.read_bytes()).hexdigest(), 'page': page + 1}
        done = folder / 'result.json'
        if done.exists():
            previous = json.loads(done.read_text(encoding='utf-8'))
            if previous['provenance'] != provenance:
                raise ValueError(f'{folder}: source/model/settings changed; keep this run and choose a fresh output directory in the script')
            if is_complete(folder, previous):
                print(f'[{i}/{len(jobs)}] {name} p{page + 1}: already complete', flush=True)
                continue
        try:
            start = time.perf_counter()
            doc = pdfium.PdfDocument(pdf)
            try:
                image = doc[page].render(scale=2.5).to_pil()
                image.save(folder / 'source.png')
            finally:
                doc.close()
            payload = {'model': args.model, 'temperature': 0, 'reasoning': 'off', 'store': False,
                       'max_output_tokens': 14000,
                       'input': [{'type': 'image', 'data_url': 'data:image/png;base64,' + base64.b64encode((folder / 'source.png').read_bytes()).decode()},
                                 {'type': 'text', 'content': prompt}]}
            request = urllib.request.Request('http://localhost:1234/api/v1/chat',
                       data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
            result = request_completion(request)
            write_json(folder / 'response.json', result)
            raw = ''.join(item.get('content', '') for item in result.get('output', []) if item.get('type') == 'message')
            if not raw.strip():
                raise ValueError('Model returned no answer content')
            (folder / 'page.html').write_text(raw, encoding='utf-8')
            soup = BeautifulSoup(raw, 'html.parser')
            tables = soup.find_all('table')
            warnings = []
            if len(tables) != expected:
                warnings.append(f'Saved detector found {expected} tables; model returned {len(tables)}')
            if result.get('stats', {}).get('total_output_tokens', 0) >= 14000:
                warnings.append('Output reached token limit; may be truncated')
            for n, table in enumerate(tables, 1):
                warnings.extend(f'Table {n}: {w}' for w in table_checks(table))
                # Keep only inert table markup in the review; raw response stays separate.
                for tag in table.find_all(True):
                    if tag.name not in {'tr', 'td', 'th', 'thead', 'tbody', 'tfoot', 'caption', 'p', 'br', 'sub', 'sup', 'b', 'i', 'math'}:
                        tag.unwrap()
                        continue
                    tag.attrs = {k: v for k, v in tag.attrs.items() if k in {'colspan', 'rowspan'}}
                table.attrs = {}
                (folder / f'table-{n}.html').write_text(str(table), encoding='utf-8')
            summary = {'provenance': provenance, 'status': 'needs_review', 'warnings': warnings,
                       'tables': len(tables), 'seconds': time.perf_counter() - start}
            review = '<!doctype html><meta charset="utf-8"><title>Table repair candidate</title><style>body{font:16px system-ui;margin:24px}main{display:grid;grid-template-columns:45% 55%;gap:20px}img{width:100%}article{overflow:auto}table{border-collapse:collapse;font-size:13px;margin:20px 0}td,th{border:1px solid #aaa;padding:5px;white-space:nowrap}</style>'
            review += '<h1>' + html.escape(f'{name}, page {page + 1}') + '</h1><p>Candidate only: check every value against its row, column, reference and method. A valid grid does not establish accuracy.</p>'
            review += '<p>' + html.escape('; '.join(warnings)) + '</p><main><img src="source.png"><article>' + ''.join(str(t) for t in tables) + '</article></main>'
            (folder / 'review.html').write_text(review, encoding='utf-8')
            write_json(done, summary)
            (folder / 'error.json').unlink(missing_ok=True)
            consecutive_failures = 0
            print(f'[{i}/{len(jobs)}] {name} p{page + 1}: {len(tables)} candidates, {len(warnings)} warnings', flush=True)
        except Exception as exc:
            failures += 1
            consecutive_failures += 1
            write_json(folder / 'error.json', {'error': str(exc), 'provenance': provenance})
            print(f'[{i}/{len(jobs)}] {name} p{page + 1}: ERROR {exc}', flush=True)
            server_unavailable = isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError)
            configuration_error = isinstance(exc, urllib.error.HTTPError) and exc.code in {401, 403, 404}
            if server_unavailable or configuration_error or consecutive_failures >= 3:
                print('Stopping after unavailable server, configuration failure, or three consecutive failed pages. Completed pages are saved.', flush=True)
                break
    links = []
    for result_path in sorted(OUT.glob('*/page-*/result.json')):
        entry = json.loads(result_path.read_text(encoding='utf-8'))
        relative = result_path.parent.relative_to(OUT)
        label = str(relative) + ': ' + '; '.join(entry['warnings'] or ['needs review'])
        links.append(f'<li><a href="{html.escape(relative.as_posix())}/review.html">{html.escape(label)}</a></li>')
    (OUT / 'index.html').write_text('<!doctype html><meta charset="utf-8"><h1>Table repair candidates</h1><ul>' + ''.join(links) + '</ul>', encoding='utf-8')
    incomplete = []
    for name, page, _ in jobs:
        folder = OUT / name / f'page-{page + 1}'
        result_path = folder / 'result.json'
        if not result_path.exists() or not is_complete(folder, json.loads(result_path.read_text(encoding='utf-8'))):
            incomplete.append({'pdf': name, 'page': page + 1})
    write_json(OUT / 'run-summary.json', {'selected_pages': len(jobs), 'complete_pages': len(jobs) - len(incomplete),
                                        'failed_this_run': failures, 'incomplete_pages': incomplete})
    print(f'Completed {len(jobs) - len(incomplete)}/{len(jobs)} selected pages; {len(incomplete)} remain.', flush=True)
    print(f'Review: {OUT / "index.html"}', flush=True)
    if failures or incomplete:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
