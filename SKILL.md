---
name: chem-literature
description: >-
  Automated scientific literature discovery, full-text/SI paired harvesting,
  deterministic document preprocessing, and source-package generation for chemistry,
  battery, and materials science. Use this skill whenever the user asks to discover,
  download, pair, or preprocess research papers and supporting information.
---

# Chem-Literature Discovery, Harvesting & Preprocessing Skill

This skill runs a complete local workflow:

**Topic discovery → candidate screening → MAIN PDF/SI harvesting → physical pair verification → original-research corpus construction → deterministic preprocessing → downstream-ready source packages.**

The document-preprocessing stage reuses the deterministic implementation extracted from Chem-Literacy. The harvesting stage remains independent of document parsing.

## When to Use

Use this skill when the user requests:
- Literature discovery, gathering, or harvesting for a chemistry/materials topic.
- A research-paper corpus in which MAIN full-text PDFs and Supporting Information (SI) files must both exist physically on disk.
- Filtering reviews, meeting abstracts, editorials, or other non-original-research records.
- Deterministic preprocessing of paired papers into structured text, tables, figure/table assets, and evidence indexes.
- A complete discovery-to-preprocessing workflow for downstream extraction or analysis skills.

## End-to-End Workflow

### Step 1 — Resolve the literature search request
Obtain the topic, keywords, inclusive publication-year range, and maximum candidate count from the user or config/settings.yaml. Do not invent missing bibliographic information.

### Step 2 — Discover and screen candidates
Run:

~~~bash
python run_workflow.py --topic "<TOPIC>" --year_start <START> --year_end <END>
~~~

The discovery stage uses scholarly metadata sources and records DOI, title, year, journal, article type, abstract, and local-file status. Review filtering is applied again when constructing the paired research corpus.

### Step 3 — Harvest source documents
The harvester downloads supported publishers' MAIN PDFs and SI files into:
- papers_pdf/
- papers_si/

WebVPN cookies are user-supplied. Never print, expose, commit, or modify active cookie contents. If no credentials are available, report that authenticated harvesting is unavailable; public/open-access retrieval may still be possible.

### Step 4 — Verify physical pairing and create the baseline corpus
A record enters research_corpus_paired.csv only when:
- The local MAIN PDF exists.
- The local SI source file exists.
- The record passes the configured original-research screening.

A filename or download status alone is not proof of physical presence.

### Step 5 — Preprocess each paired research paper
By default, Stage 5 automatically processes every row in research_corpus_paired.csv. To skip this stage for a harvesting-only run, use --skip-preprocessing.

Each paper is written to:

~~~text
runs/preprocessed/<paper_id>/01_preprocessing/
~~~

The deterministic preprocessor provides:
- MAIN PDF page text, layout blocks, page metadata, and scanned-page flags.
- MAIN figure/table detection and image crops, using GROBID TEI coordinates when explicitly enabled and available, otherwise PyMuPDF layout/caption fallback.
- Native PDF table extraction to CSV and Markdown.
- SI PDF text, embedded-image extraction, and Figure S/Table S page composites.
- SI DOCX paragraph/table/equation text and OOXML media extraction.
- SI ZIP handling that safely extracts only PDF/DOCX members for parsing while preserving the original ZIP in the package. Unsupported, unsafe, encrypted, or oversized members are recorded as unparsed.
- SHA-256 source/asset records, a validated evidence_index.json, and a PREPROCESSING_QC_REPORT.md.

Dependencies for the preprocessing stage are installed through requirements.txt. GROBID is optional and disabled by default. The local PyMuPDF fallback does not make network calls.

### Step 6 — Verify outputs and report
Check the physical existence of:
1. literature_candidates.csv
2. research_corpus_paired.csv
3. Each referenced preprocessing_path
4. evidence_index.json and PREPROCESSING_QC_REPORT.md for each preprocessed paper

Report candidate count, physically harvested MAIN/SI counts, paired original-research count, preprocessing status counts, failures, and storage size. Never count a failed or unsupported SI parse as a successful complete source package.

## Downstream Handoff Contract

The authoritative handoff index is research_corpus_paired.csv. The following fields are added after preprocessing:

| Field | Meaning |
|---|---|
| preprocessing_path | Directory containing the paper's 01_preprocessing package |
| preprocessing_status | Package QC status |
| preprocessing_action | PROCESSED or REUSED_UNCHANGED |
| preprocessing_error | Error details when preprocessing failed |
| preprocessing_qc_report | Path to the human-readable preprocessing QC report |
| preprocessing_grobid_status | DISABLED, SUCCESS, or UNAVAILABLE_FALLBACK |

Downstream skills should read the following package components:

| Need | File/location |
|---|---|
| Primary source inventory, DOI/title, page coordinates, SI references, hashes, asset inventory | evidence_index.json |
| MAIN manuscript text, page by page | main_analysis.md |
| SI text, page by page / extracted document text | si_analysis.md |
| Native extracted tables | extracted_tables/*.csv and *.md |
| MAIN figures and table crops | extracted_figures/MAIN/ |
| SI embedded images and labelled page composites | extracted_figures/SI/ and supplementary/ |
| QC warnings, scanned-page flags, processing scope | PREPROCESSING_QC_REPORT.md |
| Original SI ZIP archive, where applicable | source_archives/ |

Use preprocessing_status before downstream extraction:
- COMPLETE: preprocessing completed for supported inputs.
- WARNING_SCANNED_PAGES: one or more pages appear scanned; OCR was not performed.
- PARTIAL_NO_SI: no SI source was available to preprocess.
- PARTIAL_SI_UNSUPPORTED: some SI inputs/members were preserved but not parsed.
- FAILED: preprocessing failed; inspect preprocessing_error.

Do not infer that an image has been visually interpreted merely because an image file exists. This stage extracts and indexes image assets; visual inspection/interpretation is a separate downstream action requiring the applicable human authorization.

## Configuration and Controls

The preprocessing block in config/settings.yaml controls automatic Stage 5 execution:
- enabled: defaults to true.
- output_dir: defaults to runs/preprocessed.
- grobid_enabled: defaults to false; use PyMuPDF fallback unless GROBID is intentionally configured.
- ZIP extraction limits constrain the number and uncompressed size of supported members.

~~~bash
# Skip preprocessing for this run
python run_workflow.py --skip-preprocessing

# Change preprocessing package destination
python run_workflow.py --preprocessed-dir "E:/Research/preprocessed"
~~~

## Failure and Integrity Rules

- Preprocessing is deterministic and uses no LLM/API model calls.
- No OCR is performed; scanned pages are flagged.
- A per-paper preprocessing failure must not abort processing of the remaining corpus.
- Original PDFs and SI files remain in papers_pdf/ and papers_si/; preprocessing does not overwrite or modify them.
- Existing packages are reused only when the input signature matches and a valid evidence index is present.
- Unsupported SI formats are preserved and reported, not silently treated as parsed text.
- Never fabricate DOI, title, year, file paths, parsed text, table values, or QC status.
