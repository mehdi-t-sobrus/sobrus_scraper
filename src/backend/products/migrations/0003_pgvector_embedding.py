"""
src/backend/products/migrations/0003_pgvector_embedding.py
===========================================================
Enables the pgvector PostgreSQL extension and adds a vector
embedding column to MasterProduct for Tier 4 semantic similarity search.

Install pgvector first:
    brew install pgvector          # macOS
    apt install postgresql-17-pgvector  # Ubuntu/Debian

Then run:
    python manage.py migrate products 0003
"""

from django.db import migrations


UPGRADE_SQL = """
-- Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Add 768-dimensional embedding column to MasterProduct.
-- 768 = output dimension of paraphrase-multilingual-mpnet-base-v2
-- and multilingual-e5-base — both standard sentence-transformer models.
ALTER TABLE products_masterproduct
    ADD COLUMN IF NOT EXISTS name_embedding vector(768);

-- HNSW index for fast approximate nearest-neighbour cosine search.
-- Much faster than exact search at 400k+ vectors.
-- ef_construction=128 and m=16 are good defaults for this scale.
CREATE INDEX IF NOT EXISTS idx_masterproduct_embedding_hnsw
    ON products_masterproduct
    USING hnsw (name_embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128);
"""

DOWNGRADE_SQL = """
DROP INDEX IF EXISTS idx_masterproduct_embedding_hnsw;
ALTER TABLE products_masterproduct DROP COLUMN IF EXISTS name_embedding;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("products", "0002_dailypricelog_hypertable"),
    ]

    operations = [
        migrations.RunSQL(
            sql=UPGRADE_SQL,
            reverse_sql=DOWNGRADE_SQL,
        ),
    ]
