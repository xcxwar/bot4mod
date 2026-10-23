# Render Web Service wrapper: HTTP health endpoint + four long-polling Bale bots.
import os, sys, time, signal, threading, subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
ROOT = Path(__file__).resolve().parent
BOTS = [
    ("gersha", "gersha.py"),
    ("gpt", "GPT5_Bale_Neon_Premium_groq_fixed_config.py"),
    ("downloader", "bot.py"),
    ("news", "botkhabar.py"),
]
procs = {}
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health", "/healthz"):
            body = ("Bale bots supervisor is running.\n" + "\n".join(f"{n}: {'running' if p.poll() is None else 'stopped'}" for n,p in procs.items())).encode()
            self.send_response(200); self.send_header("Content-Type", "text/plain; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        else:
            self.send_error(404)
    def log_message(self, *args): pass

def launch(name, script):
    print(f"[supervisor] starting {name}: {script}", flush=True)
    return subprocess.Popen([sys.executable, "-u", str(ROOT/script)], cwd=str(ROOT), env=os.environ.copy())

def main():
    # Bind HTTP first so Render health checks succeed even while bots initialize.
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    for name, script in BOTS:
        if not (ROOT/script).is_file():
            print(f"[supervisor] missing script: {script}", flush=True); continue
        procs[name] = launch(name, script); time.sleep(1)
    try:
        while True:
            for name, script in BOTS:
                p = procs.get(name)
                if p is not None and p.poll() is not None:
                    print(f"[supervisor] {name} exited ({p.returncode}); restarting in 5s", flush=True)
                    time.sleep(5); procs[name] = launch(name, script)
            time.sleep(3)
    except KeyboardInterrupt: pass
    finally:
        server.shutdown()
        for p in procs.values():
            if p.poll() is None: p.terminate()
        for p in procs.values():
            try: p.wait(timeout=5)
            except Exception: p.kill()
if __name__ == '__main__': main()
