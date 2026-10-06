from __future__ import annotations

import uuid
from typing import Any

from .connection import get_db


# ── Categories ──────────────────────────────────────────────────────

async def get_categories() -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, name, display_order FROM categories ORDER BY display_order"
    )
    return [dict(r) for r in rows]


async def get_subcategories(category_id: int | None = None) -> list[dict]:
    db = await get_db()
    if category_id:
        rows = await db.execute_fetchall(
            "SELECT id, category_id, name, display_order FROM subcategories "
            "WHERE category_id = ? ORDER BY display_order",
            (category_id,),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT id, category_id, name, display_order FROM subcategories "
            "ORDER BY category_id, display_order"
        )
    return [dict(r) for r in rows]


async def get_or_create_subcategory(category_name: str, subcategory_name: str) -> int:
    db = await get_db()
    row = await db.execute_fetchall(
        "SELECT c.id as cat_id FROM categories c WHERE c.name = ?",
        (category_name,),
    )
    if not row:
        cursor = await db.execute(
            "INSERT INTO categories (name, display_order) VALUES (?, 99)",
            (category_name,),
        )
        cat_id = cursor.lastrowid
        await db.commit()
    else:
        cat_id = row[0]["cat_id"]

    existing = await db.execute_fetchall(
        "SELECT id FROM subcategories WHERE category_id = ? AND name = ?",
        (cat_id, subcategory_name),
    )
    if existing:
        return existing[0]["id"]

    max_order = await db.execute_fetchall(
        "SELECT COALESCE(MAX(display_order), 0) as mx FROM subcategories WHERE category_id = ?",
        (cat_id,),
    )
    cursor = await db.execute(
        "INSERT INTO subcategories (category_id, name, display_order) VALUES (?, ?, ?)",
        (cat_id, subcategory_name, max_order[0]["mx"] + 1),
    )
    await db.commit()
    return cursor.lastrowid


async def delete_category(category_id: int) -> bool:
    db = await get_db()
    await db.execute(
        "UPDATE charts SET subcategory_id = NULL WHERE subcategory_id IN "
        "(SELECT id FROM subcategories WHERE category_id = ?)", (category_id,)
    )
    await db.execute("DELETE FROM subcategories WHERE category_id = ?", (category_id,))
    cursor = await db.execute("DELETE FROM categories WHERE id = ?", (category_id,))
    await db.commit()
    return cursor.rowcount > 0


async def delete_subcategory(subcategory_id: int) -> bool:
    db = await get_db()
    await db.execute(
        "UPDATE charts SET subcategory_id = NULL WHERE subcategory_id = ?",
        (subcategory_id,),
    )
    cursor = await db.execute("DELETE FROM subcategories WHERE id = ?", (subcategory_id,))
    await db.commit()
    return cursor.rowcount > 0


async def create_category(name: str) -> int:
    db = await get_db()
    max_order = await db.execute_fetchall(
        "SELECT COALESCE(MAX(display_order), 0) as mx FROM categories"
    )
    cursor = await db.execute(
        "INSERT INTO categories (name, display_order) VALUES (?, ?)",
        (name, max_order[0]["mx"] + 1),
    )
    await db.commit()
    return cursor.lastrowid


# ── Companies ──────────────────────────────────────────────────────

async def get_companies() -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, name, display_order FROM companies ORDER BY display_order"
    )
    return [dict(r) for r in rows]


async def get_or_create_company(name: str) -> int:
    db = await get_db()
    row = await db.execute_fetchall(
        "SELECT id FROM companies WHERE name = ?", (name,)
    )
    if row:
        return row[0]["id"]
    max_order = await db.execute_fetchall(
        "SELECT COALESCE(MAX(display_order), 0) as mx FROM companies"
    )
    cursor = await db.execute(
        "INSERT INTO companies (name, display_order) VALUES (?, ?)",
        (name, max_order[0]["mx"] + 1),
    )
    await db.commit()
    return cursor.lastrowid


async def link_chart_companies(chart_id: int, company_names: list[str]) -> None:
    db = await get_db()
    await db.execute("DELETE FROM chart_companies WHERE chart_id = ?", (chart_id,))
    for name in company_names:
        comp_id = await get_or_create_company(name)
        await db.execute(
            "INSERT OR IGNORE INTO chart_companies (chart_id, company_id) VALUES (?, ?)",
            (chart_id, comp_id),
        )
    await db.commit()


async def get_chart_company_names(chart_id: int) -> list[str]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT c.name FROM chart_companies cc "
        "JOIN companies c ON cc.company_id = c.id "
        "WHERE cc.chart_id = ? ORDER BY c.display_order",
        (chart_id,),
    )
    return [r["name"] for r in rows]


async def get_charts_by_company() -> list[dict]:
    """Return charts grouped by company, with industry subcategory info."""
    db = await get_db()
    sql = (
        "SELECT ch.*, sc.name as subcategory_name, cat.name as category_name, "
        "cat.display_order as cat_order, sc.display_order as sub_order, "
        "sp.filename as source_filename, "
        "comp.name as company_name, comp.display_order as comp_order "
        "FROM chart_companies cc "
        "JOIN charts ch ON cc.chart_id = ch.id "
        "JOIN companies comp ON cc.company_id = comp.id "
        "LEFT JOIN subcategories sc ON ch.subcategory_id = sc.id "
        "LEFT JOIN categories cat ON sc.category_id = cat.id "
        "LEFT JOIN source_pdfs sp ON ch.source_pdf_id = sp.id "
        "WHERE ch.quality_status = 'approved' "
        "ORDER BY comp.display_order, cat.display_order, sc.display_order, ch.chart_date_int DESC"
    )
    rows = await db.execute_fetchall(sql)
    return [dict(r) for r in rows]


# ── Source PDFs ─────────────────────────────────────────────────────

async def insert_source_pdf(filename: str, broker: str = "", page_count: int = 0) -> int:
    db = await get_db()
    existing = await db.execute_fetchall(
        "SELECT id FROM source_pdfs WHERE filename = ?", (filename,)
    )
    if existing:
        return existing[0]["id"]
    cursor = await db.execute(
        "INSERT INTO source_pdfs (filename, broker, page_count) VALUES (?, ?, ?)",
        (filename, broker, page_count),
    )
    await db.commit()
    return cursor.lastrowid


async def update_source_pdf_chart_count(pdf_id: int, count: int) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE source_pdfs SET charts_extracted = ? WHERE id = ?",
        (count, pdf_id),
    )
    await db.commit()


# ── Charts ──────────────────────────────────────────────────────────

async def insert_chart(
    source_pdf_id: int,
    image_path: str,
    page_number: int,
    region_type: str = "chart",
    bbox: tuple[float, float, float, float] | None = None,
) -> int:
    db = await get_db()
    x0, y0, x1, y1 = bbox if bbox else (0, 0, 0, 0)
    cursor = await db.execute(
        "INSERT OR IGNORE INTO charts "
        "(source_pdf_id, image_path, page_number, region_type, bbox_x0, bbox_y0, bbox_x1, bbox_y1) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (source_pdf_id, image_path, page_number, region_type, x0, y0, x1, y1),
    )
    await db.commit()
    return cursor.lastrowid


async def update_chart_metadata(
    chart_id: int,
    *,
    tag: str | None = None,
    caption: str | None = None,
    chart_type: str | None = None,
    broker: str | None = None,
    chart_date: str | None = None,
    chart_date_int: int | None = None,
    quality_status: str | None = None,
    subcategory_id: int | None = None,
    is_multi_chart: bool | None = None,
    estimates: str | None = None,
) -> None:
    db = await get_db()
    updates = []
    params: list[Any] = []
    if tag is not None:
        updates.append("tag = ?"); params.append(tag)
    if caption is not None:
        updates.append("caption = ?"); params.append(caption)
    if chart_type is not None:
        updates.append("chart_type = ?"); params.append(chart_type)
    if broker is not None:
        updates.append("broker = ?"); params.append(broker)
    if chart_date is not None:
        updates.append("chart_date = ?"); params.append(chart_date)
    if chart_date_int is not None:
        updates.append("chart_date_int = ?"); params.append(chart_date_int)
    if quality_status is not None:
        updates.append("quality_status = ?"); params.append(quality_status)
    if subcategory_id is not None:
        updates.append("subcategory_id = ?"); params.append(subcategory_id)
    if is_multi_chart is not None:
        updates.append("is_multi_chart = ?"); params.append(int(is_multi_chart))
    if estimates is not None:
        updates.append("estimates = ?"); params.append(estimates)
    if not updates:
        return
    updates.append("updated_at = datetime('now')")
    params.append(chart_id)
    await db.execute(
        f"UPDATE charts SET {', '.join(updates)} WHERE id = ?", params
    )
    await db.commit()


async def get_chart_by_id(chart_id: int) -> dict | None:
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM charts WHERE id = ?", (chart_id,))
    return dict(rows[0]) if rows else None


async def get_charts_by_image_path(image_path: str) -> dict | None:
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM charts WHERE image_path = ?", (image_path,))
    return dict(rows[0]) if rows else None


async def get_charts_flat(
    category_name: str | None = None,
    broker: str | None = None,
    date_from: int | None = None,
    date_to: int | None = None,
    search: str | None = None,
) -> list[dict]:
    db = await get_db()
    sql = (
        "SELECT ch.*, sc.name as subcategory_name, cat.name as category_name, sp.filename as source_filename "
        "FROM charts ch "
        "LEFT JOIN subcategories sc ON ch.subcategory_id = sc.id "
        "LEFT JOIN categories cat ON sc.category_id = cat.id "
        "LEFT JOIN source_pdfs sp ON ch.source_pdf_id = sp.id "
        "WHERE ch.quality_status = 'approved' "
    )
    params: list[Any] = []
    if category_name:
        sql += "AND cat.name = ? "; params.append(category_name)
    if broker:
        sql += "AND ch.broker = ? "; params.append(broker)
    if date_from:
        sql += "AND ch.chart_date_int >= ? "; params.append(date_from)
    if date_to:
        sql += "AND ch.chart_date_int <= ? "; params.append(date_to)
    if search:
        sql += "AND (ch.tag LIKE ? OR ch.caption LIKE ?) "
        params.extend([f"%{search}%", f"%{search}%"])
    sql += "ORDER BY ch.chart_date_int DESC"
    rows = await db.execute_fetchall(sql, params)
    return [dict(r) for r in rows]


async def get_charts_by_category() -> list[dict]:
    db = await get_db()
    sql = (
        "SELECT ch.*, sc.name as subcategory_name, sc.display_order as sub_order, "
        "cat.name as category_name, cat.display_order as cat_order, "
        "sp.filename as source_filename "
        "FROM charts ch "
        "LEFT JOIN subcategories sc ON ch.subcategory_id = sc.id "
        "LEFT JOIN categories cat ON sc.category_id = cat.id "
        "LEFT JOIN source_pdfs sp ON ch.source_pdf_id = sp.id "
        "WHERE ch.quality_status = 'approved' "
        "ORDER BY cat.display_order, sc.display_order, ch.chart_date_int DESC"
    )
    rows = await db.execute_fetchall(sql)
    return [dict(r) for r in rows]


async def delete_chart(chart_id: int) -> bool:
    db = await get_db()
    cursor = await db.execute("DELETE FROM charts WHERE id = ?", (chart_id,))
    await db.commit()
    return cursor.rowcount > 0


async def get_distinct_brokers() -> list[str]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT DISTINCT broker FROM charts WHERE broker != '' ORDER BY broker"
    )
    return [r["broker"] for r in rows]


# ── Processing Jobs ─────────────────────────────────────────────────

async def create_job(filename: str) -> str:
    db = await get_db()
    job_id = uuid.uuid4().hex[:12]
    await db.execute(
        "INSERT INTO processing_jobs (id, filename) VALUES (?, ?)",
        (job_id, filename),
    )
    await db.commit()
    return job_id


async def update_job(
    job_id: str,
    *,
    status: str | None = None,
    total_charts: int | None = None,
    processed_charts: int | None = None,
    error_message: str | None = None,
) -> None:
    db = await get_db()
    updates = ["updated_at = datetime('now')"]
    params: list[Any] = []
    if status is not None:
        updates.append("status = ?"); params.append(status)
    if total_charts is not None:
        updates.append("total_charts = ?"); params.append(total_charts)
    if processed_charts is not None:
        updates.append("processed_charts = ?"); params.append(processed_charts)
    if error_message is not None:
        updates.append("error_message = ?"); params.append(error_message)
    params.append(job_id)
    await db.execute(
        f"UPDATE processing_jobs SET {', '.join(updates)} WHERE id = ?", params
    )
    await db.commit()


async def get_job(job_id: str) -> dict | None:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM processing_jobs WHERE id = ?", (job_id,)
    )
    return dict(rows[0]) if rows else None


# ── Category tree with counts ──────────────────────────────────────

async def get_category_tree() -> list[dict]:
    cats = await get_categories()
    subs = await get_subcategories()
    db = await get_db()

    counts = await db.execute_fetchall(
        "SELECT subcategory_id, COUNT(*) as cnt FROM charts "
        "WHERE quality_status = 'approved' GROUP BY subcategory_id"
    )
    count_map = {r["subcategory_id"]: r["cnt"] for r in counts}

    tree = []
    for cat in cats:
        cat_subs = [s for s in subs if s["category_id"] == cat["id"]]
        total = sum(count_map.get(s["id"], 0) for s in cat_subs)
        tree.append({
            "id": cat["id"],
            "name": cat["name"],
            "display_order": cat["display_order"],
            "chart_count": total,
            "subcategories": [
                {
                    "id": s["id"],
                    "name": s["name"],
                    "display_order": s["display_order"],
                    "chart_count": count_map.get(s["id"], 0),
                }
                for s in cat_subs
            ],
        })
    return tree
