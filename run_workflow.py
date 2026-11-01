"""
run_workflow.py
===============
主控运行脚本。只需用自然语言指定主题参数或传入配置文件，
即可一键串联：
  1. 主题检索与智能初筛；
  2. 自动发现 cookies/ 目录下所有账号凭据，动态启动对应数量的隔离沙箱多线程采集；
  3. 跨出版社交错调度、礼貌间隔频控与熔断；
  4. 自动生成配齐的研究型文献基础库 (research_corpus_paired.csv) 与详细清单。
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

# 添加模块路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.topic_discoverer import TopicDiscoverer
from core.vpn_session import VpnBrowserSession
from core.paper_harvester import PaperHarvester

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
    args = parser.parse_args()

    cfg = load_config(args.config)
    wf_cfg = cfg.get("workflow", {})
    topic = args.topic or wf_cfg.get("topic", "Zinc-Ion Battery Anode Interface")
    year_start = args.year_start or wf_cfg.get("year_start", 2021)
    year_end = args.year_end or wf_cfg.get("year_end", 2026)
    keywords = wf_cfg.get("keywords", [topic])
    max_cand = wf_cfg.get("max_candidates", 3000)

    print("=============================================================")
    print(f"🚀 Launching Chemical Literature Workflow for Topic: {topic}")
    print(f"📅 Target Year Range: {year_start} - {year_end}")
    print("=============================================================")

    # 阶段 1：确定主题与检索候选文献
    cand_csv = "literature_candidates.csv"
    if not os.path.exists(cand_csv):
        discoverer = TopicDiscoverer(topic, keywords, year_start, year_end, max_candidates=max_cand)
        df_candidates = discoverer.fetch_candidates()
        df_candidates.to_csv(cand_csv, index=False, encoding="utf-8-sig")
    else:
        print(f"[*] Found existing {cand_csv}, loading records...")
        df_candidates = pd.read_csv(cand_csv, dtype=str).fillna("")

    # 阶段 2：检测 cookies 目录中的可用多账号凭据
    cookies_dir = cfg.get("webvpn", {}).get("cookies_dir", "cookies")
    cookie_files = sorted(glob.glob(os.path.join(cookies_dir, "*.json")))
    if not cookie_files:
        print(f"[!] Warning: No account cookie files found in '{cookies_dir}'. Please place account01.json, account02.json etc. in {cookies_dir}/")
        print("[!] The workflow will continue with public Open Access extraction only.")

    print(f"[+] Detected {len(cookie_files)} active WebVPN accounts: {[os.path.basename(f) for f in cookie_files]}")

    # 阶段 3：构建任务队列并交错调度
    harvester = PaperHarvester(output_dir=".")
    
    # 筛选未下载正文的目标
    pending = df_candidates[df_candidates["local_pdf_path"].str.strip() == ""].copy()
    print(f"[*] Total pending papers to harvest: {len(pending)}")

    if not cookie_files or len(pending) == 0:
        print("[*] No active accounts or no pending downloads. Proceeding to partition...")
    else:
        # 预解析 Elsevier PII
        els_dois = pending[pending["doi"].str.lower().str.startswith("10.1016/")]["doi"].tolist()
        print(f"[*] Pre-resolving PII for {len(els_dois)} Elsevier papers...")
        pii_map = {}
        for d in els_dois:
            pii = harvester.resolve_elsevier_pii(d)
            if pii:
                pii_map[d] = pii

        # 将任务队列平分给检测到的所有账号，动态支持 1~N 个账号并行
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
                        print(f"[{tag}] Bases connected: Elsevier, Wiley, IOP.")

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

        # 启动多线程
        threads = []
        base_port = 9550
        for i, c_file in enumerate(cookie_files):
            t = threading.Thread(target=worker_thread, args=(i, c_file, base_port + i, worker_queues[i]))
            threads.append(t)
            t.start()
            time.sleep(4) # 错峰启动浏览器

        for t in threads:
            t.join()

    # 阶段 4：自动分离“正文与 SI 双配齐的研究型论文”基础库
    print("\n[*] Partitioning Paired Research Base Corpus...")
    def check_file(p):
        return str(p).startswith("papers_") and os.path.exists(p)

    df_candidates["has_pdf"] = df_candidates["local_pdf_path"].apply(check_file)
    df_candidates["has_si"] = df_candidates["local_si_info"].apply(check_file)
    
    review_pattern = r"\b(review|progress|advances|perspective|perspectives|outlook)\b"
    is_rev = (df_candidates["article_type"].str.contains("Review", case=False)) | (df_candidates["title"].str.contains(review_pattern, case=False, regex=True))
    
    paired_research = df_candidates[(~is_rev) & df_candidates["has_pdf"] & df_candidates["has_si"]].copy()
    out_paired_csv = "research_corpus_paired.csv"
    paired_research.to_csv(out_paired_csv, index=False, encoding="utf-8-sig")
    
    print("=============================================================")
    print(f"🎉 Workflow Successfully Completed!")
    print(f"📊 Total Candidates: {len(df_candidates)}")
    print(f"📑 Physical Full-Text PDFs: {df_candidates['has_pdf'].sum()}")
    print(f"📦 Physical SI Attachments: {df_candidates['has_si'].sum()}")
    print(f"💎 High-Quality Dual-Paired Research Corpus: {len(paired_research)} -> Saved to {out_paired_csv}")
    print("=============================================================")

if __name__ == "__main__":
    main()
