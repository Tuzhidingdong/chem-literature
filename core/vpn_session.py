"""
core/vpn_session.py
===================
CDP (Chrome DevTools Protocol) isolated browser session manager.
Key specifications:
1. Zero visual disturbance: Forces `--window-position=-32000,-32000`;
2. Isolated sandboxing: Each account uses an independent temp profile & debug port;
3. Dynamic multi-account support: Automatically loads and injects session cookies;
4. Anti-bot challenge detection and connection auto-recovery.
"""

import os
import json
import time
import shutil
import subprocess
import requests
import websocket
from typing import Dict, List, Optional, Tuple

USER_EDGE_EXE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if not os.path.exists(USER_EDGE_EXE):
    USER_EDGE_EXE = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"

RESOURCE_MAP = {
    "wiley": 119,
    "springer": 64,
    "acs": 86,
    "elsevier": 100,
    "rsc": 51,
    "nature": 47,
    "science": 28,
    "iop": 106
}

BLOCK_MARKERS = [
    "just a moment", "verify you are human", "captcha", "checking your browser",
    "non human", "unusual traffic", "access denied", "too many requests",
    "cf-chl", "restricted access", "automated requests"
]

class CDPClient:
    def __init__(self, ws_url: str, timeout: int = 120):
        self.ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
        self.msg_id = 0

    def call(self, method: str, params: Optional[dict] = None) -> dict:
        self.msg_id += 1
        curr_id = self.msg_id
        self.ws.send(json.dumps({"id": curr_id, "method": method, "params": params or {}}))
        while True:
            resp = json.loads(self.ws.recv())
            if resp.get("id") == curr_id:
                if "error" in resp:
                    raise RuntimeError(f"CDP call error {method}: {resp['error']}")
                return resp.get("result", {})

    def eval(self, expr: str, await_promise: bool = False):
        res = self.call("Runtime.evaluate", {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": await_promise
        })
        return res.get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass

class VpnBrowserSession:
    def __init__(self, cookie_file: str, sandbox_port: int, workspace_dir: str):
        self.cookie_file = cookie_file
        self.sandbox_port = sandbox_port
        self.workspace_dir = workspace_dir
        self.sandbox_profile = os.path.join(workspace_dir, f".edge_profile_{sandbox_port}")
        self.proc = None
        self.cdp = None
        self.prefix_cache: Dict[str, str] = {}

    def _read_cookies(self) -> List[dict]:
        with open(self.cookie_file, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        return cookies

    def __enter__(self):
        cookies = self._read_cookies()
        shutil.rmtree(self.sandbox_profile, ignore_errors=True)
        os.makedirs(self.sandbox_profile, exist_ok=True)
        
        args = [
            USER_EDGE_EXE,
            f"--remote-debugging-port={self.sandbox_port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={self.sandbox_profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--disable-features=msEdgeSidebarV2,EdgeCollections",
            "--window-position=-32000,-32000",
            "--window-size=1280,900",
            "about:blank"
        ]
        self.proc = subprocess.Popen(args)
        
        ws_url = None
        for _ in range(40):
            try:
                tabs = requests.get(f"http://127.0.0.1:{self.sandbox_port}/json", timeout=2).json()
                pages = [t for t in tabs if t.get("type") == "page"]
                if pages:
                    ws_url = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
            time.sleep(0.5)
            
        if not ws_url:
            raise RuntimeError(f"Failed to launch sandbox browser on port {self.sandbox_port}")

        self.cdp = CDPClient(ws_url)
        self.cdp.call("Page.enable")
        self.cdp.call("Network.enable")
        
        cookie_params = []
        for c in cookies:
            p = {k: c[k] for k in ("name", "value", "domain", "path", "secure", "httpOnly") if k in c}
            if c.get("sameSite"):
                p["sameSite"] = c["sameSite"]
            if not c.get("session") and c.get("expires", -1) > 0:
                p["expires"] = c["expires"]
            cookie_params.append(p)
        self.cdp.call("Network.setCookies", {"cookies": cookie_params})
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.cdp:
            self.cdp.close()
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True)
        time.sleep(1)
        shutil.rmtree(self.sandbox_profile, ignore_errors=True)

    def navigate(self, url: str, wait_secs: int = 8) -> Tuple[str, str, str]:
        self.cdp.call("Page.navigate", {"url": url})
        time.sleep(wait_secs)
        for _ in range(10):
            if self.cdp.eval("document.readyState") == "complete":
                break
            time.sleep(1)
        href = self.cdp.eval("location.href") or ""
        title = self.cdp.eval("document.title") or ""
        body = (self.cdp.eval("document.body ? document.body.innerText.slice(0, 3000) : ''") or "").lower()
        
        for m in BLOCK_MARKERS:
            if m in title.lower() or m in body:
                raise RuntimeError(f"Anti-bot verification encountered: {m}")
        return href, title, body

    def get_proxy_base(self, publisher_key: str, portal_url: str = "https://libywn.ucas.ac.cn") -> str:
        if publisher_key in self.prefix_cache:
            return self.prefix_cache[publisher_key]
        rid = RESOURCE_MAP.get(publisher_key.lower())
        if not rid:
            raise ValueError(f"Unknown publisher: {publisher_key}")
            
        visit_url = f"{portal_url}/user/resource/visit?id={rid}"
        href, title, _ = self.navigate(visit_url, wait_secs=8)
        
        # Extract dynamic proxy routing prefix
        # URL structure is typically https://libywn.ucas.ac.cn/https/<token>/
        parts = href.split("/")
        if len(parts) >= 5 and "libywn.ucas.ac.cn" in href:
            base = f"https://libywn.ucas.ac.cn/{parts[3]}/{parts[4]}/"
        else:
            base = href.split("?")[0]
            if not base.endswith("/"):
                base += "/"
        self.prefix_cache[publisher_key] = base
        return base

    def fetch_binary(self, url: str) -> Tuple[Optional[bytes], str]:
        fetch_js = """
        (async () => {
            const resp = await fetch(%s, { credentials: 'include' });
            if (!resp.ok) return { status: resp.status, data: null, ctype: '' };
            const buf = await resp.arrayBuffer();
            const bytes = new Uint8Array(buf);
            let binary = '';
            const chunkSize = 32768;
            for (let i = 0; i < bytes.length; i += chunkSize) {
                binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
            }
            return {
                status: 200,
                data: btoa(binary),
                ctype: resp.headers.get('content-type') || ''
            };
        })()
        """
        raw = self.cdp.eval(fetch_js % json.dumps(url), await_promise=True)
        if not raw or raw.get("status") != 200 or not raw.get("data"):
            return None, ""
        import base64
        return base64.b64decode(raw["data"]), raw.get("ctype", "")
