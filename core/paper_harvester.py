"""
core/paper_harvester.py
=======================
高度优化的多出版社文献与 SI 自动化采集执行器。
沉淀的核心能力：
1. Elsevier：自动解析 Crossref PII -> 加载全量 ScienceDirect DOM -> 滚动加载高清图表/公式 -> 移除 @media print 隐藏规则 -> 高清无损 printToPDF 导出；
2. Wiley：直取 /doi/pdfdirect/{doi}?download=true 与 downloadSupplement 端点；
3. IOP / ECS：直链提取 /article/{doi}/pdf 并自动跳过纯网页会议摘要；
4. 严格魔数与字节大小校验（%PDF, PK ZIP, DOCX），杜绝虚假 HTML 错误页写入。
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
        """通过 Crossref API 获取 ScienceDirect 的 PII"""
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
        """执行 Elsevier 离线无损矢量渲染与 MMC 附件抓取"""
        sid = self.safe_filename(doi)
        pdf_out = os.path.join(self.pdf_dir, f"{sid}.pdf")
        rel_pdf = f"papers_pdf/{sid}.pdf"
        rel_si = None

        url = f"{sd_base}science/article/pii/{pii}"
        vpn.navigate(url, wait_secs=9)

        # 1. 探查并下载 MMC 支持信息附件
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

        # 2. 如果 PDF 已经存在则跳过
        if os.path.exists(pdf_out) and os.path.getsize(pdf_out) > 50 * 1024:
            return rel_pdf, rel_si

        # 3. 页面滚动与打印样式净化
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
        """提取 Wiley PDFDirect 与 downloadSupplement"""
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

        # 访问 fulltext 抓取 downloadSupplement
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
        """提取 IOP / ECS 标准期刊论文 PDF"""
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
