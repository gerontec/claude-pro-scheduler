"""ClassDiagram — zeichnet das OO-Modell des Batch-Servers als PNG (graphviz dot)."""
import subprocess


DOT_SOURCE = """\
digraph BatchServer {
    graph [
        label="Batch-Job-Server — OO-Klassendiagramm"
        labelloc=t
        fontsize=16
        fontname="DejaVu Sans"
        bgcolor="#f8fafc"
        rankdir=TB
        splines=ortho
        nodesep=0.6
        ranksep=0.8
    ]
    node [shape=record fontname="DejaVu Sans" fontsize=9 style=filled]
    edge [fontname="DejaVu Sans" fontsize=8]

    // ── Entry Points ──────────────────────────────────────────
    dispatcher_ep [label="{dispatcher.py\\n(Entry Point)|main()\\l→ Dispatcher().run()\\l}"
                   fillcolor="#fee2e2" color="#475569"]
    poller_ep     [label="{poller.py\\n(Entry Point)|main()\\l→ JobProcessor.process()\\l}"
                   fillcolor="#fee2e2" color="#475569"]

    // ── Kern-Klassen ──────────────────────────────────────────
    Dispatcher [label="{Dispatcher|_notifier: Notifier\\l|run(n=MAX_RUNNING)\\l_cleanup_zombies()\\l}"
                fillcolor="#dcfce7" color="#475569"]

    JobProcessor [label="{JobProcessor|_repo: JobRepository\\l_runners: dict[str,Runner]\\l_context: ContextBuilder\\l_notifier: Notifier\\l_tracker: UsageTracker\\l|process(job)\\l_execute(job)\\l_maybe_escalate(job,run)\\l}"
                  fillcolor="#dcfce7" color="#475569"]

    JobRepository [label="{JobRepository|_db: Connection (Pool)\\l_ctx_cache (TTL=300s)\\l|claim_next() → JobRecord\\lwrite_result(id, run)\\lis_killed(id) → bool\\lget_context_blocks()\\lescalate_to_sonnet(id)\\lsave_openrouter_balance()\\l}"
                   fillcolor="#dcfce7" color="#475569"]

    // ── Support-Klassen ───────────────────────────────────────
    ContextBuilder [label="{ContextBuilder|_repo: JobRepository\\l|build_prompt(job) → str\\lsystem_prompt() → str\\lneeds_escalation(...)\\l}"
                    fillcolor="#f3e8ff" color="#475569"]

    Notifier [label="{Notifier||notify(job, run)\\lsend_mail_direct(...)\\l_send_mail(...)\\l→ PdfRenderer.render()\\l→ smtplib.SMTP(localhost)\\l}"
              fillcolor="#f3e8ff" color="#475569"]

    PdfRenderer [label="{PdfRenderer|FONT = DejaVu (Unicode)\\lMONO = DejaVuMono\\l|render(id,model,...) → bytes\\l_render_line(pdf, line)\\l}"
                 fillcolor="#f3e8ff" color="#475569"]

    UsageTracker [label="{UsageTracker|file: str (JSON)\\l|record(run)\\l_load() / _save()\\l_week_start()\\l}"
                  fillcolor="#f3e8ff" color="#475569"]

    // ── Runner ────────────────────────────────────────────────
    ModelRunner [label="{\\<\\<abstract\\>\\>\\nModelRunner||run(prompt, sys, job_id, cb) → RunResult\\l}"
                 fillcolor="#fef9c3" color="#475569"]

    OpenRouterRunner [label="{OpenRouterRunner|model_id: str\\lapi_key: str\\lstall_counts: dict\\l|run() — agentic loop\\l_call_api_with_retry()\\l_exec_tool(cmd, timeout)\\l}"
                      fillcolor="#fef9c3" color="#475569"]

    ClaudeCliRunner [label="{ClaudeCliRunner|model: str\\lTIMEOUT_SEC = 14400\\l|run() → subprocess\\lclaud --model sonnet\\l_run_process(...)\\l}"
                     fillcolor="#fef9c3" color="#475569"]

    // ── Dataclasses ───────────────────────────────────────────
    JobRecord [label="{\\<\\<dataclass\\>\\>\\nJobRecord|id: int\\lmodel: str\\lprompt: str\\ltargetdate: date\\lresume_session: bool\\l}"
               fillcolor="#dbeafe" color="#475569"]

    RunResult [label="{\\<\\<dataclass\\>\\>\\nRunResult|result: str\\lstatus: done|failed\\lin_tok: int\\lout_tok: int\\lcache_tok: int\\lcost: float\\lerror: str\\liters: int\\l}"
               fillcolor="#dbeafe" color="#475569"]

    config [label="{config.py|get_connection() Pool(20)\\lrelease_connection()\\lMAX_RUNNING = 16\\lMAX_TOOL_ITERATIONS = 30\\lSYSTEM_PROMPT\\l}"
            fillcolor="#dbeafe" color="#475569"]

    // ── Entry Point → Klassen ─────────────────────────────────
    dispatcher_ep -> Dispatcher    [arrowhead=vee style=solid color="#166534"]
    poller_ep     -> JobProcessor  [arrowhead=vee style=solid color="#166534"]

    // ── Composition ───────────────────────────────────────────
    Dispatcher    -> JobProcessor   [arrowhead=vee style=solid color="#166534"]
    Dispatcher    -> Notifier       [arrowhead=open style=dashed color="#475569" label="send_mail_direct"]
    JobProcessor  -> JobRepository  [arrowhead=vee style=solid color="#166534"]
    JobProcessor  -> ContextBuilder [arrowhead=vee style=solid color="#166534"]
    JobProcessor  -> Notifier       [arrowhead=vee style=solid color="#166534"]
    JobProcessor  -> UsageTracker   [arrowhead=vee style=solid color="#166534"]
    JobProcessor  -> ModelRunner    [arrowhead=open style=dashed color="#475569"]
    ContextBuilder -> JobRepository [arrowhead=open style=dashed color="#475569"]
    Notifier      -> PdfRenderer    [arrowhead=vee style=solid color="#166534"]
    config        -> JobRepository  [arrowhead=open style=dashed color="#475569" label="Pool"]

    // ── Vererbung ─────────────────────────────────────────────
    OpenRouterRunner -> ModelRunner [arrowhead=onormal color="#1e40af"]
    ClaudeCliRunner  -> ModelRunner [arrowhead=onormal color="#1e40af"]
}
"""


def render_png() -> bytes:
    """Erzeugt das Klassendiagramm via graphviz dot und gibt PNG-Bytes zurück."""
    result = subprocess.run(
        ['dot', '-Tpng', '-Gdpi=150'],
        input=DOT_SOURCE.encode('utf-8'),
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"dot fehlgeschlagen: {result.stderr.decode()}")
    return result.stdout
