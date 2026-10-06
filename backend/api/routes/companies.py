from __future__ import annotations

from fastapi import APIRouter

from ...db import queries

router = APIRouter(prefix="/api/companies", tags=["companies"])


@router.get("")
async def list_companies():
    return await queries.get_companies()


@router.get("/charts")
async def charts_by_company():
    rows = await queries.get_charts_by_company()
    grouped: dict[str, dict] = {}
    for r in rows:
        comp_name = r.get("company_name") or "General"
        cat_name = r.get("category_name") or "Uncategorized"
        sub_name = r.get("subcategory_name") or "Uncategorized"

        if comp_name not in grouped:
            grouped[comp_name] = {
                "company": comp_name,
                "comp_order": r.get("comp_order", 99),
                "subcategories": {},
            }

        comp = grouped[comp_name]
        sub_key = f"{cat_name} > {sub_name}"
        if sub_key not in comp["subcategories"]:
            comp["subcategories"][sub_key] = {
                "subcategory": sub_name,
                "category": cat_name,
                "cat_order": r.get("cat_order", 99),
                "sub_order": r.get("sub_order", 99),
                "charts": [],
            }

        chart_id = r["id"]
        sub = comp["subcategories"][sub_key]
        if not any(c["id"] == chart_id for c in sub["charts"]):
            sub["charts"].append(r)

    result = []
    for comp in sorted(grouped.values(), key=lambda c: c["comp_order"]):
        subs = sorted(
            comp["subcategories"].values(),
            key=lambda s: (s["cat_order"], s["sub_order"]),
        )
        for s in subs:
            s["charts"] = sorted(
                s["charts"],
                key=lambda c: (
                    c.get("chart_date_int") or 0,
                    c.get("broker") or "",
                    c.get("tag") or "",
                ),
                reverse=True,
            )
        result.append({
            "company": comp["company"],
            "subcategories": subs,
        })
    return result
