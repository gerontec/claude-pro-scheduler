-- Claude Pro Scheduler – Datenbankschema
-- MariaDB / MySQL

CREATE TABLE IF NOT EXISTS claude_pro_batch (
  id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  created_at    DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  targetdate    DATE            NOT NULL,
  model         ENUM('haiku','sonnet','opus') NOT NULL DEFAULT 'haiku',
  prompt        TEXT            NOT NULL,
  status        ENUM('queued','running','done','failed') NOT NULL DEFAULT 'queued',
  result        LONGTEXT,
  input_tokens  INT UNSIGNED,
  output_tokens INT UNSIGNED,
  cache_tokens  INT UNSIGNED,
  cost_usd      DECIMAL(10,6),
  started_at    DATETIME(3),
  finished_at   DATETIME(3),
  error_msg     TEXT,

  INDEX idx_status    (status),
  INDEX idx_targetdate (targetdate),
  INDEX idx_created   (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
