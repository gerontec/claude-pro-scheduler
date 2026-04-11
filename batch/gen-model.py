#!/usr/bin/env python3
"""Generiert /var/www/html/api/batch/model.html mit Mermaid-Diagrammen.
Liest repository.py für aktuelle Methoden, fügt Versionsnr + Timestamp hinzu.
"""
import re, os, sys, subprocess
from datetime import datetime

BATCH_DIR = '/home/gh/batch'
OUT_HTML  = '/var/www/html/api/batch/model.html'

def extract_methods(pyfile):
    """Extrahiert Klassen und Methoden aus einer .py-Datei."""
    with open(pyfile) as f:
        src = f.read()
    classes = {}
    current = None
    for line in src.split('\n'):
        cm = re.match(r'^class\s+(\w+)', line)
        if cm:
            current = cm.group(1)
            classes[current] = []
            continue
        fm = re.match(r'^    def\s+(\w+)\s*\(', line)
        if fm and current:
            vis = '+' if not fm.group(1).startswith('_') else '-'
            classes[current].append(f"{vis}{fm.group(1)}()")
    return classes

# Alle Methoden sammeln
all_methods = {}
for fn in os.listdir(BATCH_DIR):
    if fn.endswith('.py') and not fn.startswith('__') and fn not in ('gen-model.py',):
        path = os.path.join(BATCH_DIR, fn)
        all_methods.update(extract_methods(path))

# Runners
runners_dir = os.path.join(BATCH_DIR, 'runners')
if os.path.isdir(runners_dir):
    for fn in os.listdir(runners_dir):
        if fn.endswith('.py') and fn != '__init__.py':
            path = os.path.join(runners_dir, fn)
            all_methods.update(extract_methods(path))

# Mermaid-Klassendiagramm bauen
now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
version = datetime.now().strftime('%Y%m%d%H%M')

def methods_to_mermaid(cls_name):
    if cls_name not in all_methods:
        return ''
    return '\n'.join(f'        {m}' for m in all_methods[cls_name])

# Komplette Mermaid-Definitionen für alle 4 Diagramme
# (Klassendiagramm, Sequenz, Module, Flow)
mermaid_class = f"""classDiagram
    %% Version: {version} | Updated: {now}
    class claude_pro_batch {{
        +bigint id
        +date targetdate
        +enum model
        +bool resume_session
        +text prompt
        +enum status
        +longtext result
        +int input_tokens
        +int output_tokens
        +int cache_tokens
        +decimal cost_usd
        +tinyint progress
        +datetime started_at
        +datetime finished_at
        +text error_msg
        +int pid
    }}
    class JobRecord {{
        +int id
        +str model
        +str prompt
        +date targetdate
        +bool resume_session
    }}
    class RunResult {{
        +str result
        +str status
        +int in_tok
        +int out_tok
        +int cache_tok
        +float cost
        +str error
        +int iters
    }}
    class JobRepository {{
        +VALID_TRANSITIONS dict
{methods_to_mermaid('JobRepository')}
    }}
    class ContextRepository {{
{methods_to_mermaid('ContextRepository')}
    }}
    class ModelRunner {{
        <<interface>>
        +run(prompt, sys, job_id, cb, max_iter, tools) RunResult
    }}
    class OpenRouterHttpClient {{
{methods_to_mermaid('OpenRouterHttpClient')}
    }}
    class OpenRouterRunner {{
        -model_id str
        -api_key str
{methods_to_mermaid('OpenRouterRunner')}
    }}
    class ClaudeCliRunner {{
        -model str
{methods_to_mermaid('ClaudeCliRunner')}
    }}
    class JobPhase {{
        <<abstract>>
        +max_iter(ctx) int
        +system_prompt(ctx) str
        +user_prompt(ctx) str
        +tools() list
        +on_complete(ctx, run)
    }}
    class PlannerPhase {{
        +name = planner
        +max_iter = 6
    }}
    class ExecutorPhase {{
        +name = executor
        +max_iter = steps*3+2
    }}
    class ReporterPhase {{
        +name = reporter
        +max_iter = 3
        +tools = empty
    }}
    class PhaseContext {{
        +job JobRecord
        +plan_path str
        +infra str
        +plan str
        +step_count int
        +total_in int
        +total_out int
        +total_cache int
        +total_cost float
        +iters int
    }}
    class JobPipeline {{
{methods_to_mermaid('JobPipeline')}
    }}
    class ContextBuilder {{
{methods_to_mermaid('ContextBuilder')}
    }}
    class Notifier {{
{methods_to_mermaid('Notifier')}
    }}
    class PdfRenderer {{
{methods_to_mermaid('PdfRenderer')}
    }}
    class UsageTracker {{
{methods_to_mermaid('UsageTracker')}
    }}
    class JobProcessor {{
{methods_to_mermaid('JobProcessor')}
    }}
    class Dispatcher {{
{methods_to_mermaid('Dispatcher')}
    }}

    JobRepository ..> claude_pro_batch : reads/writes
    JobRepository --> JobRecord : maps row
    ModelRunner <|-- OpenRouterRunner
    ModelRunner <|-- ClaudeCliRunner
    OpenRouterRunner --> OpenRouterHttpClient
    JobPhase <|-- PlannerPhase
    JobPhase <|-- ExecutorPhase
    JobPhase <|-- ReporterPhase
    JobPipeline --> JobPhase
    JobPipeline --> ModelRunner
    JobProcessor --> JobRepository
    JobProcessor --> JobPipeline
    JobProcessor --> ContextBuilder
    JobProcessor --> Notifier
    JobProcessor --> UsageTracker
    Dispatcher --> JobProcessor
    ContextBuilder --> ContextRepository
    Notifier --> PdfRenderer
"""

mermaid_seq = """sequenceDiagram
    participant Cron
    participant Dispatcher
    participant Poller
    participant Repo as JobRepository
    participant Pipe as JobPipeline
    participant Runner as ModelRunner
    participant Agent as OpenRouter API
    participant Notify as Notifier

    Cron->>Dispatcher: every minute
    Dispatcher->>Repo: cleanup_zombies()
    Dispatcher->>Poller: spawn N instances

    Poller->>Repo: claim_next() [SELECT FOR UPDATE]
    Repo-->>Poller: JobRecord

    Poller->>JobProcessor: process(job)
    JobProcessor->>Pipe: pipeline.run(job)

    rect rgb(30,50,30)
        note over Pipe,Agent: Phase 1 — Planner (max 6 iter)
        Pipe->>Runner: run(planner_prompt, max_iter=6)
        Runner->>Agent: API call
        Agent-->>Runner: exec: write plan file
        Runner-->>Agent: tool result
        Agent-->>Runner: finish_reason=stop
        Runner-->>Pipe: RunResult
        Pipe->>Pipe: on_complete → read plan file
    end

    rect rgb(30,30,60)
        note over Pipe,Agent: Phase 2 — Executor (max steps×3+2 iter)
        Pipe->>Runner: run(executor_prompt, max_iter=N)
        loop Für jeden Plan-Schritt
            Runner->>Agent: API call
            Agent-->>Runner: exec: bash cmd
            Runner-->>Agent: tool result
        end
        Agent-->>Runner: finish_reason=stop
        Runner-->>Pipe: RunResult
    end

    rect rgb(60,30,30)
        note over Pipe,Agent: Phase 3 — Reporter (max 3 iter, no tools)
        Pipe->>Runner: run(reporter_prompt, max_iter=3, tools=[])
        Runner->>Agent: API call (no tools)
        Agent-->>Runner: finish_reason=stop (pure text)
        Runner-->>Pipe: RunResult
        Pipe->>Repo: UPDATE result (on_complete)
    end

    Pipe-->>JobProcessor: RunResult(status=done)
    JobProcessor->>Repo: complete_job → RunResult
    JobProcessor->>Notify: notify(job, run)
    Notify->>Notify: _mail → PDF
    Notify->>Notify: _mqtt
"""

mermaid_modules = """graph LR
    subgraph dispatcher[dispatcher.py]
        D[Dispatcher]
    end
    subgraph poller[poller.py]
        P[Poller]
    end
    subgraph processor[processor.py]
        JP[JobProcessor]
    end
    subgraph pipeline[pipeline.py]
        PL[JobPipeline]
        PH1[PlannerPhase]
        PH2[ExecutorPhase]
        PH3[ReporterPhase]
    end
    subgraph repository[repository.py]
        R[JobRepository\n+VALID_TRANSITIONS\n+transition_status]
    end
    subgraph context_repo[context_repo.py]
        CR[ContextRepository]
    end
    subgraph runners["runners/"]
        OR[OpenRouterRunner]
        CC[ClaudeCliRunner]
        OH[OpenRouterHttpClient]
    end
    subgraph context[context.py]
        CB[ContextBuilder]
    end
    subgraph notify[notifier.py]
        N[Notifier]
        PR[PdfRenderer]
    end
    subgraph tracker[tracker.py]
        UT[UsageTracker]
    end
    subgraph db["MariaDB wagodb"]
        T1[claude_pro_batch]
        T2[ki_localhost_cache]
        T3[ki_infrastructure]
    end

    D --> P
    P --> JP
    JP --> PL
    JP --> R
    JP --> CB
    JP --> N
    JP --> UT
    PL --> PH1
    PL --> PH2
    PL --> PH3
    PL --> OR
    PL --> CC
    OR --> OH
    CB --> CR
    CR --> R
    R --> T1
    R --> T2
    R --> T3
    N --> PR
"""

mermaid_flow = """flowchart TD
    Start([Cron trigger]) --> Spawn[Spawn Dispatcher]
    Spawn --> Zombies[cleanup_zombies]
    Zombies --> CalcSlots[Calculate free slots]
    CalcSlots -->|no slots| Wait([Wait next cron])
    CalcSlots -->|slots free| Poller[Spawn Poller process]

    Poller --> Claim[claim_next\nSELECT FOR UPDATE]
    Claim -->|no job| Wait
    Claim -->|job found| Execute[JobProcessor._execute]
    Execute --> Pipeline[JobPipeline.run]

    Pipeline --> Planner[Phase 1: Planner\nmax_iter=6\nschreibt job-ID.plan]
    Planner -->|failed| PlanFail[Job failed]
    Planner -->|done| Executor[Phase 2: Executor\nmax_iter=steps×3+2\nführt Plan-Schritte aus]
    Executor -->|failed + plan exists| Reporter
    Executor -->|failed, no plan| ExecFail[Job failed]
    Executor -->|done| Reporter[Phase 3: Reporter\nmax_iter=3, no tools\ngibt reinen Text zurück]
    Reporter -->|done| DBWrite[on_complete\nschreibt result in DB]
    DBWrite --> Quality[_enforce_quality\nmin 400 Zeichen\nmin 2 Abschnitte]

    Quality -->|fail| Requeue[requeue_with_feedback\nneuer Job mit Fehler-Kontext]
    Quality -->|pass| Complete[complete_job\nmerge DB-result\ntransition_status SM]

    Complete --> Escalate{escalation\ncheck?}
    Escalate -->|yes| EscJob[escalate_to_sonnet]
    Escalate -->|no| RecordUsage[UsageTracker.record]
    EscJob --> RecordUsage
    RecordUsage --> FetchBal[Fetch OpenRouter balance]
    FetchBal --> NotifyCheck{status == done?}
    NotifyCheck -->|yes| Notify[Notifier.notify\nPDF + Mail + MQTT]
    NotifyCheck -->|no| UpdateCache
    Notify --> UpdateCache[Update session cache]
    UpdateCache --> Done([Job complete])
"""

# HTML zusammenbauen
html = f'''<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<title>Batch Job System — Objektmodell v{version}</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<style>
  :root {{
    --bg:       #0d1117;
    --surface:  #161b22;
    --border:   #30363d;
    --text:     #c9d1d9;
    --muted:    #8b949e;
    --accent:   #58a6ff;
    --accent2:  #3fb950;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: .85rem;
    padding: 1.2rem 2rem 3rem;
  }}
  h1 {{
    font-size: 1.05rem;
    color: var(--accent);
    border-bottom: 1px solid var(--border);
    padding-bottom: .5rem;
    margin-bottom: 1.2rem;
    letter-spacing: .02em;
  }}
  h2 {{
    font-size: .72rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: .08em;
    margin: 1.6rem 0 .5rem;
  }}
  .version {{
    font-size: .7rem;
    color: var(--muted);
    margin-bottom: 1rem;
  }}
  a {{ color: var(--accent); text-decoration: none; font-size: .8rem; }}
  a:hover {{ text-decoration: underline; }}
  .back {{ margin-bottom: 1rem; display: inline-block; }}

  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }}
  @media(max-width:900px){{ .grid {{ grid-template-columns: 1fr; }} }}

  .card {{
    position: relative;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
  }}
  .card-header {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: .5rem .75rem;
    border-bottom: 1px solid var(--border);
    background: #1c2128;
  }}
  .card-title {{
    font-size: .72rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: .08em;
  }}
  .fs-btn {{
    background: none;
    border: 1px solid var(--border);
    color: var(--muted);
    border-radius: 4px;
    width: 26px; height: 26px;
    cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    font-size: .8rem;
    transition: color .15s, border-color .15s;
  }}
  .fs-btn:hover {{ color: var(--accent); border-color: var(--accent); }}
  .fs-btn:disabled {{ opacity: .3; cursor: default; }}

  .diagram-wrap {{
    padding: 1rem;
    overflow: auto;
    min-height: 120px;
    display: flex;
    align-items: center;
    justify-content: center;
  }}
  .diagram-wrap svg {{ max-width: 100%; height: auto; }}
  .diagram-wrap.loading::after {{
    content: 'rendering…';
    color: var(--muted);
    font-size: .75rem;
    animation: blink 1s infinite;
  }}
  @keyframes blink {{ 0%,100%{{opacity:1}} 50%{{opacity:.3}} }}

  #fs-overlay {{
    display: none;
    position: fixed;
    inset: 0;
    z-index: 3000;
    background: var(--bg);
    flex-direction: column;
  }}
  #fs-overlay.open {{ display: flex; }}

  #fs-topbar {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: .6rem 1rem;
    border-bottom: 1px solid var(--border);
    background: #1c2128;
    flex-shrink: 0;
  }}
  #fs-label {{ font-size: .8rem; color: var(--muted); }}

  .fs-controls {{ display: flex; gap: .4rem; align-items: center; }}
  .ctrl-btn {{
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text);
    border-radius: 4px;
    padding: .25rem .55rem;
    cursor: pointer;
    font-size: .8rem;
    transition: border-color .15s, color .15s;
  }}
  .ctrl-btn:hover {{ border-color: var(--accent); color: var(--accent); }}

  #fs-viewport {{
    flex: 1;
    overflow: hidden;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: grab;
    user-select: none;
  }}
  #fs-viewport.grabbing {{ cursor: grabbing; }}
  #fs-inner {{
    width: 95vw;
    height: 90vh;
    transform-origin: center center;
  }}
  #fs-inner svg {{
    display: block;
    width: 100%;
    height: 100%;
  }}

  #fs-hint {{
    font-size: .7rem;
    color: var(--muted);
    padding: .3rem 1rem;
    border-top: 1px solid var(--border);
    text-align: center;
    flex-shrink: 0;
  }}
</style>
</head>
<body>

<a class="back" href="index.php">← zurück zur Batch-UI</a>
<h1>🤖 Batch Job System — Objektmodell</h1>
<div class="version">Version {version} | Generiert: {now} | Cronjob: <code>0 4 * * * python3 /home/gh/batch/gen-model.py &gt;&gt; /tmp/gen-model.log 2&gt;&amp;1</code> (täglich 04:00)</div>

<div class="grid">

  <div>
    <div class="card" id="card-class">
      <div class="card-header">
        <span class="card-title">Klassenstruktur</span>
        <button class="fs-btn" title="Fullscreen" data-target="class" disabled>⛶</button>
      </div>
      <div class="diagram-wrap loading" id="wrap-class"></div>
    </div>
  </div>

  <div>
    <div class="card" id="card-seq">
      <div class="card-header">
        <span class="card-title">Job-Lifecycle (Sequenz)</span>
        <button class="fs-btn" title="Fullscreen" data-target="seq" disabled>⛶</button>
      </div>
      <div class="diagram-wrap loading" id="wrap-seq"></div>
    </div>
  </div>

</div>

<h2>Modul-Übersicht</h2>
<div class="card">
  <div class="card-header">
    <span class="card-title">Module</span>
    <button class="fs-btn" title="Fullscreen" data-target="modules" disabled>⛶</button>
  </div>
  <div class="diagram-wrap loading" id="wrap-modules"></div>
</div>

<h2>Datenfluss</h2>
<div class="card">
  <div class="card-header">
    <span class="card-title">Datenfluss</span>
    <button class="fs-btn" title="Fullscreen" data-target="flow" disabled>⛶</button>
  </div>
  <div class="diagram-wrap loading" id="wrap-flow"></div>
</div>

<!-- Fullscreen overlay -->
<div id="fs-overlay">
  <div id="fs-topbar">
    <span id="fs-label"></span>
    <div class="fs-controls">
      <button class="ctrl-btn" id="btn-zoomin">＋</button>
      <button class="ctrl-btn" id="btn-zoomout">－</button>
      <button class="ctrl-btn" id="btn-reset">⟳ Reset</button>
      <button class="ctrl-btn" id="btn-close">✕ Schließen</button>
    </div>
  </div>
  <div id="fs-viewport">
    <div id="fs-inner"></div>
  </div>
  <div id="fs-hint">Scroll = Zoom | Drag = Pan | Esc = Schließen</div>
</div>

<!-- Mermaid source definitions (hidden) -->
<script id="src-class" type="text/x-mermaid">
{mermaid_class}
</script>

<script id="src-seq" type="text/x-mermaid">
{mermaid_seq}
</script>

<script id="src-modules" type="text/x-mermaid">
{mermaid_modules}
</script>

<script id="src-flow" type="text/x-mermaid">
{mermaid_flow}
</script>

<script>
mermaid.initialize({{
  startOnLoad: false,
  theme: 'dark',
  themeVariables: {{
    darkMode: true,
    background: '#0d1117',
    primaryColor: '#1f6feb',
    primaryTextColor: '#c9d1d9',
    primaryBorderColor: '#30363d',
    lineColor: '#58a6ff',
    secondaryColor: '#161b22',
    tertiaryColor: '#21262d',
    fontSize: '13px'
  }},
  class: {{ useMaxWidth: true }},
  sequence: {{ useMaxWidth: true, actorMargin: 50 }},
  flowchart: {{ useMaxWidth: true, curve: 'basis' }}
}});

const diagrams = [
  {{ id: 'class', src: 'src-class', wrap: 'wrap-class', card: 'card-class' }},
  {{ id: 'seq', src: 'src-seq', wrap: 'wrap-seq', card: 'card-seq' }},
  {{ id: 'modules', src: 'src-modules', wrap: 'wrap-modules', card: null }},
  {{ id: 'flow', src: 'src-flow', wrap: 'wrap-flow', card: null }}
];

async function renderAll() {{
  for (const d of diagrams) {{
    const srcEl = document.getElementById(d.src);
    const wrapEl = document.getElementById(d.wrap);
    const code = srcEl.textContent.trim();
    try {{
      const {{ svg }} = await mermaid.render('svg-' + d.id, code);
      wrapEl.innerHTML = svg;
      wrapEl.classList.remove('loading');
      const btn = wrapEl.parentElement.querySelector('.fs-btn');
      if (btn) btn.disabled = false;
    }} catch (e) {{
      wrapEl.innerHTML = '<pre style="color:#f85149;font-size:.75rem">' +
        d.id + ': ' + e.message + '</pre>';
      wrapEl.classList.remove('loading');
    }}
  }}
}}
renderAll();

// Fullscreen logic
const overlay = document.getElementById('fs-overlay');
const inner = document.getElementById('fs-inner');
const label = document.getElementById('fs-label');
let scale = 1, panX = 0, panY = 0;

document.querySelectorAll('.fs-btn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    const target = btn.dataset.target;
    const wrap = document.getElementById('wrap-' + target);
    const svg = wrap.querySelector('svg');
    if (!svg) return;
    inner.innerHTML = '';
    inner.appendChild(svg.cloneNode(true));
    label.textContent = target;
    scale = 1; panX = 0; panY = 0;
    updateTransform();
    overlay.classList.add('open');
  }});
}});

document.getElementById('btn-close').onclick = () => overlay.classList.remove('open');
document.getElementById('btn-zoomin').onclick = () => {{ scale *= 1.3; updateTransform(); }};
document.getElementById('btn-zoomout').onclick = () => {{ scale /= 1.3; updateTransform(); }};
document.getElementById('btn-reset').onclick = () => {{ scale=1; panX=0; panY=0; updateTransform(); }};
document.addEventListener('keydown', e => {{ if (e.key === 'Escape') overlay.classList.remove('open'); }});

const viewport = document.getElementById('fs-viewport');
viewport.addEventListener('wheel', e => {{
  e.preventDefault();
  scale *= e.deltaY < 0 ? 1.1 : 0.9;
  updateTransform();
}}, {{ passive: false }});

let dragging = false, dragStartX, dragStartY;
viewport.addEventListener('mousedown', e => {{
  dragging = true;
  dragStartX = e.clientX - panX;
  dragStartY = e.clientY - panY;
  viewport.classList.add('grabbing');
}});
document.addEventListener('mousemove', e => {{
  if (!dragging) return;
  panX = e.clientX - dragStartX;
  panY = e.clientY - dragStartY;
  updateTransform();
}});
document.addEventListener('mouseup', () => {{
  dragging = false;
  viewport.classList.remove('grabbing');
}});

function updateTransform() {{
  inner.style.transform = `translate(${{panX}}px,${{panY}}px) scale(${{scale}})`;
}}
</script>

</body>
</html>
'''

with open(OUT_HTML, 'w') as f:
    f.write(html)

print(f"OK: {OUT_HTML} geschrieben ({len(html)} Bytes)")
print(f"Version: {version}")
print(f"Timestamp: {now}")

# Verifizieren
for cls in ['JobRepository', 'JobProcessor', 'Dispatcher']:
    if cls in all_methods:
        print(f"  {cls}: {len(all_methods[cls])} Methoden")
