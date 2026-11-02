# Lunar Sample Compendium in Markdown

The primary pipeline converts complete PDFs with **local OXR**. One pass handles
text, reading order, tables, formulas, figures and layout metadata. It runs directly
from the source PDFs and does not require Marker output or a Chandra repair pass.

## Set up the local service

Requirements: Windows with Docker Desktop/WSL2, an NVIDIA GPU with BF16 support,
a CUDA 13-compatible driver, Python 3.10+ and `uv`. Upstream specifies at least
24 GB VRAM (48 GB recommended for higher concurrency). The included settings
support the local RTX 4090 using vLLM's V1 runner for WSL2 compatibility, eager
inference and recognition concurrency of two. Leave roughly 16 GB VRAM free.

From PowerShell at the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup_oxr.ps1
```

This downloads pinned code and weights, builds the Linux image and waits for the
service. First setup downloads several GB. The service is available at
[localhost:8010/demo](http://localhost:8010/demo), bound only to loopback.
Source documents are processed locally. Downloads use GitHub, Hugging Face and
package registries; document parsing never calls the public SoMark API.

The conversion client uses only the Python standard library:

```powershell
# Convert complete named PDFs, including every page.
python scripts/convert_pdfs.py 10002 12012

# Convert the entire corpus. Rerun the same command to resume.
python scripts/convert_pdfs.py --all

# Inspect the selection, or limit the number of complete documents.
python scripts/convert_pdfs.py --all --dry-run
python scripts/convert_pdfs.py --all --limit 10
```

`--limit` counts whole documents. PDFs are sent directly to OXR rather than being
rendered and submitted as separate image jobs by this client. OXR performs PDF
rendering, layout detection, recognition, reading order and Markdown assembly.
The pinned service renders PDFs internally at 150 DPI.

## Document output

The default destination is `output/oxr_documents/<PDF>/`:

- `<PDF>.md`: the complete document, including HTML tables and image links.
- `<PDF>.json`: native OXR layout JSON, with page sizes, boxes and block content.
- `<PDF>_meta.json`: engine/provenance information and per-page block counts.
- `images/*.png`: the actual figure crops produced by OXR.
- `response.json`: the original API response.
- `result.json`: completion manifest, timing, page/table/image counts and settings.

`output/oxr_documents/index.html` lists converted documents. Image links are
relative to each document folder, so the exported Markdown, JSON and images stay
usable after the service stops. The shared `.local/oxr-assets` volume allows the
client to collect OXR's original crops despite the pinned API returning container
filesystem paths. Asset preservation failures leave a document incomplete.

Headers and footers are retained because OXR sometimes classifies sample titles
as headers. Use `--drop-header-footer` to enable upstream filtering. Tables are
kept as HTML to preserve merged cells. Scientific values and attribution still
need source checks when assessing whether OXR should replace the existing corpus.

## Resuming and operating

A completed PDF is skipped only when its source hash and conversion settings
match and every exported file is present. An interrupted or failed document is
retried as a whole. Changed source/model/settings require a fresh `--output`
directory. The conversion exits nonzero when selected documents remain incomplete;
`run-summary.json` records document and page completion counts. An isolated model
failure allows later documents to proceed; unavailable services, configuration
errors or three consecutive document failures stop the run. Transient HTTP errors
receive up to three attempts. The default timeout is one hour per request;
`--timeout` changes that limit.

Use `--input`, `--output`, `--base-url` or `--asset-dir` to change locations.
The asset directory must be the host side of the OXR `/app/outputs` mount. Run one
batch at a time against an output directory. Existing Marker and Chandra outputs
are preserved while the OXR corpus is evaluated; neither is part of the new path.

```powershell
docker compose -f compose.oxr.yaml logs -f --tail 50
docker compose -f compose.oxr.yaml stop
docker compose -f compose.oxr.yaml up -d --wait --wait-timeout 960

# Run all tests in the existing isolated client environment.
.local/oxr-client/Scripts/python.exe -m unittest discover -s tests -v
```

Code is pinned to OXR commit `c1a1dcb312ee9f691404f127785510453c0d9350`;
weights are pinned to `SoMarkAI/OXR-1.0` revision
`c708407f9d2b4df658f2543d9132b62bb7221df4`. Downloads, server crops and runtime
caches live in ignored `.local/`. See [OXR setup](https://github.com/SoMarkAI/OXR#1-run-locally)
and its [custom license](https://github.com/SoMarkAI/OXR/blob/main/LICENSE), which
adds commercial-use thresholds, online attribution and hosted API restrictions
to Apache 2.0.

## Legacy and diagnostic tools

The native PDF path has been exercised on complete `10002`, `12012` and `Luna16B1`
documents: 11 pages, 9 tables and 15 images. Source PDF page counts matched the
exports, copied images matched OXR's original crops byte for byte, and a second
run skipped all three documents. Thirteen automated tests cover both current and
legacy clients. Output is available in `output/oxr_documents`.

The previous Marker conversion (`scripts/1_convert_pdfs.py`), Chandra table repair
(`scripts/2_prepare_table_repairs.py`) and page-level OXR comparison
(`scripts/3_convert_pdfs_oxr.py`) remain available for historical comparison.
Their setup and the earlier page-level pilot are documented in
[legacy_pipeline.md](docs/legacy_pipeline.md). The earlier 2.2x timing comparison
measured selected page jobs against saved Chandra timings; it does not measure
replacement of the complete Marker-plus-Chandra pipeline.
