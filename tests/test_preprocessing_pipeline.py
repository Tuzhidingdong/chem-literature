import json
import tempfile
import zipfile
from pathlib import Path

import pandas as pd
import pymupdf

from core.preprocessing.pipeline import preprocess_paired_corpus


def _make_pdf(path: Path, text: str):
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((50, 60), text)
    document.save(path)
    document.close()


def test_zip_si_members_are_preprocessed_and_original_archive_preserved():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main_pdf = root / "main.pdf"
        si_pdf = root / "support.pdf"
        _make_pdf(main_pdf, "Main paper text for preprocessing integration.")
        _make_pdf(si_pdf, "Supporting Information Figure S1 and supplementary method.")
        si_zip = root / "support.zip"
        with zipfile.ZipFile(si_zip, "w") as archive:
            archive.write(si_pdf, "docs/support.pdf")
            archive.writestr("tables/data.xlsx", b"placeholder-not-parsed")

        records = pd.DataFrame([{
            "doi": "10.0000/test-zip",
            "title": "Test paper",
            "year": "2025",
            "local_pdf_path": str(main_pdf),
            "local_si_info": str(si_zip),
        }])
        result = preprocess_paired_corpus(
            records, working_dir=root, preprocessed_dir="runs/preprocessed",
            grobid_enabled=False,
        )
        package_dir = root / result.iloc[0]["preprocessing_path"]
        assert result.iloc[0]["preprocessing_status"] == "PARTIAL_SI_UNSUPPORTED"
        assert "Supporting Information Figure S1" in (package_dir / "si_analysis.md").read_text(encoding="utf-8")
        assert (package_dir / "source_archives" / "support.zip").is_file()
        manifest = json.loads((package_dir / "evidence_index.json").read_text(encoding="utf-8"))
        assert manifest["metadata"]["has_si"] is True
        assert manifest["inventory"]["source_archives"]
        assert (package_dir / "PREPROCESSING_QC_REPORT.md").is_file()


def test_unchanged_package_is_reused():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main_pdf = root / "main.pdf"
        si_pdf = root / "support.pdf"
        _make_pdf(main_pdf, "Main source text.")
        _make_pdf(si_pdf, "SI source text.")
        records = pd.DataFrame([{
            "doi": "10.0000/reuse",
            "title": "Reuse paper",
            "year": "2024",
            "local_pdf_path": str(main_pdf),
            "local_si_info": str(si_pdf),
        }])
        first = preprocess_paired_corpus(records, working_dir=root, preprocessed_dir="processed")
        second = preprocess_paired_corpus(records, working_dir=root, preprocessed_dir="processed")
        assert first.iloc[0]["preprocessing_status"] == "COMPLETE"
        assert second.iloc[0]["preprocessing_action"] == "REUSED_UNCHANGED"
