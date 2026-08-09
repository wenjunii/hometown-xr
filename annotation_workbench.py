"""Dependency-free local web workbench for evaluation annotations."""

from __future__ import annotations

import json
import logging
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from evaluation import (
    annotation_queue,
    evaluation_campaign,
    evaluation_status,
    label_annotation,
    multilingual_recall_report,
    undo_annotation,
)

logger = logging.getLogger(__name__)

_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Hometown XR Review</title>
  <link rel="stylesheet" href="/app.css">
</head>
<body>
  <header>
    <div>
      <p class="eyebrow">Hometown XR</p>
      <h1>Review workbench</h1>
    </div>
    <div class="progress-wrap">
      <span id="progressText">Loading</span>
      <progress id="progress" max="1" value="0"></progress>
    </div>
  </header>
  <main>
    <aside aria-label="Review filters">
      <label>Queue<select id="mode"><option value="campaign">Required campaign</option><option value="all">Entire sample</option></select></label>
      <label>Language<select id="language"><option value="">All languages</option></select></label>
      <label>Decision<select id="prediction"><option value="">All decisions</option><option value="accepted">Accepted</option><option value="rejected">Rejected</option></select></label>
      <label>Split<select id="split"><option value="all">All splits</option><option value="tuning">Tuning</option><option value="holdout">Holdout</option></select></label>
      <label class="check"><input id="relabel" type="checkbox"><span>Include labeled</span></label>
      <label>Reviewer<input id="annotator" type="text" maxlength="120" autocomplete="name"></label>
      <button id="reload" class="secondary" type="button">Refresh queue</button>
      <dl id="coverage"></dl>
    </aside>
    <section class="review" aria-live="polite">
      <div id="empty" hidden>
        <h2>Queue complete</h2>
        <p>Change a filter or rebuild the annotation sample.</p>
      </div>
      <article id="sample">
        <div class="sample-head">
          <div class="tags"><span id="languageTag"></span><span id="splitTag"></span><span id="decisionTag"></span></div>
          <span id="position"></span>
        </div>
        <p id="paragraph"></p>
        <div class="source"><a id="source" target="_blank" rel="noreferrer">Open source</a><span id="scores"></span></div>
      </article>
      <fieldset id="contentType">
        <legend>Content type</legend>
        <div class="segments">
          <label><input type="radio" name="content" value="personal_prose"><span>Personal prose</span></label>
          <label><input type="radio" name="content" value="poetry"><span>Poetry</span></label>
          <label><input type="radio" name="content" value="lyrics"><span>Lyrics</span></label>
          <label><input type="radio" name="content" value="genealogy"><span>Genealogy</span></label>
          <label><input type="radio" name="content" value="commercial"><span>Commercial</span></label>
          <label><input type="radio" name="content" value="adult_content"><span>Adult</span></label>
          <label><input type="radio" name="content" value="unknown"><span>Unknown</span></label>
        </div>
      </fieldset>
      <label class="notes">Notes<textarea id="notes" rows="3"></textarea></label>
      <div class="actions">
        <button id="positive" class="positive" type="button" title="Mark positive (P)">Positive</button>
        <button id="negative" class="negative" type="button" title="Mark negative (N)">Negative</button>
        <button id="skip" class="secondary" type="button" title="Skip (S)">Skip</button>
        <button id="undo" class="secondary" type="button" title="Undo latest label (U)">Undo</button>
      </div>
      <p id="message" role="status"></p>
    </section>
  </main>
  <script src="/app.js"></script>
</body>
</html>
"""

_CSS = """
:root{color-scheme:light;--ink:#17201d;--muted:#63706b;--line:#d8dedb;--paper:#f6f7f5;--white:#fff;--green:#167552;--green-bg:#e5f4ed;--red:#a23c35;--red-bg:#f9e9e7;--focus:#176d9c}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;letter-spacing:0}header{height:92px;padding:18px 28px;border-bottom:1px solid var(--line);background:var(--white);display:flex;align-items:center;justify-content:space-between;gap:24px}h1{font-size:24px;line-height:1.1;margin:2px 0;font-weight:680}.eyebrow{margin:0;color:var(--green);font-size:12px;font-weight:700;text-transform:uppercase}.progress-wrap{width:min(320px,40vw);display:grid;gap:6px;color:var(--muted);font-size:13px}progress{width:100%;height:8px;accent-color:var(--green)}main{display:grid;grid-template-columns:250px minmax(0,900px);gap:0;min-height:calc(100vh - 92px);justify-content:center}aside{padding:28px 24px;border-right:1px solid var(--line);display:flex;flex-direction:column;gap:16px}label{font-size:13px;font-weight:650;color:#39443f}select,textarea,button,input[type=text]{font:inherit;letter-spacing:0}select,textarea,input[type=text]{width:100%;margin-top:6px;border:1px solid #bcc6c1;border-radius:5px;background:var(--white);color:var(--ink)}select,input[type=text]{height:38px;padding:0 10px}textarea{padding:10px;resize:vertical;min-height:72px}.check{display:flex;align-items:center;gap:8px}.check input{width:17px;height:17px;accent-color:var(--green)}button{border:1px solid transparent;border-radius:5px;min-height:40px;padding:8px 16px;font-weight:700;cursor:pointer}button:focus-visible,select:focus-visible,textarea:focus-visible,input:focus-visible{outline:3px solid color-mix(in srgb,var(--focus) 32%,transparent);outline-offset:2px}.secondary{background:var(--white);border-color:#b9c3be;color:#33413b}.review{padding:34px 40px 48px}.sample-head,.source,.actions{display:flex;align-items:center;justify-content:space-between;gap:12px}.tags{display:flex;flex-wrap:wrap;gap:7px}.tags span{border:1px solid var(--line);background:#f8faf9;border-radius:4px;padding:3px 8px;color:#42504a;font-size:12px;font-weight:700}#sample{background:var(--white);border:1px solid var(--line);border-radius:7px;padding:24px;min-height:330px;display:flex;flex-direction:column;justify-content:space-between}#paragraph{font-family:Georgia,serif;font-size:20px;line-height:1.72;margin:26px 0;white-space:pre-wrap;overflow-wrap:anywhere}.source{border-top:1px solid #edf0ee;padding-top:14px;color:var(--muted);font-size:12px}.source a{color:#176d9c;max-width:60%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}fieldset{border:0;padding:0;margin:24px 0 16px}legend{font-size:13px;font-weight:700;margin-bottom:8px}.segments{display:flex;flex-wrap:wrap;gap:6px}.segments label{position:relative}.segments input{position:absolute;opacity:0}.segments span{display:block;padding:7px 10px;border:1px solid #bdc6c2;border-radius:4px;background:var(--white);font-weight:600}.segments input:checked+span{border-color:var(--green);background:var(--green-bg);color:#0d5c3e}.notes{display:block}.actions{justify-content:flex-start;margin-top:18px}.positive{background:var(--green);color:white}.negative{background:var(--red);color:white}#message{min-height:24px;color:var(--muted)}dl{border-top:1px solid var(--line);padding-top:14px;display:grid;grid-template-columns:1fr auto;gap:5px;font-size:12px;color:var(--muted)}dt,dd{margin:0}dd{font-weight:700;color:var(--ink)}#empty{padding:64px 0}#empty h2{font-size:22px;margin:0 0 6px}
@media(max-width:760px){header{height:auto;padding:16px 18px;align-items:start;display:grid;grid-template-columns:minmax(0,1fr) 120px;gap:12px}.progress-wrap{width:100%;min-width:0}main{display:block}aside{border-right:0;border-bottom:1px solid var(--line);padding:16px 18px;display:grid;grid-template-columns:minmax(0,1fr)}aside>*{min-width:0}.review{padding:20px 16px}#sample{min-height:290px;padding:18px}#paragraph{font-size:18px}.actions{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr)}.source{align-items:flex-start;flex-direction:column}.source a{max-width:100%}}
"""

_JS = """
const state={queue:[],index:0,status:null,campaign:null};
const $=id=>document.getElementById(id);
async function api(path,options){const r=await fetch(path,{headers:{'Content-Type':'application/json'},...options});const body=await r.json();if(!r.ok)throw new Error(body.error||r.statusText);return body}
function selectedContent(){return document.querySelector('input[name=content]:checked')?.value||'unknown'}
function current(){return state.queue[state.index]}
function showMessage(text,error=false){$('message').textContent=text;$('message').style.color=error?'#a23c35':'#63706b'}
function render(){
  const row=current(),has=Boolean(row);$('sample').hidden=!has;$('contentType').hidden=!has;$('notes').parentElement.hidden=!has;document.querySelector('.actions').hidden=!has;$('empty').hidden=has;
  if(!has)return;
  $('languageTag').textContent=row.language||'unknown';$('splitTag').textContent=row.blind?'Blind holdout':(row.evaluation_split||'tuning');$('decisionTag').textContent=row.blind?'Protected':(row.predicted_accept?'Model accept':'Model reject');$('position').textContent=`${state.index+1} / ${state.queue.length}`;
  $('paragraph').textContent=row.paragraph||'';$('source').href=row.url||'#';$('source').textContent=row.url?'Open source':'No source URL';$('scores').textContent=row.blind?'Model evidence hidden':`Semantic ${row.semantic_score??'n/a'} | Narrative ${row.narrative_score??'n/a'}`;
  $('notes').value=row.notes||'';const content=row.content_label||row.predicted_content_category||'unknown';const radio=document.querySelector(`input[name=content][value="${content}"]`);if(radio)radio.checked=true;
}
function renderStatus(){
  const s=state.status;if(!s)return;const campaign=state.campaign,done=campaign?.completed??s.labeled??0,total=campaign?.target??s.samples??0;$('progress').max=Math.max(total,1);$('progress').value=done;$('progressText').textContent=`${done} / ${total} required labels`;
  const values=[['Positive',s.labels?.positive||0],['Negative',s.labels?.negative||0],['Languages',Object.keys(s.sampled_funnel_by_language||{}).length],['Keyword shadow',s.candidate_reservoir?.keyword_shadow||0]];$('coverage').innerHTML=values.map(([k,v])=>`<dt>${k}</dt><dd>${v}</dd>`).join('');
}
async function load(){
  showMessage('Loading queue');const q=new URLSearchParams();if($('language').value)q.set('language',$('language').value);if($('prediction').value)q.set('prediction',$('prediction').value);q.set('split',$('split').value);if($('relabel').checked)q.set('relabel','1');const campaignMode=$('mode').value==='campaign';
  try{const [queue,status]=await Promise.all([api((campaignMode?'/api/campaign?':'/api/queue?')+q),api('/api/status')]);state.queue=queue.samples;state.campaign=queue.campaign||null;state.index=0;state.status=status;const langs=Object.keys(status.sampled_funnel_by_language||{});const old=$('language').value;$('language').innerHTML='<option value="">All languages</option>'+langs.map(x=>`<option value="${x}">${x}</option>`).join('');$('language').value=old;renderStatus();render();showMessage(queue.samples.length?'Queue ready':(state.campaign?.blocked_phases?.length?'Campaign needs more representative crawl samples':'No samples match these filters'))}
  catch(e){showMessage(e.message,true)}
}
async function label(value){
  const row=current();if(!row)return;
  try{await api('/api/label',{method:'POST',body:JSON.stringify({sample_id:row.sample_id,label:value,content_label:selectedContent(),notes:$('notes').value,annotator:$('annotator').value,expected_labeled_at:row.labeled_at||null})});localStorage.setItem('hometownAnnotator',$('annotator').value);await load();showMessage(`Saved ${value} label`)}
  catch(e){showMessage(e.message,true)}
}
function skip(){if(!state.queue.length)return;state.index=(state.index+1)%state.queue.length;render();showMessage('Skipped')}
async function undo(){try{await api('/api/undo',{method:'POST',body:'{}'});showMessage('Latest label restored');await load()}catch(e){showMessage(e.message,true)}}
$('positive').onclick=()=>label('positive');$('negative').onclick=()=>label('negative');$('skip').onclick=skip;$('undo').onclick=undo;$('reload').onclick=load;$('annotator').value=localStorage.getItem('hometownAnnotator')||'';['mode','language','prediction','split','relabel'].forEach(id=>$(id).onchange=load);
document.addEventListener('keydown',e=>{if(e.target.matches('textarea,input,select'))return;const k=e.key.toLowerCase();if(k==='p')label('positive');else if(k==='n')label('negative');else if(k==='s')skip();else if(k==='u')undo()});
load();
"""


def _public_row(row: dict) -> dict:
    result = dict(row)
    blind = (
        result.get("sample_role") == "benchmark"
        and result.get("evaluation_split") == "holdout"
    )
    result["blind"] = blind
    if blind:
        for key in (
            "predicted_accept",
            "semantic_score",
            "narrative_score",
            "uncertainty_score",
            "selection_reason",
            "concept_match",
            "matched_keywords",
            "predicted_content_category",
        ):
            result.pop(key, None)
    return result


class _WorkbenchHandler(BaseHTTPRequestHandler):
    server_version = "HometownXRWorkbench/1"

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

    def _json(self, payload: object, status: int = 200) -> None:
        self._send(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def _error(self, exc: Exception, status: int = HTTPStatus.BAD_REQUEST) -> None:
        self._json({"error": str(exc)}, status)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(_HTML.encode(), "text/html; charset=utf-8")
            return
        if parsed.path == "/app.css":
            self._send(_CSS.encode(), "text/css; charset=utf-8")
            return
        if parsed.path == "/app.js":
            self._send(_JS.encode(), "text/javascript; charset=utf-8")
            return
        if parsed.path == "/favicon.ico":
            self._send(b"", "image/x-icon", HTTPStatus.NO_CONTENT)
            return
        try:
            if parsed.path == "/api/status":
                self._json(evaluation_status())
                return
            if parsed.path == "/api/multilingual":
                self._json(multilingual_recall_report())
                return
            if parsed.path == "/api/queue":
                query = parse_qs(parsed.query)
                prediction = (query.get("prediction") or [""])[0]
                rows = annotation_queue(
                    language=(query.get("language") or [None])[0],
                    predicted_accept=(
                        None if not prediction else prediction == "accepted"
                    ),
                    split=(query.get("split") or ["all"])[0],
                    relabel=(query.get("relabel") or ["0"])[0] == "1",
                )
                self._json({"samples": [_public_row(row) for row in rows]})
                return
            if parsed.path == "/api/campaign":
                query = parse_qs(parsed.query)
                prediction = (query.get("prediction") or [""])[0]
                language = (query.get("language") or [""])[0]
                split = (query.get("split") or ["all"])[0]
                campaign = evaluation_campaign()
                rows = [
                    row
                    for row in campaign.pop("samples")
                    if (not language or row.get("language") == language)
                    and (
                        not prediction
                        or bool(row.get("predicted_accept"))
                        is (prediction == "accepted")
                    )
                    and (
                        split == "all"
                        or row.get("evaluation_split", "tuning") == split
                    )
                ]
                self._json(
                    {
                        "campaign": campaign,
                        "samples": [_public_row(row) for row in rows],
                    }
                )
                return
        except Exception as exc:
            self._error(exc)
            return
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 64 * 1024:
                raise ValueError("request is too large")
            payload = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/label":
                self._json(
                    label_annotation(
                        sample_id=str(payload.get("sample_id", "")),
                        label=str(payload.get("label", "")),
                        content_label=payload.get("content_label"),
                        notes=payload.get("notes"),
                        annotator=payload.get("annotator"),
                        expected_labeled_at=payload.get("expected_labeled_at"),
                    )
                )
                return
            if self.path == "/api/undo":
                self._json(undo_annotation(sample_id=payload.get("sample_id")))
                return
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except RuntimeError as exc:
            self._error(exc, HTTPStatus.CONFLICT)
        except Exception as exc:
            self._error(exc)

    def log_message(self, format: str, *args) -> None:
        logger.debug(format, *args)


def serve_annotation_workbench(
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
) -> None:
    """Serve the annotation workbench until interrupted."""
    server = ThreadingHTTPServer((host, port), _WorkbenchHandler)
    url = f"http://{host}:{server.server_port}/"
    logger.info("Annotation workbench: %s", url)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
