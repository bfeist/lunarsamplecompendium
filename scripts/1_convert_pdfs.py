import os
import torch
import json
import glob
from marker.converters.pdf import PdfConverter
from marker.models import create_model_dict
from marker.config.parser import ConfigParser
from marker.output import text_from_rendered

def setup_converter():
    config = {
        "output_format": "markdown",
        "extract_images": True,
        "output_folder": None,  # placeholder, set later
        "debug": True
    }
    config_parser = ConfigParser(config)
    if torch.cuda.is_available():
        print("Using GPU for processing.")
    else:
        print("GPU not available, using CPU.")
    return PdfConverter(
        config=config_parser.generate_config_dict(),
        artifact_dict=create_model_dict(),
        processor_list=config_parser.get_processors(),
        renderer=config_parser.get_renderer()
    )

def convert_pdf_to_markdown(pdf_path, base_output_path, converter):
    # Ensure the PDF exists
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"The file '{pdf_path}' does not exist.")

    # Set up the output folder
    base_name = os.path.splitext(os.path.basename(pdf_path))[0]
    output_folder = os.path.join(base_output_path, base_name)
    os.makedirs(output_folder, exist_ok=True)

    # Perform the conversion
    try:
        print(f"Converting '{pdf_path}' to Markdown...")
        
        # Debugging: Print output folder
        print(f"Expected output folder: {output_folder}")
        
        # Convert the PDF
        rendered = converter(pdf_path)
        
        text, ext, images = text_from_rendered(rendered)
        text = text.encode('utf-8', errors='replace').decode('utf-8')
        with open(os.path.join(output_folder, f"{base_name}.{ext}"), "w+", encoding="utf-8") as f:
            f.write(text)
        with open(os.path.join(output_folder, f"{base_name}_meta.json"), "w+", encoding="utf-8") as f:
            f.write(json.dumps(rendered.metadata, indent=2))
        for img_name, img in images.items():
            img.save(os.path.join(output_folder, img_name), "PNG")
        
        print(f"Conversion complete. Output saved in '{output_folder}'.")
    except Exception as e:
        print(f"An error occurred during conversion: {e}")

def convert_pdfs_in_folder(pdf_folder_path, base_output_path):
    converter = setup_converter()
    pdf_files = glob.glob(os.path.join(pdf_folder_path, "*.pdf"))
    for pdf_path in pdf_files:
        convert_pdf_to_markdown(pdf_path, base_output_path, converter)

# Example usage
if __name__ == "__main__":
    pdf_folder_path = "d:/lsc/pdfs/"
    base_output_path = "d:/lsc/md_output/"
    convert_pdfs_in_folder(pdf_folder_path, base_output_path)
