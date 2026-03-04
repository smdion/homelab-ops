-- Ansible Home Lab Automation — Database Schema
-- Run once to create the ansible_logging database and all required tables.
--
-- Usage:
--   mysql -u root -p < sql/init.sql
--
-- All timestamps are stored in UTC via UTC_TIMESTAMP(). Never use NOW() —
-- the MariaDB server may be in a different timezone.

CREATE DATABASE IF NOT EXISTS ansible_logging
  CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;

USE ansible_logging;

-- Backup records — one row per backup file per run
CREATE TABLE IF NOT EXISTS backups (
  id INT AUTO_INCREMENT PRIMARY KEY,
  application VARCHAR(255),
  hostname VARCHAR(255),
  file_name VARCHAR(255),
  file_size DECIMAL(10,2),
  timestamp DATETIME,
  backup_type VARCHAR(50),
  backup_subtype VARCHAR(50),
  backup_level VARCHAR(20) NOT NULL DEFAULT 'host',
  log_date DATE GENERATED ALWAYS AS (DATE(timestamp)) STORED,
  INDEX idx_hostname (hostname),
  INDEX idx_timestamp (timestamp),
  INDEX idx_backup_type (backup_type),
  INDEX idx_backup_subtype (backup_subtype),
  INDEX idx_backup_level (backup_level),
  UNIQUE INDEX idx_daily_dedup (application, hostname, backup_type, backup_subtype, backup_level, log_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- Update records — one row per distinct version (ON DUPLICATE KEY UPDATE refreshes timestamp)
CREATE TABLE IF NOT EXISTS updates (
  id INT AUTO_INCREMENT PRIMARY KEY,
  application VARCHAR(255),
  hostname VARCHAR(255),
  version VARCHAR(100),
  timestamp DATETIME,
  update_type VARCHAR(50),
  update_subtype VARCHAR(50),
  status VARCHAR(20) NOT NULL DEFAULT 'success',
  INDEX idx_hostname (hostname),
  INDEX idx_timestamp (timestamp),
  INDEX idx_update_type (update_type),
  INDEX idx_update_subtype (update_subtype),
  UNIQUE INDEX idx_unique_version (application, hostname, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- Maintenance records — one row per host per maintenance run
CREATE TABLE IF NOT EXISTS maintenance (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  application VARCHAR(100) NOT NULL,
  hostname    VARCHAR(255) NOT NULL,
  type        VARCHAR(50)  NOT NULL,
  subtype     VARCHAR(50)  NOT NULL,
  status      VARCHAR(20)  NOT NULL DEFAULT 'success',
  timestamp   DATETIME,
  log_date    DATE GENERATED ALWAYS AS (DATE(timestamp)) STORED,
  INDEX idx_application (application),
  INDEX idx_hostname (hostname),
  INDEX idx_timestamp (timestamp),
  UNIQUE INDEX idx_daily_dedup (application, hostname, type, subtype, log_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- Health check records — one row per check per host per run
CREATE TABLE IF NOT EXISTS health_checks (
  id           INT AUTO_INCREMENT PRIMARY KEY,
  hostname     VARCHAR(255) NOT NULL,
  check_name   VARCHAR(100) NOT NULL,
  check_status VARCHAR(20)  NOT NULL,
  check_value  VARCHAR(255),
  check_detail TEXT,
  timestamp    DATETIME,
  log_date     DATE GENERATED ALWAYS AS (DATE(timestamp)) STORED,
  INDEX idx_hostname     (hostname),
  INDEX idx_check_name   (check_name),
  INDEX idx_check_status (check_status),
  INDEX idx_timestamp    (timestamp),
  UNIQUE INDEX idx_daily_dedup (hostname, check_name, log_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- Restore / verify records — one row per restore or verification operation
CREATE TABLE IF NOT EXISTS restores (
  id INT AUTO_INCREMENT PRIMARY KEY,
  application VARCHAR(255) NOT NULL,
  hostname VARCHAR(255) NOT NULL,
  source_file VARCHAR(255),
  restore_type VARCHAR(50) NOT NULL,
  restore_subtype VARCHAR(50) NOT NULL,
  operation VARCHAR(20) NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'success',
  detail TEXT,
  timestamp DATETIME,
  log_date  DATE GENERATED ALWAYS AS (DATE(timestamp)) STORED,
  INDEX idx_hostname (hostname),
  INDEX idx_timestamp (timestamp),
  INDEX idx_operation (operation),
  UNIQUE INDEX idx_daily_dedup (application, hostname, restore_subtype, operation, log_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- Health check state — single-row table tracking last successful run
CREATE TABLE IF NOT EXISTS health_check_state (
  id          INT PRIMARY KEY DEFAULT 1,
  last_check  DATETIME NOT NULL,
  CONSTRAINT single_row CHECK (id = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- Docker disk usage — one row per host per maintain_docker run
CREATE TABLE IF NOT EXISTS docker_sizes (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  hostname      VARCHAR(255) NOT NULL,
  timestamp     DATETIME     NOT NULL,
  images_count  INT,
  images_mb     DECIMAL(10,2),
  volumes_count INT,
  volumes_mb    DECIMAL(10,2),
  containers_mb DECIMAL(10,2),
  log_date      DATE GENERATED ALWAYS AS (DATE(timestamp)) STORED,
  INDEX idx_hostname  (hostname),
  INDEX idx_timestamp (timestamp),
  UNIQUE INDEX idx_daily_dedup (hostname, log_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- UniFi device metrics — one row per device per day (logged by maintain_health CHECK 29)
CREATE TABLE IF NOT EXISTS unifi_devices (
  id              INT AUTO_INCREMENT PRIMARY KEY,
  device_name     VARCHAR(255) NOT NULL,
  device_model    VARCHAR(50),
  device_mac      VARCHAR(17) NOT NULL,
  device_ip       VARCHAR(45),
  device_type     VARCHAR(50),
  firmware        VARCHAR(100),
  upgradable      BOOLEAN DEFAULT FALSE,
  state           INT,
  uptime_seconds  BIGINT,
  timestamp       DATETIME NOT NULL,
  log_date        DATE GENERATED ALWAYS AS (DATE(timestamp)) STORED,
  INDEX idx_device_name (device_name),
  INDEX idx_timestamp (timestamp),
  UNIQUE INDEX idx_daily_dedup (device_name, device_mac, log_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- Playbook run audit log — one row per invocation (per target host for distributed playbooks)
CREATE TABLE IF NOT EXISTS playbook_runs (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  playbook    VARCHAR(255) NOT NULL,
  hostname    VARCHAR(255) NOT NULL,
  run_vars    TEXT,
  timestamp   DATETIME     NOT NULL,
  INDEX idx_playbook  (playbook),
  INDEX idx_hostname  (hostname),
  INDEX idx_timestamp (timestamp)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
