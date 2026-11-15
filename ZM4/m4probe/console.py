"""Public diagnose console (open-core).

Full step-by-step workbench UI is NOT shipped publicly.
"""

from __future__ import annotations

import argparse
import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import doctor
from .detect import detect_connection, make_backend
from .install_api import MISSING_MODULE_ERROR, install_module_state

HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>ZM4 公开诊断壳</title>
<style>
body{font:14px/1.5 system-ui;max-width:720px;margin:40px auto;padding:0 16px;color:#1d1d1f}
.box{border:1px solid #d2d2d7;border-radius:12px;padding:16px;margin:16px 0}
.muted{color:#6e6e73} pre{white-space:pre-wrap;word-break:break-word}
</style></head><body>
<h1>ZM4 公开诊断壳</h1>
<p class="muted">本页<strong>不是</strong>完整安装工作台。逐步安装 UI / 剧本仅在私有完整版。</p>
<div class="box"><h2>环境自检</h2><pre id="doctor">加载中…</pre></div>
<div class="box"><h2>连接快照</h2><pre id="conn">加载中…</pre></div>
<div class="box"><h2>安装模块</h2><pre id="mod">加载中…</pre></div>
<script>
async function j(u){const r=await fetch(u);return r.json()}
(async()=>{
  for (const [id,u] of [['doctor','/api/doctor'],['conn','/api/connection'],['mod','/api/install_module']]){
    try{document.getElementById(id).textContent=JSON.stringify(await j(u),null,2)}
    catch(e){document.getElementById(id).textContent=String(e)}
  }
})();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A003
        return

    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(
            json.dumps(obj, ensure_ascii=False, indent=2, default=str).encode("utf-8"),
            "application/json; charset=utf-8",
            code,
        )

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/doctor":
            try:
                self._json({"ok": True, "doctor": doctor.check(persist=False)})
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": str(exc)}, 500)
            return
        if path == "/api/connection":
            try:
                snap = detect_connection(make_backend(None), repeats=1)
                self._json({"ok": True, "snapshot": snap})
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": str(exc)}, 500)
            return
        if path == "/api/install_module":
            st = dict(install_module_state())
            if not st.get("available"):
                st.setdefault("error", MISSING_MODULE_ERROR)
            self._json(st)
            return
        self._json({"error": "not found"}, 404)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="ZM4 public diagnose console")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--open", action="store_true")
    args = p.parse_args(argv)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print("ZM4 public diagnose shell:", url)
    print("(full install workbench is not included in this public tree)")
    if args.open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
