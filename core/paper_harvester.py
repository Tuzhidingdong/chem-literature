"""
core/paper_harvester.py
=======================
Optimized multi-publisher article and SI (Supporting Information) harvester.
Key publisher strategies:
1. Elsevier: Resolves Crossref PII -> navigates ScienceDirect DOM -> lazy image auto-scroll
   -> purges @media print CSS restrictions -> lossless high-res printToPDF export;
2. Wiley: Direct binary fetch via /doi/pdfdirect/{doi}?download=true and downloadSupplement discovery;
3. IOP / ECS: Standard journal PDF extraction via /article/{doi}/pdf;
4. Magic byte verification (%PDF, PK ZIP, DOCX) to prevent HTML error page corruption.
"""

import os
import re
import time
import base64
import random
import requests
import pymupdf
from typing import Optional, Tuple
from core.vpn_session import VpnBrowserSession

class PaperHarvester:
    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        self.pdf_dir = os.path.join(output_dir, "papers_pdf")
        self.si_dir = os.path.join(output_dir, "papers_si")
        os.makedirs(self.pdf_dir, exist_ok=True)
        os.makedirs(self.si_dir, exist_ok=True)

    @staticmethod
    def safe_filename(doi: str) -> str:
        return re.sub(r"[^\w\-]+", "_", doi.strip())[:80]

    @staticmethod
    def resolve_elsevier_pii(doi: str) -> Optional[str]:
        """Resolve ScienceDirect PII from Crossref API metadata."""
        try:
            url = f"https://api.crossref.org/works/{doi}"
            r = requests.get(url, timeout=10).json()
            items = r.get("message", {}).get("link", [])
            for item in items:
                u = item.get("URL", "")
                m = re.search(r'PII:([A-Za-z0-9]+)', u)
                if m:
                    return m.group(1)
        except Exception:
            pass
        return None

    def harvest_elsevier(self, vpn: VpnBrowserSession, sd_base: str, doi: str, pii: str) -> Tuple[Optional[str], Optional[str]]:
        """Perform headless vector rendering and MMC supplementary asset harvesting."""
        sid = self.safe_filename(doi)
        pdf_out = os.path.join(self.pdf_dir, f"{sid}.pdf")
        rel_pdf = f"papers_pdf/{sid}.pdf"
        rel_si = None

        url = f"{sd_base}science/article/pii/{pii}"
        vpn.navigate(url, wait_secs=9)

        # 1. Discover and harvest MMC supporting information attachments
        try:
            links = vpn.cdp.eval("Array.from(document.querySelectorAll('a[href]')).map(a => a.href)") or []
            mmc_links = [l for l in links if "mmc" in l.lower() or "ars.els-cdn.com" in l.lower()]
            if mmc_links:
                s_data, _ = vpn.fetch_binary(mmc_links[0])
                if s_data and len(s_data) > 1024:
                    ext = ".zip" if s_data.startswith(b"PK") else (".pdf" if s_data.startswith(b"%PDF") else ".bin")
                    si_path = os.path.join(self.si_dir, f"{sid}_SI{ext}")
                    with open(si_path, "wb") as f:
                        f.write(s_data)
                    rel_si = f"papers_si/{sid}_SI{ext}"
        except Exception:
            pass

        # 2. Skip PDF if already downloaded and valid
        if os.path.exists(pdf_out) and os.path.getsize(pdf_out) > 50 * 1024:
            return rel_pdf, rel_si

        # 3. DOM scroll and print stylesheet cleansing
        vpn.cdp.eval("""(async () => {
            const total = Math.min(document.body.scrollHeight || 15000, 45000);
            for (let y = 0; y <= total; y += 1200) {
                window.scrollTo(0, y);
                await new Promise(r => setTimeout(r, 120));
            }
            window.scrollTo(0, 0);
            for (const sheet of Array.from(document.styleSheets)) {
                try {
                    const rules = Array.from(sheet.cssRules || []);
                    for (let i = rules.length - 1; i >= 0; i--) {
                        if (rules[i].type === CSSRule.MEDIA_RULE && /print/i.test(rules[i].conditionText || '')) {
                            sheet.deleteRule(i);
                        }
                    }
                } catch (e) {}
            }
            const hideSelectors = [
                'header', 'footer', '#gh-cnt', '.Toolbar', '.TableOfContents',
                '.RelatedContent', '.RecommendedArticles', '#onetrust-consent-sdk',
                '.Outline', '.elsevier-non-solus'
            ];
            for (const sel of hideSelectors) {
                document.querySelectorAll(sel).forEach(el => el.style.display = 'none');
            }
        })()""", await_promise=True)
        vpn.cdp.call("Emulation.setEmulatedMedia", {"media": "screen"})
        time.sleep(1.5)

        pdf_res = vpn.cdp.call("Page.printToPDF", {
            "printBackground": True,
            "preferCSSPageSize": True,
            "marginTop": 0.4,
            "marginBottom": 0.4,
            "marginLeft": 0.4,
            "marginRight": 0.4
        })
        b64 = pdf_res.get("data", "")
        if b64:
            pdf_bytes = base64.b64decode(b64)
            if len(pdf_bytes) >= 50 * 1024 and pdf_bytes.startswith(b"%PDF"):
                with open(pdf_out, "wb") as f:
                    f.write(pdf_bytes)
                return rel_pdf, rel_si
        return None, rel_si

    def harvest_wiley(self, vpn: VpnBrowserSession, wiley_base: str, doi: str) -> Tuple[Optional[str], Optional[str]]:
        """Harvest Wiley direct PDF and downloadSupplement assets."""
        sid = self.safe_filename(doi)
        pdf_out = os.path.join(self.pdf_dir, f"{sid}.pdf")
        rel_pdf = None
        rel_si = None

        if not os.path.exists(pdf_out):
            pdf_url = f"{wiley_base}doi/pdfdirect/{doi}?download=true"
            p_data, _ = vpn.fetch_binary(pdf_url)
            if p_data and len(p_data) >= 30 * 1024 and p_data.startswith(b"%PDF"):
                with open(pdf_out, "wb") as f:
                    f.write(p_data)
                rel_pdf = f"papers_pdf/{sid}.pdf"
        else:
            rel_pdf = f"papers_pdf/{sid}.pdf"

        # Navigate to full text to locate supplement links
        art_url = f"{wiley_base}doi/full/{doi}"
        vpn.navigate(art_url, wait_secs=6)
        supp_links = vpn.cdp.eval("""Array.from(document.querySelectorAll('a[href*="downloadSupplement"]')).map(a => a.href)""") or []
        for sl in supp_links:
            s_data, _ = vpn.fetch_binary(sl)
            if s_data and len(s_data) > 1024 and (s_data.startswith(b"%PDF") or s_data.startswith(b"PK")):
                ext = ".pdf" if s_data.startswith(b"%PDF") else ".zip"
                si_path = os.path.join(self.si_dir, f"{sid}_SI{ext}")
                with open(si_path, "wb") as f:
                    f.write(s_data)
                rel_si = f"papers_si/{sid}_SI{ext}"
                break
        return rel_pdf, rel_si

    def harvest_iop(self, vpn: VpnBrowserSession, iop_base: str, doi: str) -> Optional[str]:
        """Harvest IOP / ECS journal article PDF."""
        sid = self.safe_filename(doi)
        pdf_out = os.path.join(self.pdf_dir, f"{sid}.pdf")
        if os.path.exists(pdf_out) and os.path.getsize(pdf_out) > 30 * 1024:
            return f"papers_pdf/{sid}.pdf"

        pdf_url = f"{iop_base}article/{doi}/pdf"
        p_data, _ = vpn.fetch_binary(pdf_url)
        if p_data and len(p_data) >= 30 * 1024 and p_data.startswith(b"%PDF"):
            with open(pdf_out, "wb") as f:
                f.write(p_data)
            return f"papers_pdf/{sid}.pdf"
        return None
