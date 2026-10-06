from __future__ import annotations

from fastapi import APIRouter

from ...services.rag_settings import SOURCE_OPTIONS, load_rag_settings, save_rag_settings

router = APIRouter(prefix="/api/settings", tags=["settings"])

_PROMPT_TEMPLATES = {
    "overview": {
        "industry_prompt": """You are a buyside semiconductor memory analyst summarizing the latest sellside research views.

CURRENT YEAR: {cur_year}
TODAY: {today}

PRIMARY SOURCE - sellside text & tables:
{extra_context}

SUPPORTING VISUAL CATALOGUE - reference only, do not lift figures from chart captions:
{catalogue_block}

Task:
Produce a structured INDUSTRY OVERVIEW summary in JSON. Use concise bullets, cite [src:S#] from primary source excerpts, keep Arete latest PDF summary, then organize Demand & Supply Summary, Demand sections, and Supply sections. Do not extract numbers from chart captions.""",
        "company_prompt": """You are a buyside semiconductor memory analyst summarizing sellside research views on specific companies.

CURRENT YEAR: {cur_year}
TODAY: {today}

PRIMARY SOURCE - sellside text & tables:
{extra_context}

SUPPORTING VISUAL CATALOGUE - reference only, do not lift figures from chart captions:
{catalogue_block}

Task:
Produce a structured COMPANY OVERVIEW summary in JSON. Consolidate competitive positioning, HBM race dynamics, capacity leadership, margin/pricing outlook, bull case, bear case, uncertainty, Arete latest summary, and company cards. Do not invent figures not supported by primary sources.""",
    },
    "dashboard": {
        "prompt": """You are extracting {broker}'s LATEST published view on ONE dashboard metric.

Metric: {metric}
Broker: {broker}
Canonical unit: {canon_unit}
Segment: {segment}
Latest source dates: {date_hint}

EXCERPTS:
{ctx}

Rules:
- Extract only values stated in the excerpts.
- Keep the selected time grain.
{frequency_rule}
- Return one row per stated period/value.
- Keep the printed unit exactly as shown.
- Require a short verbatim quote containing the value.
- Skip guessed rows.

Output:
Strict JSON with datapoints: period, value, unit, quote, source.""",
    },
    "model": {
        "prompt": """From the research excerpts, extract values for one Model tab metric across the configured calendar years.

METRIC: {metric_label} ({metric_unit})
Search intent: {query}

EXCERPTS:
{ctx}

Rules:
- Use only stated values from excerpts.
- Return values for years {min_year}-{max_year} where the excerpts state them.
- Do not extrapolate.
- Keep units aligned to the model row.
- Include quote/location evidence.

Output:
Strict JSON with year/value/location rows.""",
    },
    "ask_claude": {
        "prompt": """You are Ask Claude for the Memory Chart workspace.

RETRIEVED RESEARCH - primary evidence:
{context}

CHARTS SHOWN TO THE USER - supporting visual reference only:
{chart_catalogue}

Use retrieved ES RAG text/table chunks as primary evidence and chart captions only as supporting visual context. Answer the user's question directly, cite sources when possible, avoid unsupported numbers, and say when retrieval does not verify the requested fact.""",
    },
}


@router.get("/rag")
async def get_rag_settings():
    from .overview import _SECTION_QUERIES, default_es_overview_question

    settings = load_rag_settings()
    overview_sections = [
        {
            "key": key,
            "label": key,
            "default_query": query,
            "query": settings["tabs"]["overview"].get("sections", {}).get(key, {}).get("query", ""),
            "prompt": settings["tabs"]["overview"].get("sections", {}).get(key, {}).get("prompt", ""),
            "sources": settings["tabs"]["overview"].get("sections", {}).get(key, {}).get(
                "sources",
                settings["tabs"]["overview"].get("sources", settings["retrieval"].get("sources", [])),
            ),
        }
        for key, query in _SECTION_QUERIES.items()
    ]
    return {
        "settings": settings,
        "overview_sections": overview_sections,
        "source_options": list(SOURCE_OPTIONS),
        "prompt_templates": _PROMPT_TEMPLATES,
        "overview_api_prompts": {
            "industry": default_es_overview_question("industry"),
            "company": default_es_overview_question("company"),
        },
    }


@router.put("/rag")
async def update_rag_settings(body: dict):
    return {
        "settings": save_rag_settings(body),
    }
