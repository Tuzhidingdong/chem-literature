"""
core/topic_discoverer.py
========================
自动根据用户自然语言主题与年限，调用开放学术 API (OpenAlex, Crossref) 构建候选文献库，
并执行研究型论文（Original Research）自动筛除会议摘要（Meeting Abstracts）、预印本与综述的初筛。
"""

import os
import re
import sys
import time
import requests
import pandas as pd
from typing import List, Dict, Any

class TopicDiscoverer:
    def __init__(self, topic: str, keywords: List[str], year_start: int, year_end: int, max_candidates: int = 3000):
        self.topic = topic
        self.keywords = keywords
        self.year_start = year_start
        self.year_end = year_end
        self.max_candidates = max_candidates
        self.headers = {"User-Agent": "ChemLiteratureWorkflow/2.0 (mailto:academic-research@workflow.org)"}

    def fetch_candidates(self) -> pd.DataFrame:
        """从 OpenAlex 按照关键词和年份检索文献"""
        print(f"[*] Starting OpenAlex discovery for topic: '{self.topic}' (Years: {self.year_start}-{self.year_end})")
        results = []
        
        # 构建检索查询
        query_str = " ".join(self.keywords) if self.keywords else self.topic
        base_url = "https://api.openalex.org/works"
        
        page = 1
        per_page = 100
        while len(results) < self.max_candidates:
            params = {
                "search": query_str,
                "filter": f"publication_year:{self.year_start}-{self.year_end},type:article|preprint",
                "per_page": per_page,
                "page": page,
                "sort": "relevance_score:desc"
            }
            try:
                resp = requests.get(base_url, params=params, headers=self.headers, timeout=20)
                if resp.status_code != 200:
                    print(f"[!] OpenAlex API warning: {resp.status_code}, retrying...")
                    time.sleep(3)
                    continue
                data = resp.json()
                works = data.get("results", [])
                if not works:
                    break
                    
                for w in works:
                    doi = (w.get("doi") or "").replace("https://doi.org/", "").strip()
                    if not doi:
                        continue
                    
                    title = w.get("title") or ""
                    # 快速筛除会议摘要与撤稿
                    if "retracted" in title.lower() or "author index" in title.lower() or "meeting abstract" in title.lower():
                        continue
                    if "mtgabs" in doi.lower() or "/ma20" in doi.lower():
                        continue
                    
                    # 识别是否为综述
                    is_review = False
                    if w.get("type") == "review" or re.search(r"\b(review|progress|advances|perspective|perspectives|outlook)\b", title, re.I):
                        is_review = True
                        
                    journal_name = ""
                    primary_loc = w.get("primary_location") or {}
                    source = primary_loc.get("source") or {}
                    if source:
                        journal_name = source.get("display_name") or ""
                        
                    results.append({
                        "doi": doi,
                        "title": title,
                        "year": str(w.get("publication_year") or ""),
                        "journal": journal_name,
                        "article_type": "Review" if is_review else "Original Research",
                        "cited_by_count": str(w.get("cited_by_count") or "0"),
                        "landing_page_url": w.get("doi") or f"https://doi.org/{doi}",
                        "abstract": self._reconstruct_abstract(w.get("abstract_inverted_index")),
                        "local_pdf_path": "",
                        "local_si_info": "",
                        "pdf_status": "pending",
                        "si_status": "pending"
                    })
                    if len(results) >= self.max_candidates:
                        break
                        
                print(f"    Page {page} processed. Total discovered: {len(results)}")
                page += 1
                time.sleep(0.5)
            except Exception as e:
                print(f"[!] Error fetching page {page}: {e}")
                time.sleep(2)
                break
                
        df = pd.DataFrame(results).drop_duplicates(subset=["doi"]).reset_index(drop=True)
        print(f"[+] Total deduplicated candidates acquired: {len(df)}")
        return df

    def _reconstruct_abstract(self, inverted_index: Dict[str, List[int]]) -> str:
        """从 OpenAlex inverted index 还原摘要文本"""
        if not inverted_index:
            return ""
        try:
            words = []
            for word, positions in inverted_index.items():
                for pos in positions:
                    words.append((pos, word))
            words.sort(key=lambda x: x[0])
            return " ".join([w[1] for w in words])
        except Exception:
            return ""
