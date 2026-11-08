import json
import tempfile
import unittest
from pathlib import Path

import pymupdf
import jsonschema

from core.preprocessing.source_preprocessor import SourcePreprocessor


class TestSourcePreprocessor(unittest.TestCase):
    def test_offline_main_pdf_creates_valid_source_package_and_figure_crop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "main.pdf"
            doc = pymupdf.open()
            page = doc.new_page(width=595, height=842)
            page.insert_text((60, 60), "A deterministic test paper title")
            page.draw_rect(pymupdf.Rect(80, 120, 500, 430), color=(0, 0, 0), width=2)
            page.insert_textbox(pymupdf.Rect(60, 450, 530, 560), "FIGURE 1\nSchematic overview of the test system.\nAdditional caption details spanning multiple lines.", fontsize=10)
            doc.save(pdf_path)
            doc.close()

            output = root / "01_preprocessing"
            package = SourcePreprocessor(use_grobid=False).process(
                main_pdf=pdf_path, supplementary_files=[], output_dir=output,
                paper_id="test_paper", doi="10.0000/test"
            )
            self.assertTrue((output / "main_analysis.md").is_file())
            self.assertTrue((output / "si_analysis.md").is_file())
            self.assertTrue((output / "evidence_index.json").is_file())
            self.assertTrue((output / "PREPROCESSING_QC_REPORT.md").is_file())
            self.assertGreaterEqual(len(package["pages"]), 1)
            self.assertGreaterEqual(len(package["figures"]), 1)
            self.assertGreaterEqual(len(package["media_assets"]), 1)
            self.assertEqual(package["figures"][0]["label"], "FIGURE 1")
            self.assertIn("Additional caption details", package["figures"][0]["caption"])
            main_figure_assets = [
                asset for asset in package["media_assets"]
                if asset["doc_type"] == "MAIN" and asset["asset_type"] == "figure"
            ]
            self.assertEqual(len(main_figure_assets), 1)
            self.assertEqual(
                main_figure_assets[0]["extraction_method"],
                "pymupdf_vector_image_layout_fallback",
            )
            # The crop must cover the drawing above the caption, not the caption text block.
            self.assertLess(main_figure_assets[0]["bbox"][1], 200)
            self.assertGreater(main_figure_assets[0]["bbox"][3], 400)
            self.assertLess(main_figure_assets[0]["bbox"][3], 450)
            for asset in package["media_assets"]:
                asset_path = output / asset["relative_path"]
                self.assertTrue(asset_path.is_file())
                self.assertEqual(asset["sha256"], __import__("hashlib").sha256(asset_path.read_bytes()).hexdigest())

    def test_si_pdf_embedded_image_and_page_composite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main = pymupdf.open()
            page = main.new_page()
            page.insert_text((50, 50), "Main document with sufficient text for a test.")
            main_path = root / "main.pdf"
            main.save(main_path)
            main.close()

            si = pymupdf.open()
            page = si.new_page()
            page.insert_text((50, 50), "Figure S1. Supplementary image panel.")
            si_path = root / "support.pdf"
            si.save(si_path)
            si.close()

            output = root / "preprocessed"
            package = SourcePreprocessor(use_grobid=False).process(
                main_pdf=main_path, supplementary_files=[si_path],
                output_dir=output, paper_id="si_test"
            )
            self.assertTrue(package["metadata"]["has_si"])
            self.assertTrue((output / "supplementary" / "support" / "figures" / "composite_figures").is_dir())
            self.assertGreaterEqual(package["processing_summary"]["total_si_pages"], 1)


if __name__ == "__main__":
    unittest.main()
