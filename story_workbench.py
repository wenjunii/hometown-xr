"""Dependency-free local browser for deterministic source stories."""

from __future__ import annotations

import json
import logging
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from story_review import StoryReviewIndex, export_reviewed_stories

logger = logging.getLogger(__name__)

_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Hometown XR Stories</title>
  <link rel="stylesheet" href="/app.css">
</head>
<body>
  <header>
    <div class="brand">
      <p>Hometown XR</p>
      <h1>Story explorer</h1>
    </div>
    <div class="header-status">
      <span id="coverage">Loading stories</span>
      <button id="export" class="secondary" type="button">Export selected</button>
    </div>
  </header>
  <section class="toolbar" aria-label="Story filters">
    <label class="search">Search<input id="search" type="search" autocomplete="off"></label>
    <label>Language<select id="language"></select></label>
    <label>Domain<select id="domain"></select></label>
    <label>Keyword<select id="keyword"></select></label>
    <label>Review<select id="decision">
      <option value="">All</option>
      <option value="unreviewed">Unreviewed</option>
      <option value="selected">Selected</option>
      <option value="rejected">Rejected</option>
      <option value="unsure">Unsure</option>
    </select></label>
    <label>Sort<select id="sort">
      <option value="match">Match order</option>
      <option value="score">Semantic score</option>
      <option value="length">Passage length</option>
      <option value="review">Review status</option>
    </select></label>
  </section>
  <main>
    <aside class="results" aria-label="Story results">
      <div class="results-head">
        <strong id="resultCount">0 stories</strong>
        <span id="pageText"></span>
      </div>
      <div id="storyList" class="story-list"></div>
      <div class="pager">
        <button id="previous" class="secondary" type="button">Previous</button>
        <button id="next" class="secondary" type="button">Next</button>
      </div>
    </aside>
    <article class="detail" id="detail">
      <div id="empty" class="empty">
        <h2>No story selected</h2>
      </div>
      <div id="storyDetail" hidden>
        <div class="detail-head">
          <div>
            <div id="tags" class="tags"></div>
            <h2 id="detailTitle"></h2>
          </div>
          <a id="sourceLink" target="_blank" rel="noreferrer">Open source</a>
        </div>
        <dl id="metadata" class="metadata"></dl>
        <section class="evidence">
          <h3>Accepted filter paragraph</h3>
          <p id="seed"></p>
          <h3>Nearest semantic reference</h3>
          <p id="concept"></p>
        </section>
        <section id="passage" class="passage" aria-label="Extracted source story"></section>
        <section class="review-panel">
          <div>
            <h3>Human review</h3>
            <div class="segments" role="group" aria-label="Review decision">
              <button data-decision="selected" type="button">Selected</button>
              <button data-decision="rejected" type="button">Rejected</button>
              <button data-decision="unsure" type="button">Unsure</button>
              <button data-decision="unreviewed" type="button">Clear</button>
            </div>
          </div>
          <label>Reviewer<input id="reviewer" type="text"></label>
          <label class="notes">Notes<textarea id="notes" rows="3"></textarea></label>
          <button id="saveReview" class="primary" type="button">Save review</button>
          <p id="message" role="status"></p>
        </section>
      </div>
    </article>
  </main>
  <script src="/app.js"></script>
</body>
</html>
"""

_CSS = """
:root{color-scheme:light;--ink:#17201d;--muted:#66716c;--line:#d6ddd9;--paper:#f4f6f5;--white:#fff;--green:#176b4d;--green-soft:#e3f1ea;--red:#9b3f38;--red-soft:#f7e9e7;--blue:#176991;--blue-soft:#e7f1f6;--amber:#805c13;--amber-soft:#f7f0dd}
*{box-sizing:border-box}html,body{height:100%;max-width:100%}body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;letter-spacing:0;overflow:hidden}button,input,select,textarea{font:inherit;letter-spacing:0}button{border:1px solid transparent;border-radius:5px;min-height:36px;padding:7px 12px;font-weight:650;cursor:pointer}button:disabled{cursor:default;opacity:.45}.primary{background:var(--green);color:#fff}.secondary{background:var(--white);border-color:#b9c4bf;color:#2c3934}input,select,textarea{width:100%;border:1px solid #bac5c0;border-radius:5px;background:var(--white);color:var(--ink)}input,select{height:36px;padding:0 9px}textarea{padding:9px;resize:vertical}button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,a:focus-visible{outline:3px solid color-mix(in srgb,var(--blue) 30%,transparent);outline-offset:2px}
header{height:76px;padding:13px 22px;background:var(--white);border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;gap:20px}.brand p{margin:0;color:var(--green);font-size:11px;font-weight:750;text-transform:uppercase}.brand h1{font-size:22px;line-height:1.15;margin:2px 0}.header-status{display:flex;align-items:center;gap:14px;color:var(--muted);font-size:13px}
.toolbar{height:72px;padding:10px 18px;border-bottom:1px solid var(--line);background:#fafbfa;display:grid;grid-template-columns:minmax(220px,2fr) repeat(5,minmax(120px,1fr));gap:10px;align-items:end}.toolbar label,.review-panel label{display:block;min-width:0;color:#44504b;font-size:11px;font-weight:700}.toolbar .search{min-width:0}
main{height:calc(100vh - 148px);display:grid;grid-template-columns:370px minmax(0,1fr);max-width:1680px;margin:0 auto;background:var(--white);border-left:1px solid var(--line);border-right:1px solid var(--line)}.results{min-width:0;border-right:1px solid var(--line);display:grid;grid-template-rows:48px minmax(0,1fr) 52px}.results-head,.pager{padding:9px 12px;display:flex;align-items:center;justify-content:space-between;gap:8px;border-bottom:1px solid var(--line)}.results-head span{color:var(--muted);font-size:12px}.pager{border-top:1px solid var(--line);border-bottom:0}.pager button{width:48%}.story-list{overflow:auto}.story-row{width:100%;min-height:118px;padding:13px 14px;border:0;border-bottom:1px solid #e7ebe9;border-radius:0;background:var(--white);text-align:left;display:grid;gap:7px}.story-row:hover{background:#f5f8f6}.story-row.active{background:var(--blue-soft);box-shadow:inset 3px 0 var(--blue)}.row-head{display:flex;justify-content:space-between;gap:8px;color:var(--muted);font-size:11px}.row-seed{font-family:Georgia,serif;font-size:15px;line-height:1.45;color:var(--ink);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.row-meta{display:flex;justify-content:space-between;gap:8px;color:var(--muted);font-size:11px}.decision{font-weight:700}.decision.selected{color:var(--green)}.decision.rejected{color:var(--red)}.decision.unsure{color:var(--amber)}
.detail{min-width:0;overflow:auto;padding:28px 38px 60px}.empty{padding:80px 0;color:var(--muted)}.empty h2{font-size:20px}.detail-head{display:flex;justify-content:space-between;align-items:flex-start;gap:22px}.detail-head h2{font:600 25px/1.3 Georgia,serif;margin:10px 0 0;max-width:900px}.detail-head a{color:var(--blue);white-space:nowrap}.tags{display:flex;flex-wrap:wrap;gap:6px}.tags span{border:1px solid var(--line);border-radius:4px;padding:3px 7px;color:#4e5a55;background:#fafbfa;font-size:11px;font-weight:700}.metadata{margin:22px 0;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border-top:1px solid var(--line);border-bottom:1px solid var(--line)}.metadata div{padding:11px 12px;border-right:1px solid var(--line)}.metadata div:last-child{border-right:0}.metadata dt{font-size:10px;text-transform:uppercase;color:var(--muted);font-weight:750}.metadata dd{margin:2px 0 0;font-weight:650;overflow-wrap:anywhere}.evidence{margin:24px 0;border-left:3px solid var(--blue);padding:4px 0 4px 16px}.evidence h3,.review-panel h3{font-size:12px;margin:0 0 5px;text-transform:uppercase;color:var(--muted)}.evidence p{margin:0 0 14px;font-family:Georgia,serif;font-size:17px;line-height:1.55}.passage{max-width:900px}.source-paragraph{font:17px/1.7 Georgia,serif;white-space:pre-wrap;overflow-wrap:anywhere;margin:0;padding:13px 0}.source-paragraph.seed{background:var(--green-soft);border-left:3px solid var(--green);margin:6px -14px;padding:14px}.source-paragraph.event{background:var(--amber-soft);border-left:3px solid var(--amber);margin:6px -14px;padding:14px}.role-label{display:block;font:700 10px/1.2 system-ui,sans-serif;text-transform:uppercase;color:var(--muted);margin-bottom:5px}.omission{margin:10px 0;padding:8px 0;border-top:1px dashed #aeb9b4;border-bottom:1px dashed #aeb9b4;color:var(--muted);font-size:12px;text-align:center}.review-panel{margin-top:34px;padding-top:22px;border-top:1px solid var(--line);display:grid;grid-template-columns:minmax(360px,1fr) 180px;gap:14px;align-items:end}.segments{display:flex;gap:6px}.segments button{background:var(--white);border-color:#b9c4bf}.segments button.active[data-decision=selected]{background:var(--green-soft);border-color:var(--green);color:var(--green)}.segments button.active[data-decision=rejected]{background:var(--red-soft);border-color:var(--red);color:var(--red)}.segments button.active[data-decision=unsure]{background:var(--amber-soft);border-color:var(--amber);color:var(--amber)}.segments button.active[data-decision=unreviewed]{background:var(--blue-soft);border-color:var(--blue);color:var(--blue)}.notes{grid-column:1/-1}.review-panel .primary{justify-self:start}.review-panel #message{margin:0;color:var(--muted)}
@media(max-width:1050px){body{overflow:auto}.toolbar{height:auto;grid-template-columns:repeat(3,minmax(0,1fr))}.toolbar .search{grid-column:1/-1}main{height:auto;min-height:calc(100vh - 190px);grid-template-columns:320px minmax(0,1fr)}.metadata{grid-template-columns:1fr 1fr}.metadata div:nth-child(2){border-right:0}.detail{padding:24px}.review-panel{grid-template-columns:1fr}}
@media(max-width:720px){body{overflow-x:hidden}header{height:auto;padding:12px 15px;align-items:flex-start;gap:10px}.brand{min-width:0}.brand h1{font-size:20px}.header-status{min-width:0;max-width:150px;align-items:stretch;flex-direction:column;gap:5px}.header-status span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:right;font-size:11px}.header-status button{padding-left:8px;padding-right:8px}.toolbar{grid-template-columns:minmax(0,1fr) minmax(0,1fr);padding:10px}.toolbar .search{grid-column:1/-1}main{display:block;width:100%;border:0}.results{width:100%;height:52vh;border-right:0;border-bottom:1px solid var(--line)}.story-row,.row-seed,.row-meta span{min-width:0;overflow-wrap:anywhere}.detail{width:100%;padding:20px 16px}.detail-head{display:block}.detail-head a{display:inline-block;margin-top:10px}.metadata{grid-template-columns:1fr}.metadata div{border-right:0;border-bottom:1px solid var(--line)}.segments{display:grid;grid-template-columns:1fr 1fr}.review-panel{display:block}.review-panel>*{margin-bottom:12px}.source-paragraph.seed,.source-paragraph.event{margin-left:0;margin-right:0}}
"""

_JS = """
const state={status:null,rows:[],offset:0,limit:30,total:0,selectedId:null,detail:null,decision:"unreviewed"};
const $=id=>document.getElementById(id);
async function api(path,options={}){const response=await fetch(path,{headers:{"Content-Type":"application/json"},...options});const body=await response.json();if(!response.ok)throw new Error(body.error||response.statusText);return body}
function option(select,value,label){const node=document.createElement("option");node.value=value;node.textContent=label;select.appendChild(node)}
function fillOptions(id,values,allLabel){const select=$(id),current=select.value;select.replaceChildren();option(select,"",allLabel);values.forEach(value=>option(select,value,value));select.value=current}
function filterQuery(){const query=new URLSearchParams({offset:String(state.offset),limit:String(state.limit),sort:$("sort").value});["search","language","domain","keyword","decision"].forEach(id=>{if($(id).value)query.set(id,$(id).value)});return query}
function decisionLabel(value){return value==="unreviewed"?"Unreviewed":value[0].toUpperCase()+value.slice(1)}
function renderStatus(){const s=state.status;$("coverage").textContent=`${s.stories} stories | ${s.reviewed} reviewed | ${s.decisions.selected} selected`;fillOptions("language",s.languages,"All languages");fillOptions("domain",s.domains,"All domains");fillOptions("keyword",s.keywords,"All keywords")}
function storyButton(row){const button=document.createElement("button");button.type="button";button.className="story-row"+(row.story_id===state.selectedId?" active":"");button.onclick=()=>selectStory(row.story_id);const head=document.createElement("div");head.className="row-head";const matches=document.createElement("span");matches.textContent=`Match ${(row.match_numbers||[]).join(", ")}`;const language=document.createElement("span");language.textContent=row.language;head.append(matches,language);const seed=document.createElement("div");seed.className="row-seed";seed.textContent=row.seed_paragraph||row.excerpt;const meta=document.createElement("div");meta.className="row-meta";const domain=document.createElement("span");domain.textContent=row.domain||"unknown source";const decision=document.createElement("span");decision.className=`decision ${row.decision}`;decision.textContent=decisionLabel(row.decision);meta.append(domain,decision);button.append(head,seed,meta);return button}
function renderList(){const list=$("storyList");list.replaceChildren(...state.rows.map(storyButton));$("resultCount").textContent=`${state.total} ${state.total===1?"story":"stories"}`;const start=state.total?state.offset+1:0,end=Math.min(state.offset+state.limit,state.total);$("pageText").textContent=`${start}-${end}`;$("previous").disabled=state.offset===0;$("next").disabled=state.offset+state.limit>=state.total}
function addTag(text){const span=document.createElement("span");span.textContent=text;$("tags").appendChild(span)}
function metadata(label,value){const div=document.createElement("div"),dt=document.createElement("dt"),dd=document.createElement("dd");dt.textContent=label;dd.textContent=value;div.append(dt,dd);return div}
function renderPassage(story){const root=$("passage");root.replaceChildren();const omissions=[...(story.omissions||[])].sort((a,b)=>a.after_paragraph_index-b.after_paragraph_index);let omissionIndex=0;(story.paragraphs||[]).forEach(paragraph=>{while(omissionIndex<omissions.length&&omissions[omissionIndex].after_paragraph_index<paragraph.paragraph_index){const omission=document.createElement("div");omission.className="omission";omission.textContent=`${omissions[omissionIndex].paragraph_count} intervening source paragraphs omitted`;root.appendChild(omission);omissionIndex++}const p=document.createElement("p");p.className="source-paragraph";if(paragraph.role==="seed")p.classList.add("seed");if(paragraph.role==="referenced_event")p.classList.add("event");if(paragraph.role==="seed"||paragraph.role==="referenced_event"){const label=document.createElement("span");label.className="role-label";label.textContent=paragraph.role==="seed"?"Accepted filter paragraph":"Referenced source event";p.appendChild(label)}p.appendChild(document.createTextNode(paragraph.text||""));root.appendChild(p)})}
function renderDecision(){document.querySelectorAll("[data-decision]").forEach(button=>button.classList.toggle("active",button.dataset.decision===state.decision))}
function renderDetail(){const row=state.detail;$("empty").hidden=Boolean(row);$("storyDetail").hidden=!row;if(!row)return;$("tags").replaceChildren();addTag(row.language||"unknown");(row.seed?.matched_keywords||[]).forEach(addTag);addTag(`${row.capture_count||1} capture${row.capture_count===1?"":"s"}`);$("detailTitle").textContent=row.seed?.paragraph||"Source story";$("sourceLink").href=row.url||"#";$("metadata").replaceChildren(metadata("Matches",(row.match_numbers||[]).join(", ")),metadata("Domain",row.domain||"unknown"),metadata("Semantic",String(row.seed?.semantic_score??"n/a")),metadata("Length",`${row.story?.character_count||0} characters`));$("seed").textContent=row.seed?.paragraph||"";$("concept").textContent=row.seed?.concept_match||"";renderPassage(row.story||{});state.decision=row.review?.decision||"unreviewed";$("notes").value=row.review?.notes||"";$("reviewer").value=row.review?.reviewer||localStorage.getItem("hometownReviewer")||"";renderDecision()}
async function loadStatus(){state.status=await api("/api/status");renderStatus()}
async function loadStories(reset=false){if(reset)state.offset=0;const result=await api("/api/stories?"+filterQuery());state.rows=result.stories;state.total=result.total;renderList();if(state.selectedId&&!state.rows.some(row=>row.story_id===state.selectedId)){state.selectedId=null;state.detail=null;renderDetail()}if(!state.selectedId&&state.rows.length)await selectStory(state.rows[0].story_id)}
async function selectStory(id){state.selectedId=id;state.detail=await api("/api/stories/"+encodeURIComponent(id));renderList();renderDetail()}
async function saveReview(){if(!state.selectedId)return;const storyId=state.selectedId,reviewer=$("reviewer").value.trim();localStorage.setItem("hometownReviewer",reviewer);try{await api("/api/review",{method:"POST",body:JSON.stringify({story_id:storyId,decision:state.decision,notes:$("notes").value,reviewer})});await Promise.all([loadStatus(),loadStories(false)]);if(state.selectedId===storyId){state.detail=await api("/api/stories/"+encodeURIComponent(storyId));renderDetail()}$("message").textContent="Review saved"}catch(error){$("message").textContent=error.message}}
async function exportSelected(){try{const result=await api("/api/export",{method:"POST",body:"{}"});$("coverage").textContent=`Exported ${result.selected_stories} selected stories`}catch(error){$("coverage").textContent=error.message}}
let searchTimer;["language","domain","keyword","decision","sort"].forEach(id=>$(id).onchange=()=>loadStories(true));$("search").oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>loadStories(true),180)};$("previous").onclick=()=>{state.offset=Math.max(0,state.offset-state.limit);loadStories()};$("next").onclick=()=>{state.offset+=state.limit;loadStories()};document.querySelectorAll("[data-decision]").forEach(button=>button.onclick=()=>{state.decision=button.dataset.decision;renderDecision()});$("saveReview").onclick=saveReview;$("export").onclick=exportSelected;
Promise.all([loadStatus(),loadStories(true)]).catch(error=>{$("coverage").textContent=error.message});
"""


class _StoryWorkbenchHandler(BaseHTTPRequestHandler):
    server_version = "HometownXRStoryWorkbench/1"

    @property
    def index(self) -> StoryReviewIndex:
        return self.server.story_index

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "connect-src 'self'",
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
                self._json(self.index.status())
                return
            if parsed.path == "/api/stories":
                query = parse_qs(parsed.query)

                def value(key: str, default: str = "") -> str:
                    return (query.get(key) or [default])[0]

                self._json(
                    self.index.query(
                        search=value("search"),
                        language=value("language"),
                        domain=value("domain"),
                        keyword=value("keyword"),
                        decision=value("decision"),
                        sort=value("sort", "match"),
                        offset=int(value("offset", "0")),
                        limit=int(value("limit", "50")),
                    )
                )
                return
            prefix = "/api/stories/"
            if parsed.path.startswith(prefix):
                self._json(self.index.detail(unquote(parsed.path[len(prefix) :])))
                return
        except KeyError as exc:
            self._error(exc, HTTPStatus.NOT_FOUND)
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
            if self.path == "/api/review":
                self._json(
                    self.index.review(
                        str(payload.get("story_id", "")),
                        str(payload.get("decision", "")),
                        notes=str(payload.get("notes", "")),
                        reviewer=str(payload.get("reviewer", "")),
                    )
                )
                return
            if self.path == "/api/export":
                self._json(
                    export_reviewed_stories(
                        self.index.export_path,
                        self.index.reviews_path,
                    )
                )
                return
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._error(exc)

    def log_message(self, format: str, *args) -> None:
        logger.debug(format, *args)


def serve_story_workbench(
    host: str = "127.0.0.1",
    port: int = 8770,
    open_browser: bool = False,
) -> None:
    """Serve the local story explorer until interrupted."""
    index = StoryReviewIndex()
    server = ThreadingHTTPServer((host, port), _StoryWorkbenchHandler)
    server.story_index = index
    url = f"http://{host}:{server.server_port}/"
    logger.info("Story explorer: %s", url)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
