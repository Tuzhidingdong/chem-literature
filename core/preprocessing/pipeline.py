"""Corpus-level integration for deterministic source preprocessing.

This module runs after Chem-Literature has verified a physical MAIN+SI pair.
It does not perform literature discovery, harvesting, OCR, image interpretation,
or LLM/API calls.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable, List, Optional, Tuple

import pandas as pd

from .source_preprocessor import SourcePreprocessor


SUPPORTED_DOCUMENTS = {".pdf", ".docx"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_name(value: str) -> str:
    return (re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "paper")[:80]


def _resolve_input(value: Any, working_dir: Path) -> Path:
    path = Path(str(value).strip()).expanduser()
    return path.resolve() if path.is_absolute() else (working_dir / path).resolve()


def _output_path(value: str | Path, working_dir: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (working_dir / path).resolve()


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _extract_supported_zip_members(
    archive_path: Path,
    destination: Path,
    *,
    max_member_bytes: int,
    max_total_bytes: int,
    max_members: int = 250,
) -> Tuple[List[Path], List[str]]:
    """Extract only PDF/DOCX members, rejecting unsafe paths and oversized archives."""
    extracted: List[Path] = []
    unsupported: List[str] = []
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            if len(infos) > max_members:
                unsupported.append(
                    f"[archive member limit exceeded: {len(infos)} members, limit {max_members}]"
                )
                infos = infos[:max_members]
            total_declared = sum(max(0, info.file_size) for info in infos)
            total_budget_exceeded = total_declared > max_total_bytes
            total_written = 0
            for index, info in enumerate(infos, start=1):
                raw_name = info.filename.replace("\\", "/")
                member = PurePosixPath(raw_name)
                suffix = Path(member.name).suffix.lower()
                if member.is_absolute() or ".." in member.parts or (member.parts and ":" in member.parts[0]):
                    unsupported.append(f"{info.filename} [unsafe archive path]")
                    continue
                if info.flag_bits & 0x1:
                    unsupported.append(f"{info.filename} [encrypted]")
                    continue
                if suffix not in SUPPORTED_DOCUMENTS:
                    unsupported.append(info.filename)
                    continue
                if info.file_size > max_member_bytes:
                    unsupported.append(f"{info.filename} [member size limit exceeded]")
                    continue
                if total_budget_exceeded or total_written + info.file_size > max_total_bytes:
                    unsupported.append(f"{info.filename} [archive extraction size limit exceeded]")
                    continue
                safe_filename = _safe_name(Path(member.name).name)
                target = destination / f"{index:04d}_{safe_filename}"
                try:
                    with archive.open(info, "r") as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst, length=1024 * 1024)
                except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    target.unlink(missing_ok=True)
                    unsupported.append(f"{info.filename} [extraction failed: {exc}]")
                    continue
                actual_size = target.stat().st_size
                if actual_size > max_member_bytes or total_written + actual_size > max_total_bytes:
                    target.unlink(missing_ok=True)
                    unsupported.append(f"{info.filename} [actual extraction size limit exceeded]")
                    continue
                total_written += actual_size
                extracted.append(target)
    except (OSError, zipfile.BadZipFile) as exc:
        unsupported.append(f"[invalid or unreadable ZIP: {exc}]")
    return extracted, unsupported


def _make_input_record(path: Path) -> dict:
    return {
        "path": str(path),
        "filename": path.name,
        "format": path.suffix.lower().lstrip(".").upper(),
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _update_package_and_qc(
    package_dir: Path,
    package: dict,
    *,
    original_si_records: List[dict],
    archive_records: List[dict],
    unsupported_members: List[str],
    has_original_si: bool,
) -> dict:
    package.setdefault("inventory", {})
    package["inventory"]["source_supplementary_files"] = original_si_records
    package["inventory"]["source_archives"] = archive_records
    package.setdefault("metadata", {})["has_si"] = bool(has_original_si)

    summary = package["processing_summary"]
    warnings = summary.setdefault("warnings", [])
    if unsupported_members:
        warnings.append(
            "Some SI archive members/formats were preserved or recorded but not parsed: "
            + "; ".join(unsupported_members[:30])
        )
    if summary.get("scanned_pages_detected", 0):
        status = "WARNING_SCANNED_PAGES"
    elif unsupported_members or any(
        item.get("processing_status") == "PRESERVED_NOT_PARSED"
        for item in package["inventory"].get("supplementary_files", [])
    ):
        status = "PARTIAL_SI_UNSUPPORTED"
    elif not has_original_si:
        status = "PARTIAL_NO_SI"
    else:
        status = "COMPLETE"
    summary["status"] = status

    (package_dir / "evidence_index.json").write_text(
        json.dumps(package, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    qc_path = package_dir / "PREPROCESSING_QC_REPORT.md"
    with qc_path.open("a", encoding="utf-8") as handle:
        handle.write("\n## Chem-Literature integration notes\n")
        handle.write(f"- Original SI files: {len(original_si_records)}\n")
        handle.write(f"- Source archives preserved: {len(archive_records)}\n")
        handle.write(f"- Unsupported/unparsed SI members: {len(unsupported_members)}\n")
        if unsupported_members:
            for item in unsupported_members[:30]:
                handle.write(f"- Not parsed: {item}\n")
        handle.write("- Archive extraction is restricted to PDF/DOCX members; other members remain in the original archive.\n")
    return package


def preprocess_paired_corpus(
    paired_records: pd.DataFrame,
    *,
    working_dir: str | Path = ".",
    preprocessed_dir: str | Path = "runs/preprocessed",
    grobid_enabled: bool = False,
    grobid_url: Optional[str] = None,
    grobid_timeout_seconds: int = 30,
    max_zip_member_bytes: int = 100 * 1024 * 1024,
    max_zip_total_bytes: int = 512 * 1024 * 1024,
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> pd.DataFrame:
    """Preprocess paired records independently and return rows with QC/status columns.

    Required columns: doi, local_pdf_path, local_si_info. Paths may be absolute
    or relative to working_dir. One paper failure does not stop the remaining corpus.
    """
    results = paired_records.copy()
    columns = {
        "preprocessing_path": "",
        "preprocessing_status": "PENDING",
        "preprocessing_action": "",
        "preprocessing_error": "",
        "preprocessing_qc_report": "",
        "preprocessing_grobid_status": "",
    }
    for name, default in columns.items():
        if name not in results.columns:
            results[name] = default

    root = Path(working_dir).expanduser().resolve()
    preprocess_root = _output_path(preprocessed_dir, root)

    for index, row in results.iterrows():
        doi = str(row.get("doi", "") or "").strip()
        paper_id = _safe_name(doi or str(row.get("title", "")) or f"paper_{index}")
        main_value = str(row.get("local_pdf_path", "") or "").strip()
        si_value = str(row.get("local_si_info", "") or "").strip()
        package_dir = preprocess_root / paper_id / "01_preprocessing"
        paper_root = package_dir.parent

        try:
            package_display = str(package_dir.relative_to(root)) if package_dir.is_relative_to(root) else str(package_dir)
            qc_display = str((package_dir / "PREPROCESSING_QC_REPORT.md").relative_to(root)) \
                if (package_dir / "PREPROCESSING_QC_REPORT.md").is_relative_to(root) \
                else str(package_dir / "PREPROCESSING_QC_REPORT.md")
            record = {
                "doi": doi,
                "preprocessing_path": package_display,
                "preprocessing_status": "FAILED",
                "preprocessing_action": "FAILED",
                "preprocessing_error": "",
                "preprocessing_qc_report": qc_display,
                "preprocessing_grobid_status": "",
            }

            if not main_value or not si_value:
                raise FileNotFoundError("A paired record must contain both local_pdf_path and local_si_info.")
            main_path = _resolve_input(main_value, root)
            si_path = _resolve_input(si_value, root)
            if not main_path.is_file():
                raise FileNotFoundError(f"MAIN PDF not found: {main_path}")
            if not si_path.is_file():
                raise FileNotFoundError(f"SI source file not found: {si_path}")

            original_si_record = _make_input_record(si_path)
            signature_payload = {
                "main": _make_input_record(main_path),
                "si": original_si_record,
                "grobid_enabled": bool(grobid_enabled),
                "grobid_url": grobid_url or "",
                "grobid_timeout_seconds": int(grobid_timeout_seconds),
            }
            signature = hashlib.sha256(
                json.dumps(signature_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            input_manifest = package_dir / "PREPROCESSING_INPUTS.json"
            previous = _read_json(input_manifest)
            existing_package = _read_json(package_dir / "evidence_index.json")
            if previous and previous.get("signature") == signature and existing_package:
                package = existing_package
                record["preprocessing_action"] = "REUSED_UNCHANGED"
            else:
                if package_dir.exists():
                    shutil.rmtree(package_dir)
                unpack_root = paper_root / "_unpacked_si"
                if unpack_root.exists():
                    shutil.rmtree(unpack_root)
                package_dir.mkdir(parents=True, exist_ok=True)
                unpack_root.mkdir(parents=True, exist_ok=True)

                supplementary_inputs: List[Path] = []
                unsupported_members: List[str] = []
                archive_records: List[dict] = []
                if si_path.suffix.lower() == ".zip":
                    archive_copy_dir = package_dir / "source_archives"
                    archive_copy_dir.mkdir(parents=True, exist_ok=True)
                    archive_copy = archive_copy_dir / _safe_name(si_path.name)
                    shutil.copy2(si_path, archive_copy)
                    archive_records.append({
                        **original_si_record,
                        "relative_path": archive_copy.relative_to(package_dir).as_posix(),
                    })
                    extracted, unsupported_members = _extract_supported_zip_members(
                        si_path,
                        unpack_root,
                        max_member_bytes=max_zip_member_bytes,
                        max_total_bytes=max_zip_total_bytes,
                    )
                    supplementary_inputs.extend(extracted)
                    if not extracted and not unsupported_members:
                        unsupported_members.append("[ZIP contained no supported PDF/DOCX SI documents]")
                else:
                    supplementary_inputs.append(si_path)

                year_raw = str(row.get("year", "") or "").strip()
                publication_year = int(year_raw) if year_raw.isdigit() else None
                processor = SourcePreprocessor(
                    grobid_url=grobid_url,
                    use_grobid=bool(grobid_enabled),
                    grobid_timeout=int(grobid_timeout_seconds),
                )
                package = processor.process(
                    main_pdf=main_path,
                    supplementary_files=supplementary_inputs,
                    output_dir=package_dir,
                    paper_id=paper_id,
                    doi=doi,
                    title=str(row.get("title", "") or ""),
                    publication_year=publication_year,
                )
                package = _update_package_and_qc(
                    package_dir,
                    package,
                    original_si_records=[original_si_record],
                    archive_records=archive_records,
                    unsupported_members=unsupported_members,
                    has_original_si=True,
                )
                input_manifest.write_text(
                    json.dumps(
                        {"signature": signature, "inputs": signature_payload},
                        indent=2, ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

            record["preprocessing_status"] = package["processing_summary"]["status"]
            record["preprocessing_grobid_status"] = package["processing_summary"].get("grobid_status", "")
            record["preprocessing_action"] = record["preprocessing_action"] or "PROCESSED"
        except Exception as exc:
            record = {
                "doi": doi,
                "preprocessing_path": str(package_dir),
                "preprocessing_status": "FAILED",
                "preprocessing_action": "FAILED",
                "preprocessing_error": f"{type(exc).__name__}: {exc}",
                "preprocessing_qc_report": str(package_dir / "PREPROCESSING_QC_REPORT.md"),
                "preprocessing_grobid_status": "",
            }

        for key, value in record.items():
            if key in results.columns:
                results.at[index, key] = value
        if progress_callback:
            progress_callback(record)

    return results
