<?php
$pdo = new PDO('mysql:host=localhost;dbname=wagodb;charset=utf8mb4', 'gh', 'a12345',
    [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]);

$id  = (int)($_GET['id'] ?? 0);

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    if (isset($_POST['kill_job'])) {
        $pdo->exec("UPDATE claude_pro_batch SET status='failed', error_msg='Abgebrochen (kill)' WHERE id=$id AND status='running'");
        header("Location: job.php?id=$id");
        exit;
    }
    if (isset($_POST['delete_job'])) {
        $pdo->exec("DELETE FROM claude_pro_batch WHERE id=$id AND status IN ('queued','done','failed')");
        header("Location: index.php?msg=deleted&job=$id");
        exit;
    }
    if (isset($_POST['reschedule_job'])) {
        $pdo->exec("UPDATE claude_pro_batch SET status='queued', result=NULL, error_msg=NULL,
                    input_tokens=NULL, output_tokens=NULL, cache_tokens=NULL, cost_usd=NULL,
                    started_at=NULL, finished_at=NULL WHERE id=$id AND status IN ('done','failed')");
        header("Location: job.php?id=$id");
        exit;
    }
}

$job = $pdo->prepare("SELECT * FROM claude_pro_batch WHERE id=?");
$job->execute([$id]);
$j   = $job->fetch(PDO::FETCH_ASSOC);

if (!$j) { http_response_code(404); die('Job nicht gefunden.'); }

function dur($a,$b){
    if(!$a||!$b) return '–';
    $s=strtotime($b)-strtotime($a);
    return $s<60?"{$s}s":floor($s/60)."m".($s%60)."s";
}
$colors=['haiku'=>'success','sonnet'=>'primary','opus'=>'warning','xiaomi'=>'danger'];
$statusColors=['queued'=>'warning','running'=>'info','done'=>'success','failed'=>'danger'];
$autoRefresh = in_array($j['status'], ['queued','running']);
?><!DOCTYPE html>
<html lang="de" data-bs-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job #<?= $id ?> — Claude Batch</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css">
<style>
body{background:#0d1117;}
.card{background:#161b22;border-color:#30363d;}
.card-header{background:#1c2128;border-color:#30363d;font-size:.8rem;text-transform:uppercase;letter-spacing:.5px;color:#8b949e;}
pre{background:#0d1117;border:1px solid #30363d;border-radius:.375rem;padding:1rem;font-size:.82rem;white-space:pre-wrap;word-break:break-word;max-height:70vh;overflow-y:auto;}
.kv-row{display:flex;gap:.5rem;padding:.4rem 0;border-bottom:1px solid #21262d;font-size:.85rem;}
.kv-row:last-child{border-bottom:none;}
.kv-key{color:#8b949e;width:130px;flex-shrink:0;}
.spinner-dot{display:inline-block;animation:spin .8s linear infinite;}
@keyframes spin{to{transform:rotate(360deg);}}
</style>
<?php if($autoRefresh): ?>
<meta http-equiv="refresh" content="10">
<?php endif; ?>
</head>
<body>
<nav class="navbar" style="background:#161b22;border-bottom:1px solid #30363d">
    <div class="container-fluid">
        <a class="navbar-brand text-light" href="index.php">
            <i class="bi bi-arrow-left me-2"></i>Claude Pro Batch
        </a>
        <?php if ($j['status'] === 'running'): ?>
        <form method="POST" class="d-inline"
              onsubmit="return confirm('Job #<?= $id ?> abbrechen?')">
            <button class="btn btn-sm btn-outline-danger" name="kill_job">
                <i class="bi bi-stop-circle me-1"></i>Kill
            </button>
        </form>
        <?php endif; ?>
        <?php if (in_array($j['status'], ['done','failed'])): ?>
        <form method="POST" class="d-inline">
            <button class="btn btn-sm btn-outline-warning" name="reschedule_job">
                <i class="bi bi-arrow-clockwise"></i> Reschedule
            </button>
        </form>
        <?php endif; ?>
        <?php if ($j['status'] !== 'running'): ?>
        <form method="POST" class="d-inline"
              onsubmit="return confirm('Job #<?= $id ?> löschen?')">
            <button class="btn btn-sm btn-outline-danger" name="delete_job">
                <i class="bi bi-trash"></i>
            </button>
        </form>
        <?php endif; ?>
    </div>
</nav>

<div class="container-fluid py-3 px-3" style="max-width:900px">

    <!-- Status-Banner -->
    <div class="alert alert-<?= $statusColors[$j['status']] ?? 'secondary' ?> d-flex align-items-center gap-2 mb-3">
        <?php if($j['status']==='running'): ?>
        <i class="bi bi-arrow-repeat spinner-dot fs-5"></i>
        <?php elseif($j['status']==='done'): ?>
        <i class="bi bi-check-circle fs-5"></i>
        <?php elseif($j['status']==='failed'): ?>
        <i class="bi bi-x-circle fs-5"></i>
        <?php else: ?>
        <i class="bi bi-hourglass fs-5"></i>
        <?php endif; ?>
        <strong><?= ucfirst($j['status']) ?></strong>
        <?php if($autoRefresh): ?>
        <span class="ms-auto text-muted small">Auto-Refresh 10s</span>
        <?php endif; ?>
    </div>

    <!-- Job-Details -->
    <div class="card mb-3">
        <div class="card-header"><i class="bi bi-info-circle me-1"></i>Details</div>
        <div class="card-body py-2">
            <div class="kv-row"><span class="kv-key">ID</span><span>#<?= $j['id'] ?></span></div>
            <div class="kv-row"><span class="kv-key">Erstellt</span><span><?= $j['created_at'] ?></span></div>
            <div class="kv-row"><span class="kv-key">Deadline</span><span><?= $j['targetdate'] ?></span></div>
            <div class="kv-row"><span class="kv-key">Modell</span>
                <span><span class="badge bg-<?= $colors[$j['model']] ?? 'secondary' ?>"><?= $j['model'] ?></span></span>
            </div>
            <div class="kv-row"><span class="kv-key">Gestartet</span><span><?= $j['started_at'] ?? '–' ?></span></div>
            <div class="kv-row"><span class="kv-key">Fertig</span><span><?= $j['finished_at'] ?? '–' ?></span></div>
            <div class="kv-row"><span class="kv-key">Dauer</span><span><?= dur($j['started_at'],$j['finished_at']) ?></span></div>
            <?php if($j['input_tokens']): ?>
            <div class="kv-row"><span class="kv-key">Tokens</span>
                <span>
                    <span class="text-primary"><?= number_format($j['input_tokens']) ?> in</span> /
                    <span class="text-success"><?= number_format($j['output_tokens']) ?> out</span> /
                    <span style="color:#bc8cff"><?= number_format($j['cache_tokens']) ?> cache</span>
                </span>
            </div>
            <div class="kv-row"><span class="kv-key">Kosten</span>
                <span class="text-warning fw-bold">$<?= number_format($j['cost_usd'],6) ?></span>
            </div>
            <?php endif; ?>
        </div>
    </div>

    <!-- Prompt -->
    <div class="card mb-3">
        <div class="card-header"><i class="bi bi-chat-left-text me-1"></i>Auftragstext</div>
        <div class="card-body p-0">
            <pre class="m-0 rounded-0"><?= htmlspecialchars($j['prompt']) ?></pre>
        </div>
    </div>

    <!-- Ergebnis -->
    <?php if($j['result']): ?>
    <div class="card mb-3">
        <div class="card-header d-flex justify-content-between">
            <span><i class="bi bi-file-text me-1"></i>Ergebnis</span>
            <button class="btn btn-sm btn-outline-secondary py-0"
                    onclick="navigator.clipboard.writeText(document.getElementById('result-text').innerText)">
                <i class="bi bi-clipboard"></i> Kopieren
            </button>
        </div>
        <div class="card-body p-0">
            <pre id="result-text" class="m-0 rounded-0"><?= htmlspecialchars($j['result']) ?></pre>
        </div>
    </div>
    <?php elseif($j['status']==='queued'): ?>
    <div class="card mb-3">
        <div class="card-body text-center py-4 text-muted">
            <i class="bi bi-hourglass fs-2 d-block mb-2"></i>
            Wartet auf Ausführung — Cron prüft jede Minute
        </div>
    </div>
    <?php elseif($j['status']==='running'): ?>
    <div class="card mb-3">
        <div class="card-body text-center py-4 text-info">
            <i class="bi bi-arrow-repeat spinner-dot fs-2 d-block mb-2"></i>
            Claude arbeitet gerade …
        </div>
    </div>
    <?php endif; ?>

    <!-- Fehler -->
    <?php if($j['error_msg']): ?>
    <div class="card mb-3 border-danger">
        <div class="card-header text-danger"><i class="bi bi-exclamation-triangle me-1"></i>Fehler</div>
        <div class="card-body p-0">
            <pre class="m-0 rounded-0 text-danger"><?= htmlspecialchars($j['error_msg']) ?></pre>
        </div>
    </div>
    <?php endif; ?>

</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
