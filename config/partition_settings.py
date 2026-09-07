import os

# --- Time-partition management (scripts/manage_partitions.py, ADR 0005) ---
# messages is RANGE-partitioned by created_at, message_receipt_log by
# occurred_at. A standalone idempotent script creates dated partitions ahead
# of time, reports fill/DEFAULT state, and cold-freezes old ones. These are
# values only - all boundary/scheduling logic lives in the script.
#
# Partition granularity per table. messages: one ISO week (Monday 00:00 UTC).
# message_receipt_log: one calendar day (UTC). Changing these does not
# reshape existing partitions - only new ones.
MESSAGE_PARTITION_INTERVAL = os.environ.get("MESSAGE_PARTITION_INTERVAL", "week")
RECEIPT_LOG_PARTITION_INTERVAL = os.environ.get("RECEIPT_LOG_PARTITION_INTERVAL", "day")

# How far ahead --ensure keeps empty partitions pre-created, so a write is
# never the thing that discovers a missing partition. The daily cron only
# needs to add one at a time; the buffer covers missed runs.
MESSAGE_PARTITION_PRECREATE_WEEKS = int(os.environ.get("MESSAGE_PARTITION_PRECREATE_WEEKS", "6"))
RECEIPT_LOG_PRECREATE_DAYS = int(os.environ.get("RECEIPT_LOG_PRECREATE_DAYS", "10"))

# A messages partition whose whole range is older than this is eligible for
# --cold: VACUUM FREEZE, move to the cold tablespace, autovacuum_enabled=false.
# messages retention is infinite; receipt_log retention stays on
# RECEIPT_LOG_RETENTION_DAYS via scripts/prune_receipt_log.py.
MESSAGE_PARTITION_COLD_AFTER_MONTHS = int(os.environ.get("MESSAGE_PARTITION_COLD_AFTER_MONTHS", "12"))

# Tablespace that --cold moves frozen old `messages` partitions onto. A DBA must
# pre-create it (CREATE TABLESPACE ... LOCATION ...) pointing at cheaper storage.
# Empty string = skip the physical move; --cold still runs VACUUM FREEZE and
# disables autovacuum on the partition.
MESSAGE_PARTITION_COLD_TABLESPACE = os.environ.get("MESSAGE_PARTITION_COLD_TABLESPACE", "")

# Slack applied to a Snowflake-id-derived created_at bound before it is used as
# a partition-pruning predicate on `messages` (crud_message). A message id is
# minted in-process a short moment before its row's server-side created_at
# default fires, so the id's timestamp and the stored created_at can differ by
# up to a few seconds; widening the derived bound by this margin keeps the
# predicate a safe superset (never drops a matching row) while still letting
# Postgres prune whole weekly partitions.
MESSAGE_PARTITION_QUERY_SKEW_HOURS = int(os.environ.get("MESSAGE_PARTITION_QUERY_SKEW_HOURS", "1"))

__all__ = [
    "MESSAGE_PARTITION_INTERVAL",
    "RECEIPT_LOG_PARTITION_INTERVAL",
    "MESSAGE_PARTITION_PRECREATE_WEEKS",
    "RECEIPT_LOG_PRECREATE_DAYS",
    "MESSAGE_PARTITION_COLD_AFTER_MONTHS",
    "MESSAGE_PARTITION_COLD_TABLESPACE",
    "MESSAGE_PARTITION_QUERY_SKEW_HOURS",
]
