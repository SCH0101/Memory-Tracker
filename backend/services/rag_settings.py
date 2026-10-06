from __future__ import annotations

import copy
import json
from typing import Any

from ..paths import RAG_SETTINGS_PATH

SOURCE_OPTIONS: tuple[str, ...] = (
    "broker_research",
    "expert_call",
    "company_presentation",
    "industry_report",
    "earnings_transcript",
    "tables",
    "user_input",
)
DEFAULT_SOURCES: list[str] = list(SOURCE_OPTIONS)
SOURCE_ALIASES: dict[str, str] = {
    "broker": "broker_research",
    "sellside": "broker_research",
    "broker_research_note": "broker_research",
    "presentation": "company_presentation",
    "company_presentations": "company_presentation",
    "industry_reports": "industry_report",
    "transcript": "earnings_transcript",
    "earnings_call_transcript": "earnings_transcript",
}

DEFAULT_RAG_SETTINGS: dict[str, Any] = {
    "retrieval": {
        "chunks_get": 10,
        "rerank_keep": 4,
        "sources": DEFAULT_SOURCES,
    },
    "tabs": {
        "overview": {
            "industry_prompt": "",
            "company_prompt": "",
            "sources": DEFAULT_SOURCES,
            "sections": {},
        },
        "dashboard": {
            "prompt": "",
            "sources": DEFAULT_SOURCES,
        },
        "model": {
            "prompt": "",
            "sources": DEFAULT_SOURCES,
        },
        "ask_claude": {
            "prompt": "",
            "sources": DEFAULT_SOURCES,
        },
    },
}


def _deep_merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (incoming or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _clean_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        v = int(value)
    except Exception:
        return default
    return max(lo, min(hi, v))


def _clean_text(value: Any, max_len: int = 8000) -> str:
    if value is None:
        return ""
    return str(value)[:max_len]


def _clean_sources(value: Any, default: list[str] | tuple[str, ...] = DEFAULT_SOURCES) -> list[str]:
    if value is None:
        return list(default)
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        return list(default)
    allowed = set(SOURCE_OPTIONS)
    out: list[str] = []
    for item in raw:
        key = str(item or "").strip()
        key = SOURCE_ALIASES.get(key, key)
        if key in allowed and key not in out:
            out.append(key)
    return out


def render_prompt_template(template: str, values: dict[str, Any]) -> str:
    """Render simple {name} placeholders without treating JSON braces as syntax."""
    out = str(template or "")
    for key, value in values.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def _sanitize(settings: dict[str, Any]) -> dict[str, Any]:
    merged = _deep_merge(DEFAULT_RAG_SETTINGS, settings or {})
    defaults = DEFAULT_RAG_SETTINGS["retrieval"]
    retrieval = merged.setdefault("retrieval", {})
    retrieval["chunks_get"] = _clean_int(
        retrieval.get("chunks_get"), defaults["chunks_get"], 1, 80
    )
    retrieval["rerank_keep"] = _clean_int(
        retrieval.get("rerank_keep"), defaults["rerank_keep"], 0, 30
    )
    retrieval["sources"] = _clean_sources(retrieval.get("sources"), DEFAULT_SOURCES)

    raw_tabs = merged.setdefault("tabs", {})
    allowed_tabs = ("overview", "dashboard", "model", "ask_claude")
    tabs = {tab: raw_tabs.get(tab, {}) for tab in allowed_tabs}
    merged["tabs"] = tabs
    for tab in allowed_tabs:
        tabs.setdefault(tab, {})

    overview = tabs["overview"]
    overview["industry_prompt"] = _clean_text(overview.get("industry_prompt"), 20000)
    overview["company_prompt"] = _clean_text(overview.get("company_prompt"), 20000)
    overview["sources"] = _clean_sources(overview.get("sources"), retrieval["sources"])
    sections = overview.setdefault("sections", {})
    if not isinstance(sections, dict):
        overview["sections"] = {}
    else:
        clean_sections: dict[str, dict[str, Any]] = {}
        for key, value in sections.items():
            if not isinstance(value, dict):
                value = {}
            clean_sections[str(key)] = {
                "query": _clean_text(value.get("query"), 2000),
                "prompt": _clean_text(value.get("prompt"), 20000),
                "sources": _clean_sources(value.get("sources"), overview["sources"]),
            }
        overview["sections"] = clean_sections

    for tab in ("dashboard", "model", "ask_claude"):
        tabs[tab]["prompt"] = _clean_text(tabs[tab].get("prompt"), 20000)
        tabs[tab]["sources"] = _clean_sources(tabs[tab].get("sources"), retrieval["sources"])

    return merged


def load_rag_settings() -> dict[str, Any]:
    try:
        raw = json.loads(RAG_SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    return _sanitize(raw)


def save_rag_settings(settings: dict[str, Any]) -> dict[str, Any]:
    clean = _sanitize(settings)
    RAG_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RAG_SETTINGS_PATH.write_text(json.dumps(clean, indent=2), encoding="utf-8")
    return clean


def get_retrieval_settings() -> dict[str, Any]:
    return load_rag_settings()["retrieval"]


def get_rerank_candidates(retrieval: dict[str, Any], default: int) -> int:
    """Return the ES rerank candidate count.

    ES RAG treats 0 as "skip BGE rerank", so preserve 0 exactly instead of
    converting it to a local keep-count fallback.
    """
    try:
        return max(0, int(retrieval.get("rerank_keep", default)))
    except Exception:
        return max(0, default)


def get_tab_prompt(tab: str, key: str = "prompt") -> str:
    tabs = load_rag_settings().get("tabs", {})
    return str(tabs.get(tab, {}).get(key, "") or "").strip()


def get_tab_sources(tab: str) -> list[str]:
    settings = load_rag_settings()
    tabs = settings.get("tabs", {})
    return _clean_sources(
        tabs.get(tab, {}).get("sources"),
        settings.get("retrieval", {}).get("sources", DEFAULT_SOURCES),
    )


def get_overview_section_settings(section: str) -> dict[str, Any]:
    overview = load_rag_settings().get("tabs", {}).get("overview", {})
    sections = overview.get("sections", {})
    raw = sections.get(section, {}) if isinstance(sections, dict) else {}
    if not isinstance(raw, dict):
        return {"query": "", "prompt": "", "sources": get_tab_sources("overview")}
    return {
        "query": str(raw.get("query") or "").strip(),
        "prompt": str(raw.get("prompt") or "").strip(),
        "sources": _clean_sources(raw.get("sources"), get_tab_sources("overview")),
    }
