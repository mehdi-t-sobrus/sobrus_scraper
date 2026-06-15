-- docker/postgres/init/01_extensions.sql
-- Runs automatically on first DB initialisation.
-- Creates the extensions required by the pipeline.

CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
CREATE EXTENSION IF NOT EXISTS vector;
