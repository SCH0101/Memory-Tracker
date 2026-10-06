from __future__ import annotations

from pydantic import BaseModel


class UploadResponse(BaseModel):
    job_id: str
    filename: str
    message: str


class JobStatus(BaseModel):
    id: str
    filename: str
    status: str
    total_charts: int
    processed_charts: int
    error_message: str | None = None


class ChartOut(BaseModel):
    id: int
    source_pdf_id: int
    subcategory_id: int | None = None
    image_path: str
    page_number: int
    region_type: str
    tag: str
    caption: str
    chart_type: str
    broker: str
    chart_date: str
    chart_date_int: int | None = None
    estimates: str = ""
    quality_status: str
    category_name: str | None = None
    subcategory_name: str | None = None
    source_filename: str | None = None


class ChartUpdate(BaseModel):
    tag: str | None = None
    caption: str | None = None
    chart_type: str | None = None
    broker: str | None = None
    chart_date: str | None = None
    estimates: str | None = None
    category_name: str | None = None
    subcategory_name: str | None = None
    recategorize: bool = False  # When True, LLM re-categorizes based on new tag


class SubcategoryOut(BaseModel):
    id: int
    name: str
    display_order: int
    chart_count: int = 0


class CategoryOut(BaseModel):
    id: int
    name: str
    display_order: int
    chart_count: int = 0
    subcategories: list[SubcategoryOut] = []


class SubcategoryCreate(BaseModel):
    name: str
