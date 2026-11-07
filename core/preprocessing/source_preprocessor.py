"""Deterministic source preprocessing. No LLM is called by this module."""
from __future__ import annotations

import csv
import hashlib
import json
import mimetypes
import os
import re
import shutil
import urllib.error
import urllib.request
import uuid
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pymupdf

try:
    from docx import Document as open_docx
except ImportError:
    open_docx = None

TEI_NS = {"tei": "http://www.tei-c.org/ns/1.0"}
XML_ID = "{http://www.w3.org/XML/1998/namespace}id"
CAPTION_RE = re.compile(r"^\s*((?:Figure|Fig\.?|Table|Scheme)\s+[A-Z]?\d+[A-Za-z]?)\b", re.I)
SI_REF_RE = re.compile(r"\b(Fig(?:ure)?\.?\s*S\d+|Table\s*S\d+|Scheme\s*S\d+|Supplementary\s*(?:Figure|Table|Note|Information))\b", re.I)
SI_LABEL_RE = re.compile(r"\b(?:Figure|Fig\.?|Table|Scheme)\s+S\d+[A-Za-z]?\b", re.I)


class SourcePreprocessingError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe(value: str) -> str:
    return (re.sub(r"[^A-Za-z0-9._-]+", "_", Path(value).stem).strip("._") or "source")[:80]


def _node_text(node: Optional[ET.Element]) -> str:
    return " ".join("".join(node.itertext()).split()) if node is not None else ""


def _parse_coords(value: Optional[str]) -> List[Dict[str, Any]]:
    result = []
    for part in (value or "").split(";"):
        fields = [x.strip() for x in part.split(",")]
        if len(fields) != 5:
            continue
        try:
            page, x, y, w, h = fields
            item = {"page": int(float(page)), "x": float(x), "y": float(y), "w": float(w), "h": float(h)}
            if item["page"] > 0 and item["w"] > 0 and item["h"] > 0:
                result.append(item)
        except ValueError:
            continue
    return result


class SourcePreprocessor:
    """Create 01_preprocessing from MAIN PDF and optional PDF/DOCX SI files."""

    def __init__(
        self, grobid_url: Optional[str] = None, use_grobid: bool = True,
        grobid_timeout: int = 180, figure_dpi: int = 200, table_dpi: int = 200,
    ):
        self.grobid_url = (grobid_url or os.getenv("CHEM_LITERACY_GROBID_URL") or "http://localhost:8070").rstrip("/")
        self.use_grobid = use_grobid
        self.grobid_timeout = grobid_timeout
        self.figure_dpi = figure_dpi
        self.table_dpi = table_dpi
        if figure_dpi < 72 or table_dpi < 72:
            raise ValueError("DPI must be >= 72.")
        self.out: Optional[Path] = None
        self.assets: List[Dict[str, Any]] = []
        self.warnings: List[str] = []

    def process(
        self, main_pdf: str | Path, supplementary_files: Optional[Sequence[str | Path]],
        output_dir: str | Path, paper_id: str, doi: str = "", title: str = "",
        publication_year: Optional[int] = None,
    ) -> Dict[str, Any]:
        main = Path(main_pdf).expanduser().resolve()
        si_paths = [Path(p).expanduser().resolve() for p in (supplementary_files or [])]
        if not main.is_file():
            raise SourcePreprocessingError(f"MAIN PDF not found: {main}")
        missing = [str(p) for p in si_paths if not p.is_file()]
        if missing:
            raise SourcePreprocessingError("SI file(s) not found: " + ", ".join(missing))

        self.out = Path(output_dir).expanduser().resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.assets, self.warnings = [], []
        page_text_dir = self.out / "page_text"
        figures_dir = self.out / "extracted_figures"
        main_fig_dir = figures_dir / "MAIN"
        tables_dir = self.out / "extracted_tables"
        table_images_dir = tables_dir / "table_images"
        for directory in (page_text_dir, main_fig_dir, figures_dir / "SI", tables_dir, table_images_dir):
            directory.mkdir(parents=True, exist_ok=True)

        main_pages, main_tables, fallback_figures, main_refs, main_text, main_scanned = self._read_pdf(
            main, "MAIN", page_text_dir
        )
        grobid_status = "DISABLED"
        grobid_figures, grobid_tables = [], []
        if self.use_grobid:
            try:
                tei = self._call_grobid(main)
                grobid_figures, grobid_tables = self._parse_tei_media(tei)
                grobid_status = "SUCCESS"
            except Exception as exc:
                grobid_status = "UNAVAILABLE_FALLBACK"
                self.warnings.append(f"GROBID unavailable; PyMuPDF caption/layout fallback used: {exc}")
        figures = self._merge_figures(grobid_figures, fallback_figures)
        tables = self._merge_tables(main_tables, grobid_tables)
        self._crop_main(main, figures, tables, main_fig_dir, table_images_dir)

        si_pages, si_tables, si_figures, si_refs = [], [], [], []
        si_texts: List[str] = []
        si_summaries = []
        si_pdf_inventory = None
        si_scanned = 0
        for si in si_paths:
            stem = _safe(si.name)
            si_root = self.out / "supplementary" / stem
            si_root.mkdir(parents=True, exist_ok=True)
            sha = _sha256(si)
            si_meta = {"path": str(si), "filename": si.name, "sha256": sha,
                       "size_bytes": si.stat().st_size, "format": si.suffix.lower().lstrip(".").upper()}
            if si.suffix.lower() == ".pdf":
                p, t, f, r, texts, scanned = self._read_pdf(si, "SI", page_text_dir, stem)
                si_pages.extend(p)
                si_tables.extend(t)
                si_figures.extend(f)
                si_refs.extend(r)
                si_texts.extend(texts)
                si_scanned += scanned
                si_meta.update(self._extract_si_pdf(si, stem, si_root))
                si_meta["processing_status"] = "PARSED"
                if si_pdf_inventory is None:
                    si_pdf_inventory = {"path": str(si), "sha256": sha, "size_bytes": si.stat().st_size, "page_count": len(p)}
                (si_root / "si_cleaned.md").write_text(
                    f"# Supporting Information: {si.name}\n\n" + "\n".join(texts), encoding="utf-8"
                )
            elif si.suffix.lower() == ".docx":
                text, docx_tables, metadata = self._extract_si_docx(si, stem, si_root)
                si_tables.extend(docx_tables)
                si_texts.append(text)
                si_meta.update(metadata)
                si_meta["processing_status"] = "PARSED"
            else:
                target_dir = si_root / "original"
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / si.name
                shutil.copy2(si, target)
                self._asset("SI", "source_file", si, target, "source_file_copy")
                si_meta["preserved_path"] = target.relative_to(self.out).as_posix()
                si_meta["processing_status"] = "PRESERVED_NOT_PARSED"
                self.warnings.append(
                    f"Unsupported SI format '{si.suffix or 'unknown'}' preserved but not parsed: {si.name}"
                )
            si_summaries.append(si_meta)

        all_tables = tables + si_tables
        all_figures = figures + si_figures
        table_index = self._write_tables(all_tables, tables_dir)
        (tables_dir / "tables_summary.json").write_text(
            json.dumps(table_index, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (self.out / "main_analysis.md").write_text(
            self._pages_markdown("Main Analysis", title, doi, main_text), encoding="utf-8"
        )
        (self.out / "si_analysis.md").write_text(
            self._pages_markdown("Supporting Information Analysis", title, doi, si_texts), encoding="utf-8"
        )
        figure_index = [
            {"figure_id": f.get("figure_id"), "doc_type": f.get("doc_type"),
             "page_number": f.get("page_number"), "bbox": f.get("bbox"), "caption": f.get("caption")}
            for f in all_figures
        ]
        (figures_dir / "figures_summary.json").write_text(
            json.dumps(figure_index, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (figures_dir / "figures_summary.md").write_text(
            "# Figures and Captions\n\n" + "\n".join(
                f"### {x['figure_id']} (Page {x['page_number']}, {x['doc_type']})\n> {x.get('caption') or ''}\n"
                for x in figure_index
            ), encoding="utf-8"
        )
        (figures_dir / "media_assets.json").write_text(
            json.dumps(self.assets, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        if not title:
            title = self._infer_title(main_text)
        if not doi:
            doi = self._infer_doi(main_text)
        has_unsupported_si = any(x.get("processing_status") == "PRESERVED_NOT_PARSED" for x in si_summaries)
        status = (
            "WARNING_SCANNED_PAGES" if main_scanned + si_scanned else
            "PARTIAL_SI_UNSUPPORTED" if has_unsupported_si else
            "COMPLETE" if si_paths else
            "PARTIAL_NO_SI"
        )
        package = {
            "package_version": "1.1.0",
            "paper_id": paper_id,
            "metadata": {"doi": doi, "title": title, "has_si": bool(si_paths), "publication_year": publication_year},
            "inventory": {
                "main_pdf": {"path": str(main), "sha256": _sha256(main), "size_bytes": main.stat().st_size, "page_count": len(main_pages)},
                "si_pdf": si_pdf_inventory,
                "supplementary_files": si_summaries,
            },
            "pages": main_pages + si_pages,
            "tables": all_tables,
            "figures": all_figures,
            "si_references": main_refs + si_refs,
            "media_assets": self.assets,
            "processing_summary": {
                "total_main_pages": len(main_pages), "total_si_pages": len(si_pages),
                "total_tables_detected": len(all_tables), "total_figures_detected": len(all_figures),
                "total_si_references": len(main_refs) + len(si_refs),
                "scanned_pages_detected": main_scanned + si_scanned,
                "images_extracted": len(self.assets), "grobid_status": grobid_status,
                "warnings": self.warnings, "status": status,
            },
        }
        schema_path = Path(__file__).resolve().parents[2] / "schemas" / "source_package.schema.json"
        if not schema_path.exists():
            raise SourcePreprocessingError(f"Schema not found: {schema_path}")
        import jsonschema
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        errors = sorted(jsonschema.Draft7Validator(schema).iter_errors(package), key=lambda e: list(e.path))
        if errors:
            detail = "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:8])
            raise SourcePreprocessingError(f"Source package schema validation failed: {detail}")
        (self.out / "evidence_index.json").write_text(
            json.dumps(package, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self._write_qc(package, main, si_paths, grobid_status)
        return package

    def _call_grobid(self, pdf: Path) -> str:
        endpoint = self.grobid_url if self.grobid_url.endswith("/api/processFulltextDocument") else self.grobid_url + "/api/processFulltextDocument"
        boundary = "----ChemLiteracy" + uuid.uuid4().hex
        chunks = []
        data = pdf.read_bytes()
        chunks.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"input\"; filename=\"{pdf.name}\"\r\nContent-Type: application/pdf\r\n\r\n").encode() + data + b"\r\n")
        for value in ("figure", "table"):
            chunks.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"teiCoordinates\"\r\n\r\n{value}\r\n").encode())
        chunks.append(f"--{boundary}--\r\n".encode())
        request = urllib.request.Request(
            endpoint, data=b"".join(chunks),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.grobid_timeout) as response:
                result = response.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SourcePreprocessingError(f"{endpoint}: {exc}") from exc
        if "<TEI" not in result and ":TEI" not in result:
            raise SourcePreprocessingError("GROBID response did not contain TEI XML.")
        return result

    def _parse_tei_media(self, tei_xml: str):
        root = ET.fromstring(tei_xml)
        figures, tables = [], []
        index = 0
        for node in root.iter():
            if node.tag.split("}")[-1] != "figure":
                continue
            index += 1
            figure_id = node.get(XML_ID) or f"GROBID_FIG_{index:03d}"
            label = _node_text(node.find("tei:head", TEI_NS))
            desc = _node_text(node.find("tei:figDesc", TEI_NS))
            caption = ": ".join(x for x in (label, desc) if x)
            coords = _parse_coords(node.get("coords"))
            table_node = node.find(".//tei:table", TEI_NS)
            is_table = node.get("type", "").lower() == "table" or table_node is not None
            rows = []
            if table_node is not None:
                for row in table_node.findall(".//tei:row", TEI_NS):
                    rows.append([_node_text(cell) for cell in row.findall("./tei:cell", TEI_NS)])
            for coord in coords:
                bbox = [coord["x"], coord["y"], coord["x"] + coord["w"], coord["y"] + coord["h"]]
                if is_table:
                    tables.append({
                        "table_id": figure_id, "doc_type": "MAIN", "page_number": coord["page"],
                        "bbox": bbox, "rows": rows, "row_count": len(rows),
                        "col_count": max((len(x) for x in rows), default=0),
                        "caption": caption or None, "label": label, "coords": coords,
                    })
                else:
                    figures.append({
                        "figure_id": figure_id, "doc_type": "MAIN", "page_number": coord["page"],
                        "bbox": bbox, "caption": caption or None, "label": label, "coords": coords,
                    })
        collapsed = {}
        for item in figures:
            old = collapsed.get(item["figure_id"])
            if old is None or self._area(item["bbox"]) > self._area(old["bbox"]):
                collapsed[item["figure_id"]] = item
        return list(collapsed.values()), tables

    @staticmethod
    def _area(bbox):
        return max(0.0, bbox[2]-bbox[0]) * max(0.0, bbox[3]-bbox[1])

    @staticmethod
    def _merge_figures(grobid, fallback):
        if not grobid:
            return fallback
        result = list(grobid)
        known = {re.sub(r"\W+", "", str(x.get("caption") or "").lower()) for x in result}
        for item in fallback:
            key = re.sub(r"\W+", "", str(item.get("caption") or "").lower())
            if key and key not in known:
                result.append(item)
        return sorted(result, key=lambda x: (x.get("page_number") or 0, (x.get("bbox") or [0, 0])[1]))

    def _merge_tables(self, native, tei):
        result = list(native)
        for item in tei:
            bbox = item.get("bbox")
            overlap = False
            for other in native:
                if other.get("page_number") != item.get("page_number") or not bbox:
                    continue
                b = other["bbox"]
                ix = max(0, min(bbox[2], b[2])-max(bbox[0], b[0]))
                iy = max(0, min(bbox[3], b[3])-max(bbox[1], b[1]))
                if ix*iy > .25*min(self._area(bbox), self._area(b)):
                    overlap = True
                    break
            if not overlap:
                result.append(item)
        return result

    def _read_pdf(self, pdf, doc_type, page_text_dir, stem=""):
        pages, tables, figures, refs, texts = [], [], [], [], []
        scanned = 0
        doc = pymupdf.open(str(pdf))
        try:
            for idx, page in enumerate(doc):
                page_num = idx+1
                blocks, pieces, char_count = [], [], 0
                for block in page.get_text("blocks"):
                    value = block[4].strip() if len(block)>4 and isinstance(block[4], str) else ""
                    bbox = [round(float(x), 2) for x in block[:4]]
                    blocks.append({"block_index": int(block[5]), "bbox": bbox, "text": value,
                                   "type": "text" if len(block)<7 or block[6]==0 else "image"})
                    if value:
                        pieces.append(value); char_count += len(value)
                        refs.extend({"ref_text": m.group(0), "doc_type": doc_type, "page_number": page_num, "bbox": bbox}
                                    for m in SI_REF_RE.finditer(value))
                        cap = CAPTION_RE.match(value)
                        if cap and cap.group(1).lower().startswith(("figure", "fig", "scheme")):
                            figures.append({
                                "figure_id": f"{doc_type}_FIG_P{page_num}_{len(figures)+1:02d}",
                                "doc_type": doc_type, "page_number": page_num, "bbox": bbox,
                                "caption": value, "label": cap.group(1),
                                "coords": [],  # Caption bbox is not a figure/image bbox.
                            })
                page_text = "\n\n".join(pieces)
                texts.append(f"## Page {page_num} ({doc_type})\n\n{page_text}\n\n---\n")
                scanned += int(char_count < 50)
                prefix = f"{doc_type.lower()}_{_safe(stem)}_" if stem else f"{doc_type.lower()}_"
                (page_text_dir / f"{prefix}page_{page_num:03d}.txt").write_text(page_text, encoding="utf-8")
                pages.append({
                    "doc_type": doc_type, "page_index": idx, "page_number": page_num,
                    "width": round(page.rect.width, 2), "height": round(page.rect.height, 2),
                    "text_block_count": len(blocks), "char_count": char_count,
                    "is_scanned": char_count < 50, "blocks": blocks,
                })
                try:
                    finder = page.find_tables()
                    found_tables = getattr(finder, "tables", finder)
                    for n, table in enumerate(found_tables, 1):
                        rows = [[None if cell is None else str(cell).strip() for cell in row]
                                for row in (table.extract() or [])]
                        tid = f"{doc_type}_TAB_P{page_num}_{n:02d}"
                        tables.append({
                            "table_id": tid, "doc_type": doc_type, "page_number": page_num,
                            "bbox": [round(float(x), 2) for x in table.bbox], "rows": rows,
                            "row_count": len(rows), "col_count": max((len(x) for x in rows), default=0),
                            "caption": None,
                        })
                except Exception as exc:
                    self.warnings.append(f"TableFinder failed on {pdf.name}, page {page_num}: {exc}")
        finally:
            doc.close()
        return pages, tables, figures, refs, texts, scanned

    def _crop_main(self, pdf, figures, tables, figure_dir, table_image_dir):
        doc = pymupdf.open(str(pdf))
        try:
            by_page = {}
            for figure in figures:
                by_page.setdefault(int(figure.get("page_number") or 0), []).append(figure)
            for page_num, entries in by_page.items():
                if not 1 <= page_num <= len(doc):
                    self.warnings.append(f"Figure coordinate page out of range: {page_num}")
                    continue
                page = doc[page_num-1]
                previous_caption_bottom = 24.0
                for idx, figure in enumerate(sorted(entries, key=lambda x: (x.get("bbox") or [0,0])[1]), 1):
                    bbox = self._largest_bbox(figure.get("coords") or [])
                    caption_bbox = figure.get("bbox") or bbox
                    if not bbox or self._area(bbox)<5000 or bbox[3]-bbox[1]<25:
                        bbox = self._infer_figure_bbox(page, caption_bbox, previous_caption_bottom)
                        method = "pymupdf_vector_image_layout_fallback"
                    else:
                        method = "grobid_tei_coordinate_crop"
                    if caption_bbox:
                        previous_caption_bottom = max(previous_caption_bottom, caption_bbox[3])
                    if not bbox:
                        self.warnings.append(f"No crop region found for {figure.get('figure_id')}")
                        continue
                    bbox = self._clamp(page, bbox, 3)
                    fig_id = figure.get("figure_id") or f"FIG_{idx}"
                    target = figure_dir / f"MAIN_{_safe(str(fig_id))}.png"
                    self._render(page, bbox, target, self.figure_dpi)
                    self._asset("MAIN", "figure", pdf, target, method, page_num, bbox,
                                figure_id=fig_id, caption=figure.get("caption"))
            for table in tables:
                page_num, bbox = table.get("page_number"), table.get("bbox")
                if not page_num or not bbox or not 1 <= int(page_num) <= len(doc):
                    continue
                page = doc[int(page_num)-1]
                bbox = self._clamp(page, bbox, 2)
                if not bbox or bbox[2]-bbox[0]<20 or bbox[3]-bbox[1]<20:
                    continue
                target = table_image_dir / f"{_safe(table['table_id'])}.png"
                self._render(page, bbox, target, self.table_dpi)
                self._asset("MAIN", "table_image", pdf, target, "pymupdf_table_bbox_crop",
                            int(page_num), bbox, table_id=table["table_id"])
        finally:
            doc.close()

    @staticmethod
    def _largest_bbox(coords):
        if not coords:
            return None
        item = max(coords, key=lambda x: x.get("w", 0)*x.get("h", 0))
        if not all(k in item for k in ("x","y","w","h")):
            return None
        return [item["x"], item["y"], item["x"]+item["w"], item["y"]+item["h"]]

    def _infer_figure_bbox(self, page, caption_bbox, previous_caption_bottom):
        if not caption_bbox:
            return None
        caption_top = float(caption_bbox[1])
        lower = max(12.0, min(previous_caption_bottom, caption_top-30.0))
        candidates = []
        try:
            for drawing in page.get_drawings():
                rect = pymupdf.Rect(drawing.get("rect", (0,0,0,0)))
                if rect.width>=24 and rect.height>=12 and rect.width*rect.height>=500 and lower-2<=rect.y0 and rect.y1<=caption_top+3:
                    candidates.append(rect)
        except Exception:
            pass
        try:
            for image in page.get_images(full=True):
                for rect in page.get_image_rects(image[0]):
                    rect = pymupdf.Rect(rect)
                    if rect.width>15 and rect.height>15 and lower-2<=rect.y0 and rect.y1<=caption_top+3:
                        candidates.append(rect)
        except Exception:
            pass
        if candidates:
            box = candidates[0]
            for rect in candidates[1:]:
                box |= rect
            result = self._clamp(page, [box.x0, box.y0, box.x1, min(box.y1, caption_top-2)], 3)
            if result and result[2]-result[0]>30 and result[3]-result[1]>30:
                return result
        return self._clamp(page, [page.rect.width*.05, max(lower, caption_top-page.rect.height*.55),
                                  page.rect.width*.95, caption_top-3], 0)

    @staticmethod
    def _clamp(page, bbox, margin=0):
        rect = page.rect
        x0,y0,x1,y1 = map(float, bbox)
        result = [max(rect.x0,x0-margin), max(rect.y0,y0-margin),
                  min(rect.x1,x1+margin), min(rect.y1,y1+margin)]
        return [round(x,2) for x in result] if result[2]>result[0] and result[3]>result[1] else None

    @staticmethod
    def _render(page, bbox, target, dpi):
        clip = pymupdf.Rect(*bbox)
        if clip.width<=0 or clip.height<=0:
            raise SourcePreprocessingError(f"Invalid crop: {bbox}")
        page.get_pixmap(matrix=pymupdf.Matrix(dpi/72,dpi/72), clip=clip, alpha=False).save(str(target))

    def _extract_si_pdf(self, pdf, stem, si_root):
        fig_dir = si_root / "figures"
        embedded_dir = fig_dir / "embedded_images"
        composite_dir = fig_dir / "composite_figures"
        embedded_dir.mkdir(parents=True, exist_ok=True)
        composite_dir.mkdir(parents=True, exist_ok=True)
        doc = pymupdf.open(str(pdf))
        images, composites, hashes = [], [], set()
        tiny = 0
        try:
            for idx, page in enumerate(doc):
                page_num = idx+1
                seen = set()
                for image in page.get_images(full=True):
                    xref = int(image[0])
                    if xref in seen:
                        continue
                    seen.add(xref)
                    try:
                        item = doc.extract_image(xref)
                        data = item.get("image", b"")
                        if not data:
                            continue
                        ext = item.get("ext", "bin")
                        name = f"{stem}_P{page_num:03d}_IMG{len(images)+1:03d}.{ext}"
                        target = embedded_dir / name
                        target.write_bytes(data)
                        width, height = int(item.get("width") or 0), int(item.get("height") or 0)
                        rects = page.get_image_rects(xref)
                        bbox = [round(float(x),2) for x in rects[0]] if rects else None
                        digest = _sha256_bytes(data)
                        hashes.add(digest)
                        is_tiny = width<50 or height<50
                        tiny += int(is_tiny)
                        self._asset("SI", "embedded_image", pdf, target, "pymupdf_extract_image",
                                    page_num, bbox, width=width, height=height, tiny=is_tiny)
                        images.append({"page":page_num,"filename":name,"width":width,"height":height,
                                       "sha256":digest,"bbox":bbox,"is_tiny_icon":is_tiny})
                    except Exception as exc:
                        self.warnings.append(f"SI image xref {xref} failed in {pdf.name} page {page_num}: {exc}")
                labels = list(dict.fromkeys(m.group(0) for m in SI_LABEL_RE.finditer(page.get_text())))
                if labels:
                    name = f"{stem}_P{page_num:03d}_COMPOSITE.png"
                    target = composite_dir / name
                    pix = page.get_pixmap(matrix=pymupdf.Matrix(self.figure_dpi/72,self.figure_dpi/72), alpha=False)
                    pix.save(str(target))
                    asset_type = "composite_figure" if any("table" not in x.lower() for x in labels) else "page_render"
                    self._asset("SI", asset_type, pdf, target, "pymupdf_full_page_render",
                                page_num, [0,0,round(page.rect.width,2),round(page.rect.height,2)],
                                caption="Labels: " + "; ".join(labels), width=pix.width, height=pix.height)
                    composites.append({"page":page_num,"filename":name,"labels":labels,
                                        "dpi":self.figure_dpi,"width":pix.width,"height":pix.height})
        finally:
            doc.close()
        return {"format":"PDF","image_count":len(images),"unique_image_assets":len(hashes),
                "duplicate_placement_instances":max(0,len(images)-len(hashes)),
                "tiny_icons_or_decorations_count":tiny,"composite_figures_count":len(composites),
                "embedded_images":images,"composite_figures":composites}

    def _extract_si_docx(self, path, stem, si_root):
        if open_docx is None:
            raise SourcePreprocessingError("python-docx is required for SI DOCX.")
        document = open_docx(str(path))
        lines = [f"# Supplementary Information: {path.name}", f"SHA256: {_sha256(path)}", ""]
        for paragraph in document.paragraphs:
            value = paragraph.text.strip()
            if value:
                style = paragraph.style.name.lower() if paragraph.style else ""
                level = 2 if ("heading 1" in style or "title" in style) else (3 if "heading 2" in style else (4 if "heading 3" in style else 0))
                lines.append("#"*level + " " + value if level else value)
        tables = []
        table_dir = si_root / "tables"
        table_dir.mkdir(parents=True, exist_ok=True)
        for idx, table in enumerate(document.tables, 1):
            rows = [[cell.text.strip().replace("\n"," ") for cell in row.cells] for row in table.rows]
            tid = f"SI_{stem}_TABLE_S{idx:03d}"
            record = {"table_id":tid,"doc_type":"SI","page_number":1,"bbox":[0,0,0,0],
                      "rows":rows,"row_count":len(rows),"col_count":max((len(x) for x in rows),default=0),
                      "caption":f"Table S{idx}"}
            tables.append(record)
            self._write_table(record, table_dir/f"{tid}.md", table_dir/f"{tid}.csv")
        media = []
        media_dir = si_root / "figures" / "embedded_images"
        media_dir.mkdir(parents=True, exist_ok=True)
        equations = []
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            for part in (x for x in names if x.startswith("word/media/") and not x.endswith("/")):
                data = archive.read(part)
                target = media_dir / Path(part).name
                if target.exists():
                    target = media_dir / f"{target.stem}_{len(media)+1}{target.suffix}"
                target.write_bytes(data)
                width = height = None
                try:
                    from PIL import Image
                    with Image.open(target) as im:
                        width,height = im.size
                except Exception:
                    pass
                self._asset("SI","embedded_image",path,target,"ooxml_word_media_extraction",
                            width=width,height=height)
                media.append({"filename":target.name,"original_part":part,"size_bytes":len(data),
                              "sha256":_sha256_bytes(data),"width":width,"height":height})
            if "word/document.xml" in names:
                root = ET.fromstring(archive.read("word/document.xml"))
                for node in root.iter():
                    if node.tag.endswith("}oMath"):
                        value = " ".join("".join(node.itertext()).split())
                        if value:
                            equations.append(value)
        text = "\n".join(lines) + "\n"
        (si_root/"si_cleaned.md").write_text(text,encoding="utf-8")
        meta = {"format":"DOCX","paragraph_count":len(document.paragraphs),"table_count":len(tables),
                "media_count":len(media),"equations_count":len(equations),"media":media,"equations":equations[:20]}
        (si_root/"si_metadata.json").write_text(json.dumps(meta,indent=2,ensure_ascii=False),encoding="utf-8")
        return text,tables,meta

    def _write_tables(self, tables, directory):
        index = []
        for table in tables:
            tid = _safe(table["table_id"])
            md_path, csv_path = directory/f"{tid}.md", directory/f"{tid}.csv"
            self._write_table(table, md_path, csv_path)
            table["md_file"] = md_path.relative_to(self.out).as_posix()
            table["csv_file"] = csv_path.relative_to(self.out).as_posix()
            index.append({"table_id":table["table_id"],"doc_type":table["doc_type"],
                          "page_number":table.get("page_number"),"row_count":table["row_count"],
                          "col_count":table["col_count"],"md_file":table["md_file"],"csv_file":table["csv_file"]})
        return index

    @staticmethod
    def _write_table(table, md_path, csv_path):
        rows = table.get("rows") or []
        with md_path.open("w",encoding="utf-8") as handle:
            handle.write(f"### {table.get('label') or table['table_id']}\n\n{table.get('caption') or ''}\n\n")
            if rows:
                header = [str(x or "").replace("\n"," ").strip() for x in rows[0]]
                handle.write("| "+" | ".join(header)+" |\n")
                handle.write("| "+" | ".join("---" for _ in header)+" |\n")
                for row in rows[1:]:
                    cells = ([str(x or "").replace("\n"," ").strip() for x in row]+[""]*len(header))[:len(header)]
                    handle.write("| "+" | ".join(cells)+" |\n")
            else:
                handle.write("No native table cells were extracted. See table image when available.\n")
        with csv_path.open("w",encoding="utf-8-sig",newline="") as handle:
            csv.writer(handle).writerows(rows)

    def _asset(self, doc_type, asset_type, source, target, method, page=None, bbox=None,
               figure_id=None, table_id=None, caption=None, width=None, height=None, tiny=False):
        source,target = Path(source),Path(target)
        self.assets.append({
            "asset_id":f"ASSET_{len(self.assets)+1:05d}","doc_type":doc_type,"asset_type":asset_type,
            "source_file":source.name,"source_sha256":_sha256(source),
            "relative_path":target.relative_to(self.out).as_posix(),"sha256":_sha256(target),
            "extraction_method":method,"page_number":page,
            "bbox":[round(float(x),2) for x in bbox] if bbox else None,
            "figure_id":figure_id,"table_id":table_id,"caption":caption,
            "width":width,"height":height,"mime_type":mimetypes.guess_type(target.name)[0],
            "is_tiny_icon":bool(tiny),
        })

    @staticmethod
    def _pages_markdown(heading,title,doi,pages):
        return f"# {heading}: {title or ''}\nDOI: {doi or ''}\n\n" + "\n".join(pages) + "\n"

    @staticmethod
    def _infer_title(pages):
        for line in (pages[0].splitlines() if pages else []):
            if len(line.strip())>15 and not line.strip().lower().startswith("doi"):
                return line.strip()[:500]
        return ""

    @staticmethod
    def _infer_doi(pages):
        match = re.search(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+","\n".join(pages))
        return match.group(0).rstrip(".,)") if match else ""

    def _write_qc(self, package, main, si_paths, grobid_status):
        summary = package["processing_summary"]
        lines = [
            "# Source Preprocessing QC Report", "",
            f"- Paper ID: {package['paper_id']}", f"- MAIN PDF: {main.name}",
            f"- MAIN SHA256: {package['inventory']['main_pdf']['sha256']}",
            f"- SI file count: {len(si_paths)}", f"- GROBID: {grobid_status}",
            f"- Status: {summary['status']}",
            f"- Pages: MAIN {summary['total_main_pages']}; SI {summary['total_si_pages']}",
            f"- Tables: {summary['total_tables_detected']}; Figures/captions: {summary['total_figures_detected']}",
            f"- Media assets: {len(self.assets)}",
            f"- Scanned pages flagged (OCR not performed): {summary['scanned_pages_detected']}",
            "", "## Warnings",
        ]
        lines.extend([f"- {item}" for item in self.warnings] or ["- None"])
        lines.extend([
            "", "## Processing scope",
            "- Text, table and image preprocessing is deterministic; no LLM/API model calls are made.",
            "- GROBID is optional and supplies structural TEI coordinates only.",
            "- MAIN figures use TEI bounding boxes or PyMuPDF layout fallback.",
            "- SI PDFs preserve embedded image streams and render pages containing Figure S/Table S labels.",
            "- SI DOCX files preserve OOXML media and extract text, tables and equation text.",
            "- Scanned pages are flagged; OCR is not performed.",
        ])
        (self.out/"PREPROCESSING_QC_REPORT.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
