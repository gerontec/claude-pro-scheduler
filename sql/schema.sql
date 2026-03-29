-- Claude Pro Scheduler – Datenbankschema
-- MariaDB / MySQL

CREATE TABLE IF NOT EXISTS claude_pro_batch (
  id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  targetdate      DATE            NOT NULL,
  model           ENUM('haiku','sonnet','opus') NOT NULL DEFAULT 'haiku',
  resume_session  TINYINT(1)      NOT NULL DEFAULT 0,
  prompt          TEXT            NOT NULL,
  status          ENUM('queued','running','done','failed') NOT NULL DEFAULT 'queued',
  result          LONGTEXT,
  input_tokens    INT UNSIGNED,
  output_tokens   INT UNSIGNED,
  cache_tokens    INT UNSIGNED,
  cost_usd        DECIMAL(10,6),
  started_at      DATETIME,
  finished_at     DATETIME,
  error_msg       TEXT,

  INDEX idx_status     (status),
  INDEX idx_targetdate (targetdate),
  INDEX idx_created    (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


CREATE TABLE IF NOT EXISTS claude_context_cache (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  scope         VARCHAR(64)  NOT NULL,
  version       INT          NOT NULL DEFAULT 1,
  created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME     ON UPDATE CURRENT_TIMESTAMP,
  updated_by    VARCHAR(128),
  summary       VARCHAR(500) DEFAULT NULL,
  ttl_hours     INT          DEFAULT 168,
  context_json  LONGTEXT     NOT NULL,
  UNIQUE KEY uq_scope (scope)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
