-- 001_purchase_review.sql
-- Crea las tablas para el módulo de Compras Automáticas.
-- Ejecutar: psql $NEON_DB_URL -f migrations/001_purchase_review.sql

CREATE TABLE IF NOT EXISTS purchase_review (
    id SERIAL PRIMARY KEY,
    doc_id UUID NOT NULL,
    company_id VARCHAR(64) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    ai_suggestions JSONB,
    manual_lines JSONB,
    purchase_order_id VARCHAR(64),
    groq_response_raw TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    reviewed_at TIMESTAMP,
    confirmed_at TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_purchase_review_doc_unique ON purchase_review(doc_id);
CREATE INDEX IF NOT EXISTS idx_purchase_review_status ON purchase_review(status);

CREATE TABLE IF NOT EXISTS product_mapping_cache (
    id SERIAL PRIMARY KEY,
    line_hash VARCHAR(64) UNIQUE NOT NULL,
    line_items_json TEXT NOT NULL,
    catalogo_json TEXT,
    resultado_json TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- correction_history (aprendizaje automático)
CREATE TABLE IF NOT EXISTS correction_history (
    id SERIAL PRIMARY KEY,
    normalized_desc TEXT NOT NULL,
    producto_id INTEGER NOT NULL,
    producto_nombre TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Deduplicar antes de agregar restricción de unicidad
DELETE FROM correction_history a
USING correction_history b
WHERE a.id < b.id AND a.normalized_desc = b.normalized_desc;

-- Crear un índice único sobre normalized_desc para permitir ON CONFLICT
CREATE UNIQUE INDEX IF NOT EXISTS idx_correction_history_desc_uniq ON correction_history(normalized_desc);

