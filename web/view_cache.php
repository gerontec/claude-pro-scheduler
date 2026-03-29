<?php
$pdo = new PDO('mysql:host=localhost;dbname=wagodb;charset=utf8mb4', 'gh', 'a12345',
    [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]);

$scopes = $pdo->query("
    SELECT scope, version, created_at, updated_at, updated_by, ttl_hours,
           LENGTH(context_json) AS bytes
    FROM claude_context_cache
    ORDER BY updated_at DESC
")->fetchAll(PDO::FETCH_ASSOC);

$sel      = $_GET['scope'] ?? ($scopes[0]['scope'] ?? null);
$viewMode = $_GET['view'] ?? 'auto';   // auto | json | text
$entry    = null;
$pretty   = null;
$textView = null;

if ($sel) {
    $st = $pdo->prepare("SELECT * FROM claude_context_cache WHERE scope = ?");
    $st->execute([$sel]);
    $entry = $st->fetch(PDO::FETCH_ASSOC);
    if ($entry) {
        $decoded = json_decode($entry['context_json'], true);
        $pretty  = json_encode($decoded, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);

        // session-compact: Summary-Text direkt lesbar
        if (str_starts_with($sel, 'session-compact') && isset($decoded['summary'])) {
            $textView = $decoded['summary'];
        }
        // session: Q&A als lesbaren Text aufbereiten
        if ($sel === 'session' && isset($decoded['qa'])) {
            $lines = ["# Session-Kontext  –  " . ($decoded['session_date'] ?? '')];
            $lines[] = "Dateien: " . implode(', ', array_map('basename', $decoded['files_modified'] ?? []));
            $lines[] = str_repeat('─', 60);
            foreach ($decoded['qa'] as $i => $qa) {
                $lines[] = "\n▶ " . ($i+1) . ". " . trim($qa['q']);
                $lines[] = trim($qa['a']);
                $lines[] = str_repeat('·', 40);
            }
            $textView = implode("\n", $lines);
        }

        // view-Modus: wenn explizit json gewünscht, textView ignorieren
        if ($viewMode === 'json') $textView = null;
        if ($viewMode === 'text' && !$textView) $textView = $pretty;
    }
}
?><!DOCTYPE html>
<html lang="de" data-bs-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Cache Viewer</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css">
<style>
body { background:#0d1117; }
.card { background:#161b22; border-color:#30363d; }
.card-header { background:#1c2128; border-color:#30363d; font-size:.8rem; text-transform:uppercase; letter-spacing:.5px; color:#8b949e; }
.scope-btn { background:#1c2128; border:1px solid #30363d; border-radius:.375rem; padding:.4rem .75rem;
             font-size:.8rem; color:#c9d1d9; cursor:pointer; transition:all .15s; white-space:nowrap; }
.scope-btn:hover, .scope-btn.active { background:#388bfd22; border-color:#388bfd; color:#58a6ff; }
.scope-btn .badge { font-size:.65rem; }
#json-output { background:#0d1117; border:1px solid #30363d; border-radius:.375rem;
               padding:1rem; font-size:.78rem; font-family:'SFMono-Regular',Consolas,monospace;
               white-space:pre-wrap; word-break:break-all; min-height:300px;
               max-height:75vh; overflow-y:auto; color:#e6edf3; line-height:1.5; }
.meta-chip { background:#21262d; border-radius:.25rem; padding:.15rem .5rem;
             font-size:.72rem; color:#8b949e; }
</style>
</head>
<body>

<nav class="navbar navbar-dark" style="background:#161b22;border-bottom:1px solid #30363d">
    <div class="container-fluid">
        <span class="navbar-brand">
            <a href="index.php" class="text-decoration-none text-muted me-2">
                <i class="bi bi-arrow-left"></i>
            </a>
            <i class="bi bi-database me-2 text-primary"></i>Claude Context Cache
        </span>
        <small class="text-muted"><?= count($scopes) ?> Scope<?= count($scopes) !== 1 ? 's' : '' ?></small>
    </div>
</nav>

<div class="container-fluid py-3 px-3 px-md-4" style="max-width:1200px">

<?php if (empty($scopes)): ?>
<div class="alert alert-secondary">
    <i class="bi bi-inbox me-2"></i>Noch kein Cache vorhanden. Läuft <code>cache-saver.py</code>?
</div>
<?php else: ?>

<!-- ── Scope-Auswahl ── -->
<div class="d-flex gap-2 flex-wrap mb-3">
    <?php foreach ($scopes as $s): ?>
    <a href="?scope=<?= urlencode($s['scope']) ?>"
       class="scope-btn <?= $s['scope'] === $sel ? 'active' : '' ?> text-decoration-none">
        <i class="bi bi-tag me-1"></i><?= htmlspecialchars($s['scope']) ?>
        <span class="badge bg-secondary ms-1"><?= number_format($s['bytes']/1024, 1) ?>KB</span>
    </a>
    <?php endforeach; ?>
</div>

<?php if ($entry): ?>

<!-- ── Meta-Info ── -->
<div class="d-flex flex-wrap gap-2 mb-2 align-items-center">
    <span class="meta-chip"><i class="bi bi-layers me-1"></i>v<?= $entry['version'] ?></span>
    <span class="meta-chip"><i class="bi bi-clock me-1"></i><?= $entry['updated_at'] ?></span>
    <span class="meta-chip"><i class="bi bi-person me-1"></i><?= htmlspecialchars($entry['updated_by'] ?? '–') ?></span>
    <span class="meta-chip"><i class="bi bi-hourglass me-1"></i>TTL <?= $entry['ttl_hours'] ?>h</span>
    <span class="meta-chip"><i class="bi bi-file-code me-1"></i><?= number_format($entry['bytes']) ?> Bytes</span>
    <?php if ($entry['summary']): ?>
    <span class="meta-chip text-info" style="max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
          title="<?= htmlspecialchars($entry['summary']) ?>">
        <i class="bi bi-card-text me-1"></i><?= htmlspecialchars(mb_substr($entry['summary'],0,80)) ?>…
    </span>
    <?php endif; ?>
    <div class="ms-auto d-flex gap-2">
        <?php if ($textView): ?>
        <a href="?scope=<?= urlencode($sel) ?>&view=json" class="btn btn-sm btn-outline-secondary">
            <i class="bi bi-braces me-1"></i>JSON
        </a>
        <?php else: ?>
        <a href="?scope=<?= urlencode($sel) ?>&view=text" class="btn btn-sm btn-outline-secondary">
            <i class="bi bi-text-left me-1"></i>Text
        </a>
        <?php endif; ?>
        <button id="btn-copy" class="btn btn-sm btn-outline-primary" onclick="copyContent()">
            <i class="bi bi-clipboard me-1"></i>Copy
        </button>
        <a href="?scope=<?= urlencode($sel) ?>&dl=1" class="btn btn-sm btn-outline-secondary">
            <i class="bi bi-download me-1"></i>Download
        </a>
    </div>
</div>

<!-- ── Ausgabe ── -->
<div class="card">
    <div class="card-header d-flex justify-content-between">
        <span>
            <i class="bi bi-<?= $textView ? 'text-paragraph' : 'braces' ?> me-1"></i>
            scope: <?= htmlspecialchars($sel) ?>
            <?= $textView ? '<span class="badge bg-info ms-2">Lesemodus</span>' : '' ?>
        </span>
        <span class="text-muted small">erstellt: <?= $entry['created_at'] ?></span>
    </div>
    <div class="card-body p-0">
        <div id="json-output"><?= htmlspecialchars($textView ?? $pretty ?? '') ?></div>
    </div>
</div>

<?php endif; ?>
<?php endif; ?>

</div>

<?php
// Download-Modus
if (isset($_GET['dl']) && $entry) {
    header('Content-Type: application/json');
    header('Content-Disposition: attachment; filename="cache_' . preg_replace('/[^a-z0-9_-]/i','_',$sel) . '_' . date('Ymd_His') . '.json"');
    echo $pretty;
    exit;
}
?>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
function copyContent() {
    const txt = document.getElementById('json-output').innerText;
    navigator.clipboard.writeText(txt).then(() => {
        const btn = document.getElementById('btn-copy');
        btn.innerHTML = '<i class="bi bi-check2 me-1"></i>Kopiert!';
        btn.classList.replace('btn-outline-primary','btn-success');
        setTimeout(() => {
            btn.innerHTML = '<i class="bi bi-clipboard me-1"></i>Copy';
            btn.classList.replace('btn-success','btn-outline-primary');
        }, 2000);
    });
}
</script>
</body>
</html>
