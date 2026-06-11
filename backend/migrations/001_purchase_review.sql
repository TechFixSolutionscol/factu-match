-- 001_purchase_review.sql
-- Crea las tablas para el módulo de Compras Automáticas.
-- Ejecutar: psql $NEON_DB_URL -f migrations/001_purchase_review.sql

DROP TABLE IF EXISTS purchase_review CASCADE;

CREATE TABLE purchase_review (
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
