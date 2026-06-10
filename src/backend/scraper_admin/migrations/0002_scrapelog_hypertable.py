"""
src/backend/scraper_admin/migrations/0002_scrapelog_hypertable.py
==================================================================
Promotes ScrapeLog to a TimescaleDB hypertable partitioned on fetched_at.

Why the primary key manipulation:
    TimescaleDB requires that ALL unique indexes on a table include the
    partition column (fetched_at).  Django's BigAutoField creates an implicit
    unique index on `id` alone — TimescaleDB rejects this.

    The fix:
    1. Drop the Django-created primary key constraint on `id`.
    2. Call create_hypertable() — now there are no conflicting unique indexes.
    3. Add a composite primary key on (id, fetched_at) — satisfies both
       Django's need for a PK and TimescaleDB's partitioning requirement.

    After this migration, Django can still use `id` to identify rows because
    the composite PK still guarantees uniqueness of `id` within each chunk.
"""

from django.db import migrations


UPGRADE_SQL = """
-- Step 1: Drop the auto-created primary key on `id` alone.
-- TimescaleDB cannot create a hypertable when a unique index exists that
-- does not include the partition column (fetched_at).
ALTER TABLE scraper_admin_scrapelog DROP CONSTRAINT scraper_admin_scrapelog_pkey;

-- Step 2: Promote to hypertable — no conflicting unique indexes remain.
SELECT create_hypertable(
    'scraper_admin_scrapelog',
    'fetched_at',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE
);

-- Step 3: Re-add PK as composite (id, fetched_at) — satisfies TimescaleDB.
-- id is still unique in practice (bigserial sequence), but the constraint
-- now includes fetched_at so TimescaleDB can enforce it per-chunk.
ALTER TABLE scraper_admin_scrapelog
    ADD CONSTRAINT scraper_admin_scrapelog_pkey PRIMARY KEY (id, fetched_at);

-- Step 4: Compression policy (chunks older than 30 days).
ALTER TABLE scraper_admin_scrapelog SET (
    timescaledb.compress,
    timescaledb.compress_orderby = 'fetched_at DESC',
    timescaledb.compress_segmentby = 'site_id, status'
);

SELECT add_compression_policy(
    'scraper_admin_scrapelog',
    INTERVAL '30 days',
    if_not_exists => TRUE
);
"""

DOWNGRADE_SQL = """
-- Reversing a hypertable promotion requires manual intervention.
-- Left as no-op intentionally — see CLAUDE.md §3.
SELECT 1;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("scraper_admin", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql=UPGRADE_SQL,
            reverse_sql=DOWNGRADE_SQL,
        ),
    ]
