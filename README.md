<!-- filepath: d:\lsc\README.md -->

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
