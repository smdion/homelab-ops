-- Migrate ansible_logging tables to daily dedup.
-- Adds a generated log_date column and UNIQUE index to each table so that
-- repeated same-day runs UPSERT instead of creating duplicate rows.
--
-- Usage:
--   mysql -u root -p ansible_logging < sql/migrate_daily_dedup.sql
--
-- Safe to re-run: uses IF NOT EXISTS / IF EXISTS guards.
-- Tables affected: backups, maintenance, health_checks, docker_sizes, restores
-- Tables NOT affected: updates (already has dedup), playbook_runs (audit trail),
--                       health_check_state (single-row)

USE ansible_logging;

-- ═══════════════════════════════════════════════════════════════════
-- backups — dedup on (application, hostname, backup_type, backup_subtype, backup_level, date)
-- ═══════════════════════════════════════════════════════════════════

-- Remove existing duplicates (keep row with highest id per group per day)
DELETE b1 FROM backups b1
INNER JOIN backups b2
  ON  b1.application  = b2.application
  AND b1.hostname     = b2.hostname
  AND b1.backup_type  = b2.backup_type
  AND b1.backup_subtype = b2.backup_subtype
  AND b1.backup_level = b2.backup_level
  AND DATE(b1.timestamp) = DATE(b2.timestamp)
  AND b1.id < b2.id;

ALTER TABLE backups
  ADD COLUMN IF NOT EXISTS log_date DATE GENERATED ALWAYS AS (DATE(timestamp)) STORED;

-- Drop index if it already exists (idempotent re-run)
SET @idx_exists = (SELECT COUNT(*) FROM information_schema.statistics
  WHERE table_schema = 'ansible_logging' AND table_name = 'backups' AND index_name = 'idx_daily_dedup');
SET @sql = IF(@idx_exists = 0,
  'ALTER TABLE backups ADD UNIQUE INDEX idx_daily_dedup (application, hostname, backup_type, backup_subtype, backup_level, log_date)',
  'SELECT 1');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- ═══════════════════════════════════════════════════════════════════
-- maintenance — dedup on (application, hostname, type, subtype, date)
-- ═══════════════════════════════════════════════════════════════════

DELETE m1 FROM maintenance m1
INNER JOIN maintenance m2
  ON  m1.application = m2.application
  AND m1.hostname    = m2.hostname
  AND m1.type        = m2.type
  AND m1.subtype     = m2.subtype
  AND DATE(m1.timestamp) = DATE(m2.timestamp)
  AND m1.id < m2.id;

ALTER TABLE maintenance
  ADD COLUMN IF NOT EXISTS log_date DATE GENERATED ALWAYS AS (DATE(timestamp)) STORED;

SET @idx_exists = (SELECT COUNT(*) FROM information_schema.statistics
  WHERE table_schema = 'ansible_logging' AND table_name = 'maintenance' AND index_name = 'idx_daily_dedup');
SET @sql = IF(@idx_exists = 0,
  'ALTER TABLE maintenance ADD UNIQUE INDEX idx_daily_dedup (application, hostname, type, subtype, log_date)',
  'SELECT 1');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- ═══════════════════════════════════════════════════════════════════
-- health_checks — dedup on (hostname, check_name, date)
-- ═══════════════════════════════════════════════════════════════════

DELETE h1 FROM health_checks h1
INNER JOIN health_checks h2
  ON  h1.hostname   = h2.hostname
  AND h1.check_name = h2.check_name
  AND DATE(h1.timestamp) = DATE(h2.timestamp)
  AND h1.id < h2.id;

ALTER TABLE health_checks
  ADD COLUMN IF NOT EXISTS log_date DATE GENERATED ALWAYS AS (DATE(timestamp)) STORED;

SET @idx_exists = (SELECT COUNT(*) FROM information_schema.statistics
  WHERE table_schema = 'ansible_logging' AND table_name = 'health_checks' AND index_name = 'idx_daily_dedup');
SET @sql = IF(@idx_exists = 0,
  'ALTER TABLE health_checks ADD UNIQUE INDEX idx_daily_dedup (hostname, check_name, log_date)',
  'SELECT 1');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- ═══════════════════════════════════════════════════════════════════
-- docker_sizes — dedup on (hostname, date)
-- ═══════════════════════════════════════════════════════════════════

DELETE d1 FROM docker_sizes d1
INNER JOIN docker_sizes d2
  ON  d1.hostname = d2.hostname
  AND DATE(d1.timestamp) = DATE(d2.timestamp)
  AND d1.id < d2.id;

ALTER TABLE docker_sizes
  ADD COLUMN IF NOT EXISTS log_date DATE GENERATED ALWAYS AS (DATE(timestamp)) STORED;

SET @idx_exists = (SELECT COUNT(*) FROM information_schema.statistics
  WHERE table_schema = 'ansible_logging' AND table_name = 'docker_sizes' AND index_name = 'idx_daily_dedup');
SET @sql = IF(@idx_exists = 0,
  'ALTER TABLE docker_sizes ADD UNIQUE INDEX idx_daily_dedup (hostname, log_date)',
  'SELECT 1');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- ═══════════════════════════════════════════════════════════════════
-- restores — dedup on (application, hostname, restore_subtype, operation, date)
-- ═══════════════════════════════════════════════════════════════════

DELETE r1 FROM restores r1
INNER JOIN restores r2
  ON  r1.application     = r2.application
  AND r1.hostname        = r2.hostname
  AND r1.restore_subtype = r2.restore_subtype
  AND r1.operation       = r2.operation
  AND DATE(r1.timestamp) = DATE(r2.timestamp)
  AND r1.id < r2.id;

ALTER TABLE restores
  ADD COLUMN IF NOT EXISTS log_date DATE GENERATED ALWAYS AS (DATE(timestamp)) STORED;

SET @idx_exists = (SELECT COUNT(*) FROM information_schema.statistics
  WHERE table_schema = 'ansible_logging' AND table_name = 'restores' AND index_name = 'idx_daily_dedup');
SET @sql = IF(@idx_exists = 0,
  'ALTER TABLE restores ADD UNIQUE INDEX idx_daily_dedup (application, hostname, restore_subtype, operation, log_date)',
  'SELECT 1');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
