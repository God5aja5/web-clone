# (Start of file)
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
from urllib.parse import urlparse, urljoin, unquote, urldefrag
from collections import deque

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

def make_local_path(root: str, raw_url: str, snapshot_idx: int = 0) -> str:
    """
    Map a URL to a local path under tmpdir. Snapshot idx used to separate snapshots.
    """
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
    # place under snapshot folder
    local = os.path.join(root, f"snapshot_{snapshot_idx}", netloc, path)
    return local

def ensure_parent(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)

def ext_from_content_type(ct: str):
    if not ct:
        return ''
    return mimetypes.guess_extension(ct.split(';', 1)[0].strip() or '') or ''

def normalize_url(base: str, link: str):
    if not link:
        return None
    link = link.strip()
    # ignore javascript:, mailto:, tel:, data:
    if re.match(r'^(javascript:|mailto:|tel:|data:)', link, re.I):
        return None
    # remove fragments
    try:
        joined = urljoin(base, link)
        joined = urldefrag(joined)[0]
        return joined
    except Exception:
        return None

def same_host(base: str, url: str):
    try:
        b = urlparse(base).netloc.split(':')[0].lower()
        u = urlparse(url).netloc.split(':')[0].lower()
        return b == u
    except Exception:
        return False

def ensure_playwright_browsers():
    """
    Ensure Playwright browsers are installed. Prefer doing this at build time,
    but if not installed, try to run: python -m playwright install chromium
    """
    global sync_playwright
    if sync_playwright is None:
        try:
            # Try import again
            from playwright.sync_api import sync_playwright as sp, TimeoutError as PlaywrightTimeoutLocal
            sync_playwright = sp
        except Exception:
            pass

    if sync_playwright is None:
        return False, "playwright package not installed. pip install -r requirements.txt"

    # Test launching
    try:
        with PLAYWRIGHT_LOCK:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                browser.close()
        return True, None
    except Exception as e:
        # Try install
        try:
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
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

    # parameters
    advanced = job.get('advanced', False)
    max_pages = int(job.get('max_pages', 50) or 50)
    snapshots = int(job.get('snapshots', 1) or 1)
    same_host_only = bool(job.get('same_host_only', True))
    follow_buttons = bool(job.get('follow_buttons', False))
    per_page_timeout = int(job.get('per_page_timeout', 30) or 30)

    job['logs'].append(f"Options: advanced={advanced}, max_pages={max_pages}, snapshots={snapshots}, same_host_only={same_host_only}, follow_buttons={follow_buttons}")

    try:
        with PLAYWRIGHT_LOCK:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
                context = browser.new_context(ignore_https_errors=True)
                # set default extra headers if needed
                try:
                    context.set_extra_http_headers(HEADERS)
                except Exception:
                    pass

                # For basic mode (existing behavior): just load landing page and save resources once
                if not advanced:
                    page = context.new_page()
                    job['logs'].append("Browser launched. Attaching response handler...")

                    def handle_response_simple(response):
                        try:
                            rurl = response.url
                            if not isinstance(rurl, str) or not rurl.startswith('http'):
                                return
                            try:
                                body = response.body()
                            except Exception:
                                return
                            if not body:
                                return
                            local = make_local_path(tmpdir, rurl, snapshot_idx=0)
                            ensure_parent(local)
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

                    page.on("response", handle_response_simple)
                    try:
                        page.goto(url, timeout=per_page_timeout * 1000, wait_until="domcontentloaded")
                        job['logs'].append("DOM content loaded.")
                    except PlaywrightTimeout:
                        job['logs'].append("Note: page.goto timed out; continuing.")
                    except Exception as e:
                        job['logs'].append(f"Note: page.goto error: {e}")
                    page.wait_for_timeout(2000)
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
                    # screenshot
                    try:
                        shot_path = os.path.join(tmpdir, 'screenshot.png')
                        page.screenshot(path=shot_path, full_page=False)
                        job['screenshot'] = shot_path
                        job['logs'].append("Saved screenshot.")
                    except Exception as e:
                        job['logs'].append(f"Warn: screenshot failed: {e}")
                    page.close()
                else:
                    # Advanced crawling mode
                    job['logs'].append("Advanced crawling enabled. Starting BFS crawl(s).")
                    # We'll perform 'snapshots' separate crawl runs. Each snapshot gets its own folder.
                    total_saved = 0
                    for sidx in range(snapshots):
                        job['logs'].append(f"Snapshot {sidx+1}/{snapshots} starting...")
                        # Use a fresh context for each snapshot to avoid cross-contamination
                        try:
                            scontext = browser.new_context(ignore_https_errors=True)
                            scontext.set_extra_http_headers(HEADERS)
                        except Exception:
                            scontext = context
                        page = scontext.new_page()
                        # response handler for this snapshot
                        def make_handle_response(snapshot_index):
                            def handle_response(response):
                                try:
                                    rurl = response.url
                                    if not isinstance(rurl, str) or not rurl.startswith('http'):
                                        return
                                    try:
                                        body = response.body()
                                    except Exception:
                                        return
                                    if not body:
                                        return
                                    local = make_local_path(tmpdir, rurl, snapshot_idx=snapshot_index)
                                    ensure_parent(local)
                                    if os.path.splitext(local)[1] == '':
                                        ctype = response.headers.get('content-type', '')
                                        ext = ext_from_content_type(ctype)
                                        if ext:
                                            local += ext
                                    # Avoid re-writing identical file content repeatedly; use hashing
                                    if os.path.exists(local):
                                        # quick size check
                                        if os.path.getsize(local) == len(body):
                                            return
                                    with open(local, 'wb') as fh:
                                        fh.write(body)
                                    job['saved_count'] += 1
                                    job['logs'].append(f"[S{sidx+1}] Saved resource: {rurl} -> {os.path.relpath(local, tmpdir)}")
                                except Exception as e:
                                    job['logs'].append(f"Warn: failed saving response {getattr(response,'url', 'unknown')}: {e}")
                            return handle_response

                        page.on("response", make_handle_response(sidx))

                        # BFS queue
                        q = deque()
                        visited = set()
                        q.append(url)
                        visited.add(url)
                        pages_crawled = 0

                        while q and pages_crawled < max_pages:
                            cur = q.popleft()
                            job['logs'].append(f"[S{sidx+1}] Crawling: {cur} (queued: {len(q)})")
                            try:
                                page.goto(cur, timeout=per_page_timeout * 1000, wait_until="domcontentloaded")
                            except PlaywrightTimeout:
                                job['logs'].append(f"[S{sidx+1}] Note: goto timeout for {cur}")
                            except Exception as e:
                                job['logs'].append(f"[S{sidx+1}] goto error for {cur}: {e}")
                            # small wait for XHR/fetch
                            page.wait_for_timeout(1000 + (sidx * 200))
                            # try to extract links from DOM
                            try:
                                # evaluate in page: return arrays of hrefs, srcs, forms, onclicks
                                data = page.evaluate("""() => {
                                    const out = {links: [], imgs: [], forms: [], onclicks: []};
                                    try {
                                        Array.from(document.querySelectorAll('a[href]')).forEach(a => out.links.push(a.getAttribute('href')));
                                        Array.from(document.querySelectorAll('area[href]')).forEach(a => out.links.push(a.getAttribute('href')));
                                        Array.from(document.querySelectorAll('img[src]')).forEach(i => out.imgs.push(i.getAttribute('src')));
                                        Array.from(document.querySelectorAll('link[href]')).forEach(l => out.links.push(l.getAttribute('href')));
                                        Array.from(document.querySelectorAll('script[src]')).forEach(s => out.links.push(s.getAttribute('src')));
                                        Array.from(document.querySelectorAll('form[action]')).forEach(f => out.forms.push(f.getAttribute('action')));
                                        Array.from(document.querySelectorAll('[onclick]')).forEach(e => out.onclicks.push(e.getAttribute('onclick') || ''));
                                        // buttons with data-href or aria-role link
                                        Array.from(document.querySelectorAll('button[data-href]')).forEach(b => out.links.push(b.getAttribute('data-href')));
                                        Array.from(document.querySelectorAll('[role=\"link\"][data-href]')).forEach(b => out.links.push(b.getAttribute('data-href')));
                                    } catch (e) {}
                                    return out;
                                }""")
                            except Exception as e:
                                job['logs'].append(f"[S{sidx+1}] DOM eval failed for {cur}: {e}")
                                data = {'links': [], 'imgs': [], 'forms': [], 'onclicks': []}

                            # get page HTML and save it
                            try:
                                html = page.content()
                                # save under snapshot_dir/netloc/path
                                parsed = urlparse(cur)
                                index_path = os.path.join(tmpdir, f"snapshot_{sidx}", parsed.netloc, (parsed.path.lstrip('/') or 'index.html'))
                                if index_path.endswith('/'):
                                    index_path = index_path + 'index.html'
                                if index_path.endswith('/'):
                                    index_path = index_path + 'index.html'
                                # ensure file ends with .html
                                if not os.path.splitext(index_path)[1]:
                                    index_path = index_path + '.html'
                                ensure_parent(index_path)
                                with open(index_path, 'w', encoding='utf-8') as fh:
                                    fh.write(html)
                                job['logs'].append(f"[S{sidx+1}] Saved HTML: {os.path.relpath(index_path, tmpdir)}")
                            except Exception as e:
                                job['logs'].append(f"[S{sidx+1}] Could not save HTML for {cur}: {e}")

                            # heuristic: from onclick attributes, try to extract URL patterns
                            extra_links = []
                            for oc in data.get('onclicks', []) or []:
                                # simplistic regex looking for location.href='...'
                                if not oc:
                                    continue
                                m = re.search(r'(?:location|window\.location|location\.href)\s*=\s*[\'"](.*?)[\'"]', oc)
                                if m:
                                    extra_links.append(m.group(1))
                                m2 = re.search(r'open\(\s*[\'"](.*?)[\'"]', oc)
                                if m2:
                                    extra_links.append(m2.group(1))
                            # combine discovered links
                            discovered = []
                            for L in (data.get('links') or []) + (data.get('imgs') or []) + (data.get('forms') or []) + extra_links:
                                if not L:
                                    continue
                                n = normalize_url(cur, L)
                                if n:
                                    # optionally ignore non-http
                                    if not n.startswith('http'):
                                        continue
                                    if same_host_only and not same_host(url, n):
                                        continue
                                    if n not in visited:
                                        discovered.append(n)
                            # enqueue discovered links
                            for n in discovered:
                                if len(visited) >= max_pages:
                                    break
                                if n not in visited:
                                    visited.add(n)
                                    q.append(n)

                            pages_crawled += 1

                        # After snapshot crawl, take a screenshot of the landing page if possible
                        try:
                            dest_shot = os.path.join(tmpdir, f"screenshot_snapshot_{sidx+1}.png")
                            page.screenshot(path=dest_shot, full_page=False)
                            # keep last snapshot's screenshot as job['screenshot']
                            job['screenshot'] = dest_shot
                            job['logs'].append(f"[S{sidx+1}] Screenshot saved.")
                        except Exception as e:
                            job['logs'].append(f"[S{sidx+1}] Screenshot failed: {e}")
                        # close page and context if we created new
                        try:
                            page.close()
                        except Exception:
                            pass
                        if scontext is not context:
                            try:
                                scontext.close()
                            except Exception:
                                pass
                        job['logs'].append(f"Snapshot {sidx+1} finished. Crawled up to {pages_crawled} pages.")
                        total_saved = job['saved_count']

                    job['logs'].append(f"Advanced crawling finished. Total resources saved: {total_saved}")
                    browser.close()

        # After browser closed (or after simple mode), attempt to rewrite HTMLs to local references
        try:
            # Build map of original URL -> local relpath across snapshots
            url_to_local = {}
            for root_dir, _, files in os.walk(tmpdir):
                for fn in files:
                    full = os.path.join(root_dir, fn)
                    rel = os.path.relpath(full, tmpdir).replace(os.sep, '/')
                    # attempt to reverse map: snapshot_X/netloc/path => https://netloc/path
                    parts = rel.split('/', 2)
                    if len(parts) >= 3 and parts[0].startswith('snapshot_'):
                        _, netloc, pth = parts[0], parts[1], parts[2]
                        possible_url = f"https://{netloc}/{pth}"
                        url_to_local[possible_url] = rel
                        # also store without https prefix (http)
                        possible_url2 = f"http://{netloc}/{pth}"
                        url_to_local[possible_url2] = rel
                    # also map root files (non-snapshot) if any
                    elif len(parts) >= 2:
                        netloc = parts[0]
                        pth = '/'.join(parts[1:])
                        possible_url = f"https://{netloc}/{pth}"
                        url_to_local[possible_url] = rel
                        url_to_local[f"http://{netloc}/{pth}"] = rel

            # Walk HTML files and rewrite references
            for root_dir, _, files in os.walk(tmpdir):
                for fn in files:
                    if not fn.lower().endswith(('.html', '.htm')):
                        continue
                    full = os.path.join(root_dir, fn)
                    try:
                        with open(full, 'r', encoding='utf-8') as fh:
                            html = fh.read()
                        base_url = None
                        # try to determine original base from path: /snapshot_X/netloc/...
                        rel = os.path.relpath(full, tmpdir).replace(os.sep, '/')
                        m = re.match(r'(snapshot_\d+)\/([^\/]+)\/(.*)', rel)
                        if m:
                            netloc = m.group(2)
                            base_url = f"https://{netloc}/"
                        else:
                            # fallback: use job url
                            base_url = url
                        # replace src/href/action/poster values
                        def repl_attr(m):
                            prefix = m.group(1)
                            orig = m.group(2)
                            quote = m.group(3)
                            abs_url = normalize_url(base_url, orig) or orig
                            local = url_to_local.get(abs_url)
                            if local:
                                return prefix + local + quote
                            # if relative local file exists, keep as-is
                            return prefix + orig + quote
                        new_html = re.sub(r'(\b(?:src|href|poster|action)\s*=\s*["\'])(.*?)(["\'])', repl_attr, html, flags=re.I)
                        # rewrite CSS url(...) occurrences
                        def repl_css(m):
                            prefix = m.group(1)
                            orig = m.group(2)
                            abs_url = normalize_url(base_url, orig) or orig
                            local = url_to_local.get(abs_url)
                            if local:
                                return 'url(' + local + ')'
                            return 'url(' + orig + ')'
                        new_html = re.sub(r'(?i)(url\(["\']?)(.*?)["\']?\)', repl_css, new_html)
                        # write back
                        with open(full, 'w', encoding='utf-8') as fh:
                            fh.write(new_html)
                        job['logs'].append(f"Rewrote HTML links in {os.path.relpath(full, tmpdir)}")
                    except Exception as e:
                        job['logs'].append(f"Warn: rewriting {full} failed: {e}")
        except Exception as e:
            job['logs'].append(f"Warn: rewriting HTMLs failed: {e}")

        # Package into ZIP
        job['logs'].append("Packaging files into ZIP...")
        zip_io = io.BytesIO()
        with zipfile.ZipFile(zip_io, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            for root_dir, _, files in os.walk(tmpdir):
                for fn in files:
                    full = os.path.join(root_dir, fn)
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

# HTML UI (extended with advanced options)
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
    input[type=url], input[type=number], input[type=text] {width:100%;padding:10px;border-radius:8px;border:1px solid rgba(255,255,255,0.04);background:var(--glass);color:inherit}
    button{background:var(--accent);border:none;color:#012;padding:10px 12px;border-radius:8px;cursor:pointer;font-weight:600}
    .row{display:flex;gap:12px;align-items:center}
    .logs{height:260px;overflow:auto;background:#021024;border-radius:8px;padding:12px;font-family:monospace;font-size:13px;color:#cfeaff;white-space:pre-wrap}
    .meta{display:flex;gap:12px;align-items:center;margin-top:12px}
    .muted{color:var(--muted);font-size:13px}
    .screenshot{max-width:100%;border-radius:8px;border:1px solid rgba(255,255,255,0.04);margin-top:12px}
    .actions{display:flex;gap:8px;margin-left:auto}
    footer{margin-top:18px;color:var(--muted);font-size:12px}
    .opts{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}
    .small{font-size:13px;color:var(--muted)}
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

      <div class="opts">
        <div>
          <label><input type="checkbox" id="advanced"> Advanced cloning (follow links, images, pages)</label>
          <div class="small">When on, the crawler will attempt to discover and save multiple pages. Use with caution.</div>
        </div>
        <div>
          <label for="max_pages">Max pages (advanced)</label>
          <input id="max_pages" type="number" value="50" min="1" max="1000">
        </div>
        <div>
          <label for="snapshots">Snapshots (repeat crawls)</label>
          <input id="snapshots" type="number" value="1" min="1" max="10">
        </div>
        <div>
          <label><input type="checkbox" id="same_host" checked> Same-host only</label>
          <div class="small">Restrict crawling to the initial hostname (recommended)</div>
        </div>
        <div>
          <label><input type="checkbox" id="follow_buttons"> Follow data-href / onclick heuristics</label>
        </div>
        <div>
          <label for="per_page_timeout">Per-page timeout (s)</label>
          <input id="per_page_timeout" type="number" value="30" min="5" max="120">
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
  const payload = {
    url,
    advanced: document.getElementById('advanced').checked,
    max_pages: Number(document.getElementById('max_pages').value || 50),
    snapshots: Number(document.getElementById('snapshots').value || 1),
    same_host_only: document.getElementById('same_host').checked,
    follow_buttons: document.getElementById('follow_buttons').checked,
    per_page_timeout: Number(document.getElementById('per_page_timeout').value || 30)
  };
  const res = await fetch('/start', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(payload)
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
        # options
        'advanced': bool(data.get('advanced', False)),
        'max_pages': int(data.get('max_pages', 50) or 50),
        'snapshots': int(data.get('snapshots', 1) or 1),
        'same_host_only': bool(data.get('same_host_only', True)),
        'follow_buttons': bool(data.get('follow_buttons', False)),
        'per_page_timeout': int(data.get('per_page_timeout', 30) or 30),
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
# (End of file)
