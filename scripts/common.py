"""Helpers shared by the demo, attack-demo and benchmark scripts."""
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from nyx.cli import api  # noqa: E402

results = []


def node_url(i, base_port=0):
    return f"http://127.0.0.1:{8000 + base_port + i}"


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"   [{'PASS' if ok else 'FAIL'}] {name}{'  (' + detail + ')' if detail else ''}", flush=True)


def heading(s):
    print(f"\n== {s} " + "=" * max(0, 66 - len(s)), flush=True)


def status(i, base_port=0):
    try:
        return api(node_url(i, base_port), "/status", timeout=3)
    except (SystemExit, OSError, ValueError):
        return None


def table(nodes, base_port=0):
    print(f"   {'node':<7}{'height':>7}{'final':>7}  {'tip':<12}{'peers':>6}")
    for i in nodes:
        s = status(i, base_port)
        if s:
            print(f"   node{i:<3}{s['height']:>7}{s['finalized_height']:>7}  {s['tip'][:10]:<12}{len(s['peers']):>6}")
        else:
            print(f"   node{i:<3}   (offline)")


def validators_table(i, base_port=0):
    s = status(i, base_port)
    print(f"   {'validator':<15}{'stake':>7}{'reputation':>12}{'weight':>8}")
    for v in s["validators"]:
        print(f"   {v['address'][:12]:<15}{v['stake']:>7}{v['reputation_pct']:>11.0f}%{v['weight']:>8}")
    for v in s["slashed"]:
        print(f"   {v['address'][:12]:<15}{v['stake']:>7}{'SLASHED':>12}{0:>8}")
    return {v["address"]: v for v in s["validators"]}


class Network:
    """Starts node processes from a testnet directory and cleans up afterwards."""

    def __init__(self, prefix="nyx-"):
        self.dir = tempfile.mkdtemp(prefix=prefix)
        self.procs, self.logs = {}, {}

    def start(self, i):
        f = open(os.path.join(self.dir, f"node{i}.log"), "a")
        self.logs[i] = f
        self.procs[i] = subprocess.Popen(
            [sys.executable, "-m", "nyx", "node", "--config", os.path.join(self.dir, f"node{i}.json")],
            cwd=ROOT, stdout=f, stderr=subprocess.STDOUT,
        )

    def kill(self, i):
        self.procs[i].kill()   # hard crash: no clean shutdown (works on Windows and Linux)
        self.procs[i].wait()

    def log(self, i):
        with open(os.path.join(self.dir, f"node{i}.log")) as f:
            return f.read()

    def stop(self, keep_to=None):
        for p in self.procs.values():
            if p.poll() is None:
                p.terminate()
        for p in self.procs.values():
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        for f in self.logs.values():
            f.close()
        if keep_to:
            os.makedirs(keep_to, exist_ok=True)
            for name in os.listdir(self.dir):
                if name.endswith(".log"):
                    shutil.copy(os.path.join(self.dir, name), os.path.join(keep_to, name))
        if os.environ.get("KEEP_DEMO") != "1":
            shutil.rmtree(self.dir, ignore_errors=True)


def screenshot(url, path, wait_ms=2500):
    """Optional: capture the dashboard with headless Chrome if NYX_SHOTS is set and Chrome is installed."""
    if not os.environ.get("NYX_SHOTS"):
        return
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chrome")
    if not chrome:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(
        [chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-proxy-server", "--hide-scrollbars",
         "--window-size=1440,1500", f"--virtual-time-budget={wait_ms}", f"--screenshot={path}", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
    )
    print(f"   (screenshot saved: {os.path.basename(path)})")


def summary():
    heading("Summary")
    passed = sum(ok for _, ok in results)
    print(f"   {passed}/{len(results)} checks passed", flush=True)
    return 0 if passed == len(results) else 1


def wait_until(fn, timeout, step=0.5):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(step)
    return None
