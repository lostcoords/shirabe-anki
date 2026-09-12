"""Exercise capture in Anki's Qt WebEngine with a local request recorder."""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
import threading

from gui_safety import gui_arguments

gui_arguments(argparse.ArgumentParser(description=__doc__), allow_offscreen=True)

import support
from shirabe_addon.offline_ui import CAPTURE_CSP, capture_script

from PyQt6.QtCore import QEventLoop, QTimer, QUrl
from PyQt6.QtWidgets import QApplication
from PyQt6.QtWebEngineWidgets import QWebEngineView


bundle = sys.stdin.read().encode()
requests = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        requests.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript" if self.path == "/bundle.js" else "text/plain")
        self.end_headers()
        self.wfile.write(bundle if self.path == "/bundle.js" else b"blocked fixture")


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
base = f"http://127.0.0.1:{server.server_port}"
app = QApplication(["shirabe-offline-probe"])
view = QWebEngineView()
view.resize(640, 480)
view.show()
loop = QEventLoop()
answer = {}
fixture = f'''<style>
@import url("{base}/leak-css");
.hidden {{ display:none }}
.picture {{ background-image:url("{base}/leak-background") }}
</style><div><ruby>猫<rt>ねこ</rt></ruby><b>です</b>。</div>
<span class="hidden">秘密</span><span hidden>答え</span>
<div class="picture">犬</div><div>前\r\n後\r末</div><script>fetch("{base}/leak-script")</script>
<img src="{base}/leak-img" onerror="fetch('{base}/leak-event')">
<iframe src="{base}/leak-frame"></iframe><audio src="{base}/leak-audio" autoplay></audio>
<video src="{base}/leak-video" poster="{base}/leak-poster" autoplay></video>
<link rel="stylesheet" href="{base}/leak-link"><meta http-equiv="refresh" content="0;url={base}/leak-refresh">
'''


def captured(value):
    answer.update(value or {})
    QTimer.singleShot(500, loop.quit)


def loaded(ok):
    if not ok:
        answer["error"] = "load_failed"
        loop.quit()
        return
    view.page().runJavaScript(capture_script(fixture, 1), captured)


view.loadFinished.connect(loaded)
view.setHtml(f'<meta http-equiv="Content-Security-Policy" content="{CAPTURE_CSP}"><script src="{base}/bundle.js"></script><main id="qa"></main>', QUrl(base + "/"))
QTimer.singleShot(15000, loop.quit)
loop.exec()
view.close()
server.shutdown()
server.server_close()
thread.join()
assert answer.get("texts") == ["猫です。", "犬", "前\n後\n末"], answer
assert answer.get("scripted") is True, answer
assert requests == ["/bundle.js"], requests
print(json.dumps({"qtWebEngine": True, "requests": requests, "texts": answer["texts"]}, ensure_ascii=False))
