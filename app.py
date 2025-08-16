# app.py
#!/usr/bin/env python3
"""
Ghost Advance — Selenium Website Cloner (single-file)
- Web UI to mirror a page into an ephemeral folder (no persistent history).
- Uses Selenium + Chrome/Chromium. You must have Chrome/Chromium installed on the host.
- webdriver-manager is used to automatically fetch a matching chromedriver binary.
- Run:
    pip install -r requirements.txt
    python app.py
Notes:
- On some hosts (containers), you may need to run Chrome with extra flags (--no-sandbox, --disable-dev-shm-usage).
- This app is designed for quick ephemeral snapshots. Large sites may take a long time or fail due to resource/time limits.
"""
import os
import re
import io
import sys
import time
import json
import zipfile
import uuid
import hashlib
import tempfile
import threading
import traceback
import mimetypes
import shutil
from urllib.parse import urljoin, urlparse, unquote

import requests
from flask import Flask, request, jsonify, send_file, render_template_string, abort

# Selenium imports
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

HEADERS = {'User-Agent': 'Mozilla/5.0 (mirror script)'}

app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False

# In-memory job store
JOBS = {}
JOBS_LOCK = threading.Lock()
CLEANUP_SECONDS = 15 * 60  # seconds after completion to keep data

# ---------------- Utilities ----------------
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

# ---------------- Mirror worker (Selenium) ----------------
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

    # Setup Selenium Chrome options
    options = Options()
    options.add_argument('--headless=new')  # use new headless where available
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--log-level=3')
    options.add_experimental_option('excludeSwitches', ['enable-logging'])
    # Enable performance logging to capture network events
    options.set_capability('goog:loggingPrefs', {'performance': 'ALL'})

    try:
        # Install or find chromedriver via webdriver-manager
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
    except Exception as e:
        job['logs'].append(f"ERROR launching Chrome driver: {e}")
        job['logs'].append(traceback.format_exc())
        job['status'] = 'error'
        job['finished_at'] = time.time()
        return

    try:
        job['logs'].append("Chrome started. Navigating to URL...")
        driver.set_page_load_timeout(60)
        try:
            driver.get(url)
        except Exception as e:
            # page load may timeout but we still want to try to collect resources/page_source
            job['logs'].append(f"Note: page.get() raised: {e}")

        # small wait for dynamic loads
        driver.implicitly_wait(2)
        time.sleep(1.5)

        # 1) Collect resource URLs via Chrome performance logs (Network.requestWillBeSent)
        resource_urls = set()
        try:
            for entry in driver.get_log('performance'):
                try:
                    msg = json.loads(entry['message'])['message']
                    if msg.get('method') == 'Network.requestWillBeSent':
                        req_url = msg.get('params', {}).get('request', {}).get('url', '')
                        if req_url.startswith('http'):
                            resource_urls.add(req_url)
                except Exception:
                    continue
            job['logs'].append(f"Collected {len(resource_urls)} resources from performance log.")
        except Exception as e:
            job['logs'].append(f"Warn: could not get performance logs: {e}")

        # 2) Scrape final DOM for common resource attributes
        try:
            html = driver.page_source
            attrs = re.findall(
                r'(?:src|href|poster|action|data-src)\s*=\s*["\'](.*?)["\']',
                html,
                flags=re.I
            )
            added = 0
            for attr in attrs:
                abs_url = urljoin(url, attr)
                if abs_url.startswith('http') and abs_url not in resource_urls:
                    resource_urls.add(abs_url)
                    added += 1
            job['logs'].append(f"Scraped DOM and added {added} resources.")
        except Exception as e:
            html = ""
            job['logs'].append(f"Warn: could not read page_source: {e}")

        # 3) Use JS performance entries (XHR/fetch etc)
        try:
            entries = driver.execute_script(
                'return window.performance.getEntriesByType("resource").map(e=>e.name)'
            )
            added = 0
            for u in entries:
                if isinstance(u, str) and u.startswith('http') and u not in resource_urls:
                    resource_urls.add(u)
                    added += 1
            job['logs'].append(f"Performance entries added {added} resources.")
        except Exception:
            pass

        # Ensure we include the page URL itself
        resource_urls.add(url)

        # Download each resource
        url_to_local = {}
        job['logs'].append(f"Downloading {len(resource_urls)} resources...")
        for remote_url in sorted(resource_urls):
            try:
                parsed = urlparse(remote_url)
                path = parsed.path.lstrip('/') or 'index'
                local_path = os.path.join(tmpdir, parsed.netloc, path)
                ensure_parent(local_path)

                # Add extension if missing by checking HEAD content-type
                try:
                    h = requests.head(remote_url, headers=HEADERS, allow_redirects=True, timeout=10)
                    ctype = h.headers.get('content-type', '').split(';')[0]
                    ext = mimetypes.guess_extension(ctype) or ''
                    if ext and not local_path.lower().endswith(ext.lower()):
                        local_path += ext
                except Exception:
                    pass

                if os.path.exists(local_path):
                    url_to_local[remote_url] = os.path.relpath(local_path, tmpdir).replace(os.sep, '/')
                    continue

                job['logs'].append(f"⬇ {remote_url}")
                r = requests.get(remote_url, headers=HEADERS, stream=True, timeout=20)
                r.raise_for_status()
                with open(local_path, 'wb') as f:
                    for chunk in r.iter_content(8192):
                        if not chunk:
                            continue
                        f.write(chunk)
                url_to_local[remote_url] = os.path.relpath(local_path, tmpdir).replace(os.sep, '/')
                job['saved_count'] += 1
            except Exception as e:
                job['logs'].append(f"⚠ skip {remote_url} -> {e}")

        # If we have page HTML (from earlier), rewrite resource URLs to local paths
        try:
            if not html:
                html = driver.page_source or ''
            def replace_url(match):
                orig = match.group(2)
                abs_url = urljoin(url, orig)
                local = url_to_local.get(abs_url, orig)
                return match.group(1) + local + match.group(3)

            new_html = re.sub(
                r'(\b(?:src|href|poster|action)\s*=\s*["\'])(.*?)(["\'])',
                replace_url,
                html,
                flags=re.I,
            )
            # rewrite CSS url(...)
            new_html = re.sub(
                r'(?i)(url\(["\']?)(.*?)["\']?\)',
                lambda m: 'url(' + url_to_local.get(urljoin(url, m.group(2)), m.group(2)) + ')',
                new_html,
            )

            index_path = os.path.join(tmpdir, urlparse(url).netloc, 'index.html')
            ensure_parent(index_path)
            with open(index_path, 'w', encoding='utf-8') as f:
                f.write(new_html)
            job['logs'].append(f"Wrote rewritten HTML to {os.path.relpath(index_path, tmpdir)}")
        except Exception as e:
            job['logs'].append(f"Warn: failed to rewrite/save HTML: {e}")

        # Save screenshot
        try:
            shot_path = os.path.join(tmpdir, 'screenshot.png')
            driver.save_screenshot(shot_path)
            job['screenshot'] = shot_path
            job['logs'].append("Saved screenshot.")
        except Exception as e:
            job['logs'].append(f"Warn: screenshot failed: {e}")

        # Package into ZIP
        job['logs'].append("Packaging into ZIP...")
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
        try:
            driver.quit()
        except Exception:
            pass

# ---------------- Cleanup thread ----------------
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

# ---------------- Web UI HTML ----------------
INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Ghost Advance — Selenium Cloner</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    :root{--bg:#071026;--card:#0b1220;--muted:#94a3b8;--accent:#06b6d4;--glass:rgba(255,255,255,0.03)}
    body{margin:0;font-family:Inter,system-ui,Segoe UI,Arial;background:linear-gradient(180deg,#071024 0%, #071530 100%);color:#e6eef6}
    .wrap{max-width:980px;margin:28px auto;padding:20px}
    header{display:flex;align-items:center;gap:16px}
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
    <header>
      <div>
        <h1>Ghost Advance — Selenium Website Cloner</h1>
        <small class="muted">Ephemeral one-shot mirror. No persistent history is stored.</small>
      </div>
    </header>

    <div class="card" id="ui-card">
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
      Tip: For very large or complex sites, consider using specialized tools. This snapshot is ephemeral and short-lived.
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

# ---------------- Routes ----------------
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

# ---------------- Run ----------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
