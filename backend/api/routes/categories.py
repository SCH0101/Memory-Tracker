from __future__ import annotations

from pydantic import BaseModel
from fastapi import APIRouter, HTTPException

from ..models import CategoryOut, SubcategoryCreate
from ...db import queries

router = APIRouter(prefix="/api/categories", tags=["categories"])


class CategoryCreate(BaseModel):
    name: str


@router.get("", response_model=list[CategoryOut])
async def list_categories():
    return await queries.get_category_tree()


@router.post("")
async def create_category(body: CategoryCreate):
    cat_id = await queries.create_category(body.name)
    return {"id": cat_id, "name": body.name}


@router.delete("/{category_id}")
async def delete_category(category_id: int):
    deleted = await queries.delete_category(category_id)
    if not deleted:
        raise HTTPException(404, "Category not found")
    return {"ok": True}


@router.post("/{category_id}/subcategories")
async def add_subcategory(category_id: int, body: SubcategoryCreate):
    cats = await queries.get_categories()
    cat = next((c for c in cats if c["id"] == category_id), None)
    if not cat:
        raise HTTPException(404, "Category not found")
    sub_id = await queries.get_or_create_subcategory(cat["name"], body.name)
    return {"id": sub_id, "name": body.name}


@router.delete("/{category_id}/subcategories/{subcategory_id}")
async def delete_subcategory(category_id: int, subcategory_id: int):
    deleted = await queries.delete_subcategory(subcategory_id)
    if not deleted:
        raise HTTPException(404, "Subcategory not found")
    return {"ok": True}
