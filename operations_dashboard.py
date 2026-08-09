"""Read-only localhost dashboard for crawl, evidence, evaluation, and stories."""

from __future__ import annotations

import json
import logging
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from config import DATA_DIR, HARDWARE_PROFILES, WORKSTATION_OWNER_PATH, get_hardware_profile
from evaluation import evaluation_campaign
from evidence_bundle import portable_evidence_status
from maintenance import collect_maintenance_plan
from metrics import compare_profiles, latest_metrics
from progress import ProgressTracker
from project_health import git_health
from story_operations import read_story_run_state, story_failure_status

logger = logging.getLogger(__name__)
_CACHE_LOCK = threading.Lock()
_CACHE: dict = {"at": 0.0, "profile": None, "payload": None}


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None


def collect_operations_status(profile_name: str = "auto", cache_seconds: int = 5) -> dict:
    profile = get_hardware_profile(profile_name)
    now = time.monotonic()
    with _CACHE_LOCK:
        if (
            _CACHE["payload"] is not None
            and _CACHE["profile"] == profile.name
            and now - float(_CACHE["at"]) < cache_seconds
        ):
            return _CACHE["payload"]

    tracker = ProgressTracker()
    progress = tracker.get_summary()
    failures = tracker.get_failure_summary(examples_per_category=0)
    campaign = evaluation_campaign(include_samples=False)
    evidence = portable_evidence_status()
    stories = read_story_run_state() or {"status": "not_started", "process_running": False}
    story_failures = story_failure_status()
    curation = _read_json(DATA_DIR / "exports" / "story_curation_report.json") or {}
    owner = _read_json(WORKSTATION_OWNER_PATH) or {}
    maintenance = collect_maintenance_plan(profile.name)
    metrics = compare_profiles()
    latest = latest_metrics() or {}
    total = int(progress.get("total_files", 0))
    completed = int(progress.get("completed", 0))
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "profile": {
            "name": profile.name,
            "workers": profile.workers,
            "gpu": latest.get("gpu"),
        },
        "git": git_health(),
        "workstation": {
            "state": owner.get("state", "unclaimed"),
            "profile": owner.get("profile"),
            "checkpoint_state": owner.get("checkpoint_state"),
            "expires_at": owner.get("expires_at"),
        },
        "crawl": {
            **progress,
            "completion_pct": round(completed / total * 100, 2) if total else 0.0,
            "failure_categories": {
                name: int(row.get("count", 0))
                for name, row in failures.get("categories", {}).items()
            },
            "attempts_exhausted": failures.get("attempts_exhausted", 0),
        },
        "evaluation": campaign,
        "evidence": evidence,
        "metrics": metrics,
        "stories": {
            "run": {
                key: stories.get(key)
                for key in (
                    "status",
                    "process_running",
                    "configured_workers",
                    "active_workers",
                    "completed_sources",
                    "remaining_run_sources",
                    "stories_written",
                    "sources_per_hour",
                    "eta_seconds",
                )
            },
            "failures": {
                key: story_failures.get(key)
                for key in (
                    "sources",
                    "ready_sources",
                    "cooldown_sources",
                    "quarantined_sources",
                    "categories",
                )
            },
            "curation": curation,
        },
        "actions": maintenance.get("actions", []),
    }
    with _CACHE_LOCK:
        _CACHE.update({"at": now, "profile": profile.name, "payload": payload})
    return payload


_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Hometown XR Operations</title><link rel="stylesheet" href="/app.css"></head>
<body><header><div><strong>Hometown XR</strong><span>Operations</span></div><div class="header-meta"><span id="profile">...</span><span id="updated">Loading</span><button id="refresh" type="button">Refresh</button></div></header>
<nav aria-label="Views"><button class="tab active" data-view="overview">Overview</button><button class="tab" data-view="crawl">Crawl</button><button class="tab" data-view="evidence">Evidence</button><button class="tab" data-view="stories">Stories</button></nav>
<main><div id="error" hidden></div>
<section data-panel="overview"><h1>Project status</h1><div class="metrics" id="summary"></div><div class="band"><div class="band-head"><h2>Next actions</h2><span id="branch"></span></div><div id="actions"></div></div></section>
<section data-panel="crawl" hidden><h1>Crawl and recovery</h1><div class="metrics" id="crawlMetrics"></div><div class="band table-band"><div class="band-head"><h2>Failure categories</h2><span id="failureTotal"></span></div><table><thead><tr><th>Category</th><th>Sources</th></tr></thead><tbody id="failures"></tbody></table></div></section>
<section data-panel="evidence" hidden><h1>Evaluation and profile evidence</h1><div class="metrics" id="evaluationMetrics"></div><div class="band table-band"><div class="band-head"><h2>GPU profiles</h2><span>Model and real-source checks</span></div><table><thead><tr><th>Profile</th><th>Bundle</th><th>Model</th><th>Workload</th></tr></thead><tbody id="profiles"></tbody></table></div><div class="band table-band"><div class="band-head"><h2>Campaign phases</h2><span id="campaignProgress"></span></div><table><thead><tr><th>Queue</th><th>Target</th><th>Complete</th><th>Available</th><th>Status</th></tr></thead><tbody id="phases"></tbody></table></div></section>
<section data-panel="stories" hidden><h1>Story pipeline</h1><div class="metrics" id="storyMetrics"></div><div class="band table-band"><div class="band-head"><h2>Curation</h2><span>Verbatim source stories</span></div><table><tbody id="curation"></tbody></table></div></section>
</main><script src="/status.js"></script><script src="/app.js"></script></body></html>"""

_CSS = """
:root{--ink:#17201d;--muted:#63706b;--line:#d8dfdc;--paper:#f6f8f7;--white:#fff;--green:#17664b;--amber:#956715;--red:#a23c35}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;letter-spacing:0}header{height:64px;padding:0 28px;background:#17201d;color:#fff;display:flex;align-items:center;justify-content:space-between}header strong{font-size:17px}header div>span{margin-left:9px;color:#b8c4bf}.header-meta{display:flex;align-items:center;gap:14px}.header-meta span{margin:0;font-size:12px}button{font:inherit;letter-spacing:0}#refresh{border:1px solid #53605b;background:transparent;color:#fff;padding:7px 11px;border-radius:4px;cursor:pointer}nav{height:47px;padding:0 28px;background:var(--white);border-bottom:1px solid var(--line);display:flex;gap:24px}.tab{border:0;border-bottom:3px solid transparent;background:transparent;padding:0 2px;color:var(--muted);font-weight:650;cursor:pointer}.tab.active{border-color:var(--green);color:var(--ink)}main{max-width:1180px;margin:0 auto;padding:30px 28px 60px}h1{font-size:25px;margin:0 0 22px}h2{font-size:15px;margin:0}.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border:1px solid var(--line);background:var(--white);margin-bottom:22px}.metric{min-width:0;padding:17px 18px;border-right:1px solid var(--line)}.metric:last-child{border-right:0}.metric span{display:block;color:var(--muted);font-size:12px;margin-bottom:5px}.metric strong{display:block;font-size:22px;overflow-wrap:anywhere}.metric small{display:block;color:var(--muted);margin-top:3px}.band{background:var(--white);border:1px solid var(--line);margin-top:18px}.band-head{min-height:48px;padding:0 16px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line)}.band-head span{color:var(--muted);font-size:12px}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:11px 16px;border-bottom:1px solid #edf0ef}th{font-size:11px;text-transform:uppercase;color:var(--muted);font-weight:700}tr:last-child td{border-bottom:0}.action{display:grid;grid-template-columns:42px minmax(0,1fr);gap:12px;padding:14px 16px;border-bottom:1px solid #edf0ef}.action:last-child{border-bottom:0}.priority{font-weight:700;color:var(--green)}code{display:block;margin-top:5px;color:var(--muted);white-space:normal;overflow-wrap:anywhere}.pass{color:var(--green);font-weight:700}.warn{color:var(--amber);font-weight:700}.fail{color:var(--red);font-weight:700}#error{padding:12px 14px;border:1px solid #e0aaa6;color:var(--red);background:#fff2f1;margin-bottom:18px}
@media(max-width:760px){header{height:auto;min-height:72px;padding:14px 16px;align-items:flex-start}.header-meta{display:grid;grid-template-columns:auto auto;gap:4px 10px;text-align:right}.header-meta #refresh{grid-column:2}nav{padding:0 16px;gap:17px;overflow-x:auto}main{padding:24px 16px 44px}h1{font-size:21px}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.metric{border-bottom:1px solid var(--line)}.metric:nth-child(2n){border-right:0}.metric:nth-last-child(-n+2){border-bottom:0}.metric strong{font-size:19px}.table-band{overflow-x:auto}.table-band .band-head,.table-band table{min-width:520px}.action{grid-template-columns:34px minmax(0,1fr)}}
"""

_JS = """
const $=id=>document.getElementById(id);const esc=v=>String(v??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));const number=v=>new Intl.NumberFormat().format(Number(v||0));
function metric(label,value,small=''){return `<div class="metric"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(small)}</small></div>`}function state(value,good=true){return `<span class="${good?'pass':'warn'}">${esc(value)}</span>`}
function render(d){$('profile').textContent=`${d.profile.name} | ${d.profile.workers} workers`;$('updated').textContent=new Date(d.generated_at).toLocaleTimeString();$('branch').textContent=`${d.git.branch} @ ${String(d.git.commit).slice(0,8)}`;const c=d.crawl,e=d.evaluation,s=d.stories.run,profiles=d.evidence.profiles;
$('summary').innerHTML=metric('Crawl',`${c.completion_pct}%`,`${number(c.completed)} / ${number(c.total_files)} sources`)+metric('Failures',number(c.failed),`${number(c.retryable)} retryable`)+metric('Evaluation',`${e.completed} / ${e.target}`,e.blocked_phases.length?'queues need refill':'campaign supplied')+metric('Stories',number(s.stories_written||0),s.status||'not started');
$('actions').innerHTML=d.actions.length?d.actions.map(a=>`<div class="action"><div class="priority">P${esc(a.priority)}</div><div><strong>${esc(a.area)}</strong><div>${esc(a.action)}</div><code>${esc(a.command)}</code></div></div>`).join(''):'<div class="action"><div class="priority">OK</div><div>No pending maintenance actions</div></div>';
$('crawlMetrics').innerHTML=metric('Completed',number(c.completed),`${c.completion_pct}%`)+metric('Pending',number(c.pending))+metric('Processing',number(c.processing))+metric('Exhausted',number(c.attempts_exhausted));$('failureTotal').textContent=`${number(c.failed)} failed sources`;$('failures').innerHTML=Object.entries(c.failure_categories).map(([k,v])=>`<tr><td>${esc(k)}</td><td>${number(v)}</td></tr>`).join('')||'<tr><td colspan="2">No failures</td></tr>';
$('evaluationMetrics').innerHTML=metric('Campaign',`${e.completed} / ${e.target}`,`${e.queued} queued`)+metric('Remaining',e.remaining)+metric('Model bundles',d.evidence.complete_model_profiles.length,'of 3 profiles')+metric('Workload bundles',d.evidence.complete_workload_profiles.length,'of 3 profiles');$('profiles').innerHTML=Object.entries(profiles).map(([name,p])=>`<tr><td>${esc(name)}</td><td>${state(p.present?'present':'missing',p.present&&p.valid!==false)}</td><td>${state(p.model_snapshot?'ready':'missing',p.model_snapshot)}</td><td>${state(p.workload_benchmark?'ready':'missing',p.workload_benchmark)}</td></tr>`).join('');$('campaignProgress').textContent=`${Math.round(e.progress*100)}% complete`;$('phases').innerHTML=e.phases.map(p=>`<tr><td>${esc(p.id)}</td><td>${p.target}</td><td>${p.completed}</td><td>${p.available}</td><td>${state(p.blocked?'blocked':'ready',!p.blocked)}</td></tr>`).join('');
const sf=d.stories.failures,cur=d.stories.curation||{};$('storyMetrics').innerHTML=metric('Run',s.status||'not started',s.process_running?`${s.active_workers||0} active workers`:'stopped')+metric('Completed sources',number(s.completed_sources))+metric('Remaining',number(s.remaining_run_sources))+metric('Story failures',number(sf.sources),`${number(sf.quarantined_sources)} quarantined`);$('curation').innerHTML=[['Ranked stories',cur.stories],['Canonical stories',cur.canonical_stories],['Default eligible',cur.eligible_default],['Exact duplicates',cur.exact_duplicates],['Near duplicates',cur.near_duplicates]].map(([k,v])=>`<tr><th>${esc(k)}</th><td>${number(v)}</td></tr>`).join('');}
function load(){const d=window.__HOMETOWN_STATUS__;if(d?.error){$('error').hidden=false;$('error').textContent=d.error;return}if(!d){$('error').hidden=false;$('error').textContent='Status payload unavailable';return}$('error').hidden=true;render(d)}$('refresh').onclick=()=>location.reload();document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x===b));document.querySelectorAll('[data-panel]').forEach(x=>x.hidden=x.dataset.panel!==b.dataset.view)});load();setTimeout(()=>location.reload(),30000);
"""


class _OperationsHandler(BaseHTTPRequestHandler):
    server_version = "HometownXROperations/1"
    profile_name = "auto"

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'",
        )
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send(_HTML.encode(), "text/html; charset=utf-8")
        elif path == "/app.css":
            self._send(_CSS.encode(), "text/css; charset=utf-8")
        elif path == "/app.js":
            self._send(_JS.encode(), "text/javascript; charset=utf-8")
        elif path == "/status.js":
            try:
                payload = collect_operations_status(self.profile_name)
            except Exception as exc:
                logger.exception("Unable to collect operations status")
                payload = {"error": str(exc)}
            body = "window.__HOMETOWN_STATUS__=" + json.dumps(payload) + ";\n"
            self._send(body.encode(), "text/javascript; charset=utf-8")
        elif path == "/favicon.ico":
            self._send(b"", "image/x-icon", HTTPStatus.NO_CONTENT)
        elif path == "/api/status":
            try:
                payload = collect_operations_status(self.profile_name)
                self._send(
                    json.dumps(payload).encode(),
                    "application/json; charset=utf-8",
                )
            except Exception as exc:
                logger.exception("Unable to collect operations status")
                self._send(
                    json.dumps({"error": str(exc)}).encode(),
                    "application/json; charset=utf-8",
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
        else:
            self._send(b'{"error":"not found"}', "application/json", HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args) -> None:
        logger.debug(format, *args)


def serve_operations_dashboard(
    host: str = "127.0.0.1",
    port: int = 8770,
    profile_name: str = "auto",
    open_browser: bool = False,
) -> None:
    if profile_name != "auto" and profile_name not in HARDWARE_PROFILES:
        raise ValueError(f"unknown hardware profile: {profile_name}")
    handler = type("OperationsHandler", (_OperationsHandler,), {"profile_name": profile_name})
    server = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{server.server_port}/"
    logger.info("Operations dashboard: %s", url)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
