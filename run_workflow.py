"""
run_workflow.py
================
Main workflow orchestrator. Specify a research topic and year range via CLI
or config file to execute an end-to-end pipeline:
  1. Literature discovery and candidate screening via open APIs;
  2. Dynamic multi-account session detection from cookies/ directory;
  3. Parallel headless harvesting with rate-limiting and circuit breakers;
  4. Automatic generation of a paired research baseline corpus (research_corpus_paired.csv).
"""

import os
import sys
import glob
import time
import yaml
import random
import threading
import argparse
import pandas as pd
from typing import List, Dict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.topic_discoverer import TopicDiscoverer
from core.vpn_session import VpnBrowserSession
from core.paper_harvester import PaperHarvester
from core.preprocessing.pipeline import preprocess_paired_corpus

def load_config(config_path: str = "config/settings.yaml") -> dict:
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    return {}

def main():
    parser = argparse.ArgumentParser(description="Automated Chemical Literature Discovery and Paired Harvester Workflow")
    parser.add_argument("--topic", type=str, help="Research topic description in natural language", default=None)
    parser.add_argument("--year_start", type=int, help="Start year", default=None)
    parser.add_argument("--year_end", type=int, help="End year", default=None)
    parser.add_argument("--config", type=str, help="Path to settings.yaml", default="config/settings.yaml")
    parser.add_argument("--skip-preprocessing", action="store_true", help="Skip the deterministic paired-PDF/SI preprocessing stage.")
    parser.add_argument("--preprocessed-dir", type=str, default=None, help="Override the preprocessing output directory.")
    args = parser.parse_args()

    cfg = load_config(args.config)
    wf_cfg = cfg.get("workflow", {})
    topic = args.topic or wf_cfg.get("topic", "Zinc-Ion Battery Anode Interface")
    year_start = args.year_start or wf_cfg.get("year_start", 2021)
    year_end = args.year_end or wf_cfg.get("year_end", 2026)
    keywords = wf_cfg.get("keywords", [topic])
    max_cand = wf_cfg.get("max_candidates", 3000)

    print("=============================================================")
    print(f"[*] Launching Chemical Literature Workflow for Topic: {topic}")
    print(f"[*] Target Year Range: {year_start} - {year_end}")
    print("=============================================================")

    # Stage 1: Candidate discovery and metadata retrieval
    cand_csv = "literature_candidates.csv"
    if not os.path.exists(cand_csv):
        discoverer = TopicDiscoverer(topic, keywords, year_start, year_end, max_candidates=max_cand)
        df_candidates = discoverer.fetch_candidates()
        df_candidates.to_csv(cand_csv, index=False, encoding="utf-8-sig")
    else:
        print(f"[*] Found existing {cand_csv}, loading records...")
        df_candidates = pd.read_csv(cand_csv, dtype=str).fillna("")

    # Stage 2: Detect active account credentials in cookies directory
    cookies_dir = cfg.get("webvpn", {}).get("cookies_dir", "cookies")
    cookie_files = sorted(glob.glob(os.path.join(cookies_dir, "*.json")))
    if not cookie_files:
        print(f"[!] Warning: No account cookie files found in '{cookies_dir}'. Please place account01.json, account02.json etc. in {cookies_dir}/")
        print("[!] The workflow will continue with public Open Access extraction only.")

    print(f"[+] Detected {len(cookie_files)} active WebVPN accounts: {[os.path.basename(f) for f in cookie_files]}")

    # Stage 3: Task queue partitioning and parallel worker dispatch
    harvester = PaperHarvester(output_dir=".")
    
    pending = df_candidates[df_candidates["local_pdf_path"].str.strip() == ""].copy()
    print(f"[*] Total pending papers to harvest: {len(pending)}")

    if not cookie_files or len(pending) == 0:
        print("[*] No active accounts or no pending downloads. Proceeding to partition...")
    else:
        # Pre-resolve ScienceDirect PIIs
        els_dois = pending[pending["doi"].str.lower().str.startswith("10.1016/")]["doi"].tolist()
        print(f"[*] Pre-resolving PII for {len(els_dois)} Elsevier papers...")
        pii_map = {}
        for d in els_dois:
            pii = harvester.resolve_elsevier_pii(d)
            if pii:
                pii_map[d] = pii

        # Partition targets across available workers
        num_workers = len(cookie_files)
        targets = pending["doi"].tolist()
        worker_queues = [targets[i::num_workers] for i in range(num_workers)]
        
        csv_lock = threading.Lock()
        safety = cfg.get("webvpn", {}).get("safety", {})
        min_sleep = safety.get("min_delay_seconds", 16.0)
        max_sleep = safety.get("max_delay_seconds", 28.0)

        def worker_thread(worker_id: int, cookie_path: str, port: int, queue: List[str]):
            tag = f"Worker-{worker_id+1}"
            print(f"[{tag}] Starting on port {port} with account: {os.path.basename(cookie_path)}")
            while queue:
                try:
                    with VpnBrowserSession(cookie_file=cookie_path, sandbox_port=port, workspace_dir=".") as vpn:
                        sd_base = vpn.get_proxy_base("elsevier")
                        wiley_base = vpn.get_proxy_base("wiley")
                        iop_base = vpn.get_proxy_base("iop")
                        print(f"[{tag}] Connected to endpoints: Elsevier, Wiley, IOP.")

                        consec_fail = 0
                        while queue:
                            doi = queue.pop(0)
                            rel_pdf, rel_si = None, None
                            try:
                                d_lower = doi.lower()
                                if d_lower.startswith("10.1016/"):
                                    pii = pii_map.get(doi)
                                    if pii:
                                        rel_pdf, rel_si = harvester.harvest_elsevier(vpn, sd_base, doi, pii)
                                elif d_lower.startswith("10.1002/"):
                                    rel_pdf, rel_si = harvester.harvest_wiley(vpn, wiley_base, doi)
                                elif d_lower.startswith("10.1088/") or d_lower.startswith("10.1149/"):
                                    rel_pdf = harvester.harvest_iop(vpn, iop_base, doi)

                                if rel_pdf:
                                    consec_fail = 0
                                    with csv_lock:
                                        m = df_candidates["doi"].str.strip().str.lower() == doi.lower()
                                        df_candidates.loc[m, "local_pdf_path"] = rel_pdf
                                        df_candidates.loc[m, "pdf_status"] = "downloaded"
                                        if rel_si:
                                            df_candidates.loc[m, "local_si_info"] = rel_si
                                            df_candidates.loc[m, "si_status"] = "downloaded"
                                        df_candidates.to_csv(cand_csv, index=False, encoding="utf-8-sig")
                                    print(f"  [{tag} OK] {doi} -> {rel_pdf}")
                                else:
                                    consec_fail += 1
                                    if consec_fail >= safety.get("max_consecutive_fails", 3):
                                        print(f"  [{tag} CIRCUIT BREAK] {consec_fail} fails. Cooldown {safety.get('cooldown_seconds', 60)}s...")
                                        time.sleep(safety.get("cooldown_seconds", 60))
                                        break
                            except Exception as e:
                                print(f"  [{tag} ERR] {doi}: {e}")
                                break
                            time.sleep(random.uniform(min_sleep, max_sleep))
                except Exception as e:
                    print(f"[{tag} RETRY] Session error: {e}")
                    time.sleep(20)

        threads = []
        base_port = 9550
        for i, c_file in enumerate(cookie_files):
            t = threading.Thread(target=worker_thread, args=(i, c_file, base_port + i, worker_queues[i]))
            threads.append(t)
            t.start()
            time.sleep(4)

        for t in threads:
            t.join()

    # Stage 4: Partition paired research corpus (both full text and SI verified)
    print("\n[*] Partitioning Paired Research Baseline Corpus...")
    def check_file(p):
        return str(p).startswith("papers_") and os.path.exists(p)

    df_candidates["has_pdf"] = df_candidates["local_pdf_path"].apply(check_file)
    df_candidates["has_si"] = df_candidates["local_si_info"].apply(check_file)
    
    review_pattern = r"\b(review|progress|advances|perspective|perspectives|outlook)\b"
    is_rev = (df_candidates["article_type"].str.contains("Review", case=False)) | (df_candidates["title"].str.contains(review_pattern, case=False, regex=True))
    
    paired_research = df_candidates[(~is_rev) & df_candidates["has_pdf"] & df_candidates["has_si"]].copy()
    out_paired_csv = "research_corpus_paired.csv"
    paired_research.to_csv(out_paired_csv, index=False, encoding="utf-8-sig")

    # Stage 5: Deterministic preprocessing of each physically verified MAIN+SI pair.
    preprocessing_cfg = cfg.get("preprocessing", {})
    preprocessing_enabled = bool(preprocessing_cfg.get("enabled", True)) and not args.skip_preprocessing
    preprocessing_output = args.preprocessed_dir or preprocessing_cfg.get("output_dir", "runs/preprocessed")
    if preprocessing_enabled and not paired_research.empty:
        print("\n[*] Preprocessing paired research papers (zero LLM/API tokens)...")

        def persist_preprocessing_progress(result: dict):
            doi_value = str(result.get("doi", "")).strip().lower()
            if doi_value and "doi" in df_candidates.columns:
                mask = df_candidates["doi"].astype(str).str.strip().str.lower() == doi_value
                for key, value in result.items():
                    if key == "doi":
                        continue
                    if key not in df_candidates.columns:
                        df_candidates[key] = ""
                    df_candidates.loc[mask, key] = value
                df_candidates.to_csv(cand_csv, index=False, encoding="utf-8-sig")

        paired_research = preprocess_paired_corpus(
            paired_research,
            working_dir=".",
            preprocessed_dir=preprocessing_output,
            grobid_enabled=bool(preprocessing_cfg.get("grobid_enabled", False)),
            grobid_url=preprocessing_cfg.get("grobid_url"),
            grobid_timeout_seconds=int(preprocessing_cfg.get("grobid_timeout_seconds", 30)),
            max_zip_member_bytes=int(preprocessing_cfg.get("max_zip_member_bytes", 104857600)),
            max_zip_total_bytes=int(preprocessing_cfg.get("max_zip_total_bytes", 536870912)),
            progress_callback=persist_preprocessing_progress,
        )
        for column in paired_research.columns:
            if column not in df_candidates.columns:
                df_candidates[column] = ""
            df_candidates.loc[paired_research.index, column] = paired_research[column]
        counts = paired_research["preprocessing_status"].value_counts().to_dict()
        print(f"[+] Preprocessing status counts: {counts}")
        print(f"[+] Preprocessing packages: {preprocessing_output}/<paper_id>/01_preprocessing/")
    elif args.skip_preprocessing or not preprocessing_enabled:
        print("[*] Preprocessing stage skipped by CLI/configuration.")
    else:
        print("[*] No paired research papers are available for preprocessing.")

    # Persist the paired corpus with preprocessing paths/status fields for downstream skills.
    paired_research.to_csv(out_paired_csv, index=False, encoding="utf-8-sig")
    df_candidates.to_csv(cand_csv, index=False, encoding="utf-8-sig")

    print("=============================================================")
    print("[*] Workflow execution completed.")
    print(f"[*] Total candidate records: {len(df_candidates)}")
    print(f"[*] Physical full-text PDFs: {df_candidates['has_pdf'].sum()}")
    print(f"[*] Physical SI attachments: {df_candidates['has_si'].sum()}")
    print(f"[*] Dual-paired research articles: {len(paired_research)} -> {out_paired_csv}")
    print("=============================================================")

if __name__ == "__main__":
    main()
