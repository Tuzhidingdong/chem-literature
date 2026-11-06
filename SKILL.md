---
name: chem-literature
description: >-
  Automated scientific literature discovery and full-text/SI paired harvesting
  workflow for chemistry, battery, and materials science. Use this skill whenever
  the user asks to search, harvest, crawl, or download research papers with their
  supporting information (SI), construct a paired literature database, or filter
  original research articles for a given topic and year range.
---

# Chem-Literature Harvesting Skill

This skill provides an automated, zero-token-waste pipeline for discovering, downloading, and pairing full-text PDFs with their Supporting Information (SI) for academic literature in chemistry, materials science, and energy storage.

## When to Use

Use this skill when:
- The user requests discovering, gathering, or crawling research papers on a specific chemistry/materials topic (e.g., "zinc-ion battery", "solid-state electrolyte").
- The user asks to construct a paired research paper database where both the main text PDF and the Supporting Information (SI) are physically required on disk.
- The user wants to filter out review papers, meeting abstracts, and retractions to obtain pure original research articles.
- The user supplies one or more institutional WebVPN accounts (JSON cookies) and requests downloading literature through them.

---

## Architecture and Design Principles

1. **Deterministic Publisher Routing (Zero Token Waste)**:
   - **Elsevier (ScienceDirect)**: Resolves DOI to PII via Crossref -> DOM lazy scroll -> prints vector PDF via CDP `Page.printToPDF` -> extracts MMC supplementary files.
   - **Wiley Online Library**: Directly fetches binary stream via `/doi/pdfdirect/{doi}?download=true` and detects `downloadSupplement` assets.
   - **IOP Publishing / ECS**: Direct endpoint extraction via `/article/{doi}/pdf`, automatically bypassing meeting abstracts.
   - **Magic Byte Validation**: All downloads are checked for `%PDF-`, `PK\x03\x04` (ZIP/DOCX) before saving to disk to prevent saving HTML error pages.

2. **Isolated Multi-Account Sandboxing**:
   - Automatically detects all JSON cookie files in the `cookies/` directory.
   - Launches independent offscreen Edge instances (`--window-position=-32000,-32000`) with dedicated user profiles and debugging ports to avoid colliding or stealing window focus.

---

## Agent Step-by-Step Execution Guide

When executing a literature discovery or harvesting request, follow these steps:

### Step 1: Parse User Requirements
Extract the following parameters from the user's prompt:
- **`topic`**: Natural language topic description (e.g., `"aqueous zinc battery cathode"`).
- **`year_start`**: Starting publication year (e.g., `2021`).
- **`year_end`**: Ending publication year (e.g., `2026`).
- **`max_papers`**: Maximum number of papers to harvest (default: `1000`).

### Step 2: Check Credentials
Check the `cookies/` directory in the current working directory:
- Ensure at least one valid WebVPN session cookie JSON file (e.g., `account1.json`) exists.
- If none exist, warn the user and refer to `resources/account_template.json`.

### Step 3: Run the Workflow Script
Execute the workflow via CLI using Python:

```bash
python scripts/run_workflow.py --topic "<TOPIC>" --year_start <START> --year_end <END>
```

Or if executing from an external workspace:
```bash
python <SKILL_DIR>/scripts/run_workflow.py --topic "<TOPIC>" --year_start <START> --year_end <END>
```

### Step 4: Verify Outputs
Upon execution completion, verify the physical presence of the generated artifacts:
1. `literature_candidates.csv`: Complete metadata table of candidate publications.
2. `papers_pdf/`: Physical full-text PDFs.
3. `papers_si/`: Physical supporting information files.
4. `research_corpus_paired.csv`: Filtered dataset containing **only** original research articles where both PDF and SI are present on disk.

### Step 5: Report to User
Summarize the results cleanly:
- Total candidate papers screened.
- Number of reviews/abstracts excluded.
- Number of paired research papers physically on disk.
- Total storage consumed (in MB or GB).
- Publisher breakdown (Wiley, Elsevier, Springer, RSC, etc.).
