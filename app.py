#!/usr/bin/env python3
"""
app.py — Ghost Advance (single-file) website cloner using Playwright + Flask.

Save this file as app.py. To run:
  python -m venv venv
  source venv/bin/activate   (Windows: venv\Scripts\activate)
  pip install flask playwright
  python -m playwright install chromium
  python app.py

Open http://localhost:5000
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
import queue
import urllib.robotparser

from flask import Flask, request, jsonify, send_file, render_template_string, abort

# Playwright (lazy import)
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
except Exception:
    sync_playwright = None
    PlaywrightTimeout = Exception

app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False

JOBS = {}
JOBS_LOCK = threading.Lock()
CLEANUP_SECONDS = 15 * 60
HEADERS = {'User-Agent': 'Mozilla/5.0 (ghost-clone)'}

# Helpers
def safe_component(s: str) -> str:
    s = unquote(s or "")
    return re.sub(r'[^A-Za-z0-9._\-/]', '_', s)

def make_local_path(root: str, raw_url: str, snapshot_idx: int = 0, subfolder: str = None) -> str:
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
    parts = [root, f"snapshot_{snapshot_idx}", netloc]
    if subfolder:
        parts.append(subfolder)
    parts.append(path)
    return os.path.join(*parts)

def ensure_parent(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)

def ext_from_content_type(ct: str):
    if not ct:
        return ''
    ext = mimetypes.guess_extension(ct.split(';', 1)[0].strip() or '')
    return ext or ''

def normalize_url(base: str, link: str):
    if not link:
        return None
    link = link.strip()
    if re.match(r'^(javascript:|mailto:|tel:|data:)', link, re.I):
        return None
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
    global sync_playwright
    if sync_playwright is None:
        try:
            from playwright.sync_api import sync_playwright as sp, TimeoutError as PlaywrightTimeoutLocal
            sync_playwright = sp
        except Exception:
            pass
    if sync_playwright is None:
        return False, "playwright package not installed. pip install playwright"
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            b.close()
        return True, None
    except Exception as e:
        try:
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True)
                b.close()
            return True, None
        except Exception as ex2:
            return False, f"Playwright browser launch failed: {ex2} (original: {e})"

# Worker
def worker_func(jobid, url_queue, visited_set, visited_lock, job_opts, tmpdir, snapshot_idx, job_ref, stop_event):
    try:
        with sync_playwright() as p:
            mobile_device = p.devices.get('iPhone 12') or p.devices.get('iPhone 11') or {}
            try:
                browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
            except Exception:
                browser = p.chromium.launch(headless=True)
            desktop_ctx = browser.new_context(ignore_https_errors=True)
            try:
                mobile_ctx = browser.new_context(ignore_https_errors=True, **(mobile_device or {}))
            except Exception:
                mobile_ctx = browser.new_context(ignore_https_errors=True)
            try:
                desktop_ctx.set_extra_http_headers(HEADERS)
            except Exception:
                pass
            try:
                mobile_ctx.set_extra_http_headers(HEADERS)
            except Exception:
                pass

            while not stop_event.is_set():
                try:
                    cur, depth = url_queue.get(timeout=2)
                except queue.Empty:
                    break

                # runtime/resource caps
                if job_opts.get('max_runtime') and job_opts['max_runtime'] > 0:
                    if time.time() - job_opts['job_start_time'] > job_opts['max_runtime']:
                        with JOBS_LOCK:
                            job_ref['logs'].append("Worker: job max runtime reached.")
                        url_queue.task_done()
                        break
                if job_opts.get('max_resources') and job_opts['max_resources'] > 0 and job_ref.get('saved_count',0) >= job_opts['max_resources']:
                    with JOBS_LOCK:
                        job_ref['logs'].append("Worker: resource cap reached.")
                    url_queue.task_done()
                    break

                with JOBS_LOCK:
                    job_ref['logs'].append(f"[worker] Processing {cur} (depth={depth})")

                rp = job_opts.get('robots_parser')
                if rp:
                    try:
                        ua = HEADERS.get('User-Agent','*')
                        if not rp.can_fetch(ua, cur):
                            with JOBS_LOCK:
                                job_ref['logs'].append(f"[worker] robots.txt disallows {cur}; skipping.")
                            url_queue.task_done()
                            continue
                    except Exception:
                        pass

                discovered_links = set()
                discovered_endpoints = set()

                for label, ctx in (('desktop', desktop_ctx), ('mobile', mobile_ctx)):
                    try:
                        page = ctx.new_page()
                    except Exception as e:
                        with JOBS_LOCK:
                            job_ref['logs'].append(f"[{label}] open page failed: {e}")
                        continue

                    def handle_resp(resp):
                        try:
                            rurl = resp.url
                            if not isinstance(rurl, str) or not rurl.startswith('http'):
                                return
                            try:
                                body = resp.body()
                            except Exception:
                                return
                            if body is None:
                                return
                            headers = resp.headers or {}
                            local = make_local_path(tmpdir, rurl, snapshot_idx=snapshot_idx, subfolder='assets')
                            ensure_parent(local)
                            if os.path.splitext(local)[1] == '':
                                ext = ext_from_content_type(headers.get('content-type',''))
                                if ext:
                                    local += ext
                            if os.path.exists(local) and os.path.getsize(local) == len(body):
                                return
                            with open(local, 'wb') as fh:
                                fh.write(body)
                            rel = os.path.relpath(local, tmpdir).replace(os.sep, '/')
                            with JOBS_LOCK:
                                job_ref['saved_count'] = job_ref.get('saved_count',0) + 1
                                job_ref['logs'].append(f"[{label}] Saved: {rurl} -> {rel}")
                                job_ref.setdefault('url_to_local', {})[rurl] = rel
                        except Exception as e:
                            with JOBS_LOCK:
                                job_ref['logs'].append(f"[{label}] resp save error: {e}")

                    page.on("response", handle_resp)

                    def handle_req(req):
                        try:
                            ru = req.url
                            if isinstance(ru,str) and ru.startswith('http'):
                                rt = req.resource_type
                                if rt in ('xhr','fetch','document','script'):
                                    discovered_endpoints.add(ru)
                        except Exception:
                            pass
                    page.on("request", handle_req)

                    main_resp = None
                    try:
                        main_resp = page.goto(cur, timeout=max(1, int(job_opts.get('per_page_timeout',30)))*1000, wait_until="domcontentloaded")
                    except PlaywrightTimeout:
                        with JOBS_LOCK:
                            job_ref['logs'].append(f"[{label}] timeout loading {cur}")
                    except Exception as e:
                        with JOBS_LOCK:
                            job_ref['logs'].append(f"[{label}] error loading {cur}: {e}")

                    try:
                        page.wait_for_timeout(800)
                    except Exception:
                        pass

                    if job_opts.get('follow_buttons'):
                        try:
                            elems = page.query_selector_all('button[data-href], [role="link"][data-href], a[data-auto-click]')
                            for e in elems[:5]:
                                try:
                                    dh = e.get_attribute('data-href')
                                    if dh:
                                        target = normalize_url(cur, dh)
                                        if target:
                                            discovered_links.add(target)
                                except Exception:
                                    pass
                        except Exception:
                            pass

                    # Decide whether to save page content as HTML
                    save_html = False
                    try:
                        content_type = None
                        if main_resp:
                            headers = getattr(main_resp, 'headers', {}) or {}
                            content_type = headers.get('content-type', '')
                        parsed = urlparse(cur)
                        ext = os.path.splitext(parsed.path)[1].lower()
                        is_directory = parsed.path.endswith('/') or parsed.path == '' or ext == ''
                        if content_type and 'text/html' in content_type.lower():
                            save_html = True
                        elif is_directory:
                            save_html = True
                    except Exception:
                        save_html = True

                    if save_html:
                        try:
                            html = page.content()
                        except Exception:
                            html = ''
                        try:
                            parsed = urlparse(cur)
                            relpath = parsed.path.lstrip('/') or 'index.html'
                            if relpath.endswith('/'):
                                relpath = relpath + 'index.html'
                            if not os.path.splitext(relpath)[1]:
                                relpath = relpath + '.html'
                            local_html = os.path.join(tmpdir, f"snapshot_{snapshot_idx}", parsed.netloc, relpath)
                            ensure_parent(local_html)
                            with open(local_html, 'w', encoding='utf-8') as fh:
                                fh.write(html)
                            rel = os.path.relpath(local_html, tmpdir).replace(os.sep, '/')
                            with JOBS_LOCK:
                                job_ref['logs'].append(f"[{label}] Saved HTML: {rel}")
                                # only set HTML mapping if asset mapping doesn't already exist
                                job_ref.setdefault('url_to_local', {}).setdefault(cur, rel)
                        except Exception as e:
                            with JOBS_LOCK:
                                job_ref['logs'].append(f"[{label}] html save warn: {e}")
                    else:
                        with JOBS_LOCK:
                            job_ref['logs'].append(f"[{label}] Skipped saving HTML for non-HTML resource: {cur}")

                    if job_opts.get('auto_snapshot'):
                        try:
                            shot_name = os.path.join(tmpdir, f"screenshot_snapshot_{snapshot_idx}_{label}.png")
                            page.screenshot(path=shot_name, full_page=False)
                            with JOBS_LOCK:
                                job_ref['screenshot'] = shot_name
                                job_ref['logs'].append(f"[{label}] screenshot saved: {os.path.relpath(shot_name, tmpdir)}")
                        except Exception as e:
                            with JOBS_LOCK:
                                job_ref['logs'].append(f"[{label}] screenshot failed: {e}")

                    try:
                        data = page.evaluate("""() => {
                            const out = {links: [], imgs: [], forms: [], onclicks: [], scripts: []};
                            try {
                                Array.from(document.querySelectorAll('a[href]')).forEach(a => out.links.push(a.getAttribute('href')));
                                Array.from(document.querySelectorAll('img[src]')).forEach(i => out.imgs.push(i.getAttribute('src')));
                                Array.from(document.querySelectorAll('link[href]')).forEach(l => out.links.push(l.getAttribute('href')));
                                Array.from(document.querySelectorAll('script[src]')).forEach(s => out.scripts.push(s.getAttribute('src')));
                                Array.from(document.querySelectorAll('script:not([src])')).forEach(s => out.scripts.push(s.textContent || ''));
                                Array.from(document.querySelectorAll('form[action]')).forEach(f => out.forms.push(f.getAttribute('action')));
                                Array.from(document.querySelectorAll('[onclick]')).forEach(e => out.onclicks.push(e.getAttribute('onclick') || ''));
                            } catch (e) {}
                            return out;
                        }""")
                    except Exception:
                        data = {'links': [], 'imgs': [], 'forms': [], 'onclicks': [], 'scripts': []}

                    for L in (data.get('links') or []) + (data.get('imgs') or []) + (data.get('forms') or []):
                        if not L:
                            continue
                        n = normalize_url(cur, L)
                        if n and n.startswith('http'):
                            if job_opts.get('same_host_only') and not same_host(job_opts['start_url'], n):
                                continue
                            discovered_links.add(n)

                    for oc in (data.get('onclicks') or []):
                        if not oc:
                            continue
                        m = re.search(r'(?:location|window\.location|location\.href)\s*=\s*[\'"](.*?)[\'"]', oc)
                        if m:
                            candidate = normalize_url(cur, m.group(1))
                            if candidate:
                                discovered_links.add(candidate)
                        m2 = re.search(r'open\(\s*[\'"](.*?)[\'"]', oc)
                        if m2:
                            candidate = normalize_url(cur, m2.group(1))
                            if candidate:
                                discovered_links.add(candidate)

                    for st in (data.get('scripts') or []):
                        if not st:
                            continue
                        for m in re.finditer(r'(https?://[^\s"\'<>]+)', st):
                            discovered_endpoints.add(m.group(1))
                        for m in re.finditer(r'(["\'])(/[^"\']*?/api/[^"\']*)(["\'])', st, flags=re.I):
                            candidate = m.group(2)
                            candidate_abs = normalize_url(cur, candidate)
                            if candidate_abs:
                                discovered_endpoints.add(candidate_abs)

                    with visited_lock:
                        if discovered_endpoints:
                            job_ref.setdefault('endpoints', set()).update(discovered_endpoints)
                        for n in discovered_links:
                            if n in visited_set:
                                continue
                            if job_opts.get('depth_limit') and job_opts['depth_limit'] > 0 and depth+1 > job_opts['depth_limit']:
                                continue
                            visited_set.add(n)
                            try:
                                url_queue.put((n, depth+1))
                            except Exception:
                                pass

                    try:
                        page.close()
                    except Exception:
                        pass

                url_queue.task_done()

            try:
                desktop_ctx.close()
            except Exception:
                pass
            try:
                mobile_ctx.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
    except Exception as e:
        with JOBS_LOCK:
            job_ref['logs'].append("Worker encountered error: " + str(e))
            job_ref['logs'].append(traceback.format_exc())

# Main runner
def run_mirror_job(jobid: str, start_url: str):
    with JOBS_LOCK:
        job = JOBS.get(jobid)
    if not job:
        return

    job['logs'].append(f"Job {jobid} starting for {start_url}")
    job['status'] = 'running'
    job['started_at'] = time.time()
    tmpdir = tempfile.mkdtemp(prefix='ghost_clone_')
    job['tmpdir'] = tmpdir

    ok, msg = ensure_playwright_browsers()
    if not ok:
        job['logs'].append("ERROR: Playwright/browser missing: " + (msg or "unknown"))
        job['status'] = 'error'
        job['finished_at'] = time.time()
        return

    # options
    advanced = bool(job.get('advanced', True))
    obey_robots = bool(job.get('obey_robots', False))
    concurrency = max(1, int(job.get('concurrency', 1) or 1))
    crawl_mode = job.get('crawl_mode', 'bfs')
    depth_limit = int(job.get('depth_limit', 0) or 0)
    no_page_limit = job.get('no_page_limit', True)
    max_pages = None if no_page_limit else int(job.get('max_pages', 100) or 100)
    snapshots = int(job.get('snapshots', 1) or 1)
    same_host_only = bool(job.get('same_host_only', True))
    follow_buttons = bool(job.get('follow_buttons', True))
    per_page_timeout = int(job.get('per_page_timeout', 30) or 30)
    capture_api = bool(job.get('capture_api', True))
    max_runtime = int(job.get('max_runtime', 0) or 0)
    max_resources = int(job.get('max_resources', 0) or 0)
    auto_snapshot = bool(job.get('auto_snapshot', True))
    auto_clone_all = True

    job_opts_template = {
        'start_url': start_url,
        'follow_buttons': follow_buttons,
        'per_page_timeout': per_page_timeout,
        'same_host_only': same_host_only,
        'capture_api': capture_api,
        'max_runtime': max_runtime,
        'max_resources': max_resources,
        'auto_snapshot': auto_snapshot,
        'depth_limit': depth_limit,
    }

    rp = None
    if obey_robots:
        try:
            parsed = urlparse(start_url)
            robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(robots_url)
            rp.read()
            job['logs'].append(f"Loaded robots.txt from {robots_url}")
        except Exception as e:
            job['logs'].append(f"robots.txt load failed: {e}")
            rp = None

    try:
        for sidx in range(snapshots):
            job['logs'].append(f"Snapshot {sidx+1}/{snapshots} starting")
            url_q = queue.Queue()
            visited = set()
            visited_lock = threading.Lock()
            visited.add(start_url)
            url_q.put((start_url, 0))

            job['endpoints'] = set()
            job['url_to_local'] = job.get('url_to_local', {})
            job['saved_count'] = job.get('saved_count', 0)

            job_opts = job_opts_template.copy()
            job_opts['robots_parser'] = rp
            job_opts['follow_buttons'] = follow_buttons
            job_opts['start_url'] = start_url
            job_opts['auto_snapshot'] = auto_snapshot
            job_opts['depth_limit'] = depth_limit
            job_opts['per_page_timeout'] = per_page_timeout
            job_opts['max_runtime'] = max_runtime
            job_opts['max_resources'] = max_resources
            job_opts['job_start_time'] = time.time()
            job_opts['max_pages'] = max_pages

            stop_event = threading.Event()
            workers = []
            for i in range(concurrency):
                t = threading.Thread(target=worker_func, args=(jobid, url_q, visited, visited_lock, job_opts, tmpdir, sidx, job, stop_event), daemon=True)
                t.start()
                workers.append(t)

            try:
                while True:
                    if url_q.empty():
                        all_dead = all(not t.is_alive() for t in workers)
                        if all_dead:
                            break
                    if max_runtime and (time.time() - job_opts['job_start_time'] > max_runtime):
                        job['logs'].append("Snapshot runtime cap reached; stopping workers.")
                        stop_event.set()
                        break
                    if max_resources and max_resources > 0 and job.get('saved_count',0) >= max_resources:
                        job['logs'].append("Snapshot resource cap reached; stopping workers.")
                        stop_event.set()
                        break
                    if max_pages is not None and not auto_clone_all:
                        if len(visited) >= max_pages:
                            job['logs'].append("Configured max_pages reached; stopping workers.")
                            stop_event.set()
                            break
                    time.sleep(1)
            finally:
                stop_event.set()
                try:
                    while not url_q.empty():
                        url_q.get_nowait()
                        url_q.task_done()
                except Exception:
                    pass
                for t in workers:
                    t.join(timeout=5)

            job['logs'].append(f"Snapshot {sidx+1} finished. Saved: {job.get('saved_count',0)}")

        # Build url_to_local mapping and prefer assets/* when duplicates exist
        job['logs'].append("Rewriting HTML to point to local assets where possible...")
        url_to_local = job.get('url_to_local', {}) or {}

        candidates = {}
        for root_dir, _, files in os.walk(tmpdir):
            for fn in files:
                full = os.path.join(root_dir, fn)
                rel = os.path.relpath(full, tmpdir).replace(os.sep, '/')
                parts = rel.split('/')
                if len(parts) >= 3 and parts[0].startswith('snapshot_'):
                    netloc = parts[1]
                    rest = '/'.join(parts[2:])
                    https_url = f"https://{netloc}/{rest}"
                    http_url = f"http://{netloc}/{rest}"
                    for url in (https_url, http_url):
                        candidates.setdefault(url, []).append(rel)
                elif len(parts) >= 2:
                    netloc = parts[0]
                    rest = '/'.join(parts[1:])
                    https_url = f"https://{netloc}/{rest}"
                    http_url = f"http://{netloc}/{rest}"
                    for url in (https_url, http_url):
                        candidates.setdefault(url, []).append(rel)

        for url, rels in candidates.items():
            if url in url_to_local:
                continue
            preferred = None
            for r in rels:
                if '/assets/' in r:
                    preferred = r
                    break
            if not preferred:
                for r in rels:
                    ext = os.path.splitext(r)[1].lower()
                    if ext and ext not in ('.html', '.htm'):
                        preferred = r
                        break
            if not preferred:
                preferred = rels[0]
            url_to_local[url] = preferred

        # Rewrite HTML attribute refs and CSS url(...)
        attr_pattern = re.compile(r'(\b(?:src|href|poster|action)\s*=\s*)(["\'])(.*?)\2', flags=re.I | re.S)
        css_url_pattern = re.compile(r'url\(\s*(["\']?)(.*?)\1\s*\)', flags=re.I | re.S)

        for root_dir, _, files in os.walk(tmpdir):
            for fn in files:
                if not fn.lower().endswith(('.html', '.htm')):
                    continue
                full = os.path.join(root_dir, fn)
                try:
                    with open(full, 'r', encoding='utf-8', errors='replace') as fh:
                        html = fh.read()
                    rel = os.path.relpath(full, tmpdir).replace(os.sep, '/')
                    m = re.match(r'(snapshot_\d+)\/([^\/]+)\/(.*)', rel)
                    base_url = start_url
                    if m:
                        netloc = m.group(2)
                        base_url = f"https://{netloc}/"

                    def repl_attr(match):
                        prefix = match.group(1)
                        quote = match.group(2)
                        orig = match.group(3) or ''
                        abs_url = normalize_url(base_url, orig) or orig
                        local = url_to_local.get(abs_url)
                        if local:
                            return f"{prefix}{quote}{local}{quote}"
                        return f"{prefix}{quote}{orig}{quote}"

                    new_html = attr_pattern.sub(repl_attr, html)

                    def repl_css(match):
                        orig = match.group(2) or ''
                        abs_url = normalize_url(base_url, orig) or orig
                        local = url_to_local.get(abs_url)
                        if local:
                            return f"url({local})"
                        return f"url({orig})"

                    new_html = css_url_pattern.sub(repl_css, new_html)

                    with open(full, 'w', encoding='utf-8') as fh:
                        fh.write(new_html)
                    job['logs'].append(f"Rewrote {os.path.relpath(full, tmpdir)}")
                except Exception as e:
                    job['logs'].append(f"Warn rewrite {full}: {e}")

        job['logs'].append("Packaging into ZIP...")
        zip_io = io.BytesIO()
        with zipfile.ZipFile(zip_io, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            for root_dir, _, files in os.walk(tmpdir):
                for fn in files:
                    full = os.path.join(root_dir, fn)
                    arcname = os.path.relpath(full, tmpdir).replace(os.sep, '/')
                    zf.write(full, arcname)
        zip_io.seek(0)
        job['zip_bytes'] = zip_io.read()
        job['status'] = 'done'
        job['finished_at'] = time.time()
        job['logs'].append("Job completed successfully.")
    except Exception as exc:
        tb = traceback.format_exc()
        job['logs'].append("ERROR during mirroring:")
        job['logs'].append(tb)
        job['status'] = 'error'
        job['finished_at'] = time.time()

# Cleanup thread
def cleanup_worker():
    while True:
        now = time.time()
        with JOBS_LOCK:
            to_delete = []
            for jid, j in list(JOBS.items()):
                if j.get('status') in ('done','error') and j.get('finished_at'):
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

# Full UI HTML
INDEX_HTML = """
<!doctype html>
<html>
<head><meta charset="utf-8"><title>Ghost Advance</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{background:#071024;color:#e6eef6;font-family:Inter,Arial;padding:14px}
.card{background:#0b1220;padding:16px;border-radius:10px;max-width:1000px;margin:0 auto}
input,select,textarea{padding:8px;border-radius:6px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.03);color:inherit}
button{background:#06b6d4;border:none;color:#012;padding:8px 12px;border-radius:8px;cursor:pointer}
.logs{height:300px;overflow:auto;background:#021024;padding:12px;border-radius:8px;font-family:monospace}
.endpoints{max-height:120px;overflow:auto;background:#021024;padding:8px;border-radius:8px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.small{color:#94a3b8;font-size:13px}
.row{display:flex;align-items:center}
a.link{color:#99f}
</style>
</head>
<body>
<div class="card">
  <h2>Ghost Advance — Render-friendly</h2>
  <div class="small">Use responsibly. Only crawl sites you own or have permission to crawl.</div>

  <div style="margin-top:12px">
    <label style="width:100%;">URL: <input id="url" type="url" placeholder="https://example.com" style="width:78%"></label>
  </div>

  <div class="grid" style="margin-top:8px">
    <label><input id="advanced" type="checkbox" checked> Advanced</label>
    <label><input id="obey" type="checkbox"> Obey robots.txt</label>
    <label>Concurrency <input id="concurrency" type="number" value="1" min="1" max="4"></label>

    <label>Crawl mode <select id="crawl_mode"><option value="bfs">BFS</option><option value="dfs">DFS</option></select></label>
    <label>Depth limit <input id="depth_limit" type="number" value="0" min="0"></label>
    <label>No page limit <input id="no_page_limit" type="checkbox" checked></label>

    <label>Max pages <input id="max_pages" type="number" value="100" min="1"></label>
    <label>Snapshots <input id="snapshots" type="number" value="1" min="1" max="5"></label>
    <label>Same-host only <input id="same_host" type="checkbox" checked></label>

    <label>Follow onclick heuristics <input id="follow_buttons" type="checkbox" checked></label>
    <label>Capture API <input id="capture_api" type="checkbox" checked></label>
    <label>Auto snapshot <input id="auto_snapshot" type="checkbox" checked></label>

    <label>Per-page timeout (s) <input id="per_page_timeout" type="number" value="30" min="5"></label>
    <label>Max runtime (s, 0 unlimited) <input id="max_runtime" type="number" value="0" min="0"></label>
    <label>Max resources (0 unlimited) <input id="max_resources" type="number" value="0" min="0"></label>
  </div>

  <div style="margin-top:8px" class="row">
    <button id="startBtn">Start</button>
    <button id="clearBtn" style="margin-left:8px">Clear</button>
    <div style="margin-left:auto" id="jobLinks"></div>
  </div>

  <div style="margin-top:12px" class="logs" id="logs">Logs will appear here...</div>

  <h4 style="margin-top:12px">Discovered endpoints (select and Capture)</h4>
  <div class="endpoints" id="endpoints">No endpoints yet</div>
  <div style="margin-top:8px" class="row">
    <button id="capture_selected">Capture selected endpoints</button>
    <button id="capture_all" style="margin-left:8px">Capture all endpoints</button>
  </div>

  <div style="margin-top:12px" id="screenshotArea"></div>
</div>

<script>
let jobid = null; let pollTimer = null;
document.getElementById('startBtn').onclick = async () => {
  const url = document.getElementById('url').value.trim();
  if (!url) { alert('Enter URL'); return; }
  const payload = {
    url,
    advanced: document.getElementById('advanced').checked,
    obey_robots: document.getElementById('obey').checked,
    concurrency: Number(document.getElementById('concurrency').value || 1),
    crawl_mode: document.getElementById('crawl_mode').value,
    depth_limit: Number(document.getElementById('depth_limit').value || 0),
    no_page_limit: document.getElementById('no_page_limit').checked,
    max_pages: Number(document.getElementById('max_pages').value || 100),
    snapshots: Number(document.getElementById('snapshots').value || 1),
    same_host_only: document.getElementById('same_host').checked,
    follow_buttons: document.getElementById('follow_buttons').checked,
    capture_api: document.getElementById('capture_api').checked,
    auto_snapshot: document.getElementById('auto_snapshot').checked,
    per_page_timeout: Number(document.getElementById('per_page_timeout').value || 30),
    max_runtime: Number(document.getElementById('max_runtime').value || 0),
    max_resources: Number(document.getElementById('max_resources').value || 0)
  };
  const res = await fetch('/start', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
  const j = await res.json();
  if (!j.ok) { alert('Start failed: '+(j.error||'unknown')); return; }
  jobid = j.jobid; document.getElementById('logs').innerText = '';
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(pollStatus, 1200);
};

document.getElementById('clearBtn').onclick = () => {
  document.getElementById('logs').innerText = '';
  document.getElementById('endpoints').innerHTML = 'No endpoints yet';
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  jobid = null; document.getElementById('jobLinks').innerHTML = ''; document.getElementById('screenshotArea').innerHTML = '';
};

async function pollStatus(){
  if (!jobid) return;
  try {
    const [lres, sres, eres] = await Promise.all([fetch('/logs/'+jobid), fetch('/status/'+jobid), fetch('/endpoints/'+jobid)]);
    const lj = await lres.json(); const sj = await sres.json(); const ej = await eres.json();
    if (lj.ok) { document.getElementById('logs').innerText = lj.logs.join('\\n'); const el=document.getElementById('logs'); el.scrollTop = el.scrollHeight; }
    if (sj.ok) {
      const links = document.getElementById('jobLinks'); links.innerHTML = '';
      if (sj.status === 'done') { links.innerHTML = `<a class="link" href="/download/${jobid}">Download ZIP</a>`; document.getElementById('screenshotArea').innerHTML = `<img src="/screenshot/${jobid}" style="max-width:100%;margin-top:8px">`; }
    }
    if (ej.ok) {
      const endpoints = ej.endpoints || []; const container = document.getElementById('endpoints');
      if (!endpoints.length) { container.innerHTML = 'No endpoints yet'; } else {
        container.innerHTML = endpoints.map(url => {
          return `<div><label><input type="checkbox" data-url="${encodeURIComponent(url)}"> ${url}</label></div>`;
        }).join('');
      }
    }
  } catch(e){ console.error(e); }
}

document.getElementById('capture_selected').onclick = async () => {
  if (!jobid) { alert('No job'); return; }
  const checks = Array.from(document.querySelectorAll('#endpoints input[type=checkbox]:checked'));
  if (!checks.length) { alert('Select endpoints'); return; }
  const endpoints = checks.map(c => decodeURIComponent(c.getAttribute('data-url')));
  const res = await fetch('/capture_endpoints/'+jobid, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({endpoints})});
  const j = await res.json(); if (!j.ok) alert('Failed: '+(j.error||'unknown')); else alert('Capture started. See logs.');
};

document.getElementById('capture_all').onclick = async () => {
  if (!jobid) { alert('No job'); return; }
  const res = await fetch('/capture_endpoints/'+jobid, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({endpoints: null})});
  const j = await res.json(); if (!j.ok) alert('Failed: '+(j.error||'unknown')); else alert('Capture started. See logs.');
};
</script>
</body>
</html>
"""

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
        'logs': [f"Queued {jobid} for {url}"],
        'tmpdir': None,
        'screenshot': None,
        'zip_bytes': None,
        'saved_count': 0,
        'started_at': None,
        'finished_at': None,
        'advanced': bool(data.get('advanced', True)),
        'obey_robots': bool(data.get('obey_robots', False)),
        'concurrency': int(data.get('concurrency', 1) or 1),
        'crawl_mode': data.get('crawl_mode', 'bfs'),
        'depth_limit': int(data.get('depth_limit', 0) or 0),
        'no_page_limit': bool(data.get('no_page_limit', True)),
        'max_pages': int(data.get('max_pages', 100) or 100),
        'snapshots': int(data.get('snapshots', 1) or 1),
        'same_host_only': bool(data.get('same_host_only', True)),
        'follow_buttons': bool(data.get('follow_buttons', True)),
        'capture_api': bool(data.get('capture_api', True)),
        'auto_snapshot': bool(data.get('auto_snapshot', True)),
        'per_page_timeout': int(data.get('per_page_timeout', 30) or 30),
        'max_runtime': int(data.get('max_runtime', 0) or 0),
        'max_resources': int(data.get('max_resources', 0) or 0),
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
        return jsonify({'ok': True, 'status': job.get('status')})

@app.route('/endpoints/<jobid>')
def endpoints(jobid):
    with JOBS_LOCK:
        job = JOBS.get(jobid)
        if not job:
            return jsonify({'ok': False, 'error': 'job not found'}), 404
        eps = sorted(list(job.get('endpoints') or []))
        return jsonify({'ok': True, 'endpoints': eps})

@app.route('/capture_endpoints/<jobid>', methods=['POST'])
def capture_endpoints(jobid):
    data = request.get_json(silent=True) or {}
    endpoints = data.get('endpoints')
    with JOBS_LOCK:
        job = JOBS.get(jobid)
        if not job:
            return jsonify({'ok': False, 'error': 'job not found'}), 404
        eps = sorted(list(job.get('endpoints') or []))
    if endpoints is None:
        to_capture = eps
    else:
        to_capture = endpoints
    if not to_capture:
        return jsonify({'ok': True, 'message': 'no endpoints to capture'})

    def do_capture():
        with JOBS_LOCK:
            job['logs'].append(f"Starting capture of {len(to_capture)} endpoints")
        try:
            with sync_playwright() as p:
                try:
                    browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
                except Exception:
                    browser = p.chromium.launch(headless=True)
                context = browser.new_context(ignore_https_errors=True)
                api_folder = os.path.join(job.get('tmpdir') or tempfile.gettempdir(), f"snapshot_0", "api_responses")
                ensure_parent(os.path.join(api_folder, 'placeholder.txt'))
                for ep in to_capture:
                    try:
                        if job.get('same_host_only') and not same_host(job.get('url'), ep):
                            with JOBS_LOCK:
                                job['logs'].append(f"Skipping endpoint (different host): {ep}")
                            continue
                        resp = context.request.get(ep, timeout=max(1, int(job.get('per_page_timeout',30)))*1000)
                        content = resp.body()
                        if content is None or len(content) == 0:
                            with JOBS_LOCK:
                                job['logs'].append(f"Endpoint empty: {ep} (status {getattr(resp,'status', 'unknown')})")
                            continue
                        ct = resp.headers.get('content-type', '')
                        ext = ext_from_content_type(ct) or '.bin'
                        h = hashlib.sha1(ep.encode('utf-8')).hexdigest()[:12]
                        fname = f"{h}_{getattr(resp,'status',0)}{ext}"
                        local = os.path.join(api_folder, fname)
                        ensure_parent(local)
                        with open(local, 'wb') as fh:
                            fh.write(content)
                        rel = os.path.relpath(local, job.get('tmpdir')).replace(os.sep, '/')
                        with JOBS_LOCK:
                            job.setdefault('url_to_local', {})[ep] = rel
                            job['logs'].append(f"Captured endpoint: {ep} -> {rel}")
                            job['saved_count'] = job.get('saved_count',0) + 1
                    except Exception as e:
                        with JOBS_LOCK:
                            job['logs'].append(f"Failed capture {ep}: {e}")
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass
        except Exception as e:
            with JOBS_LOCK:
                job['logs'].append("Error capturing endpoints: " + str(e))
    threading.Thread(target=do_capture, daemon=True).start()
    return jsonify({'ok': True})

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
    bio = io.BytesIO(data)
    try:
        return send_file(bio, mimetype='application/zip', as_attachment=True, download_name=f'ghost_clone_{jobid}.zip')
    except TypeError:
        return send_file(bio, mimetype='application/zip', as_attachment=True, attachment_filename=f'ghost_clone_{jobid}.zip')

if __name__ == '__main__':
    try:
        ok, msg = ensure_playwright_browsers()
        if not ok:
            print("Playwright/browsers not ready:", msg, file=sys.stderr)
            print("Run: python -m playwright install chromium", file=sys.stderr)
    except Exception as e:
        print("Playwright check failed:", e, file=sys.stderr)
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
