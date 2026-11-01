# Chem-Literature: Automated Scientific Literature Discovery and Full-Text/SI Paired Harvesting Workflow

> **An engineering-grade literature mining pipeline tailored for chemistry and materials science.**  
> Seamlessly orchestrates: Natural Language Topic Formulation $\to$ Scholarly Candidate Screening (with automatic review/abstract filtering) $\to$ Multi-Account WebVPN Parallel Ingestion $\to$ Physical File Integrity Verification $\to$ Dual-Paired Research Corpus Construction.

---

## Key Features

1. **Zero-Token Waste Engine**
   - Eliminates redundant LLM token consumption caused by iterative DOM exploration or trial-and-error URL parsing.
   - Hardcodes battle-tested harvesting routes for major scientific publishers:
     - **Elsevier (ScienceDirect)**: Crossref DOI-to-PII resolution $\to$ Headless DOM pre-rendering $\to$ CSS print stylesheet purge $\to$ Lossless `Page.printToPDF` capture $\to$ MMC supplementary asset harvesting.
     - **Wiley Online Library**: Direct binary retrieval via `/doi/pdfdirect/{doi}?download=true` $\to$ Automated `downloadSupplement` discovery.
     - **IOP Publishing / ECS**: Standard direct endpoint extraction with automatic meeting abstract bypassing.
     - **Cross-Publisher Robustness**: Real-time Magic Byte validation (`%PDF-`, `PK\x03\x04` for ZIP/DOCX) prevents saving HTML error pages as corrupted documents.

2. **Multi-Account WebVPN Dynamic Load Balancing & Auto-Healing**
   - Automatically detects 1 to $N$ institutional WebVPN cookie JSON files within `cookies/`.
   - Provisions isolated browser profiles and dedicated debugging ports per account.
   - Distributes tasks via round-robin scheduling to prevent individual account rate-limiting.
   - Built-in circuit breakers with cooldown periods and automatic session reconnection.

3. **Dual-Paired Research Baseline Corpus**
   - Automatically identifies and filters out review articles, perspectives, editorials, and meeting abstracts during the discovery phase.
   - Verifies physical on-disk presence and file sizes for both the main PDF and the Supporting Information (SI).
   - Generates a certified `research_corpus_paired.csv` baseline dataset and statistical manifest (`research_corpus_paired_manifest.json`) for downstream structural extraction and knowledge graph modeling.

4. **Zero User Desktop Disturbance**
   - Enforces headless browser launch with `--window-position=-32000,-32000` off-screen coordinates, ensuring zero window popups or mouse/keyboard focus disruption.

---

## Repository Structure

```text
chem-literature/
├── config/
│   └── settings.yaml            # Global workflow configuration (query, year range, rate limits)
├── cookies/
│   ├── account_template.json    # Standard JSON cookie format template
│   └── (account01.json, ...)    # User-supplied WebVPN session cookies (ignored by git)
├── core/
│   ├── topic_discoverer.py      # Candidate query and metadata parser via OpenAlex API
│   ├── vpn_session.py           # Multi-account isolated CDP browser session manager
│   └── paper_harvester.py       # Publisher-specific harvesting algorithms
├── run_workflow.py              # Central workflow execution CLI
├── requirements.txt             # Python dependencies
└── README.md                    # Project documentation
```

---

## Quick Start

### 1. Install Dependencies

Python 3.9+ is recommended:

```bash
pip install -r requirements.txt
```

### 2. Configure WebVPN Credentials

Export WebVPN cookies from your browser (e.g., via Cookie-Editor) into JSON format:
1. Navigate to the `cookies/` folder.
2. Save your exported cookie file(s) as `account01.json`, `account02.json`, etc.:
   ```json
   [
     {
       "name": "wengine_vpn_ticket",
       "value": "YOUR_SESSION_TICKET",
       "domain": "webvpn.your-institution.edu",
       "path": "/",
       "secure": true,
       "httpOnly": false
     }
   ]
   ```

### 3. Run the Workflow

Execute the pipeline with a natural language topic and specified year range:

```bash
# Example 1: Run with custom topic and year range
python run_workflow.py --topic "zinc-ion battery electrolyte" --year_start 2021 --year_end 2026

# Example 2: Run with defaults configured in config/settings.yaml
python run_workflow.py
```

### 4. Output Artifacts

Upon completion, the following artifacts are generated in the working directory:
- `literature_candidates.csv`: Complete metadata table of candidate publications.
- `papers_pdf/`: Downloaded full-text PDFs.
- `papers_si/`: Downloaded supporting information files (PDF, DOCX, ZIP, XLSX).
- `research_corpus_paired.csv`: The curated baseline dataset containing only research articles where **both** the full-text PDF and SI are verified on disk.

---

## Configuration (`config/settings.yaml`)

```yaml
workflow:
  topic: "Aqueous Zinc-Ion Battery Anode Interface"
  keywords:
    - "zinc anode"
    - "aqueous electrolyte"
    - "interface engineering"
  year_start: 2021
  year_end: 2026
  max_candidates: 3000

webvpn:
  portal_url: "https://libywn.ucas.ac.cn"
  cookies_dir: "cookies"
  safety:
    min_delay_seconds: 16.0
    max_delay_seconds: 28.0
    max_consecutive_fails: 3
    cooldown_seconds: 60.0
```

---

## Security and Compliance

1. **Account Protection**: Dynamic randomized delays (16–28s) and circuit breakers prevent triggering institution anti-bot thresholds.
2. **Credential Safety**: `.gitignore` strictly excludes `cookies/*.json` to avoid accidental exposure of active session credentials.

---

## License

MIT License.
