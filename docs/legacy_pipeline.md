Historical setup and page-level experiments. The primary OXR whole-document
workflow is documented in the [current README](../README.md).

# Lunar Sample Compendium in Markdown

This project contains a Python script (`scripts/1_convert_pdfs.py`) designed to convert PDF documents into Markdown format. It processes all PDF files found in the `pdfs` directory and saves the corresponding Markdown files, extracted images, and metadata into subdirectories within the `output/md_output` directory.

The script utilizes the `marker-pdf` library for the conversion process and can leverage a GPU if a compatible one is available (and PyTorch is installed with CUDA support) to accelerate processing.

## Requirements

- Python 3.x
- `uv` for package and virtual environment management (or `pip` for package management)

## Setup and Execution

1.  **Create and Activate Virtual Environment (Recommended):**
    Open your terminal and navigate to the root directory of this project (`d:\lsc`). Create a virtual environment using `uv`:

    ```bash
    uv venv
    ```

    Activate the virtual environment (optional if using `uv run` for script execution):

    - **For Git Bash on Windows (when `.venv` is created by `uv` on Windows), and for macOS or Linux (bash/zsh):**
      ```bash
      source .venv/Scripts/activate # For Git Bash on Windows with uv-created venv
      # source .venv/bin/activate   # Standard for macOS/Linux or non-uv venvs
      ```
      _Note: The image provided confirms `.venv/Scripts/activate` is correct for `uv` on Windows, even in Git Bash._
    - **For Windows Command Prompt or PowerShell:**
      ```bash
      .venv\Scripts\activate
      ```
      Using `uv run` (see step 3) is often simpler as it handles the environment internally.

2.  **Install Dependencies:**
    If you've activated the environment, or if you're relying on `uv run` to manage it, install the required Python packages using `uv`:

    ```bash
    uv pip install -r requirements.txt
    ```

    If you don't have `uv`, you can use `pip`:

    ```bash
    pip install -r requirements.txt
    ```

3.  **Run the Script:**
    To execute the PDF conversion script using `uv` (which will use the activated virtual environment), run the following command from the project's root directory:

    ```bash
    uv run python scripts/1_convert_pdfs.py
    ```

    Alternatively, if you have activated the virtual environment and prefer not to use `uv run`, you can directly use:

    ```bash
    python scripts/1_convert_pdfs.py
    ```

    The script will automatically detect and use a GPU if available. If not, it will fall back to using the CPU. Processed files will be saved in the `output/md_output` directory, with each PDF's output in its own subfolder.

## Prepare table repair candidates with LM Studio

`scripts/2_prepare_table_repairs.py` generates candidate tables for review. It
preserves `output/md_output`; dense tables still need numerical and attribution
checks against the original page.

With Chandra OCR 2 BF16 and its vision projector installed, run in PowerShell:

```powershell
lms server start
lms load chandra-ocr-2 --identifier lsc-table-repair --context-length 32768 --parallel 1 --gpu max --yes
python scripts/2_prepare_table_repairs.py --all
```

Python needs `pypdfium2` and `beautifulsoup4`. The command selects pages marked
`Table` in saved metadata (1,510 pages in the audited corpus), disables reasoning,
and writes candidates, source images and raw responses under `output/table_repair`.
Open `output/table_repair/index.html` to review. Rerunning skips completed pages
with matching source hashes and settings, and retries failed pages. Tables the
old detector missed are not selected.

Transient HTTP failures are retried up to three attempts. An isolated failed page
is recorded and the batch continues; an unavailable server or three consecutive
failed pages stops the run. The final message and `output/table_repair/run-summary.json`
report how many selected pages actually completed. Rerun the same command to resume.

Use `python scripts/2_prepare_table_repairs.py 12012 Luna16B1` for particular PDFs,
or append `--dry-run` to see the selected page count without inference. Grid checks
catch some structural errors; a valid grid does not establish numerical accuracy.

## Run OXR locally for comparison

OXR is an optional alternative to Marker and the Chandra table-repair pass.
`scripts/3_convert_pdfs_oxr.py` saves page Markdown, structured JSON (including
layout boxes), source images, raw API responses and comparison pages under
`output/oxr`. Existing corpus and Chandra files are preserved.

The GPU services run in Linux containers through Docker Desktop/WSL2 on Windows.
Upstream requires an NVIDIA GPU with BF16 support, a CUDA 13-compatible driver,
and at least 24 GB VRAM; 48 GB is recommended for higher concurrency. The included
Compose settings target a 24 GB RTX 4090, use eager inference with vLLM's V1 model
runner for WSL2 compatibility, and limit recognition concurrency to two. Leave
roughly 16 GB VRAM free before starting; unload other
models if startup reports insufficient memory. The first image build downloads
several GB of runtime dependencies, in addition to the model weights.

From PowerShell at the repository root:

```powershell
# Download pinned code/weights, build and wait for the local service.
powershell -ExecutionPolicy Bypass -File scripts/setup_oxr.ps1

# Install just the client dependencies in a separate environment.
uv venv .local/oxr-client
uv pip install --python .local/oxr-client/Scripts/python.exe -r requirements-oxr.txt

# Start with the same table pages selected for Chandra.
.local/oxr-client/Scripts/python.exe scripts/3_convert_pdfs_oxr.py 12012 --tables-only

# Explicit one-based pages; this can include tables Marker missed.
.local/oxr-client/Scripts/python.exe scripts/3_convert_pdfs_oxr.py 12012 --pages 1,3-4

# Full document (all pages), or a bounded sample across the corpus.
.local/oxr-client/Scripts/python.exe scripts/3_convert_pdfs_oxr.py 12012
.local/oxr-client/Scripts/python.exe scripts/3_convert_pdfs_oxr.py --all --tables-only --limit 10
```

Use `--dry-run` to inspect the selection without inference. Open
`output/oxr/index.html` to compare OXR tables with the scan and saved Chandra
candidates. Each page includes the complete OXR Markdown source. Tables remain
review candidates: table counts and timing do not establish numerical accuracy.
This pipeline submits one 180-DPI page image at a time, matching Chandra's input
resolution; it does not infer relationships across page boundaries. For a complete
document selection, `<PDF>/<PDF>.md` is assembled only when every page succeeds.
Headers and footers are retained because OXR can classify a sample's title as a
header. `--drop-header-footer` enables upstream's default filtering instead.
The pinned API returns container paths for figures even when base64 is requested.
The client uses OXR's layout boxes to crop figures from the source scan and embeds
them as base64 PNGs in the Markdown and JSON exports. Raw responses are also kept.

Rerun the same command to resume. Completed pages are skipped when source hashes,
model revisions and client settings match. Failed or interrupted pages are retried;
HTTP transient failures get up to three attempts. An unavailable service or three
consecutive failed pages stops the batch. `run-summary.json` reports completion,
and incomplete runs exit nonzero. Changed settings require a fresh `--output`
directory. Do not run two writers against the same output directory simultaneously.

The service binds only to `127.0.0.1:8010`; the client accepts loopback URLs only.
It never calls the public SoMark parsing API. Model downloads use Hugging Face;
container dependencies come from their package registries. Visit
`http://localhost:8010/demo` for OXR's interactive parser.

```powershell
docker compose -f compose.oxr.yaml logs -f --tail 50
docker compose -f compose.oxr.yaml stop
docker compose -f compose.oxr.yaml up -d --wait --wait-timeout 960
python -m unittest discover -s tests -v
```

Code is pinned to OXR commit `c1a1dcb312ee9f691404f127785510453c0d9350`;
weights are pinned to `SoMarkAI/OXR-1.0` revision
`c708407f9d2b4df658f2543d9132b62bb7221df4`. Downloads and runtime caches live
in ignored `.local/`. See [OXR's setup documentation](https://github.com/SoMarkAI/OXR#1-run-locally)
and its [custom license](https://github.com/SoMarkAI/OXR/blob/main/LICENSE), which
adds commercial-use thresholds, online attribution and hosted API restrictions
to Apache 2.0.

Local pilot on November 1, 2026: all six pages of `10002` and `12012` completed,
including nine embedded figures, and a second run skipped all completed pages.
The four pages with saved Chandra counterparts took 48 seconds with OXR versus
105 seconds in the stored Chandra runs (about 2.2x faster). This is a small warmed
pilot, not a controlled benchmark or an accuracy score. Both returned four tables;
method-marker placement differs on `10002` page 1. Review candidates before using
their scientific values. The local measurements are saved in
`output/oxr/pilot-comparison.json`.

## Script Overview (`scripts/1_convert_pdfs.py`)

- **`setup_converter()`**: Initializes the `PdfConverter` from the `marker-pdf` library. This setup is done once to improve efficiency when processing multiple files. It checks for GPU availability and prints a message indicating whether GPU or CPU will be used.
- **`convert_pdf_to_markdown(pdf_path, base_output_path, converter)`**: Handles the conversion of a single PDF file.
  - It takes the path to a PDF, the base output directory, and the initialized converter as input.
  - Creates a unique output subfolder for the PDF's content (Markdown, images, metadata).
  - Calls the converter to process the PDF.
  - Saves the extracted text as a `.md` file, metadata as a `_meta.json` file, and any extracted images (as PNGs) into the designated output folder.
- **`convert_pdfs_in_folder(pdf_folder_path, base_output_path)`**:
  - Initializes the converter using `setup_converter()`.
  - Finds all `*.pdf` files in the specified `pdf_folder_path`.
  - Iterates through each found PDF and calls `convert_pdf_to_markdown` to process it.
- **Main Execution Block (`if __name__ == "__main__":`)**:
  - Defines the input PDF folder (`../pdfs` relative to the script) and the base output folder (`../output/md_output` relative to the script).
  - Calls `convert_pdfs_in_folder` to start the batch conversion process.

The script is designed to be run from the root of the `lunarsamplecompendium` project directory.
