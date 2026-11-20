-- ============================================================================
--  Smart Bill AI - MySQL / MariaDB schema (version 001)
--  Target: MySQL 8.0+ or MariaDB 10.4+ (XAMPP ships MariaDB 10.4)
--
--  Design rules
--    * InnoDB for transactions and real foreign keys.
--    * utf8mb4 everywhere, so names and notes can contain any language.
--    * Exact money: DECIMAL(12,2). Never FLOAT for currency.
--    * Measured amounts (kWh, confidence): DOUBLE, they are display inputs.
--    * UTC timestamps in DATETIME(6); the app connects with time_zone='+00:00'.
--    * JSON for model output only (bills.analysis/feedback, ai_insights.payload).
--    * Only indexes that match a real query are created; every index is listed
--      with the code path that uses it.
--    * Foreign keys cascade on account deletion (right to erasure), and an
--      alert keeps existing if its bill is removed (ON DELETE SET NULL).
-- ============================================================================

-- @migration: 001 | initial schema: users, profiles, bills, alerts, ai insights
CREATE TABLE IF NOT EXISTS schema_migrations (
    version      VARCHAR(20)  NOT NULL,
    description  VARCHAR(200) NOT NULL,
    applied_at   DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- users: one row per account. Sessions are stateless signed cookies (JWT); the
-- per-user token_version lets logout-all and password changes revoke every token
-- without a sessions table.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id             INT UNSIGNED NOT NULL AUTO_INCREMENT,
    email          VARCHAR(254) NOT NULL,
    name           VARCHAR(80)  NOT NULL,
    password_hash  VARCHAR(255) NOT NULL,
    notify_email   TINYINT(1)   NOT NULL DEFAULT 1,
    failed_logins  INT UNSIGNED NOT NULL DEFAULT 0,
    locked_until   DATETIME(6)  NULL,
    created_at     DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    -- Login and registration both look up the lower-cased address.
    UNIQUE KEY uq_users_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- household_profiles: 1:1 with users (UNIQUE user_id). Every answer is
-- optional; NULL means "not answered", which the forecast model treats as a
-- national-average household.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS household_profiles (
    id                INT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id           INT UNSIGNED NOT NULL,
    city              VARCHAR(60)  NULL,
    climate_zone      VARCHAR(20)  NULL,
    area_sqft         INT UNSIGNED NULL,
    occupants         TINYINT UNSIGNED NULL,
    ac_count          TINYINT UNSIGNED NULL,
    ac_hours_peak     DOUBLE NULL,
    fridges           TINYINT UNSIGNED NULL,
    electric_geyser   TINYINT(1) NULL,
    induction         TINYINT(1) NULL,
    washing_machine   TINYINT(1) NULL,
    water_pump        TINYINT(1) NULL,
    ev_km_per_day     DOUBLE NULL,
    solar_kw          DOUBLE NULL,
    wfh_days          TINYINT UNSIGNED NULL,
    tariff_per_unit   DOUBLE NULL,
    fixed_charge      DOUBLE NULL,
    updated_at        DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                      ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_profiles_user (user_id),          -- enforces 1:1 + covers the FK
    CONSTRAINT fk_profiles_user
        FOREIGN KEY (user_id) REFERENCES users (id)
        ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- bills: the core table.
--
--   ENUM category  -> compact, self-validating; matches models.CATEGORIES.
--   DECIMAL amount -> exact rupee totals (SUM and forecasts stay exact).
--   file_hash      -> SHA-256 of the uploaded file, ASCII/binary, so duplicate
--                     uploads are caught by a unique key instead of a race-prone
--                     application check.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bills (
    id              INT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id         INT UNSIGNED NOT NULL,
    category        ENUM('electricity','mobile','internet','water','gas','other')
                    NOT NULL DEFAULT 'other',
    provider        VARCHAR(80)  NULL,
    account_number  VARCHAR(40)  NULL,
    bill_date       DATE NULL,
    due_date        DATE NULL,
    amount          DECIMAL(12,2) NULL,
    units           DOUBLE NULL,
    notes           VARCHAR(300) NULL,
    paid            TINYINT(1) NOT NULL DEFAULT 0,
    paid_on         DATE NULL,
    reviewed        TINYINT(1) NOT NULL DEFAULT 0,
    confidence      DOUBLE NULL,
    file_path       VARCHAR(200) NULL,
    file_mime       VARCHAR(40)  NULL,
    file_hash       CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
    raw_text        MEDIUMTEXT NULL,
    analysis        JSON NULL,
    feedback        JSON NULL,
    reminded_at     DATETIME(6) NULL,
    created_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),

    -- GET /bills?month=YYYY-MM and the month table: user + due-date range.
    KEY ix_bills_user_due (user_id, due_date),
    -- GET /bills?category=..., analytics and per-category forecasts.
    KEY ix_bills_user_cat_due (user_id, category, due_date),
    -- paid / unpaid / overdue / draft filters and the dashboard headline.
    KEY ix_bills_user_state_due (user_id, reviewed, paid, due_date),
    -- "newest bills" lists.
    KEY ix_bills_user_created (user_id, created_at),
    -- Upload de-duplication: one file per user (NULL hashes are allowed many times).
    UNIQUE KEY uq_bills_user_file (user_id, file_hash),
    -- Hourly reminder loop scans all users for reviewed, unpaid, due bills.
    KEY ix_bills_reminder_scan (reviewed, paid, due_date, reminded_at),

    CONSTRAINT fk_bills_user
        FOREIGN KEY (user_id) REFERENCES users (id)
        ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- alerts: in-app notifications. Anomaly and reminder alerts are de-duplicated
-- by (bill_id, kind, created_at), and the bell only reads unread rows.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alerts (
    id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id     INT UNSIGNED NOT NULL,
    bill_id     INT UNSIGNED NULL,
    kind        ENUM('anomaly','reminder','info') NOT NULL,
    severity    ENUM('info','warning','critical') NOT NULL DEFAULT 'info',
    message     VARCHAR(500) NOT NULL,
    is_read     TINYINT(1) NOT NULL DEFAULT 0,
    created_at  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    -- GET /alerts: user + is_read, newest first (InnoDB appends the PK).
    KEY ix_alerts_user_unread (user_id, is_read, id),
    -- Anomaly/reminder de-duplication and "alerts for this bill".
    KEY ix_alerts_bill_kind (bill_id, kind, created_at),
    CONSTRAINT fk_alerts_user
        FOREIGN KEY (user_id) REFERENCES users (id)
        ON DELETE CASCADE ON UPDATE RESTRICT,
    CONSTRAINT fk_alerts_bill
        FOREIGN KEY (bill_id) REFERENCES bills (id)
        ON DELETE SET NULL ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- ai_insights: one current model answer per (user, kind, bill). ai_cache.save()
-- deletes the previous row before inserting, hence the covering lookup index.
-- payload stays JSON because the shape differs per kind (extract/bill/portfolio).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_insights (
    id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id     INT UNSIGNED NOT NULL,
    bill_id     INT UNSIGNED NULL,
    kind        ENUM('extract','bill','portfolio') NOT NULL,
    model       VARCHAR(80) NOT NULL DEFAULT '',
    payload     JSON NOT NULL,
    created_at  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    -- ai_cache.latest(): user + kind + bill, newest id first.
    KEY ix_ai_user_kind_bill (user_id, kind, bill_id, id),
    -- FK and ai_cache.clear(bill_id=...) / cascade when a bill is deleted.
    KEY ix_ai_bill (bill_id),
    CONSTRAINT fk_ai_user
        FOREIGN KEY (user_id) REFERENCES users (id)
        ON DELETE CASCADE ON UPDATE RESTRICT,
    CONSTRAINT fk_ai_bill
        FOREIGN KEY (bill_id) REFERENCES bills (id)
        ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Normalized migration ledger row: description matches the marker verbatim and
-- the timestamp comes from the server clock (UTC session on every app connection).
INSERT INTO schema_migrations (version, description, applied_at)
VALUES ('001', 'initial schema: users, profiles, bills, alerts, ai insights', CURRENT_TIMESTAMP(6))
ON DUPLICATE KEY UPDATE description = VALUES(description);

-- ---------------------------------------------------------------------------
-- Version 002: per-user alert de-duplication key.
-- The ALTERs are deliberately explicit so a production upgrade cannot silently
-- change shape. Duplicate legacy alerts are removed first, keeping the lowest
-- id for each (user_id, dedupe_key); NULL keys are left alone (SQL equality with
-- NULL never matches, and MySQL allows many NULLs in a unique key).
-- The DELETE is a portable MySQL/MariaDB multi-table delete.
-- ---------------------------------------------------------------------------

-- @migration: 002 | alerts: dedupe_key column and unique (user_id, dedupe_key)
ALTER TABLE alerts ADD COLUMN dedupe_key VARCHAR(80) NULL AFTER kind;

DELETE a FROM alerts AS a
JOIN alerts AS d
  ON d.user_id = a.user_id
 AND d.dedupe_key = a.dedupe_key
 AND d.id < a.id;

ALTER TABLE alerts ADD UNIQUE KEY uq_alerts_user_dedupe (user_id, dedupe_key);

INSERT INTO schema_migrations (version, description, applied_at)
VALUES ('002', 'alerts: dedupe_key column and unique (user_id, dedupe_key)', CURRENT_TIMESTAMP(6))
ON DUPLICATE KEY UPDATE description = VALUES(description);

-- ---------------------------------------------------------------------------
-- Version 003: session revocation and account disable flag.
-- token_version is embedded in every JWT. Bumping it on logout-all or a password
-- change invalidates tokens that were stolen or left on another device.
-- is_active lets an administrator disable an account without deleting its data.
-- ---------------------------------------------------------------------------

-- @migration: 003 | users: token_version and is_active for session revocation
ALTER TABLE users ADD COLUMN token_version INT UNSIGNED NOT NULL DEFAULT 0 AFTER password_hash;
ALTER TABLE users ADD COLUMN is_active TINYINT(1) NOT NULL DEFAULT 1 AFTER token_version;

INSERT INTO schema_migrations (version, description, applied_at)
VALUES ('003', 'users: token_version and is_active for session revocation', CURRENT_TIMESTAMP(6))
ON DUPLICATE KEY UPDATE description = VALUES(description);

-- ---------------------------------------------------------------------------
-- Version 004: durable rate-limit counters. Storing hits in the database keeps
-- limits correct across Uvicorn workers, containers and restarts. The bucket is
-- a SHA-256 digest so raw IPs/emails never land in the table.
-- ---------------------------------------------------------------------------

-- @migration: 004 | rate_limit_hits: durable shared rate-limit buckets
CREATE TABLE IF NOT EXISTS rate_limit_hits (
    id           INT UNSIGNED NOT NULL AUTO_INCREMENT,
    bucket       CHAR(64)     NOT NULL,
    occurred_at  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY ix_rate_limit_hits_bucket_time (bucket, occurred_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT INTO schema_migrations (version, description, applied_at)
VALUES ('004', 'rate_limit_hits: durable shared rate-limit buckets', CURRENT_TIMESTAMP(6))
ON DUPLICATE KEY UPDATE description = VALUES(description);

-- ---------------------------------------------------------------------------
-- Version 005: one lock row per rate-limit bucket. SELECT ... FOR UPDATE on this
-- row serialises concurrent count/insert operations across workers and hosts.
-- The bucket is the same keyed HMAC digest stored in rate_limit_hits.
-- ---------------------------------------------------------------------------

-- @migration: 005 | rate_limit_locks: per-bucket rows for atomic rate limiting
CREATE TABLE IF NOT EXISTS rate_limit_locks (
    bucket      CHAR(64)     NOT NULL,
    created_at  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (bucket)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT INTO schema_migrations (version, description, applied_at)
VALUES ('005', 'rate_limit_locks: per-bucket rows for atomic rate limiting', CURRENT_TIMESTAMP(6))
ON DUPLICATE KEY UPDATE description = VALUES(description);
