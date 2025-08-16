"""
Ghost Advance — Playwright Website Cloner (single-file, Render-friendly)
- Uses Playwright (Chromium). This script will attempt to auto-install Playwright browsers
  at first run by invoking: python -m playwright install chromium
- Install requirements: pip install -r requirements.txt
- If deploying to Render, add a build step to run: python -m playwright install chromium
  (or let the app attempt to install at runtime; build-time is preferred).
"""
import os
import re
import io
import sys
import time
import json
import zipfile
import hashlib
import tempfile
import threading
import traceback
import mimetypes
import shutil
import uuid
import subprocess
from urllib.parse import urlparse, urljoin, unquote

from flask import Flask, request, jsonify, send_file, render_template_string, abort
import requests

# Try import playwright
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
except Exception:
    sync_playwright = None
    PlaywrightTimeout = Exception

app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False

# In-memory job store
JOBS = {}
JOBS_LOCK = threading.Lock()
CLEANUP_SECONDS = 15 * 60  # seconds to keep artifacts after finish
PLAYWRIGHT_LOCK = threading.Lock()  # serialize access to playwright (safer)

HEADERS = {'User-Agent': 'Mozilla/5.0 (ghost-clone)'}

# Utility helpers
def safe_component(s: str) -> str:
    s = unquote(s or "")
    return re.sub(r'[^A-Za-z0-9._\-/]', '_', s)

def make_local_path(root: str, raw_url: str) -> str:
    p = urlparse(raw_url)
    netloc = re.sub(r'[:]', '_', p.netloc)
    path = p.path or '/'
    if path.endswith('/'):
        path = path + 'index.html'
    else:
        if path == '':
            path = '/index.html'
    path = path.lstrip('/')
    path = safe_component(path)
    if p.query:
        h = hashlib.sha1(p.query.encode('utf-8')).hexdigest()[:8]
        base, ext = os.path.splitext(path)
        path = f"{base}__q{h}{ext or ''}"
    local = os.path.join(root, netloc, path)
    return local

def ensure_parent(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)

def ext_from_content_type(ct: str):
    if not ct:
        return ''
    return mimetypes.guess_extension(ct.split(';', 1)[0].strip() or '') or ''

def ensure_playwright_browsers():
    """
    Ensure Playwright browsers are installed. Prefer doing this at build time,
    but if not installed, try to run: python -m playwright install chromium
    """
    global sync_playwright
    if sync_playwright is None:
        try:
            # Try import again in case package was just installed
            from playwright.sync_api import sync_playwright as sp, TimeoutError as PlaywrightTimeoutLocal
            sync_playwright = sp
        except Exception:
            pass

    # If playwright import exists, test launching. If fails, attempt install.
    if sync_playwright is None:
        return False, "playwright package not installed. pip install -r requirements.txt"

    # Test if browsers are installed by trying to run sync_playwright and launching browser
    try:
        with PLAYWRIGHT_LOCK:
            with sync_playwright() as p:
                # attempt to launch headless chromium
                browser = p.chromium.launch(headless=True)
                browser.close()
        return True, None
    except Exception as e:
        # Try to run install
        try:
            # Run install - this may be slow; do at startup/build time if possible
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
            # try again
            with PLAYWRIGHT_LOCK:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    browser.close()
            return True, None
        except Exception as ex2:
            return False, f"Playwright browser launch failed: {ex2} (original: {e})"

# Background mirror worker using Playwright sync API
def run_mirror_job(jobid: str, url: str):
    with JOBS_LOCK:
        job = JOBS.get(jobid)
    if job is None:
        return

    job['logs'].append(f"Starting mirror job for: {url}")
    tmpdir = tempfile.mkdtemp(prefix='ghost_clone_')
    job['tmpdir'] = tmpdir
    job['status'] = 'running'
    job['started_at'] = time.time()

    ok, msg = ensure_playwright_browsers()
    if not ok:
        job['logs'].append("ERROR: Playwright/browser not available: " + (msg or "unknown"))
        job['status'] = 'error'
        job['finished_at'] = time.time()
        return

    try:
        with PLAYWRIGHT_LOCK:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
                context = browser.new_context(ignore_https_errors=True)
                page = context.new_page()

                job['logs'].append("Browser launched. Attaching response handler...")

                # Save responses
                def handle_response(response):
                    try:
                        rurl = response.url
                        if not isinstance(rurl, str) or not rurl.startswith('http'):
                            return
                        # Avoid saving huge streaming requests repeatedly: skip very large or non-needed types?
                        try:
                            body = response.body()
                        except Exception:
                            return
                        if not body:
                            return
                        local = make_local_path(tmpdir, rurl)
                        ensure_parent(local)
                        # add extension if missing
                        if os.path.splitext(local)[1] == '':
                            ctype = response.headers.get('content-type', '')
                            ext = ext_from_content_type(ctype)
                            if ext:
                                local += ext
                        with open(local, 'wb') as fh:
                            fh.write(body)
                        job['saved_count'] += 1
                        job['logs'].append(f"Saved resource: {rurl} -> {os.path.relpath(local, tmpdir)}")
                    except Exception as e:
                        job['logs'].append(f"Warn: failed saving response {getattr(response,'url', 'unknown')}: {e}")

                page.on("response", handle_response)

                job['logs'].append("Navigating to URL...")
                try:
                    page.goto(url, timeout=60_000, wait_until="domcontentloaded")
                    job['logs'].append("DOM content loaded.")
                except PlaywrightTimeout:
                    job['logs'].append("Note: page.goto timed out; continuing to collect resources.")
                except Exception as e:
                    job['logs'].append(f"Note: page.goto error: {e}")

                job['logs'].append("Waiting briefly for late XHR/fetch...")
                page.wait_for_timeout(3000)

                # Save main HTML
                try:
                    html = page.content()
                    index_path = os.path.join(tmpdir, urlparse(url).netloc, 'index.html')
                    ensure_parent(index_path)
                    with open(index_path, 'w', encoding='utf-8') as fh:
                        fh.write(html)
                    job['logs'].append(f"Saved main HTML: {os.path.relpath(index_path, tmpdir)}")
                except Exception as e:
                    job['logs'].append(f"Warn: could not save main HTML: {e}")
                    html = ''

                # Screenshot
                try:
                    shot_path = os.path.join(tmpdir, 'screenshot.png')
                    page.screenshot(path=shot_path, full_page=False)
                    job['screenshot'] = shot_path
                    job['logs'].append("Saved screenshot.")
                except Exception as e:
                    job['logs'].append(f"Warn: screenshot failed: {e}")

                browser.close()

        # After browser closed, rewrite HTML to point to local assets (attempt)
        try:
            if not html:
                # If earlier saving failed, try to read the saved file
                saved_index = os.path.join(tmpdir, urlparse(url).netloc, 'index.html')
                if os.path.exists(saved_index):
                    with open(saved_index, 'r', encoding='utf-8') as fh:
                        html = fh.read()
                else:
                    html = ''
            # Build map of saved files
            url_to_local = {}
            for root, _, files in os.walk(tmpdir):
                for fn in files:
                    full = os.path.join(root, fn)
                    rel = os.path.relpath(full, tmpdir).replace(os.sep, '/')
                    # reconstruct original URL heuristically from folder structure: netloc/path...
                    parts = rel.split('/', 1)
                    if len(parts) == 2:
                        netloc, pth = parts
                        possible_url = f"https://{netloc}/{pth}"
                        url_to_local[possible_url] = rel

            # Replace src/href/action/poster values
            def replace_url(match):
                orig = match.group(2)
                abs_url = urljoin(url, orig)
                local = url_to_local.get(abs_url, orig)
                return match.group(1) + local + match.group(3)

            new_html = re.sub(r'(\b(?:src|href|poster|action)\s*=\s*["\'])(.*?)(["\'])',
                              replace_url, html, flags=re.I)
            new_html = re.sub(r'(?i)(url\(["\']?)(.*?)["\']?\)',
                              lambda m: 'url(' + url_to_local.get(urljoin(url, m.group(2)), m.group(2)) + ')',
                              new_html)

            index_path = os.path.join(tmpdir, urlparse(url).netloc, 'index.html')
            ensure_parent(index_path)
            with open(index_path, 'w', encoding='utf-8') as fh:
                fh.write(new_html)
            job['logs'].append(f"Wrote rewritten HTML to {os.path.relpath(index_path, tmpdir)}")
        except Exception as e:
            job['logs'].append(f"Warn: rewriting HTML failed: {e}")

        # Package into ZIP
        job['logs'].append("Packaging files into ZIP...")
        zip_io = io.BytesIO()
        with zipfile.ZipFile(zip_io, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(tmpdir):
                for fn in files:
                    full = os.path.join(root, fn)
                    arcname = os.path.relpath(full, tmpdir)
                    zf.write(full, arcname)
        zip_io.seek(0)
        job['zip_bytes'] = zip_io.read()
        job['status'] = 'done'
        job['finished_at'] = time.time()
        job['logs'].append("ZIP ready. Job finished successfully.")
    except Exception as exc:
        tb = traceback.format_exc()
        job['logs'].append("ERROR during mirroring:")
        job['logs'].append(tb)
        job['status'] = 'error'
        job['finished_at'] = time.time()
    finally:
        # leave tmpdir for a bit; cleanup thread will remove it
        pass

# Cleanup thread to remove old job artifacts
def cleanup_worker():
    while True:
        now = time.time()
        with JOBS_LOCK:
            to_delete = []
            for jid, j in list(JOBS.items()):
                if j.get('status') in ('done', 'error') and j.get('finished_at'):
                    if now - j['finished_at'] > CLEANUP_SECONDS:
                        to_delete.append(jid)
            for jid in to_delete:
                job = JOBS.pop(jid, None)
                try:
                    tmpdir = job.get('tmpdir')
                    if tmpdir and os.path.exists(tmpdir):
                        shutil.rmtree(tmpdir, ignore_errors=True)
                except Exception:
                    pass
        time.sleep(5)

cleanup_thread = threading.Thread(target=cleanup_worker, daemon=True)
cleanup_thread.start()

# HTML UI
INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Ghost Advance — Playwright Cloner</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    :root{--card:#0b1220;--muted:#94a3b8;--accent:#06b6d4;--glass:rgba(255,255,255,0.03)}
    body{margin:0;font-family:Inter,system-ui,Segoe UI,Arial;background:linear-gradient(180deg,#071024 0%, #071530 100%);color:#e6eef6}
    .wrap{max-width:980px;margin:28px auto;padding:20px}
    h1{margin:0;font-size:20px}
    .card{background:var(--card);padding:16px;border-radius:12px;box-shadow:0 6px 24px rgba(2,6,23,0.6);margin-top:18px}
    label{display:block;font-size:13px;color:var(--muted);margin-bottom:6px}
    input[type=url]{width:100%;padding:10px;border-radius:8px;border:1px solid rgba(255,255,255,0.04);background:var(--glass);color:inherit}
    button{background:var(--accent);border:none;color:#012;padding:10px 12px;border-radius:8px;cursor:pointer;font-weight:600}
    .row{display:flex;gap:12px;align-items:center}
    .logs{height:260px;overflow:auto;background:#021024;border-radius:8px;padding:12px;font-family:monospace;font-size:13px;color:#cfeaff;white-space:pre-wrap}
    .meta{display:flex;gap:12px;align-items:center;margin-top:12px}
    .muted{color:var(--muted);font-size:13px}
    .screenshot{max-width:100%;border-radius:8px;border:1px solid rgba(255,255,255,0.04);margin-top:12px}
    .actions{display:flex;gap:8px;margin-left:auto}
    footer{margin-top:18px;color:var(--muted);font-size:12px}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Ghost Advance — Playwright Website Cloner</h1>
    <small class="muted">Ephemeral one-shot mirror. No persistent history is stored.</small>

    <div class="card">
      <label for="url">Website URL</label>
      <div class="row">
        <input id="url" type="url" placeholder="https://example.com/">
        <div class="actions">
          <button id="startBtn">Start Cloning</button>
          <button id="clearBtn">Clear Logs</button>
        </div>
      </div>

      <div class="meta" style="margin-top:12px">
        <div><span class="muted">Status:</span> <span id="status">idle</span></div>
        <div id="savedCount" class="muted"></div>
        <div style="margin-left:auto"><span id="jobLinks"></span></div>
      </div>

      <div style="margin-top:12px">
        <div class="logs" id="logs">Logs will appear here...</div>
      </div>

      <div id="screenshotArea"></div>
    </div>

    <footer>
      Tip: For large sites, use dedicated mirroring tools. This snapshot is ephemeral and short-lived.
    </footer>
  </div>

<script>
let jobid = null;
let pollTimer = null;

document.getElementById('startBtn').addEventListener('click', async () => {
  const url = document.getElementById('url').value.trim();
  if (!url) { alert('Enter a URL'); return; }
  startJob(url);
});

document.getElementById('clearBtn').addEventListener('click', () => {
  document.getElementById('logs').innerText = '';
  document.getElementById('screenshotArea').innerHTML = '';
  document.getElementById('status').innerText = 'idle';
  jobid = null;
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  document.getElementById('jobLinks').innerHTML = '';
});

async function startJob(url) {
  document.getElementById('logs').innerText = 'Submitting job...';
  const res = await fetch('/start', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({url})
  });
  const j = await res.json();
  if (!j.ok) {
    document.getElementById('logs').innerText = 'Error: ' + (j.error || 'unknown');
    return;
  }
  jobid = j.jobid;
  document.getElementById('status').innerText = 'processing';
  document.getElementById('logs').innerText = '';
  startPolling();
}

function startPolling(){
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async ()=>{
    if (!jobid) return;
    try {
      const [lres, sres] = await Promise.all([fetch('/logs/'+jobid), fetch('/status/'+jobid)]);
      const lj = await lres.json();
      const sj = await sres.json();
      if (lj.ok) {
        document.getElementById('logs').innerText = lj.logs.join('\\n');
        const el = document.getElementById('logs');
        el.scrollTop = el.scrollHeight;
      }
      if (sj.ok) {
        document.getElementById('status').innerText = sj.status;
        if (sj.saved_count !== undefined) {
          document.getElementById('savedCount').innerText = 'Saved: ' + sj.saved_count;
        }
        const links = document.getElementById('jobLinks');
        links.innerHTML = '';
        if (sj.status === 'done') {
          links.innerHTML = `<a href="/download/${jobid}" target="_blank" style="color:#99f">Download ZIP</a>`;
          const ss = document.getElementById('screenshotArea');
          ss.innerHTML = `<img src="/screenshot/${jobid}" class="screenshot" alt="screenshot">`;
        } else if (sj.status === 'error') {
          links.innerText = 'error';
        }
      }
    } catch (e) {
      console.error(e);
    }
  }, 1200);
}
</script>
</body>
</html>
"""

# API routes
@app.route('/')
def index():
    return render_template_string(INDEX_HTML)

@app.route('/start', methods=['POST'])
def start():
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'ok': False, 'error': 'no url provided'}), 400
    if not re.match(r'^https?://', url):
        return jsonify({'ok': False, 'error': 'url must start with http:// or https://'}), 400

    jobid = hashlib.sha1(f"{url}-{time.time()}-{uuid.uuid4()}".encode('utf-8')).hexdigest()[:16]
    job = {
        'id': jobid,
        'url': url,
        'status': 'queued',
        'logs': [f"Queued job {jobid} for {url}"],
        'tmpdir': None,
        'screenshot': None,
        'zip_bytes': None,
        'saved_count': 0,
        'started_at': None,
        'finished_at': None,
    }
    with JOBS_LOCK:
        JOBS[jobid] = job

    t = threading.Thread(target=lambda: run_mirror_job(jobid, url), daemon=True)
    t.start()
    return jsonify({'ok': True, 'jobid': jobid})

@app.route('/logs/<jobid>')
def logs(jobid):
    with JOBS_LOCK:
        job = JOBS.get(jobid)
        if not job:
            return jsonify({'ok': False, 'error': 'job not found'}), 404
        return jsonify({'ok': True, 'logs': job.get('logs', [])})

@app.route('/status/<jobid>')
def status(jobid):
    with JOBS_LOCK:
        job = JOBS.get(jobid)
        if not job:
            return jsonify({'ok': False, 'error': 'job not found'}), 404
        return jsonify({
            'ok': True,
            'status': job.get('status'),
            'saved_count': job.get('saved_count', 0),
        })

@app.route('/screenshot/<jobid>')
def screenshot(jobid):
    with JOBS_LOCK:
        job = JOBS.get(jobid)
        if not job:
            abort(404)
        shot = job.get('screenshot')
        if not shot or not os.path.exists(shot):
            abort(404)
        return send_file(shot, mimetype='image/png')

@app.route('/download/<jobid>')
def download(jobid):
    with JOBS_LOCK:
        job = JOBS.get(jobid)
        if not job:
            abort(404)
        if job.get('status') != 'done' or not job.get('zip_bytes'):
            abort(404)
        data = job['zip_bytes']
    return send_file(
        io.BytesIO(data),
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'ghost_clone_{jobid}.zip'
    )

# Run server
if __name__ == '__main__':
    # Attempt to install browsers at startup (best to have done this during build)
    try:
        ok, msg = ensure_playwright_browsers()
        if not ok:
            print("Playwright/browsers not ready:", msg, file=sys.stderr)
            print("You can run: python -m playwright install chromium", file=sys.stderr)
    except Exception as e:
        print("Playwright install check failed:", e, file=sys.stderr)

    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
