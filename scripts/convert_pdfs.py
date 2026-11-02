"""Convert complete PDFs with local OXR: text, tables, formulas, figures and layout.

No Marker metadata or Chandra repair pass is required. Each PDF is submitted as
one document; completed documents are resumable using source/settings hashes.
"""
import argparse
from collections import Counter
import hashlib
import html
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
OXR_REVISION = 'c1a1dcb312ee9f691404f127785510453c0d9350'
MODEL_REVISION = 'c708407f9d2b4df658f2543d9132b62bb7221df4'
ASSET_REFERENCE = re.compile(r'outputs/[^\s<>"\'()]+')


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2), encoding='utf-8')
    temporary.replace(path)


def local_url(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != 'http' or parsed.hostname not in {'localhost', '127.0.0.1', '::1'}
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise argparse.ArgumentTypeError('Use a local OXR URL, e.g. http://localhost:8010/v1')
    return value.rstrip('/')


def make_request(pdf_bytes, upload_name, base_url, keep_header_footer):
    boundary = uuid.uuid4().hex
    fields = [('api_key', 'dummy'), ('output_formats', 'markdown'), ('output_formats', 'json'),
              ('element_formats', json.dumps({'image': 'url', 'table': 'html'})),
              ('feature_config', json.dumps({'keep_header_footer': keep_header_footer})),
              ('extra', json.dumps({'temperature': 0}))]
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
             for key, value in fields]
    parts.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{upload_name}"\r\nContent-Type: application/pdf\r\n\r\n'.encode(),
                  pdf_bytes, f'\r\n--{boundary}--\r\n'.encode()])
    return urllib.request.Request(base_url + '/parse/sync', data=b''.join(parts),
                                  headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})


def request_parse(request, timeout):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    for attempt in range(3):
        try:
            with opener.open(request, timeout=timeout) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError) as exc:
            transient = isinstance(exc, urllib.error.HTTPError) and exc.code in {408, 429, 500, 502, 503, 504}
            if not transient or attempt == 2:
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
    if not isinstance(pages, list) or not pages:
        raise ValueError('OXR returned no pages')
    if [p.get('page_num') for p in pages] != list(range(len(pages))):
        raise ValueError('OXR returned missing, duplicate or unordered page numbers')
    if any(not isinstance(p.get('blocks'), list) for p in pages):
        raise ValueError('OXR omitted page blocks')
    if data.get('metadata', {}).get('page_num') != len(pages):
        raise ValueError('OXR metadata and layout page counts disagree')
    if not markdown.strip() and not any(p['blocks'] for p in pages):
        raise ValueError('OXR returned an empty document')
    return markdown, structured


def preserve_assets(markdown, structured, asset_root, folder, upload_stem):
    """Copy OXR's actual crops from the shared volume and rewrite both exports."""
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
        elif isinstance(value, dict):
            for item in value.values():
                yield from strings(item)
    references = {match for value in [markdown, *strings(structured)] for match in ASSET_REFERENCE.findall(value)}
    replacements = {}
    for reference in sorted(references):
        path = PurePosixPath(reference)
        if (len(path.parts) != 4 or path.parts[:3] != ('outputs', upload_stem, 'imgs')
                or '\\' in reference or path.suffix.lower() != '.png' or '..' in path.parts):
            raise ValueError(f'Unexpected OXR image path: {reference}')
        source = (asset_root / Path(*path.parts[1:])).resolve()
        if asset_root.resolve() not in source.parents:
            raise ValueError(f'OXR image path escapes asset directory: {reference}')
        if not source.is_file():
            raise FileNotFoundError(f'OXR crop missing: {source}. Start the service with compose.oxr.yaml to mount its outputs.')
        relative = Path('images') / path.name
        target = folder / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        replacements[reference] = relative.as_posix()

    def replace(value):
        if isinstance(value, str):
            return ASSET_REFERENCE.sub(lambda match: replacements[match[0]], value)
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        return value
    return replace(markdown), replace(structured), sorted(replacements.values())


def complete(folder, provenance):
    marker = folder / 'result.json'
    if not marker.exists():
        return False
    result = json.loads(marker.read_text(encoding='utf-8'))
    if result['provenance'] != provenance:
        raise ValueError(f'{folder}: source/settings changed; use a fresh --output directory')
    return bool(result.get('files')) and all((folder / name).is_file() for name in result['files'])


def convert(source, folder, provenance, config, asset_root, timeout):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'result.json').unlink(missing_ok=True)
    started = time.perf_counter()
    upload_stem = 'lsc_' + hashlib.sha256(json.dumps(provenance, sort_keys=True).encode()).hexdigest()[:24]
    response = request_parse(make_request(source.read_bytes(), upload_stem + '.pdf', config['base_url'],
                                         config['keep_header_footer']), timeout)
    write_json(folder / 'response.json', response)
    markdown, structured = unpack_response(response)
    markdown, structured, images = preserve_assets(markdown, structured, asset_root, folder, upload_stem)
    name = source.stem
    (folder / (name + '.md')).write_text(markdown, encoding='utf-8')
    write_json(folder / (name + '.json'), structured)
    page_stats = [{'page_id': p['page_num'], 'block_counts': sorted(Counter(b['type'] for b in p['blocks']).items()),
                   'page_size': p.get('page_size')} for p in structured['pages']]
    write_json(folder / (name + '_meta.json'), {'engine': 'OXR-1.0', 'provenance': provenance,
               'page_stats': page_stats, 'server_metadata': response['data']['metadata'],
               'warnings': response.get('warnings', [])})
    result = {'provenance': provenance, 'status': 'converted', 'pages': len(page_stats), 'images': len(images),
              'tables': sum(dict(p['block_counts']).get('Table', 0) for p in page_stats),
              'seconds': time.perf_counter() - started, 'warnings': response.get('warnings', []),
              'files': [name + '.md', name + '.json', name + '_meta.json', 'response.json', *images]}
    write_json(folder / 'result.json', result)
    (folder / 'error.json').unlink(missing_ok=True)
    return result


def write_index(out):
    links = []
    for marker in sorted(out.glob('*/result.json')):
        result = json.loads(marker.read_text(encoding='utf-8'))
        if not complete(marker.parent, result['provenance']):
            continue
        name = marker.parent.name
        prefix = urllib.parse.quote(name)
        links.append(f'<li><a href="{prefix}/{prefix}.md">{html.escape(name)}</a> '
                     f'({result["pages"]} pages, {result["tables"]} tables, {result["images"]} images) '
                     f'<a href="{prefix}/{prefix}.json">layout JSON</a></li>')
    (out / 'index.html').write_text('<!doctype html><meta charset="utf-8"><h1>OXR documents</h1><ul>' + ''.join(links) + '</ul>', encoding='utf-8')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('pdfs', nargs='*', help='PDF basenames without .pdf')
    ap.add_argument('--all', action='store_true', help='Convert every PDF in the input directory')
    ap.add_argument('--input', type=Path, default=ROOT / 'pdfs')
    ap.add_argument('--output', type=Path, default=ROOT / 'output/oxr_documents')
    ap.add_argument('--asset-dir', type=Path, default=ROOT / '.local/oxr-assets')
    ap.add_argument('--base-url', type=local_url, default='http://localhost:8010/v1')
    ap.add_argument('--drop-header-footer', action='store_true')
    ap.add_argument('--limit', type=int, help='Maximum number of complete PDFs to select')
    ap.add_argument('--timeout', type=int, default=3600, help='Seconds allowed per document request')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args(argv)
    if args.all == bool(args.pdfs):
        ap.error('Specify PDF basenames or --all')
    if args.timeout < 1 or (args.limit is not None and args.limit < 1):
        ap.error('--timeout and --limit must be positive')
    out, inputs, assets = args.output.resolve(), args.input.resolve(), args.asset_dir.resolve()
    for protected in (inputs, assets, (ROOT / 'output/md_output').resolve(), (ROOT / 'output/table_repair').resolve()):
        if out == protected or out in protected.parents or protected in out.parents:
            ap.error('--output must be separate from source PDFs, server assets and legacy corpus outputs')
    if args.all:
        sources = sorted(inputs.glob('*.pdf'))
    else:
        for name in args.pdfs:
            if name in {'.', '..'} or Path(name).name != name or '/' in name or '\\' in name:
                ap.error('Use PDF basenames without paths')
        sources = [inputs / (name + '.pdf') for name in sorted(set(args.pdfs))]
    if args.limit:
        sources = sources[:args.limit]
    if not sources:
        ap.error(f'No PDFs selected in {inputs}')
    for source in sources:
        if not source.is_file():
            ap.error(f'PDF not found: {source}')
    print(f'Selected {len(sources)} complete PDFs.', flush=True)
    if args.dry_run:
        for source in sources[:10]:
            print(f'  {source.name}')
        return
    config = {'engine': 'OXR-1.0', 'mode': 'native_pdf', 'client_version': 1,
              'oxr_revision': OXR_REVISION, 'model_revision': MODEL_REVISION,
              'base_url': args.base_url, 'keep_header_footer': not args.drop_header_footer,
              'table_format': 'html', 'image_format': 'oxr_crops', 'temperature': 0}
    jobs = []
    for source in sources:
        provenance = {**config, 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
        folder = out / source.stem
        jobs.append((source, folder, provenance, complete(folder, provenance)))
    failures = consecutive = 0
    for i, (source, folder, provenance, done) in enumerate(jobs, 1):
        if done:
            print(f'[{i}/{len(jobs)}] {source.name}: already complete', flush=True)
            continue
        try:
            print(f'[{i}/{len(jobs)}] Converting {source.name}...', flush=True)
            result = convert(source, folder, provenance, config, assets, args.timeout)
            consecutive = 0
            print(f'  {result["pages"]} pages, {result["tables"]} tables, {result["images"]} images; {result["seconds"]:.1f}s', flush=True)
        except Exception as exc:
            failures += 1
            consecutive += 1
            folder.mkdir(parents=True, exist_ok=True)
            write_json(folder / 'error.json', {'error': str(exc), 'provenance': provenance})
            print(f'  ERROR: {exc}', flush=True)
            network = isinstance(exc, (urllib.error.URLError, TimeoutError)) and not isinstance(exc, urllib.error.HTTPError)
            configuration = isinstance(exc, urllib.error.HTTPError) and exc.code in {301, 302, 303, 307, 308, 401, 403, 404}
            if network or configuration or consecutive >= 3:
                break
    out.mkdir(parents=True, exist_ok=True)
    incomplete = [source.stem for source, folder, provenance, _ in jobs if not complete(folder, provenance)]
    results = [json.loads((folder / 'result.json').read_text(encoding='utf-8'))
               for _, folder, provenance, _ in jobs if complete(folder, provenance)]
    write_json(out / 'run-summary.json', {'selected_documents': len(jobs), 'complete_documents': len(results),
               'complete_pages': sum(r['pages'] for r in results), 'failed_this_run': failures, 'incomplete_documents': incomplete})
    write_index(out)
    print(f'Completed {len(results)}/{len(jobs)} documents. Output: {out}', flush=True)
    if incomplete:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
