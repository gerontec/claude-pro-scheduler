<?php
/**
 * Batch Federation API  — /api/batch/api.php
 *
 * POST  /api/batch/api.php          submit job    → {"id":N,"status":"queued"}
 * GET   /api/batch/api.php?id=N     job status    → {"id":N,"status":…,"result":…}
 * GET   /api/batch/api.php?list=1   last 20 jobs  → [{…},…]
 *
 * Auth: Header  X-API-Key: <key>
 *       or GET  ?apikey=<key>
 *
 * POST body (JSON):
 *   { "prompt": "…", "model": "xiaomi", "targetdate": "2026-04-04", "resume_session": false }
 *   model defaults to "xiaomi", targetdate defaults to today.
 */

header('Content-Type: application/json; charset=utf-8');
header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Headers: X-API-Key, Content-Type');

// ── Auth ──────────────────────────────────────────────────
$API_KEY = '2a61f527ded09cc2832cb49f8829f299';
$given   = $_SERVER['HTTP_X_API_KEY']
         ?? $_GET['apikey']
         ?? '';
if (!hash_equals($API_KEY, $given)) {
    http_response_code(401);
    echo json_encode(['error' => 'Unauthorized']);
    exit;
}

// ── DB ───────────────────────────────────────────────────
$pdo = new PDO(
    'mysql:host=localhost;dbname=wagodb;charset=utf8mb4',
    'gh', 'a12345',
    [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
     PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC]
);

$VALID_MODELS = ['sonnet','opus','xiaomi','mimo-pro','qwen'];

// ── POST → submit job ─────────────────────────────────────
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $body = json_decode(file_get_contents('php://input'), true);
    if (!$body || empty($body['prompt'])) {
        http_response_code(400);
        echo json_encode(['error' => 'Missing prompt']);
        exit;
    }
    $prompt         = trim($body['prompt']);
    $model          = in_array($body['model'] ?? '', $VALID_MODELS) ? $body['model'] : 'xiaomi';
    $targetdate     = preg_match('/^\d{4}-\d{2}-\d{2}$/', $body['targetdate'] ?? '')
                      ? $body['targetdate'] : date('Y-m-d');
    $resume_session = !empty($body['resume_session']) ? 1 : 0;

    $stmt = $pdo->prepare(
        "INSERT INTO claude_pro_batch (targetdate, model, resume_session, prompt)
         VALUES (?, ?, ?, ?)"
    );
    $stmt->execute([$targetdate, $model, $resume_session, $prompt]);
    $id = (int)$pdo->lastInsertId();

    http_response_code(201);
    echo json_encode(['id' => $id, 'status' => 'queued', 'model' => $model,
                      'targetdate' => $targetdate]);
    exit;
}

// ── GET ?id=N → job status ────────────────────────────────
if (isset($_GET['id'])) {
    $id  = (int)$_GET['id'];
    $row = $pdo->prepare("SELECT * FROM claude_pro_batch WHERE id=?");
    $row->execute([$id]);
    $j   = $row->fetch();
    if (!$j) {
        http_response_code(404);
        echo json_encode(['error' => 'Not found']);
        exit;
    }
    // strip heavy fields unless explicitly requested
    if (empty($_GET['full'])) {
        unset($j['prompt']);
    }
    echo json_encode($j);
    exit;
}

// ── GET ?list=1 → recent jobs ─────────────────────────────
if (isset($_GET['list'])) {
    $limit  = min((int)($_GET['limit'] ?? 20), 100);
    $status = $_GET['status'] ?? '';
    if ($status && in_array($status, ['queued','running','done','failed'])) {
        $rows = $pdo->prepare(
            "SELECT id,created_at,targetdate,model,status,cost_usd,started_at,finished_at,error_msg
             FROM claude_pro_batch WHERE status=? ORDER BY id DESC LIMIT $limit"
        );
        $rows->execute([$status]);
    } else {
        $rows = $pdo->query(
            "SELECT id,created_at,targetdate,model,status,cost_usd,started_at,finished_at,error_msg
             FROM claude_pro_batch ORDER BY id DESC LIMIT $limit"
        );
    }
    echo json_encode($rows->fetchAll());
    exit;
}

// ── Fallback ──────────────────────────────────────────────
http_response_code(400);
echo json_encode(['error' => 'Use POST to submit, GET ?id=N for status, GET ?list=1 for list']);
