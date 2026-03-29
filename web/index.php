<?php
// ── DB-Verbindung ──────────────────────────────────────────
$pdo = new PDO('mysql:host=localhost;dbname=wagodb;charset=utf8mb4', 'gh', 'a12345',
    [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]);

$msg = '';

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    if (isset($_POST['submit_job'])) {
        $targetdate     = $_POST['targetdate'] ?: date('Y-m-d');
        $model          = in_array($_POST['model'], ['haiku','sonnet','opus']) ? $_POST['model'] : 'haiku';
        $prompt         = trim($_POST['prompt']);
        $resume_session = isset($_POST['resume_session']) ? 1 : 0;
        if ($prompt) {
            $stmt = $pdo->prepare("INSERT INTO claude_pro_batch (targetdate, model, resume_session, prompt) VALUES (?,?,?,?)");
            $stmt->execute([$targetdate, $model, $resume_session, $prompt]);
            $id = $pdo->lastInsertId();
            header("Location: ?msg=ok&job=$id&model=$model&date=$targetdate");
        } else {
            header("Location: ?msg=err");
        }
        exit;
    }
    if (isset($_POST['cancel_job'])) {
        $id = (int)$_POST['cancel_job'];
        $pdo->exec("UPDATE claude_pro_batch SET status='failed', error_msg='Abgebrochen' WHERE id=$id AND status='queued'");
        header("Location: ?msg=cancelled&job=$id");
        exit;
    }
    if (isset($_POST['delete_job'])) {
        $id = (int)$_POST['delete_job'];
        $pdo->exec("DELETE FROM claude_pro_batch WHERE id=$id AND status IN ('queued','done','failed')");
        header("Location: ?msg=deleted&job=$id");
        exit;
    }
    if (isset($_POST['reschedule_job'])) {
        $id = (int)$_POST['reschedule_job'];
        $pdo->exec("UPDATE claude_pro_batch SET status='queued', result=NULL, error_msg=NULL,
                    input_tokens=NULL, output_tokens=NULL, cache_tokens=NULL, cost_usd=NULL,
                    started_at=NULL, finished_at=NULL WHERE id=$id AND status IN ('done','failed')");
        header("Location: ?msg=rescheduled&job=$id");
        exit;
    }
}

// Flash-Message aus GET-Parameter
$msg = '';
if (isset($_GET['msg'])) {
    match($_GET['msg']) {
        'ok'        => $msg = ['success', "Job #".htmlspecialchars($_GET['job'] ?? '')." eingereiht &mdash; ".htmlspecialchars($_GET['date'] ?? '')." &mdash; ".htmlspecialchars($_GET['model'] ?? '')],
        'err'       => $msg = ['danger',  'Kein Auftragstext eingegeben.'],
        'cancelled' => $msg = ['warning', "Job #".htmlspecialchars($_GET['job'] ?? '')." abgebrochen."],
        'deleted'      => $msg = ['secondary', "Job #".htmlspecialchars($_GET['job'] ?? '')." gelöscht."],
        'rescheduled'  => $msg = ['info',  "Job #".htmlspecialchars($_GET['job'] ?? '')." neu eingereiht."],
        default     => null,
    };
}

// ── Daten ──────────────────────────────────────────────────
$jobs = $pdo->query("
    SELECT id, created_at, targetdate, model, status,
           LEFT(prompt,100) AS prompt_short, prompt AS prompt_full,
           input_tokens, output_tokens, cache_tokens, cost_usd,
           started_at, finished_at, result, error_msg
    FROM claude_pro_batch ORDER BY created_at DESC LIMIT 50
")->fetchAll(PDO::FETCH_ASSOC);

$weekCost = $pdo->query("
    SELECT COALESCE(SUM(cost_usd),0) AS cost,
           COALESCE(SUM(input_tokens),0) AS i_tok,
           COALESCE(SUM(output_tokens),0) AS o_tok,
           COALESCE(SUM(cache_tokens),0) AS c_tok,
           COUNT(*) AS tasks
    FROM claude_pro_batch
    WHERE status='done'
      AND finished_at >= (
          SELECT CASE
            WHEN DAYOFWEEK(CURDATE())=6 AND CURTIME()>='08:00:00'
                THEN CONCAT(CURDATE(),' 08:00:00')
            ELSE
                CONCAT(DATE_SUB(CURDATE(), INTERVAL ((DAYOFWEEK(CURDATE())+1) % 7) DAY),' 08:00:00')
          END)
")->fetch(PDO::FETCH_ASSOC);

$modelStats = $pdo->query("
    SELECT model, COUNT(*) AS tasks,
           COALESCE(SUM(cost_usd),0) AS cost,
           COALESCE(SUM(output_tokens),0) AS o_tok
    FROM claude_pro_batch WHERE status='done'
    GROUP BY model ORDER BY cost DESC
")->fetchAll(PDO::FETCH_ASSOC);

$dailyStats = $pdo->query("
    SELECT DATE(finished_at) AS day, COUNT(*) AS tasks,
           COALESCE(SUM(cost_usd),0) AS cost
    FROM claude_pro_batch WHERE status='done'
      AND finished_at >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
    GROUP BY DATE(finished_at) ORDER BY day ASC
")->fetchAll(PDO::FETCH_ASSOC);

$totals = $pdo->query("
    SELECT COUNT(*) AS tasks, COALESCE(SUM(cost_usd),0) AS cost,
           COALESCE(SUM(output_tokens),0) AS o_tok
    FROM claude_pro_batch WHERE status='done'
")->fetch();

$usageFile = '/home/gh/.claude_weekly_usage.json';
$usage = file_exists($usageFile) ? json_decode(file_get_contents($usageFile), true) : [];
$weekStart  = $usage['week_start']      ?? '–';
$weekReset  = $usage['week_reset_raw']  ?? ($usage['week_reset'] ?? '–');
$usagePct   = $usage['usage_pct']       ?? null;
$sessionPct = $usage['session_pct']     ?? null;
$pctSnap    = $usage['pct_snapshot_at'] ?? null;
$lastRun    = $usage['last_run']        ?? null;
$jsonInTok  = $usage['input_tokens']    ?? null;
$jsonOutTok = $usage['output_tokens']   ?? null;
$jsonCachTok= $usage['cache_tokens']    ?? null;
$jsonCost   = $usage['cost_usd']        ?? null;
$jsonTasks  = $usage['tasks']           ?? null;

// ── Nächster Reset: Freitag 08:00 Europe/Berlin ────────────
$tz        = new DateTimeZone('Europe/Berlin');
$now       = new DateTime('now', $tz);
$resetDay  = clone $now;
$resetDay->modify('friday this week');
$resetDay->setTime(8, 0, 0);
if ($resetDay <= $now) {                        // Freitag schon vorbei → nächste Woche
    $resetDay->modify('+7 days');
}
$resetLabel  = $resetDay->format('D d.m. H:i');
$secsLeft    = $resetDay->getTimestamp() - $now->getTimestamp();
$daysLeft    = floor($secsLeft / 86400);
$hoursLeft   = floor(($secsLeft % 86400) / 3600);
$minsLeft    = floor(($secsLeft % 3600) / 60);
$restCountdown = ($daysLeft > 0 ? "{$daysLeft}T " : '') . "{$hoursLeft}h {$minsLeft}m";
$restPct     = ($usagePct !== null) ? max(0, 100 - $usagePct) : null;

$hasActive = (bool)array_filter($jobs, fn($j) => in_array($j['status'], ['queued','running']));

// ── Session-Compact Cache vorhanden? ──────────────────────────────────
$compact = $pdo->query("
    SELECT COALESCE(updated_at, created_at) AS ts, summary, version
    FROM claude_context_cache
    WHERE scope='session-compact' LIMIT 1
")->fetch(PDO::FETCH_ASSOC);
$hasCompact  = (bool)$compact;
$compactAge  = '';
$compactSummaryHead = '';
if ($compact && $compact['ts']) {
    $secs = max(0, time() - strtotime($compact['ts']));
    $compactAge = $secs < 3600
        ? floor($secs / 60) . 'min'
        : (floor($secs / 3600) < 48
            ? floor($secs / 3600) . 'h'
            : floor($secs / 86400) . 'd');
    $compactSummaryHead = substr($compact['summary'] ?? '', 0, 120);
}

// ── Hilfsfunktionen ────────────────────────────────────────
function modelBadge($m) {
    $map = ['haiku'=>'success','sonnet'=>'primary','opus'=>'warning'];
    $cls = $map[$m] ?? 'secondary';
    return "<span class=\"badge bg-$cls\">$m</span>";
}
function statusBadge($s) {
    $map = ['queued'=>'warning','running'=>'info','done'=>'success','failed'=>'danger'];
    $cls = $map[$s] ?? 'secondary';
    $dot = $s === 'running' ? ' spinner' : '';
    return "<span class=\"badge bg-$cls$dot\">$s</span>";
}
function dur($a, $b) {
    if (!$a || !$b) return '–';
    $s = strtotime($b) - strtotime($a);
    return $s < 60 ? "{$s}s" : floor($s/60)."m".($s%60)."s";
}
?><!DOCTYPE html>
<html lang="de" data-bs-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Pro Batch</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css">
<style>
body { background:#0d1117; }
.navbar-brand { font-weight:700; letter-spacing:-.3px; }
.card { background:#161b22; border-color:#30363d; }
.card-header { background:#1c2128; border-color:#30363d; font-size:.8rem; text-transform:uppercase; letter-spacing:.5px; color:#8b949e; }
.table { --bs-table-bg: transparent; --bs-table-hover-bg: rgba(255,255,255,.03); }
.table th { font-size:.72rem; text-transform:uppercase; letter-spacing:.4px; color:#8b949e; border-color:#30363d; }
.table td { border-color:#30363d; vertical-align:middle; font-size:.85rem; }
.stat-card { background:#1c2128; border:1px solid #30363d; border-radius:.5rem; padding:1rem; }
.stat-val { font-size:1.6rem; font-weight:700; line-height:1; }
.stat-lbl { font-size:.7rem; text-transform:uppercase; letter-spacing:.5px; color:#8b949e; margin-bottom:.25rem; }
.stat-sub { font-size:.72rem; color:#8b949e; margin-top:.2rem; }
.bar-row { display:flex; align-items:center; gap:.5rem; margin:.35rem 0; }
.bar-wrap { flex:1; background:#21262d; border-radius:3px; height:14px; overflow:hidden; }
.bar-fill { height:100%; border-radius:3px; transition:width .4s; }
.bar-lbl { font-size:.72rem; color:#8b949e; width:80px; text-align:right; white-space:nowrap; flex-shrink:0; }
.bar-val { font-size:.72rem; white-space:nowrap; min-width:110px; }
.result-pre { background:#0d1117; border:1px solid #30363d; border-radius:.375rem; padding:.75rem; font-size:.78rem; white-space:pre-wrap; max-height:300px; overflow-y:auto; }
.spinner { animation: spin .8s linear infinite; display:inline-block; }
@keyframes spin { to { transform:rotate(360deg); } }
.prompt-truncate { max-width:220px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; display:block; }
@media(max-width:576px) {
    .stat-val { font-size:1.3rem; }
    .hide-mobile { display:none !important; }
}
</style>
</head>
<body>

<nav class="navbar navbar-dark" style="background:#161b22;border-bottom:1px solid #30363d">
    <div class="container-fluid">
        <span class="navbar-brand">
            <i class="bi bi-robot me-2 text-primary"></i>Claude Pro Batch
        </span>
        <div class="d-flex align-items-center gap-2">
            <small class="text-muted d-none d-sm-block">Reset: <?= htmlspecialchars($weekReset) ?></small>
            <a href="view_cache.php" class="btn btn-sm btn-outline-secondary">
                <i class="bi bi-database me-1"></i>Cache
            </a>
        </div>
    </div>
</nav>

<div class="container-fluid py-3 px-3 px-md-4" style="max-width:1300px">

<?php if ($msg): ?>
<div class="alert alert-<?= $msg[0] ?> alert-dismissible fade show" role="alert">
    <i class="bi bi-<?= $msg[0]==='success'?'check-circle':'exclamation-triangle' ?> me-2"></i><?= $msg[1] ?>
    <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
</div>
<?php endif; ?>

<!-- ── Neuer Job ── -->
<div class="card mb-3">
    <div class="card-header"><i class="bi bi-plus-circle me-1"></i>Neuen Batch-Job einreihen</div>
    <div class="card-body">
        <form method="POST">
            <div class="row g-3">
                <div class="col-12 col-sm-5 col-md-3">
                    <label class="form-label small text-muted text-uppercase">Zieldatum</label>
                    <input type="date" class="form-control" name="targetdate"
                           value="<?= date('Y-m-d') ?>" min="<?= date('Y-m-d') ?>">
                </div>
                <div class="col-12 col-sm-7 col-md-3">
                    <label class="form-label small text-muted text-uppercase">Modell</label>
                    <select class="form-select" name="model">
                        <option value="haiku" selected>🟢 Haiku — Standard (1×)</option>
                        <option value="sonnet">🔵 Sonnet — Komplex (~4×)</option>
                        <option value="opus">🟣 Opus — Maximum (~19×)</option>
                    </select>
                </div>
                <div class="col-12 col-sm-auto d-flex align-items-end pb-1">
                    <div>
                        <div class="form-check form-switch mb-1">
                            <input class="form-check-input" type="checkbox" role="switch"
                                   name="resume_session" id="resumeSwitch" value="1"
                                   <?= $hasCompact ? 'checked' : '' ?>>
                            <label class="form-check-label small" for="resumeSwitch">
                                <i class="bi bi-database-check me-1 text-info"></i>
                                Session-Cache laden
                                <?php if ($compactAge): ?>
                                <span class="text-muted">(<?= htmlspecialchars($compactAge) ?>)</span>
                                <?php endif; ?>
                            </label>
                        </div>
                        <?php if ($hasCompact): ?>
                        <button type="button" class="btn btn-link btn-sm p-0 text-muted"
                                style="font-size:.7rem" data-bs-toggle="modal" data-bs-target="#cacheModal">
                            <i class="bi bi-eye me-1"></i>Kontext ansehen (Audit)
                        </button>
                        <?php endif; ?>
                    </div>
                </div>
                <div class="col-12 col-md">
                    <label class="form-label small text-muted text-uppercase">Auftragstext</label>
                    <textarea class="form-control" name="prompt" rows="3"
                              placeholder="Schreibe eine Python-Funktion die ..."></textarea>
                </div>
            </div>
            <div class="mt-3 d-flex align-items-center gap-3 flex-wrap">
                <button class="btn btn-primary" name="submit_job">
                    <i class="bi bi-send me-1"></i>Job einreihen
                </button>
                <small class="text-muted">Start: sofort &nbsp;|&nbsp; Zieldatum = Deadline (nächste Deadline zuerst)</small>
            </div>
        </form>
    </div>
</div>

<!-- ── Wochenverbrauch (Usage) ── -->
<div class="card mb-3">
    <div class="card-header d-flex justify-content-between align-items-center flex-wrap gap-1">
        <span><i class="bi bi-speedometer2 me-1"></i>Claude Pro Wochenverbrauch</span>
        <span class="text-muted small">
            <?php if ($pctSnap): ?>
                <i class="bi bi-clock me-1"></i>Stand: <?= htmlspecialchars($pctSnap) ?>
            <?php elseif ($lastRun): ?>
                <i class="bi bi-clock me-1"></i>Letzter Lauf: <?= htmlspecialchars($lastRun) ?>
            <?php endif; ?>
        </span>
    </div>
    <div class="card-body">

        <!-- ── Zwei Limits nebeneinander ── -->
        <?php
        $wColor = $usagePct  >= 90 ? 'danger' : ($usagePct  >= 70 ? 'warning' : 'success');
        $sColor = $sessionPct >= 90 ? 'danger' : ($sessionPct >= 70 ? 'warning' : 'info');
        ?>
        <div class="row g-3 mb-3">

            <!-- Wochenlimit -->
            <div class="col-12 col-md-6">
                <div class="stat-card h-100">
                    <div class="d-flex justify-content-between align-items-baseline mb-2">
                        <span class="stat-lbl mb-0"><i class="bi bi-calendar-week me-1"></i>Wochenlimit</span>
                        <?php if ($usagePct !== null): ?>
                        <span class="fw-bold text-<?= $wColor ?>"><?= $usagePct ?>% verbraucht</span>
                        <?php endif; ?>
                    </div>
                    <?php if ($usagePct !== null): ?>
                    <div class="progress mb-2" style="height:22px;background:#21262d">
                        <div class="progress-bar bg-<?= $wColor ?>"
                             style="width:<?= min($usagePct,100) ?>%;font-size:.8rem;line-height:22px">
                            <?php if ($usagePct > 8): ?><?= $usagePct ?>%<?php endif; ?>
                        </div>
                    </div>
                    <?php else: ?>
                    <div class="progress mb-2" style="height:22px;background:#21262d"></div>
                    <?php endif; ?>
                    <div class="d-flex justify-content-between">
                        <small class="text-muted">
                            <i class="bi bi-arrow-clockwise me-1"></i>Reset: <strong class="text-light"><?= htmlspecialchars($weekReset) ?></strong>
                        </small>
                        <small class="text-<?= $hoursLeft < 6 ? 'danger' : ($hoursLeft < 24 ? 'warning' : 'info') ?>">
                            <i class="bi bi-hourglass-split me-1"></i><?= $restCountdown ?>
                        </small>
                    </div>
                    <?php if ($usagePct !== null): ?>
                    <div class="mt-1 text-end">
                        <small class="text-success fw-bold"><?= 100 - $usagePct ?>% noch frei</small>
                    </div>
                    <?php endif; ?>
                </div>
            </div>

            <!-- Session-Limit -->
            <div class="col-12 col-md-6">
                <div class="stat-card h-100 <?= $sessionPct >= 80 ? 'border border-'.$sColor : '' ?>">
                    <div class="d-flex justify-content-between align-items-baseline mb-2">
                        <span class="stat-lbl mb-0"><i class="bi bi-lightning me-1"></i>Aktuell Session</span>
                        <?php if ($sessionPct !== null): ?>
                        <span class="fw-bold text-<?= $sColor ?>"><?= $sessionPct ?>% verbraucht</span>
                        <?php endif; ?>
                    </div>
                    <?php if ($sessionPct !== null): ?>
                    <div class="progress mb-2" style="height:22px;background:#21262d">
                        <div class="progress-bar bg-<?= $sColor ?>"
                             style="width:<?= min($sessionPct,100) ?>%;font-size:.8rem;line-height:22px">
                            <?php if ($sessionPct > 8): ?><?= $sessionPct ?>%<?php endif; ?>
                        </div>
                    </div>
                    <?php else: ?>
                    <div class="progress mb-2" style="height:22px;background:#21262d"></div>
                    <?php endif; ?>
                    <div class="d-flex justify-content-between">
                        <small class="text-muted">
                            <i class="bi bi-arrow-clockwise me-1"></i>Reset: <strong class="text-light"><?= htmlspecialchars($weekReset) ?></strong>
                        </small>
                        <small class="text-<?= $hoursLeft < 6 ? 'danger' : ($hoursLeft < 24 ? 'warning' : 'info') ?>">
                            <i class="bi bi-hourglass-split me-1"></i><?= $restCountdown ?>
                        </small>
                    </div>
                    <?php if ($sessionPct !== null): ?>
                    <div class="mt-1 text-end">
                        <small class="text-<?= $sColor ?> fw-bold"><?= 100 - $sessionPct ?>% noch frei</small>
                        <?php if ($sessionPct >= 80): ?>
                        <span class="badge bg-<?= $sColor ?> ms-2">⚡ /compact empfohlen</span>
                        <?php endif; ?>
                    </div>
                    <?php endif; ?>
                </div>
            </div>

        </div>

        <?php if ($usagePct === null): ?>
        <div class="alert alert-secondary py-2 small mb-3">
            <i class="bi bi-info-circle me-1"></i>
            %-Werte ausstehend — <code>fetch-usage.py</code> läuft alle 30 min (Cron).
        </div>
        <?php endif; ?>

        <!-- ── Token-Statistiken aus usage-JSON ── -->
        <?php if ($jsonInTok !== null): ?>
        <div class="row g-2 mb-2">
            <div class="col-6 col-sm-3">
                <div class="stat-card">
                    <div class="stat-lbl">Gesamtkosten</div>
                    <div class="stat-val text-warning">$<?= number_format($jsonCost,4) ?></div>
                    <div class="stat-sub"><?= $jsonTasks ?> Tasks gesamt</div>
                </div>
            </div>
            <div class="col-6 col-sm-3">
                <div class="stat-card">
                    <div class="stat-lbl">Input Tokens</div>
                    <div class="stat-val text-primary"><?= number_format($jsonInTok) ?></div>
                    <div class="stat-sub">direkte Eingabe</div>
                </div>
            </div>
            <div class="col-6 col-sm-3">
                <div class="stat-card">
                    <div class="stat-lbl">Output Tokens</div>
                    <div class="stat-val text-success"><?= number_format($jsonOutTok) ?></div>
                    <div class="stat-sub">generiert</div>
                </div>
            </div>
            <div class="col-6 col-sm-3">
                <div class="stat-card">
                    <div class="stat-lbl">Cache Tokens</div>
                    <div class="stat-val" style="color:#bc8cff"><?= number_format($jsonCachTok) ?></div>
                    <div class="stat-sub">wiederverwendet</div>
                </div>
            </div>
        </div>
        <p class="text-muted small mb-0"><i class="bi bi-info-circle me-1"></i>Kumuliert aus <code>~/.claude_weekly_usage.json</code> — alle Jobs dieser Woche.</p>
        <?php endif; ?>

    </div>
</div>

<!-- ── Job-Liste ── -->
<div class="card">
    <div class="card-header d-flex justify-content-between align-items-center">
        <span><i class="bi bi-list-task me-1"></i>Jobs (letzte 50)</span>
        <?php if ($hasActive): ?>
        <span class="text-info small"><i class="bi bi-arrow-repeat spinner me-1"></i>Auto-Refresh 30s</span>
        <?php endif; ?>
    </div>
    <div class="card-body p-0">
        <div class="table-responsive">
        <table class="table table-hover mb-0">
            <thead><tr>
                <th>#</th>
                <th>Datum</th>
                <th>Modell</th>
                <th>Status</th>
                <th>Prompt</th>
                <th class="hide-mobile">Tokens (in/out/cache)</th>
                <th>Kosten</th>
                <th class="hide-mobile">Dauer</th>
                <th></th>
            </tr></thead>
            <tbody>
            <?php foreach ($jobs as $j): ?>
            <tr>
                <td><a href="job.php?id=<?= $j['id'] ?>" class="text-muted">#<?= $j['id'] ?></a></td>
                <td class="text-muted small"><?= $j['targetdate'] ?></td>
                <td><?= modelBadge($j['model']) ?></td>
                <td><?= statusBadge($j['status']) ?></td>
                <td>
                    <a href="job.php?id=<?= $j['id'] ?>" class="text-decoration-none">
                    <span class="prompt-truncate text-muted small"
                          title="<?= htmlspecialchars($j['prompt_short']) ?>">
                        <?= htmlspecialchars($j['prompt_short']) ?>
                    </span>
                    </a>
                </td>
                <td class="hide-mobile small text-muted">
                    <?php if ($j['input_tokens']): ?>
                    <?= number_format($j['input_tokens']) ?> / <?= number_format($j['output_tokens']) ?> / <?= number_format($j['cache_tokens']) ?>
                    <?php else: echo '–'; endif; ?>
                </td>
                <td>
                    <?php if ($j['cost_usd']): ?>
                    <strong class="text-warning">$<?= number_format($j['cost_usd'],4) ?></strong>
                    <?php else: echo '<span class="text-muted">–</span>'; endif; ?>
                </td>
                <td class="hide-mobile text-muted small"><?= dur($j['started_at'],$j['finished_at']) ?></td>
                <td>
                    <?php if ($j['result']): ?>
                    <button class="btn btn-sm btn-outline-secondary py-0"
                            data-bs-toggle="collapse"
                            data-bs-target="#res-<?= $j['id'] ?>">
                        <i class="bi bi-eye"></i>
                    </button>
                    <?php elseif ($j['status'] === 'queued'): ?>
                    <form method="POST" class="d-inline">
                        <button class="btn btn-sm btn-outline-danger py-0" name="cancel_job" value="<?= $j['id'] ?>">
                            <i class="bi bi-x"></i>
                        </button>
                    </form>
                    <?php elseif ($j['status'] === 'failed'): ?>
                    <form method="POST" class="d-inline">
                        <button class="btn btn-sm btn-outline-warning py-0"
                                name="reschedule_job" value="<?= $j['id'] ?>">
                            <i class="bi bi-arrow-clockwise"></i>
                        </button>
                    </form>
                    <?php endif; ?>
                    <?php if ($j['status'] !== 'running'): ?>
                    <form method="POST" class="d-inline"
                          onsubmit="return confirm('Job #<?= $j['id'] ?> löschen?')">
                        <button class="btn btn-sm btn-outline-danger py-0 ms-1"
                                name="delete_job" value="<?= $j['id'] ?>">
                            <i class="bi bi-trash"></i>
                        </button>
                    </form>
                    <?php endif; ?>
                </td>
            </tr>
            <?php if ($j['result']): ?>
            <tr class="collapse" id="res-<?= $j['id'] ?>">
                <td colspan="9" class="p-2">
                    <pre class="result-pre mb-0"><?= htmlspecialchars($j['result']) ?></pre>
                </td>
            </tr>
            <?php endif; ?>
            <?php endforeach; ?>
            <?php if (empty($jobs)): ?>
            <tr><td colspan="9" class="text-center py-4 text-muted">
                <i class="bi bi-inbox fs-3 d-block mb-2"></i>Noch keine Jobs. Ersten Job oben einreihen.
            </td></tr>
            <?php endif; ?>
            </tbody>
        </table>
        </div>
    </div>
</div>


<!-- ── Statistik Report ── -->
<div class="card mb-3">
    <div class="card-header"><i class="bi bi-graph-up me-1"></i>Statistik Report</div>
    <div class="card-body">
        <div class="row g-4">
            <div class="col-12 col-md-6">
                <div class="text-muted small text-uppercase mb-2" style="letter-spacing:.5px">Verbrauch nach Modell</div>
                <?php
                $maxC = max(array_column($modelStats, 'cost') ?: [0.0001]);
                $cols = ['haiku'=>'#3fb950','sonnet'=>'#58a6ff','opus'=>'#bc8cff'];
                foreach ($modelStats as $m):
                    $pct = $maxC > 0 ? round($m['cost']/$maxC*100) : 1;
                ?>
                <div class="bar-row">
                    <span class="bar-lbl"><?= $m['model'] ?> (<?= $m['tasks'] ?>×)</span>
                    <div class="bar-wrap"><div class="bar-fill" style="width:<?= $pct ?>%;background:<?= $cols[$m['model']] ?? '#8b949e' ?>"></div></div>
                    <span class="bar-val">$<?= number_format($m['cost'],4) ?> / <?= number_format($m['o_tok']) ?> tok</span>
                </div>
                <?php endforeach; ?>
                <?php if (empty($modelStats)): ?>
                <span class="text-muted small">Noch keine abgeschlossenen Jobs.</span>
                <?php endif; ?>
                <div class="mt-3 pt-2 border-top small text-muted">
                    Gesamt: <strong class="text-light"><?= $totals['tasks'] ?> Tasks</strong> &mdash;
                    <strong class="text-warning">$<?= number_format($totals['cost'],4) ?></strong> &mdash;
                    <strong class="text-success"><?= number_format($totals['o_tok']) ?></strong> Output-Tokens
                </div>
            </div>
            <div class="col-12 col-md-6">
                <div class="text-muted small text-uppercase mb-2" style="letter-spacing:.5px">Tageskosten (letzte 7 Tage)</div>
                <?php
                $maxD = max(array_column($dailyStats, 'cost') ?: [0.0001]);
                foreach ($dailyStats as $d):
                    $pct = round($d['cost']/$maxD*100);
                ?>
                <div class="bar-row">
                    <span class="bar-lbl"><?= substr($d['day'],5) ?></span>
                    <div class="bar-wrap"><div class="bar-fill" style="width:<?= max($pct,1) ?>%;background:#ffa657"></div></div>
                    <span class="bar-val">$<?= number_format($d['cost'],4) ?> / <?= $d['tasks'] ?> Task<?= $d['tasks']>1?'s':'' ?></span>
                </div>
                <?php endforeach; ?>
                <?php if (empty($dailyStats)): ?>
                <span class="text-muted small">Keine Daten der letzten 7 Tage.</span>
                <?php endif; ?>
            </div>
        </div>
    </div>
</div>

<!-- ── Modell-Kosten ── -->
<div class="card mb-3">
    <div class="card-header"><i class="bi bi-currency-dollar me-1"></i>Modell-Kostenvergleich</div>
    <div class="card-body p-0">
        <div class="table-responsive">
        <table class="table table-hover mb-0">
            <thead><tr>
                <th>Modell</th><th>Input / MTok</th><th>Output / MTok</th>
                <th>Faktor</th><th class="hide-mobile">Einsatz</th>
            </tr></thead>
            <tbody>
                <tr>
                    <td><?= modelBadge('haiku') ?></td>
                    <td>$0.80</td><td>$4.00</td>
                    <td><strong class="text-success">1× günstigste</strong></td>
                    <td class="hide-mobile text-muted small">Einfache Scripts, Tests, Funktionen</td>
                </tr>
                <tr>
                    <td><?= modelBadge('sonnet') ?></td>
                    <td>$3.00</td><td>$15.00</td>
                    <td><strong class="text-warning">~4×</strong></td>
                    <td class="hide-mobile text-muted small">Komplexes Refactoring, große Codebases</td>
                </tr>
                <tr>
                    <td><?= modelBadge('opus') ?></td>
                    <td>$15.00</td><td>$75.00</td>
                    <td><strong class="text-danger">~19×</strong></td>
                    <td class="hide-mobile text-muted small">Nur wenn Sonnet nachweislich scheitert</td>
                </tr>
            </tbody>
        </table>
        </div>
    </div>
</div>


<!-- ── Session-Cache Modal ── -->
<?php if ($hasCompact): ?>
<?php
$cacheEntry = $pdo->query("
    SELECT context_json, COALESCE(updated_at,created_at) AS ts, version, updated_by
    FROM claude_context_cache WHERE scope='session-compact' LIMIT 1
")->fetch(PDO::FETCH_ASSOC);
$cacheDecoded = json_decode($cacheEntry['context_json'], true);
$cacheSummary = $cacheDecoded['summary'] ?? $cacheEntry['context_json'];
?>
<div class="modal fade" id="cacheModal" tabindex="-1">
    <div class="modal-dialog modal-xl modal-dialog-scrollable">
        <div class="modal-content" style="background:#161b22;border-color:#30363d">
            <div class="modal-header" style="border-color:#30363d">
                <h5 class="modal-title">
                    <i class="bi bi-database-check me-2 text-info"></i>Session-Cache Audit
                    <span class="badge bg-secondary ms-2">v<?= $cacheEntry['version'] ?></span>
                </h5>
                <div class="ms-auto d-flex gap-2 me-3">
                    <button class="btn btn-sm btn-outline-primary" onclick="copyCache()">
                        <i class="bi bi-clipboard me-1"></i>Copy
                    </button>
                    <a href="view_cache.php?scope=session-compact" class="btn btn-sm btn-outline-secondary" target="_blank">
                        <i class="bi bi-box-arrow-up-right me-1"></i>Vollansicht
                    </a>
                </div>
                <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
            </div>
            <div class="modal-body p-0">
                <div class="px-3 py-2" style="background:#1c2128;border-bottom:1px solid #30363d;font-size:.75rem">
                    <span class="text-muted me-3"><i class="bi bi-clock me-1"></i><?= htmlspecialchars($cacheEntry['ts']) ?></span>
                    <span class="text-muted me-3"><i class="bi bi-person me-1"></i><?= htmlspecialchars($cacheEntry['updated_by'] ?? '–') ?></span>
                    <span class="text-muted"><i class="bi bi-file-text me-1"></i><?= number_format(strlen($cacheSummary)) ?> Zeichen</span>
                </div>
                <pre id="cache-text" style="background:#0d1117;color:#e6edf3;font-size:.76rem;
                     font-family:'SFMono-Regular',Consolas,monospace;padding:1rem;
                     margin:0;white-space:pre-wrap;word-break:break-word;
                     max-height:70vh;overflow-y:auto"><?= htmlspecialchars($cacheSummary) ?></pre>
            </div>
        </div>
    </div>
</div>
<?php endif; ?>

</div><!-- container -->

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<?php if ($hasActive): ?>
<script>setTimeout(() => location.reload(), 30000);</script>
<?php endif; ?>
<script>
function copyCache() {
    const txt = document.getElementById('cache-text').innerText;
    navigator.clipboard.writeText(txt).then(() => {
        const b = event.target.closest('button');
        b.innerHTML = '<i class="bi bi-check2 me-1"></i>Kopiert!';
        b.classList.replace('btn-outline-primary', 'btn-success');
        setTimeout(() => {
            b.innerHTML = '<i class="bi bi-clipboard me-1"></i>Copy';
            b.classList.replace('btn-success', 'btn-outline-primary');
        }, 2000);
    });
}
</script>
</body>
</html>
