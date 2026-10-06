CREATE TABLE IF NOT EXISTS categories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    display_order INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS subcategories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id   INTEGER NOT NULL REFERENCES categories(id),
    name          TEXT NOT NULL,
    display_order INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(category_id, name)
);

CREATE TABLE IF NOT EXISTS source_pdfs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    filename         TEXT NOT NULL UNIQUE,
    broker           TEXT NOT NULL DEFAULT '',
    upload_date      TEXT NOT NULL DEFAULT (datetime('now')),
    page_count       INTEGER DEFAULT 0,
    charts_extracted INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS charts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_pdf_id   INTEGER NOT NULL REFERENCES source_pdfs(id),
    subcategory_id  INTEGER REFERENCES subcategories(id),

    image_path      TEXT NOT NULL UNIQUE,

    page_number     INTEGER NOT NULL,
    region_type     TEXT NOT NULL DEFAULT 'chart',
    bbox_x0         REAL,
    bbox_y0         REAL,
    bbox_x1         REAL,
    bbox_y1         REAL,

    tag             TEXT NOT NULL DEFAULT '',
    caption         TEXT NOT NULL DEFAULT '',
    chart_type      TEXT NOT NULL DEFAULT '',
    broker          TEXT NOT NULL DEFAULT '',
    chart_date      TEXT NOT NULL DEFAULT '',
    chart_date_int  INTEGER,

    estimates       TEXT NOT NULL DEFAULT '',

    quality_status  TEXT NOT NULL DEFAULT 'pending',
    is_multi_chart  INTEGER NOT NULL DEFAULT 0,

    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_charts_subcategory ON charts(subcategory_id);
CREATE INDEX IF NOT EXISTS idx_charts_date_int ON charts(chart_date_int);
CREATE INDEX IF NOT EXISTS idx_charts_quality ON charts(quality_status);
CREATE INDEX IF NOT EXISTS idx_charts_broker ON charts(broker);

-- User-hidden virtual chart cards from the shared ES RAG gallery merge.
-- These do not delete Qdrant/vector data; they only suppress the virtual
-- card inside Memory Chart's gallery.
CREATE TABLE IF NOT EXISTS rag_chart_hidden (
    virtual_id INTEGER PRIMARY KEY,
    hidden_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- User edits for virtual ES RAG chart cards. These override only Memory Chart's
-- gallery display; they do not mutate the shared Equity Swarm/Qdrant source.
CREATE TABLE IF NOT EXISTS rag_chart_overrides (
    virtual_id       INTEGER PRIMARY KEY,
    tag              TEXT,
    caption          TEXT,
    chart_type       TEXT,
    broker           TEXT,
    chart_date       TEXT,
    chart_date_int   INTEGER,
    estimates        TEXT,
    category_name    TEXT,
    subcategory_name TEXT,
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Haiku-generated categories for virtual ES RAG chart cards. This is separate
-- from user overrides so automatic categorization can be refreshed without
-- clobbering manual edits.
CREATE TABLE IF NOT EXISTS rag_chart_categories (
    virtual_id       INTEGER PRIMARY KEY,
    caption_hash     TEXT NOT NULL,
    category_name    TEXT NOT NULL,
    subcategory_name TEXT NOT NULL,
    model            TEXT NOT NULL DEFAULT 'haiku',
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS processing_jobs (
    id               TEXT PRIMARY KEY,
    filename         TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'queued',
    total_charts     INTEGER DEFAULT 0,
    processed_charts INTEGER DEFAULT 0,
    error_message    TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Companies (for company view)
CREATE TABLE IF NOT EXISTS companies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    display_order INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chart_companies (
    chart_id   INTEGER NOT NULL REFERENCES charts(id) ON DELETE CASCADE,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    PRIMARY KEY (chart_id, company_id)
);

CREATE INDEX IF NOT EXISTS idx_chart_companies_company ON chart_companies(company_id);
CREATE INDEX IF NOT EXISTS idx_chart_companies_chart ON chart_companies(chart_id);

-- Seed companies
INSERT OR IGNORE INTO companies (name, display_order) VALUES
    ('Samsung', 1),
    ('SK Hynix', 2),
    ('Micron', 3),
    ('Kioxia', 4),
    ('Western Digital', 5),
    ('Nanya', 6),
    ('Winbond', 7),
    ('Intel', 8),
    ('YMTC', 9),
    ('CXMT', 10),
    ('General', 99);

-- Seed categories
INSERT OR IGNORE INTO categories (name, display_order) VALUES
    ('DRAM', 1),
    ('DDR DRAM', 2),
    ('HBM', 3),
    ('NAND', 4),
    ('General Memory', 5);

-- Seed subcategories
INSERT OR IGNORE INTO subcategories (category_id, name, display_order) VALUES
    ((SELECT id FROM categories WHERE name='DRAM'), 'DRAM bit growth', 1),
    ((SELECT id FROM categories WHERE name='DRAM'), 'DRAM TAM', 2),
    ((SELECT id FROM categories WHERE name='DRAM'), 'DRAM pricing', 3),
    ((SELECT id FROM categories WHERE name='DRAM'), 'DRAM supply/demand', 4),
    ((SELECT id FROM categories WHERE name='DRAM'), 'DRAM market share', 5),
    ((SELECT id FROM categories WHERE name='DRAM'), 'DRAM capex', 6),
    ((SELECT id FROM categories WHERE name='DRAM'), 'DRAM inventory', 7),
    ((SELECT id FROM categories WHERE name='DDR DRAM'), 'DDR5 pricing', 1),
    ((SELECT id FROM categories WHERE name='DDR DRAM'), 'DDR5 adoption', 2),
    ((SELECT id FROM categories WHERE name='DDR DRAM'), 'DDR4 vs DDR5', 3),
    ((SELECT id FROM categories WHERE name='DDR DRAM'), 'DDR DRAM general', 4),
    ((SELECT id FROM categories WHERE name='HBM'), 'HBM TAM', 1),
    ((SELECT id FROM categories WHERE name='HBM'), 'HBM pricing', 2),
    ((SELECT id FROM categories WHERE name='HBM'), 'HBM capacity', 3),
    ((SELECT id FROM categories WHERE name='HBM'), 'HBM market share', 4),
    ((SELECT id FROM categories WHERE name='HBM'), 'HBM roadmap', 5),
    ((SELECT id FROM categories WHERE name='HBM'), 'HBM supply/demand', 6),
    ((SELECT id FROM categories WHERE name='NAND'), 'NAND bit growth', 1),
    ((SELECT id FROM categories WHERE name='NAND'), 'NAND pricing', 2),
    ((SELECT id FROM categories WHERE name='NAND'), 'NAND supply/demand', 3),
    ((SELECT id FROM categories WHERE name='NAND'), 'NAND TAM', 4),
    ((SELECT id FROM categories WHERE name='NAND'), 'NAND market share', 5),
    ((SELECT id FROM categories WHERE name='NAND'), 'NAND capex', 6),
    ((SELECT id FROM categories WHERE name='NAND'), 'NAND inventory', 7),
    ((SELECT id FROM categories WHERE name='General Memory'), 'Overall memory market', 1),
    ((SELECT id FROM categories WHERE name='General Memory'), 'Wafer starts', 2),
    ((SELECT id FROM categories WHERE name='General Memory'), 'Memory revenue', 3),
    ((SELECT id FROM categories WHERE name='General Memory'), 'Capital spending', 4),
    ((SELECT id FROM categories WHERE name='General Memory'), 'Inventory cycle', 5),
    ((SELECT id FROM categories WHERE name='General Memory'), 'Technology roadmap', 6);
