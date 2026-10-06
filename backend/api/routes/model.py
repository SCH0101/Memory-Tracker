"""GB300 AI-memory capacity bridge and SK Hynix revenue cross-check.

Active HBM model:
  DC provisioning GW -> racks/GPU packages -> foundry wafers and kWSPM
  GPU packages -> HBM/LPDDR die -> DRAM wafers and kWSPM
  HBM GB/package -> HBM bit demand -> TAM -> SKH revenue/EBIT

Input cells default-populate from the RAG store (small-k retrieval + Haiku
value extraction, with the source location kept per cell). Users can override any
cell; formula rows are computed in the frontend for instant recalc.
"""
from __future__ import annotations

import json
import logging
import os
import re
import asyncio
import urllib.error
import urllib.request
from zipfile import BadZipFile
from datetime import date, datetime

from fastapi import APIRouter, HTTPException
from openpyxl.utils.exceptions import InvalidFileException
from pydantic import BaseModel

from ...db.connection import get_db
from ...services.rag_settings import (
    get_rerank_candidates,
    get_retrieval_settings,
    get_tab_sources,
    get_tab_prompt,
    render_prompt_template,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/model", tags=["model"])

_HAIKU_MODEL = os.getenv("OPENAI_EXTRACTION_MODEL", "gpt-5.6-luna")


def _build_periods(today: date | None = None) -> list[str]:
    current_year = (today or date.today()).year
    periods = [f"CY{str(current_year - 1)[-2:]}"]
    periods.extend(f"CY{str(year)[-2:]}E" for year in range(current_year, current_year + 5))
    return periods


PERIODS = _build_periods()
NEWS_COMPANY = "SK Hynix"
NEWS_COLUMNS = ["Company", "Topic", "News / Guidance", "Key Data", "Source"]

# ---------------------------------------------------------------------------
# Model definition. type=input rows are RAG-populated and editable;
# type=formula rows are computed in the frontend (expr uses row keys).
# defaults come from the source workbook as fallback when RAG yields nothing.
# "tabs" controls which sub-tab(s) a row appears on. Capex / Return Metrics
# are currently reserved tabs with no rows; shared anchor rows appear on the
# core HBM/DRAM/NAND tabs and edit the SAME underlying cell.
# Rows are ordered so every formula's dependencies are defined before it.
MODEL_TABS = ["hbm", "nand", "capex", "return_metrics"]
ALL_TABS = ["hbm", "nand"]
_LEGACY_REVENUE_MODEL_ROWS = [
    # ── ARETE GW ANCHOR (primary driver, per Foundry Capacity Is King Table 6) ─
    {"key": "arete_gw", "label": "Arete implied GW provisioning", "unit": "GW",
     "type": "input", "group": "Arete GW anchor (primary)", "tabs": ALL_TABS,
     "query": "Arete implied GW provisioning AI TAM average take per GW Table",
     "defaults": [7.0, 11.7, 23.6, 35.2, 41.9]},
    {"key": "arete_ai_tam", "label": "Arete AI TAM (cross-check)", "unit": "$bn",
     "type": "input", "group": "Arete GW anchor (primary)", "tabs": ALL_TABS,
     "query": "Arete AI semiconductor TAM forecast total AI revenue",
     "defaults": [136.1, 230.9, 468.3, 745.0, 939.5]},
    {"key": "avg_take_per_gw", "label": "Avg take / GW (cross-check)", "unit": "$bn/GW",
     "type": "input", "group": "Arete GW anchor (primary)", "tabs": ALL_TABS,
     "query": "average dollar take per GW deployed AI datacenter",
     "defaults": [19.4, 19.7, 19.8, 21.1, 22.4]},
    {"key": "compute_gw", "label": "Compute-derived GW (check)", "unit": "GW",
     "type": "formula", "group": "Arete GW anchor (primary)", "tabs": ALL_TABS,
     "expr": "arete_ai_tam / avg_take_per_gw"},
    {"key": "use_computed_gw", "label": "Use computed GW? (1=yes, 0=Arete)", "unit": "x",
     "type": "input", "group": "Arete GW anchor (primary)", "tabs": ALL_TABS,
     "query": None, "defaults": [0, 0, 0, 0, 0]},
    {"key": "gw_used", "label": "GW used in model", "unit": "GW",
     "type": "formula", "group": "Arete GW anchor (primary)", "tabs": ALL_TABS,
     "expr": "use_computed_gw * compute_gw + (1 - use_computed_gw) * arete_gw",
     "highlight": True},

    # ── PLATFORM MIX / HARDWARE ────────────────────────────────────────────
    {"key": "gb200_mix", "label": "GB200/B300 platform share", "unit": "x", "type": "input",
     "group": "Platform mix", "tabs": ALL_TABS,
     "query": "GB200 B300 Blackwell Blackwell Ultra share of AI accelerator datacenter deployments",
     "defaults": [1.0, 1.0, 0.7, 0.35, 0.1]},
    {"key": "other_ai_platform_mix", "label": "Other AI accelerator platform share", "unit": "x", "type": "formula",
     "group": "Platform mix", "tabs": ALL_TABS, "expr": "1 - gb200_mix"},
    {"key": "gb200_kw_per_rack", "label": "GB200/B300 kW / rack", "unit": "kW", "type": "input",
     "group": "Platform mix", "tabs": ALL_TABS,
     "query": "GB200 B300 NVL rack power kW per rack AI server",
     "defaults": [120, 120, 120, 120, 120]},
    {"key": "other_ai_kw_per_rack", "label": "Other AI platform kW / rack", "unit": "kW", "type": "input",
     "group": "Platform mix", "tabs": ALL_TABS,
     "query": "AI accelerator rack power density kW per rack general GPU TPU AMD NVIDIA custom ASIC",
     "defaults": [120, 140, 160, 180, 200]},
    {"key": "kw_per_rack", "label": "Weighted kW / rack", "unit": "kW", "type": "formula",
     "group": "Platform mix", "tabs": ALL_TABS,
     "expr": "gb200_mix * gb200_kw_per_rack + other_ai_platform_mix * other_ai_kw_per_rack"},
    {"key": "gb200_gpus_per_rack", "label": "GB200/B300 GPUs / rack", "unit": "units", "type": "input",
     "group": "Platform mix", "tabs": ALL_TABS,
     "query": "GB200 B300 NVL GPUs per rack NVL72 rack configuration",
     "defaults": [72, 72, 72, 72, 72]},
    {"key": "other_ai_gpus_per_rack", "label": "Other AI platform GPUs / rack", "unit": "units", "type": "input",
     "group": "Platform mix", "tabs": ALL_TABS,
     "query": "AI accelerator GPUs per rack general server rack TPU AMD custom ASIC",
     "defaults": [72, 72, 64, 64, 64]},
    {"key": "gpus_per_rack", "label": "Weighted accelerators / rack", "unit": "units", "type": "formula",
     "group": "Platform mix", "tabs": ALL_TABS,
     "expr": "gb200_mix * gb200_gpus_per_rack + other_ai_platform_mix * other_ai_gpus_per_rack"},
    {"key": "gpus_per_gw", "label": "Weighted accelerators / GW", "unit": "units", "type": "formula",
     "group": "Platform mix", "tabs": ALL_TABS,
     "expr": "1000000 / kw_per_rack * gpus_per_rack"},
    {"key": "total_gpus_mn", "label": "Total accelerators implied", "unit": "mn", "type": "formula",
     "group": "Platform mix", "tabs": ALL_TABS,
     "expr": "gw_used * gpus_per_gw / 1000000"},

    # ── HBM TAB ────────────────────────────────────────────────────────────
    {"key": "gb200_hbm_gb_per_gpu", "label": "GB200/B300 HBM GB / accelerator", "unit": "GB",
     "type": "input", "group": "HBM demand", "tabs": ["hbm"],
     "query": "GB200 B300 Blackwell HBM memory capacity GB per GPU accelerator",
     "defaults": [192, 192, 192, 288, 288]},
    {"key": "other_ai_hbm_gb_per_gpu", "label": "Other AI platform HBM GB / accelerator", "unit": "GB",
     "type": "input", "group": "HBM demand", "tabs": ["hbm"],
     "query": "AI accelerator HBM memory capacity GB per GPU TPU AMD custom ASIC",
     "defaults": [192, 192, 256, 288, 384]},
    {"key": "hbm_gb_per_gpu", "label": "Weighted HBM GB / accelerator", "unit": "GB",
     "type": "formula", "group": "HBM demand", "tabs": ["hbm"],
     "expr": "gb200_mix * gb200_hbm_gb_per_gpu + other_ai_platform_mix * other_ai_hbm_gb_per_gpu"},
    {"key": "hbm_demand", "label": "HBM demand", "unit": "bn Gb", "type": "formula",
     "group": "HBM demand", "tabs": ["hbm"],
     "expr": "total_gpus_mn * hbm_gb_per_gpu * 8 / 1000"},
    {"key": "hbm_asp", "label": "HBM ASP", "unit": "$/Gb", "type": "input",
     "group": "HBM revenue", "tabs": ["hbm"],
     "query": "HBM ASP price per Gb blended forecast HBM3e HBM4",
     "defaults": [1.3, 1.52, 1.73, 2.77, 2.5]},
    {"key": "hbm_tam", "label": "Industry HBM TAM", "unit": "$bn", "type": "formula",
     "group": "HBM revenue", "tabs": ["hbm"], "expr": "hbm_demand * hbm_asp"},
    {"key": "skh_hbm_share", "label": "SKH HBM share", "unit": "x", "type": "input",
     "group": "HBM revenue", "tabs": ["hbm"],
     "query": "SK Hynix HBM market share percent",
     "defaults": [0.45, 0.50, 0.52, 0.50, 0.48]},
    {"key": "skh_hbm_revenue", "label": "SKH HBM AI revenue", "unit": "$bn",
     "type": "formula", "group": "HBM revenue", "tabs": ["hbm"],
     "expr": "hbm_tam * skh_hbm_share", "highlight": True},
    {"key": "skh_hbm_ebit_margin", "label": "SKH HBM EBIT margin", "unit": "x",
     "type": "input", "group": "HBM revenue", "tabs": ["hbm"],
     "query": "SK Hynix HBM EBIT operating margin",
     "defaults": [0.52, 0.50, 0.47, 0.45, 0.43]},
    {"key": "skh_hbm_ebit", "label": "SKH HBM EBIT", "unit": "$bn", "type": "formula",
     "group": "HBM revenue", "tabs": ["hbm"],
     "expr": "skh_hbm_revenue * skh_hbm_ebit_margin", "highlight": True},
    {"key": "ubs_hbm_demand", "label": "UBS HBM demand (check)", "unit": "bn Gb",
     "type": "input", "group": "HBM bank checks", "tabs": ["hbm"],
     "query": "UBS total HBM demand bn Gb forecast",
     "defaults": [None, 17.48, 32.89, 57.97, None]},
    {"key": "ms_hbm_demand", "label": "MS HBM demand (check)", "unit": "bn Gb",
     "type": "input", "group": "HBM bank checks", "tabs": ["hbm"],
     "query": "Morgan Stanley HBM demand forecast bits",
     "defaults": [9.059, 19.516, 34.205, 56.085, None]},
    {"key": "gs_hbm_tam", "label": "GS HBM TAM (check)", "unit": "$bn", "type": "input",
     "group": "HBM bank checks", "tabs": ["hbm"],
     "query": "Goldman Sachs HBM TAM market size forecast",
     "defaults": [None, None, 56.0, 116.0, 168.0]},
    {"key": "jpm_hbm_tam", "label": "JPM HBM TAM (check)", "unit": "$bn", "type": "input",
     "group": "HBM bank checks", "tabs": ["hbm"],
     "query": "JPMorgan HBM TAM market size forecast",
     "defaults": [16.935, 33.541, 64.948, 129.092, 225.191]},
    {"key": "var_vs_jpm", "label": "Our TAM vs JPM", "unit": "$bn", "type": "formula",
     "group": "HBM bank checks", "tabs": ["hbm"], "expr": "hbm_tam - jpm_hbm_tam"},
    {"key": "var_vs_gs", "label": "Our TAM vs GS", "unit": "$bn", "type": "formula",
     "group": "HBM bank checks", "tabs": ["hbm"], "expr": "hbm_tam - gs_hbm_tam"},

    # ── DRAM TAB (non-HBM DRAM: DDR5 + DDR4 + LPDDR + other) ───────────────
    {"key": "ddr5_dram_demand", "label": "DDR5 DRAM demand", "unit": "bn Gb",
     "type": "input", "group": "Non-HBM DRAM demand", "tabs": ["dram"],
     "query": "AI server DDR5 DRAM demand bn Gb forecast",
     "defaults": [None, None, None, None, None]},
    {"key": "ddr4_dram_demand", "label": "DDR4 DRAM demand", "unit": "bn Gb",
     "type": "input", "group": "Non-HBM DRAM demand", "tabs": ["dram"],
     "query": "AI server DDR4 DRAM demand bn Gb forecast",
     "defaults": [None, None, None, None, None]},
    {"key": "lpddr_dram_demand", "label": "LPDDR DRAM demand", "unit": "bn Gb",
     "type": "input", "group": "Non-HBM DRAM demand", "tabs": ["dram"],
     "query": "AI server LPDDR DRAM demand bn Gb forecast",
     "defaults": [None, None, None, None, None]},
    {"key": "other_nonhbm_dram_demand", "label": "Other non-HBM DRAM demand", "unit": "bn Gb",
     "type": "input", "group": "Non-HBM DRAM demand", "tabs": ["dram"],
     "query": "AI server other non-HBM DRAM demand bn Gb forecast",
     "defaults": [0, 0, 0, 0, 0]},
    {"key": "nonhbm_dram_demand", "label": "Total non-HBM DRAM demand", "unit": "bn Gb",
     "type": "formula", "group": "Non-HBM DRAM demand", "tabs": ["dram"],
     "expr": "ddr5_dram_demand + ddr4_dram_demand + lpddr_dram_demand + other_nonhbm_dram_demand",
     "highlight": True},
    {"key": "dram_asp", "label": "Blended non-HBM DRAM ASP", "unit": "$/Gb",
     "type": "input", "group": "DRAM revenue", "tabs": ["dram"],
     "query": "non-HBM DRAM blended ASP price per Gb DDR5 DDR4 LPDDR forecast",
     "defaults": [0.40, 0.40, 1.61, 2.11, 1.80]},
    {"key": "skh_dram_share", "label": "SKH non-HBM DRAM share", "unit": "x",
     "type": "input", "group": "DRAM revenue", "tabs": ["dram"],
     "query": "SK Hynix non-HBM DRAM market share DDR5 DDR4 LPDDR",
     "defaults": [0.25, 0.28, 0.30, 0.30, 0.30]},
    {"key": "skh_dram_revenue", "label": "SKH non-HBM DRAM AI revenue", "unit": "$bn",
     "type": "formula", "group": "DRAM revenue", "tabs": ["dram"],
     "expr": "nonhbm_dram_demand * dram_asp * skh_dram_share", "highlight": True},

    # ── NAND TAB (enterprise SSD) ───────────────────────────────────────────
    {"key": "essd_tb_per_gpu", "label": "eSSD TB / accelerator", "unit": "TB", "type": "input",
     "group": "NAND / eSSD demand", "tabs": ["nand"],
     "query": "enterprise SSD storage TB per AI accelerator server GPU TPU custom ASIC",
     "defaults": [8, 12, 16, 24, 32]},
    {"key": "nand_demand", "label": "eSSD / NAND demand", "unit": "bn GB",
     "type": "formula", "group": "NAND / eSSD demand", "tabs": ["nand"],
     "expr": "total_gpus_mn * essd_tb_per_gpu * 1024 / 1000"},
    {"key": "nand_asp", "label": "eSSD / NAND ASP", "unit": "$/GB", "type": "input",
     "group": "NAND revenue", "tabs": ["nand"],
     "query": "enterprise SSD NAND ASP price per GB forecast",
     "defaults": [0.06, 0.07, 0.21, 0.27, 0.25]},
    {"key": "skh_nand_share", "label": "SKH eSSD / NAND share", "unit": "x",
     "type": "input", "group": "NAND revenue", "tabs": ["nand"],
     "query": "SK Hynix Solidigm enterprise SSD NAND market share",
     "defaults": [0.10, 0.12, 0.15, 0.15, 0.15]},
    {"key": "skh_nand_revenue", "label": "SKH eSSD / NAND AI revenue", "unit": "$bn",
     "type": "formula", "group": "NAND revenue", "tabs": ["nand"],
     "expr": "nand_demand * nand_asp * skh_nand_share", "highlight": True},

    # ── TOTAL (all tabs) ────────────────────────────────────────────────────
    {"key": "skh_total_ai_revenue", "label": "Total SKH AI memory revenue", "unit": "$bn",
     "type": "formula", "group": "Total", "tabs": ALL_TABS,
     "expr": "skh_hbm_revenue + skh_dram_revenue + skh_nand_revenue",
     "highlight": True},
]

# Active model: GB300 wafer-capacity bridge first, revenue as downstream output.
# The row keys `gw_used`, `total_gpus_mn`, `hbm_gb_per_gpu`, `hbm_demand`,
# and revenue outputs stay stable for saved user overrides and frontend logic.
MODEL_ROWS = [
    # ---- GB300 DC provisioning ------------------------------------------------
    {"key": "gw_used", "label": "Incremental DC provisioning used", "unit": "GW",
     "type": "input", "group": "GB300 DC provisioning", "tabs": ALL_TABS,
     "query": "incremental AI datacenter DC provisioning GW forecast GB300 B300",
     "defaults": [7.0, 11.7, 23.6, 31.0, 41.9], "highlight": True},
    {"key": "compute_load_share", "label": "Compute load share", "unit": "x",
     "type": "input", "group": "GB300 DC provisioning", "tabs": ALL_TABS,
     "query": None, "defaults": [0.80, 0.80, 0.80, 0.80, 0.80]},
    {"key": "gb300_rack_power_kw", "label": "GB300 rack power", "unit": "kW",
     "type": "input", "group": "GB300 DC provisioning", "tabs": ALL_TABS,
     "query": None, "defaults": [130, 130, 130, 130, 130]},
    {"key": "gpu_packages_per_rack", "label": "GPU packages / rack", "unit": "units",
     "type": "input", "group": "GB300 DC provisioning", "tabs": ALL_TABS,
     "query": None, "defaults": [72, 72, 72, 72, 72]},
    {"key": "compute_power_kw_per_gw", "label": "Compute power / GW", "unit": "kW",
     "type": "formula", "group": "GB300 DC provisioning", "tabs": ALL_TABS,
     "expr": "1000000 * compute_load_share"},
    {"key": "racks_per_gw", "label": "Racks / GW", "unit": "units",
     "type": "formula", "group": "GB300 DC provisioning", "tabs": ALL_TABS,
     "expr": "compute_power_kw_per_gw / gb300_rack_power_kw"},
    {"key": "gpu_packages_per_gw", "label": "GPU packages / GW", "unit": "units",
     "type": "formula", "group": "GB300 DC provisioning", "tabs": ALL_TABS,
     "expr": "racks_per_gw * gpu_packages_per_rack"},
    {"key": "total_gpus_mn", "label": "Total GPU packages implied", "unit": "mn",
     "type": "formula", "group": "GB300 DC provisioning", "tabs": ALL_TABS,
     "expr": "gw_used * gpu_packages_per_gw / 1000000"},

    # ---- Foundry wafer bridge -------------------------------------------------
    {"key": "cowos_good_packages_per_wafer", "label": "Good GPU packages / CoWoS wafer", "unit": "units",
     "type": "input", "group": "Foundry capacity bridge", "tabs": ["hbm", "capex"],
     "query": None, "defaults": [15, 15, 15, 15, 15]},
    {"key": "cowos_die_per_wafer", "label": "Input die / CoWoS wafer", "unit": "units",
     "type": "input", "group": "Foundry capacity bridge", "tabs": ["hbm", "capex"],
     "query": None, "defaults": [32, 32, 32, 32, 32]},
    {"key": "gpu_die_per_frontend_wafer", "label": "GPU die / front-end wafer", "unit": "units",
     "type": "input", "group": "Foundry capacity bridge", "tabs": ["hbm", "capex"],
     "query": None, "defaults": [60, 60, 60, 60, 60]},
    {"key": "gpu_frontend_yield", "label": "GPU front-end yield", "unit": "x",
     "type": "input", "group": "Foundry capacity bridge", "tabs": ["hbm", "capex"],
     "query": None, "defaults": [0.72, 0.72, 0.72, 0.72, 0.72]},
    {"key": "assembly_test_yield", "label": "Assembly and test yield", "unit": "x",
     "type": "input", "group": "Foundry capacity bridge", "tabs": ["hbm", "capex"],
     "query": None, "defaults": [0.68, 0.68, 0.68, 0.68, 0.68]},
    {"key": "cpu_wafers_per_gw", "label": "CPU front-end wafers / GW", "unit": "wafers/yr",
     "type": "input", "group": "Foundry capacity bridge", "tabs": ["hbm", "capex"],
     "query": None, "defaults": [4269, 4269, 4269, 4269, 4269]},
    {"key": "network_wafers_per_gw", "label": "Networking wafers / GW", "unit": "wafers/yr",
     "type": "input", "group": "Foundry capacity bridge", "tabs": ["hbm", "capex"],
     "query": None, "defaults": [7986, 7986, 7986, 7986, 7986]},
    {"key": "other_foundry_wafers_per_gw", "label": "Other foundry wafers / GW", "unit": "wafers/yr",
     "type": "input", "group": "Foundry capacity bridge", "tabs": ["hbm", "capex"],
     "query": None, "defaults": [7032, 7032, 7032, 7032, 7032]},
    {"key": "foundry_adjustment_wafers_per_gw", "label": "Other foundry adjustment / GW", "unit": "wafers/yr",
     "type": "input", "group": "Foundry capacity bridge", "tabs": ["hbm", "capex"],
     "query": None, "defaults": [2446, 2446, 2446, 2446, 2446]},
    {"key": "cowos_wafers_per_gw", "label": "CoWoS wafers / GW", "unit": "wafers/yr",
     "type": "formula", "group": "Foundry capacity bridge", "tabs": ["hbm", "capex"],
     "expr": "gpu_packages_per_gw / cowos_good_packages_per_wafer"},
    {"key": "gpu_input_die_per_gw", "label": "Input GPU die / GW", "unit": "units",
     "type": "formula", "group": "Foundry capacity bridge", "tabs": ["hbm", "capex"],
     "expr": "cowos_wafers_per_gw * cowos_die_per_wafer"},
    {"key": "gpu_frontend_wafers_per_gw", "label": "GPU front-end wafers / GW", "unit": "wafers/yr",
     "type": "formula", "group": "Foundry capacity bridge", "tabs": ["hbm", "capex"],
     "expr": "gpu_input_die_per_gw / gpu_die_per_frontend_wafer / gpu_frontend_yield / assembly_test_yield"},
    {"key": "total_foundry_wafers_per_gw", "label": "Total TSMC front-end wafers / GW", "unit": "wafers/yr",
     "type": "formula", "group": "Foundry capacity bridge", "tabs": ["hbm", "capex"],
     "expr": "gpu_frontend_wafers_per_gw + cpu_wafers_per_gw + network_wafers_per_gw + other_foundry_wafers_per_gw + foundry_adjustment_wafers_per_gw"},
    {"key": "foundry_kwspm_per_gw", "label": "Foundry capacity / GW", "unit": "kWSPM",
     "type": "formula", "group": "Foundry capacity bridge", "tabs": ["hbm", "capex"],
     "expr": "total_foundry_wafers_per_gw / 12 / 1000"},
    {"key": "foundry_kwspm_required", "label": "Incremental foundry capacity required", "unit": "kWSPM",
     "type": "formula", "group": "Foundry capacity bridge", "tabs": ["hbm", "capex"],
     "expr": "foundry_kwspm_per_gw * gw_used", "highlight": True},
    {"key": "tsmc_leading_edge_capacity_kwspm", "label": "TSMC leading-edge capacity", "unit": "kWSPM",
     "type": "input", "group": "Foundry capacity bridge", "tabs": ["hbm", "capex"],
     "query": "TSMC leading edge foundry capacity kWSPM advanced node 2027",
     "defaults": [300, 335, 370, 400, 440]},
    {"key": "foundry_capacity_share", "label": "Foundry requirement / TSMC capacity", "unit": "x",
     "type": "formula", "group": "Foundry capacity bridge", "tabs": ["hbm", "capex"],
     "expr": "foundry_kwspm_required / tsmc_leading_edge_capacity_kwspm", "highlight": True},

    # ---- Memory wafer bridge --------------------------------------------------
    {"key": "hbm_die_per_gpu_package", "label": "HBM die / GPU package", "unit": "units",
     "type": "input", "group": "Memory capacity bridge", "tabs": ["hbm", "dram"],
     "query": None, "defaults": [96, 96, 96, 96, 96]},
    {"key": "hbm_die_per_wafer", "label": "HBM die / wafer", "unit": "units",
     "type": "input", "group": "Memory capacity bridge", "tabs": ["hbm", "dram"],
     "query": None, "defaults": [600, 600, 600, 600, 600]},
    {"key": "hbm_wafer_yield", "label": "HBM wafer yield", "unit": "x",
     "type": "input", "group": "Memory capacity bridge", "tabs": ["hbm", "dram"],
     "query": None, "defaults": [0.65, 0.65, 0.65, 0.65, 0.65]},
    {"key": "lpddr_die_per_rack", "label": "LPDDR die / rack", "unit": "units",
     "type": "input", "group": "Memory capacity bridge", "tabs": ["hbm", "dram"],
     "query": None, "defaults": [8640, 8640, 8640, 8640, 8640]},
    {"key": "lpddr_die_per_wafer", "label": "LPDDR die / wafer", "unit": "units",
     "type": "input", "group": "Memory capacity bridge", "tabs": ["hbm", "dram"],
     "query": None, "defaults": [1400, 1400, 1400, 1400, 1400]},
    {"key": "lpddr_wafer_yield", "label": "LPDDR wafer yield", "unit": "x",
     "type": "input", "group": "Memory capacity bridge", "tabs": ["hbm", "dram"],
     "query": None, "defaults": [0.90, 0.90, 0.90, 0.90, 0.90]},
    {"key": "hbm_die_per_gw", "label": "HBM die / GW", "unit": "units",
     "type": "formula", "group": "Memory capacity bridge", "tabs": ["hbm", "dram"],
     "expr": "gpu_packages_per_gw * hbm_die_per_gpu_package"},
    {"key": "hbm_frontend_wafers_per_gw", "label": "HBM front-end wafers / GW", "unit": "wafers/yr",
     "type": "formula", "group": "Memory capacity bridge", "tabs": ["hbm", "dram"],
     "expr": "hbm_die_per_gw / hbm_die_per_wafer / hbm_wafer_yield"},
    {"key": "lpddr_die_per_gw", "label": "LPDDR die / GW", "unit": "units",
     "type": "formula", "group": "Memory capacity bridge", "tabs": ["hbm", "dram"],
     "expr": "racks_per_gw * lpddr_die_per_rack"},
    {"key": "lpddr_frontend_wafers_per_gw", "label": "LPDDR front-end wafers / GW", "unit": "wafers/yr",
     "type": "formula", "group": "Memory capacity bridge", "tabs": ["hbm", "dram"],
     "expr": "lpddr_die_per_gw / lpddr_die_per_wafer / lpddr_wafer_yield"},
    {"key": "dram_wafers_per_gw", "label": "Total DRAM wafers / GW", "unit": "wafers/yr",
     "type": "formula", "group": "Memory capacity bridge", "tabs": ["hbm", "dram"],
     "expr": "hbm_frontend_wafers_per_gw + lpddr_frontend_wafers_per_gw"},
    {"key": "dram_kwspm_per_gw", "label": "DRAM capacity / GW", "unit": "kWSPM",
     "type": "formula", "group": "Memory capacity bridge", "tabs": ["hbm", "dram"],
     "expr": "dram_wafers_per_gw / 12 / 1000"},
    {"key": "dram_kwspm_required", "label": "Incremental DRAM capacity required", "unit": "kWSPM",
     "type": "formula", "group": "Memory capacity bridge", "tabs": ["hbm", "dram"],
     "expr": "dram_kwspm_per_gw * gw_used", "highlight": True},

    # ---- HBM revenue cross-check ---------------------------------------------
    {"key": "hbm_gb_per_gpu", "label": "HBM GB / GPU package", "unit": "GB",
     "type": "input", "group": "HBM revenue cross-check", "tabs": ["hbm"],
     "query": "GB300 B300 Blackwell HBM memory capacity GB per GPU package accelerator",
     "defaults": [192, 192, 192, 288, 288]},
    {"key": "hbm_demand", "label": "HBM demand", "unit": "bn Gb", "type": "formula",
     "group": "HBM revenue cross-check", "tabs": ["hbm"],
     "expr": "total_gpus_mn * hbm_gb_per_gpu * 8 / 1000"},
    {"key": "hbm_asp", "label": "HBM ASP", "unit": "$/Gb", "type": "input",
     "group": "HBM revenue cross-check", "tabs": ["hbm"],
     "query": "HBM ASP price per Gb blended forecast HBM3e HBM4",
     "defaults": [1.3, 1.52, 1.73, 2.77, 2.5]},
    {"key": "hbm_tam", "label": "Industry HBM TAM", "unit": "$bn", "type": "formula",
     "group": "HBM revenue cross-check", "tabs": ["hbm"], "expr": "hbm_demand * hbm_asp"},
    {"key": "skh_hbm_share", "label": "SKH HBM share", "unit": "x", "type": "input",
     "group": "HBM revenue cross-check", "tabs": ["hbm"],
     "query": "SK Hynix HBM market share percent",
     "defaults": [0.45, 0.50, 0.52, 0.50, 0.48]},
    {"key": "skh_hbm_revenue", "label": "SKH HBM AI revenue", "unit": "$bn",
     "type": "formula", "group": "HBM revenue cross-check", "tabs": ["hbm"],
     "expr": "hbm_tam * skh_hbm_share", "highlight": True},
    {"key": "skh_hbm_ebit_margin", "label": "SKH HBM EBIT margin", "unit": "x",
     "type": "input", "group": "HBM revenue cross-check", "tabs": ["hbm"],
     "query": "SK Hynix HBM EBIT operating margin",
     "defaults": [0.52, 0.50, 0.47, 0.45, 0.43]},
    {"key": "skh_hbm_ebit", "label": "SKH HBM EBIT", "unit": "$bn", "type": "formula",
     "group": "HBM revenue cross-check", "tabs": ["hbm"],
     "expr": "skh_hbm_revenue * skh_hbm_ebit_margin", "highlight": True},
    {"key": "ubs_hbm_demand", "label": "UBS HBM demand (check)", "unit": "bn Gb",
     "type": "input", "group": "HBM bank checks", "tabs": ["hbm"],
     "query": "UBS total HBM demand bn Gb forecast",
     "defaults": [None, 17.48, 32.89, 57.97, None]},
    {"key": "ms_hbm_demand", "label": "MS HBM demand (check)", "unit": "bn Gb",
     "type": "input", "group": "HBM bank checks", "tabs": ["hbm"],
     "query": "Morgan Stanley HBM demand forecast bits",
     "defaults": [9.059, 19.516, 34.205, 56.085, None]},
    {"key": "gs_hbm_tam", "label": "GS HBM TAM (check)", "unit": "$bn", "type": "input",
     "group": "HBM bank checks", "tabs": ["hbm"],
     "query": "Goldman Sachs HBM TAM market size forecast",
     "defaults": [None, None, 56.0, 116.0, 168.0]},
    {"key": "jpm_hbm_tam", "label": "JPM HBM TAM (check)", "unit": "$bn", "type": "input",
     "group": "HBM bank checks", "tabs": ["hbm"],
     "query": "JPMorgan HBM TAM market size forecast",
     "defaults": [16.935, 33.541, 64.948, 129.092, 225.191]},
    {"key": "var_vs_jpm", "label": "Our TAM vs JPM", "unit": "$bn", "type": "formula",
     "group": "HBM bank checks", "tabs": ["hbm"], "expr": "hbm_tam - jpm_hbm_tam"},
    {"key": "var_vs_gs", "label": "Our TAM vs GS", "unit": "$bn", "type": "formula",
     "group": "HBM bank checks", "tabs": ["hbm"], "expr": "hbm_tam - gs_hbm_tam"},

    # ---- DRAM tab: non-HBM revenue layer -------------------------------------
    {"key": "ddr5_dram_demand", "label": "DDR5 DRAM demand", "unit": "bn Gb",
     "type": "input", "group": "Non-HBM DRAM demand", "tabs": ["dram"],
     "query": "AI server DDR5 DRAM demand bn Gb forecast",
     "defaults": [None, None, None, None, None]},
    {"key": "ddr4_dram_demand", "label": "DDR4 DRAM demand", "unit": "bn Gb",
     "type": "input", "group": "Non-HBM DRAM demand", "tabs": ["dram"],
     "query": "AI server DDR4 DRAM demand bn Gb forecast",
     "defaults": [None, None, None, None, None]},
    {"key": "lpddr_dram_demand", "label": "LPDDR DRAM demand", "unit": "bn Gb",
     "type": "input", "group": "Non-HBM DRAM demand", "tabs": ["dram"],
     "query": "AI server LPDDR DRAM demand bn Gb forecast",
     "defaults": [None, None, None, None, None]},
    {"key": "other_nonhbm_dram_demand", "label": "Other non-HBM DRAM demand", "unit": "bn Gb",
     "type": "input", "group": "Non-HBM DRAM demand", "tabs": ["dram"],
     "query": "AI server other non-HBM DRAM demand bn Gb forecast",
     "defaults": [0, 0, 0, 0, 0]},
    {"key": "nonhbm_dram_demand", "label": "Total non-HBM DRAM demand", "unit": "bn Gb",
     "type": "formula", "group": "Non-HBM DRAM demand", "tabs": ["dram"],
     "expr": "ddr5_dram_demand + ddr4_dram_demand + lpddr_dram_demand + other_nonhbm_dram_demand",
     "highlight": True},
    {"key": "dram_asp", "label": "Blended non-HBM DRAM ASP", "unit": "$/Gb",
     "type": "input", "group": "DRAM revenue", "tabs": ["dram"],
     "query": "non-HBM DRAM blended ASP price per Gb DDR5 DDR4 LPDDR forecast",
     "defaults": [0.40, 0.40, 1.61, 2.11, 1.80]},
    {"key": "skh_dram_share", "label": "SKH non-HBM DRAM share", "unit": "x",
     "type": "input", "group": "DRAM revenue", "tabs": ["dram"],
     "query": "SK Hynix non-HBM DRAM market share DDR5 DDR4 LPDDR",
     "defaults": [0.25, 0.28, 0.30, 0.30, 0.30]},
    {"key": "skh_dram_revenue", "label": "SKH non-HBM DRAM AI revenue", "unit": "$bn",
     "type": "formula", "group": "DRAM revenue", "tabs": ["dram"],
     "expr": "nonhbm_dram_demand * dram_asp * skh_dram_share", "highlight": True},

    # ---- NAND tab -------------------------------------------------------------
    {"key": "essd_tb_per_gpu", "label": "eSSD TB / GPU package", "unit": "TB", "type": "input",
     "group": "NAND / eSSD demand", "tabs": ["nand"],
     "query": "enterprise SSD storage TB per AI accelerator server GPU TPU custom ASIC",
     "defaults": [8, 12, 16, 24, 32]},
    {"key": "nand_demand", "label": "eSSD / NAND demand", "unit": "bn GB",
     "type": "formula", "group": "NAND / eSSD demand", "tabs": ["nand"],
     "expr": "total_gpus_mn * essd_tb_per_gpu * 1024 / 1000"},
    {"key": "nand_asp", "label": "eSSD / NAND ASP", "unit": "$/GB", "type": "input",
     "group": "NAND revenue", "tabs": ["nand"],
     "query": "enterprise SSD NAND ASP price per GB forecast",
     "defaults": [0.06, 0.07, 0.21, 0.27, 0.25]},
    {"key": "skh_nand_share", "label": "SKH eSSD / NAND share", "unit": "x",
     "type": "input", "group": "NAND revenue", "tabs": ["nand"],
     "query": "SK Hynix Solidigm enterprise SSD NAND market share",
     "defaults": [0.10, 0.12, 0.15, 0.15, 0.15]},
    {"key": "skh_nand_revenue", "label": "SKH eSSD / NAND AI revenue", "unit": "$bn",
     "type": "formula", "group": "NAND revenue", "tabs": ["nand"],
     "expr": "nand_demand * nand_asp * skh_nand_share", "highlight": True},

    # ---- Total ---------------------------------------------------------------
    {"key": "skh_total_ai_revenue", "label": "Total SKH AI memory revenue", "unit": "$bn",
     "type": "formula", "group": "Total", "tabs": ALL_TABS,
     "expr": "skh_hbm_revenue + skh_dram_revenue + skh_nand_revenue",
     "highlight": True},
]

RACK_MODEL = "hbm_rack_v2"
STATIC_MODEL = RACK_MODEL
RACK_VENDORS = ("NVDA", "AMD", "ASIC")
RACK_SPEC_PERIOD = "__spec__"

RACK_FIELDS = [
    {"key": "rack_power_kw", "label": "Rack power", "unit": "kW", "type": "input", "group": "Rack specification", "period_mode": "single"},
    {"key": "gpu_packages_per_rack", "label": "GPU packages per rack", "unit": "packages/rack", "type": "input", "group": "Rack specification", "period_mode": "single"},
    {"key": "physical_gpu_packages_per_rack", "label": "Original GPU packages per rack", "unit": "packages/rack", "type": "input", "group": "Rack specification", "period_mode": "single"},
    {"key": "hbm_gb_per_gpu", "label": "HBM capacity per GPU package", "unit": "GB/package", "type": "input", "group": "Rack specification", "period_mode": "single"},
    {"key": "gddr_gpu_packages_per_rack", "label": "GDDR accelerator packages per rack", "unit": "packages/rack", "type": "input", "group": "Rack specification", "period_mode": "single", "default": 0},
    {"key": "gddr_content_gb_per_package", "label": "GDDR capacity per accelerator package", "unit": "GB/package", "type": "input", "group": "Rack specification", "period_mode": "single", "default": 0},
    {"key": "racks_per_gw", "label": "Number of racks per 1 GW", "unit": "racks", "type": "formula", "group": "Deployment bridge", "expr": "compute_power_kw_per_gw / rack_power_kw", "highlight": True, "display": False},
    {"key": "gpu_packages_per_gw", "label": "Total GPU packages in 1 GW", "unit": "packages/yr", "type": "formula", "group": "Deployment bridge", "expr": "racks_per_gw * gpu_packages_per_rack", "highlight": True, "display": False},
    {"key": "physical_gpu_packages_per_gw", "label": "Original GPU packages in 1 GW", "unit": "packages/yr", "type": "formula", "group": "Deployment bridge", "expr": "racks_per_gw * physical_gpu_packages_per_rack", "display": False},
    {"key": "total_gpu_packages_mn", "label": "Total GPU packages", "unit": "mn", "type": "formula", "group": "Deployment bridge", "expr": "gw_used * gpu_packages_per_gw / 1000000", "highlight": True, "display": False},

    {"key": "cowos_good_packages_per_wafer", "label": "GPU packages per CoWoS wafer", "unit": "packages/wafer", "type": "input", "group": "CoWoS specification", "period_mode": "single"},
    {"key": "physical_gpu_packages_per_cowos_wafer", "label": "Original GPU packages per CoWoS wafer", "unit": "packages/wafer", "type": "input", "group": "CoWoS specification", "period_mode": "single"},
    {"key": "gpu_die_per_package", "label": "GPU dies per package", "unit": "dies/package", "type": "input", "group": "CoWoS specification", "period_mode": "single"},
    {"key": "cowos_die_per_wafer", "label": "GPU dies per CoWoS wafer", "unit": "dies/wafer", "type": "formula", "group": "CoWoS specification", "expr": "gpu_die_per_package * physical_gpu_packages_per_cowos_wafer", "period_mode": "single"},
    {"key": "cowos_wafers_per_gw", "label": "CoWoS wafers per 1 GW", "unit": "wafers/yr", "type": "formula", "group": "Required CoWoS - derived", "expr": "gpu_packages_per_gw / cowos_good_packages_per_wafer", "highlight": True, "display": False},
    {"key": "gpu_input_die_per_gw", "label": "Inserted GPU dies per 1 GW", "unit": "dies/yr", "type": "formula", "group": "Required CoWoS - derived", "expr": "cowos_wafers_per_gw * cowos_die_per_wafer", "display": False},
    {"key": "gpu_die_per_frontend_wafer", "label": "GPU dies per front-end wafer", "unit": "dies/wafer", "type": "input", "group": "Required CoWoS - derived", "period_mode": "single"},
    {"key": "gpu_frontend_yield", "label": "GPU front-end yield", "unit": "%", "type": "input", "group": "Required CoWoS - derived", "period_mode": "single"},
    {"key": "assembly_test_yield", "label": "Assembly and test yield", "unit": "%", "type": "input", "group": "Required CoWoS - derived", "period_mode": "single"},
    {"key": "gpu_frontend_wafers_per_gw", "label": "GPU front-end wafers per 1 GW", "unit": "wafers/yr", "type": "formula", "group": "Required CoWoS - derived", "expr": "gpu_input_die_per_gw / (gpu_die_per_frontend_wafer * (gpu_frontend_yield > 1 ? gpu_frontend_yield / 100 : gpu_frontend_yield) * (assembly_test_yield > 1 ? assembly_test_yield / 100 : assembly_test_yield))", "highlight": True, "display": False},
    {"key": "cpu_wafers_per_gw", "label": "CPU front-end wafers per 1 GW", "unit": "wafers/yr", "type": "input", "group": "Required CoWoS - derived", "period_mode": "single"},
    {"key": "network_wafers_per_gw", "label": "Networking front-end wafers per 1 GW", "unit": "wafers/yr", "type": "input", "group": "Required CoWoS - derived", "period_mode": "single"},
    {"key": "other_foundry_wafers_per_gw", "label": "Other front-end wafers per 1 GW", "unit": "wafers/yr", "type": "input", "group": "Required CoWoS - derived", "period_mode": "single"},
    {"key": "foundry_adjustment_wafers_per_gw", "label": "Additional foundry wafers per 1 GW", "unit": "wafers/yr", "type": "input", "group": "Required CoWoS - derived", "period_mode": "single"},
    {"key": "total_foundry_wafers_per_gw", "label": "Total TSMC front-end wafers per 1 GW", "unit": "wafers/yr", "type": "formula", "group": "Required CoWoS - derived", "expr": "gpu_frontend_wafers_per_gw + cpu_wafers_per_gw + network_wafers_per_gw + other_foundry_wafers_per_gw + foundry_adjustment_wafers_per_gw", "highlight": True, "display": False},
    {"key": "foundry_kwspm_required", "label": "TSMC front-end capacity required", "unit": "kWSPM", "type": "formula", "group": "Required CoWoS - derived", "expr": "total_foundry_wafers_per_gw * gw_used / 12 / 1000", "highlight": True, "display": False},

    {"key": "hbm_stacks_per_gpu_package", "label": "HBM stacks per GPU package", "unit": "stacks/package", "type": "input", "group": "HBM specification", "period_mode": "single"},
    {"key": "hbm_die_per_stack", "label": "HBM stack height", "unit": "dies/stack", "type": "input", "group": "HBM specification", "period_mode": "single"},
    {"key": "hbm_die_per_gpu_package", "label": "HBM die per GPU package", "unit": "dies/package", "type": "formula", "group": "HBM specification", "expr": "hbm_stacks_per_gpu_package * hbm_die_per_stack", "period_mode": "single"},
    {"key": "hbm_gb_per_die", "label": "GB per HBM die", "unit": "GB/die", "type": "formula", "group": "HBM specification", "expr": "hbm_gb_per_gpu / hbm_die_per_gpu_package", "period_mode": "single"},
    {"key": "hbm_content_gb_per_rack", "label": "HBM content per rack", "unit": "GB/rack", "type": "formula", "group": "HBM specification", "expr": "hbm_gb_per_gpu * gpu_packages_per_rack", "period_mode": "single"},
    {"key": "hbm_die_per_rack", "label": "HBM die per rack", "unit": "dies/rack", "type": "formula", "group": "HBM specification", "expr": "hbm_die_per_gpu_package * gpu_packages_per_rack", "period_mode": "single"},
    {"key": "hbm_die_per_gw", "label": "Total HBM die per 1 GW", "unit": "dies/yr", "type": "formula", "group": "HBM / DRAM Demand", "expr": "hbm_die_per_rack * racks_per_gw", "highlight": True, "display": False},
    {"key": "hbm_die_per_wafer", "label": "HBM die per wafer", "unit": "dies/wafer", "type": "input", "group": "HBM / DRAM Demand", "period_mode": "single"},
    {"key": "hbm_wafer_yield", "label": "HBM wafer yield", "unit": "%", "type": "input", "group": "HBM / DRAM Demand", "period_mode": "single"},
    {"key": "hbm_good_die_per_wafer", "label": "Good HBM die per wafer", "unit": "dies/wafer", "type": "formula", "group": "HBM / DRAM Demand", "expr": "hbm_die_per_wafer * (hbm_wafer_yield > 1 ? hbm_wafer_yield / 100 : hbm_wafer_yield)", "period_mode": "single"},
    {"key": "hbm_frontend_wafers_per_gw", "label": "HBM front-end wafers per 1 GW", "unit": "wafers/yr", "type": "formula", "group": "HBM / DRAM Demand", "expr": "hbm_die_per_gw / (hbm_die_per_wafer * (hbm_wafer_yield > 1 ? hbm_wafer_yield / 100 : hbm_wafer_yield))", "highlight": True, "display": False},

    {"key": "cpu_packages_per_rack", "label": "CPU packages per rack", "unit": "packages/rack", "type": "input", "group": "DDR5 / CPU memory specification", "period_mode": "single"},
    {"key": "lpddr_die_per_cpu_package", "label": "DDR5 2GB die per CPU package", "unit": "dies/package", "type": "input", "group": "DDR5 / CPU memory specification", "period_mode": "single"},
    {"key": "lpddr_die_per_rack", "label": "DDR5 2GB die per rack", "unit": "dies/rack", "type": "formula", "group": "DDR5 / CPU memory specification", "expr": "cpu_packages_per_rack * lpddr_die_per_cpu_package", "period_mode": "single"},
    {"key": "lpddr_content_gb_per_rack", "label": "DDR5 content per rack", "unit": "GB/rack", "type": "formula", "group": "DDR5 / CPU memory specification", "expr": "lpddr_die_per_rack * 2", "period_mode": "single"},
    {"key": "lpddr_die_per_gw", "label": "Total DDR5 die per 1 GW", "unit": "dies/yr", "type": "formula", "group": "HBM / DRAM Demand", "expr": "lpddr_die_per_rack * racks_per_gw", "highlight": True, "display": False},
    {"key": "lpddr_die_per_wafer", "label": "DDR5 die per wafer", "unit": "dies/wafer", "type": "input", "group": "HBM / DRAM Demand", "period_mode": "single"},
    {"key": "lpddr_wafer_yield", "label": "DDR5 wafer yield", "unit": "%", "type": "input", "group": "HBM / DRAM Demand", "period_mode": "single"},
    {"key": "lpddr_frontend_wafers_per_gw", "label": "DDR5 front-end wafers per 1 GW", "unit": "wafers/yr", "type": "formula", "group": "HBM / DRAM Demand", "expr": "lpddr_die_per_gw / (lpddr_die_per_wafer * (lpddr_wafer_yield > 1 ? lpddr_wafer_yield / 100 : lpddr_wafer_yield))", "highlight": True, "display": False},
    {"key": "cpu_memory_wafers_per_gw", "label": "DDR5 front-end wafers per 1 GW", "unit": "wafers/yr", "type": "formula", "group": "HBM / DRAM Demand", "expr": "lpddr_frontend_wafers_per_gw", "display": False},
    {"key": "dram_wafers_per_gw", "label": "Total DRAM memory wafers (HBM + DDR5) per 1 GW", "unit": "wafers/yr", "type": "formula", "group": "HBM / DRAM Demand", "expr": "hbm_frontend_wafers_per_gw + cpu_memory_wafers_per_gw", "highlight": True, "display": False},
    {"key": "hbm_demand", "label": "HBM bit demand", "unit": "bn Gb", "type": "formula", "group": "HBM / DRAM Demand", "expr": "total_gpu_packages_mn * hbm_gb_per_gpu * 8 / 1000", "highlight": True, "display": False},
    {"key": "system_dram_demand", "label": "DDR5 / CPU memory demand", "unit": "bn Gb", "type": "formula", "group": "HBM / DRAM Demand", "expr": "gw_used * racks_per_gw * lpddr_content_gb_per_rack * 8 / 1000000000", "display": False},
    {"key": "gddr_dram_demand", "label": "GDDR DRAM demand", "unit": "bn Gb", "type": "formula", "group": "HBM / DRAM Demand", "expr": "gw_used * racks_per_gw * (gddr_rack_attach_share > 1 ? gddr_rack_attach_share / 100 : gddr_rack_attach_share) * gddr_gpu_packages_per_rack * gddr_content_gb_per_package * 8 / 1000000000", "display": False},
    {"key": "data_center_dram_demand", "label": "Data-center DRAM demand", "unit": "bn Gb", "type": "formula", "group": "HBM / DRAM Demand", "expr": "hbm_demand + system_dram_demand", "display": False},
    {"key": "total_server_dram_demand", "label": "Total server DRAM demand", "unit": "bn Gb", "type": "formula", "group": "HBM / DRAM Demand", "expr": "data_center_dram_demand + rdimm_dram_demand + gp_server_dram_demand + gddr_dram_demand", "highlight": True, "display": False},
    {"key": "total_dram_demand", "label": "Total DRAM demand", "unit": "bn Gb", "type": "formula", "group": "HBM / DRAM Demand", "expr": "total_server_dram_demand / (server_dram_share_total_dram_market > 1 ? server_dram_share_total_dram_market / 100 : server_dram_share_total_dram_market)", "highlight": True, "display": False},
    {"key": "implied_pc_mobile_dram_demand", "label": "Implied PC, mobile and other DRAM demand", "unit": "bn Gb", "type": "formula", "group": "HBM / DRAM Demand", "expr": "total_dram_demand - total_server_dram_demand", "display": False},
    {"key": "total_dram_demand_yoy", "label": "Total DRAM demand YoY", "unit": "%", "type": "formula", "group": "HBM / DRAM Demand", "expr": "total_dram_demand / prior_year_total_dram_demand - 1", "display": False},
    {"key": "hbm_share_total_dram_demand", "label": "HBM / total DRAM demand", "unit": "%", "type": "formula", "group": "HBM / DRAM Demand", "expr": "hbm_demand / total_dram_demand", "highlight": True, "display": False},
    {"key": "ex_hbm_dram_demand", "label": "Ex-HBM DRAM demand", "unit": "bn Gb", "type": "formula", "group": "HBM / DRAM Demand", "expr": "total_dram_demand - hbm_demand", "display": False},
    {"key": "hbm_core_annual_wafer_k", "label": "HBM core annual wafers", "unit": "k wafers/yr", "type": "formula", "group": "HBM / DRAM Demand", "expr": "hbm_demand * 125000 / hbm_gb_per_die / hbm_good_die_per_wafer", "display": False},
    {"key": "hbm_core_kwspm", "label": "HBM core wafer capacity", "unit": "kWSPM", "type": "formula", "group": "HBM / DRAM Demand", "expr": "hbm_core_annual_wafer_k / 12", "highlight": True, "display": False},
    {"key": "hbm_effective_kwspm", "label": "HBM effective wafer capacity", "unit": "kWSPM", "type": "formula", "group": "HBM / DRAM Demand", "expr": "hbm_core_kwspm * hbm_combined_uplift", "highlight": True, "display": False},
    {"key": "ex_hbm_annual_wafer_k", "label": "Ex-HBM annual wafers", "unit": "k wafers/yr", "type": "formula", "group": "HBM / DRAM Demand", "expr": "ex_hbm_dram_demand * 125000 / ex_hbm_gb_per_die / ex_hbm_good_die_per_wafer", "display": False},
    {"key": "ex_hbm_kwspm", "label": "Ex-HBM wafer capacity", "unit": "kWSPM", "type": "formula", "group": "HBM / DRAM Demand", "expr": "ex_hbm_annual_wafer_k / 12", "highlight": True, "display": False},
    {"key": "dram_kwspm_required", "label": "Total DRAM kWSPM demand", "unit": "kWSPM", "type": "formula", "group": "HBM / DRAM Demand", "expr": "hbm_effective_kwspm + ex_hbm_kwspm", "highlight": True, "display": False},
    {"key": "hbm_share_dram_capacity", "label": "HBM share of DRAM wafer capacity", "unit": "%", "type": "formula", "group": "HBM / DRAM Demand", "expr": "hbm_effective_kwspm / dram_kwspm_required", "display": False},
]
RACK_FIELD_KEYS = {f["key"] for f in RACK_FIELDS}


def _rack_field_period_mode(field_key: str) -> str:
    field = next((f for f in RACK_FIELDS if f["key"] == field_key), None)
    return str((field or {}).get("period_mode") or "period")


def _rack_field_def(field_key: str) -> dict | None:
    return next((f for f in RACK_FIELDS if f["key"] == field_key), None)


def _rack_field_periods(field: dict) -> list[str]:
    return [RACK_SPEC_PERIOD] if str(field.get("period_mode") or "period") == "single" else list(PERIODS)


def _single_field_fallback(stored: dict[str, dict], field: dict | None = None) -> dict:
    if RACK_SPEC_PERIOD in stored:
        return stored[RACK_SPEC_PERIOD]
    for period in PERIODS:
        cell = stored.get(period)
        if cell and (cell.get("value") is not None or cell.get("source") == "formula"):
            return cell
    if field and "default" in field:
        return {"value": field.get("default"), "source": "default", "quote": "", "formula": ""}
    return {"value": None, "source": "default", "quote": "", "formula": ""}


def _row_default(row: dict, index: int):
    defaults = list(row.get("defaults") or [])
    return defaults[index] if index < len(defaults) else None


def _model_row_def(row_key: str) -> dict | None:
    return next((r for r in MODEL_ROWS if r["key"] == row_key), None)


def _normalize_formula_text(formula: str | None) -> str | None:
    if formula is None:
        return None
    text = formula.strip()
    if text.startswith("="):
        text = text[1:].strip()
    return text[:2000]

# Final active static rows for rack-v2. Rack rows feed these formulas through
# frontend-provided aggregate variables (rack_*). Old single-GB300 cells do not
# appear here.
MODEL_ROWS = [
    {"key": "gw_used", "label": "Incremental provisioned power", "unit": "GW",
     "type": "input", "group": "Incremental Provisioned Power", "tabs": ["hbm"],
     "defaults": [None] * len(PERIODS)},
    {"key": "compute_load_share", "label": "Compute load share", "unit": "%",
     "type": "input", "group": "Deployment Assumption - per 1GW", "tabs": ["hbm"],
     "defaults": [80] * len(PERIODS)},
    {"key": "compute_power_kw_per_gw", "label": "Compute power per 1 GW", "unit": "kW",
     "type": "formula", "group": "Deployment Assumption - per 1GW", "tabs": ["hbm"],
     "expr": "1000000 * (compute_load_share > 1 ? compute_load_share / 100 : compute_load_share)"},
    {"key": "server_dram_share_total_dram_market", "label": "Total server DRAM / total DRAM market", "unit": "%",
     "type": "input", "group": "DRAM market assumption", "tabs": ["hbm"],
     "query": "total server DRAM demand as percent of total DRAM demand forecast",
     "defaults": [53, 56, 66, 75, 80, None]},
    {"key": "rdimm_dram_demand", "label": "RDIMM DRAM demand", "unit": "bn Gb",
     "type": "input", "group": "Server DRAM demand", "tabs": ["hbm"],
     "query": "RDIMM server DRAM demand forecast",
     "defaults": [10.144, 14.632, 26.216, 34.394, 37.222, None]},
    {"key": "gp_server_dram_demand", "label": "GP server DRAM demand", "unit": "bn Gb",
     "type": "input", "group": "Server DRAM demand", "tabs": ["hbm"],
     "query": "general purpose server DRAM demand forecast",
     "defaults": [114.584, 142.952, 187.128, 244.956, 320.653, None]},
    {"key": "gddr_rack_attach_share", "label": "GDDR rack attach share", "unit": "%",
     "type": "input", "group": "Server DRAM demand", "tabs": ["hbm"],
     "query": "percent of AI accelerator racks with GDDR inference trays forecast",
     "defaults": [0, 10, 20, 20, 20, None]},
    {"key": "hbm_backend_yield_impact", "label": "HBM back-end yield", "unit": "%",
     "type": "input", "group": "DRAM wafer conversion", "tabs": ["hbm"],
     "query": None, "defaults": [80, 80, 88, 65, 76, None]},
    {"key": "hbm_logic_die_capacity_uplift", "label": "HBM logic-die capacity uplift", "unit": "x",
     "type": "input", "group": "DRAM wafer conversion", "tabs": ["hbm"],
     "query": None, "defaults": [1.05, 1.05, 1.06, 1.09, 1.10, None]},
    {"key": "hbm_combined_uplift", "label": "HBM effective capacity uplift", "unit": "x",
     "type": "formula", "group": "DRAM wafer conversion", "tabs": ["hbm"],
     "expr": "(1 / (hbm_backend_yield_impact > 1 ? hbm_backend_yield_impact / 100 : hbm_backend_yield_impact)) * hbm_logic_die_capacity_uplift"},
    {"key": "ex_hbm_gb_per_die", "label": "Ex-HBM DRAM GB per die", "unit": "GB/die",
     "type": "input", "group": "DRAM wafer conversion", "tabs": ["hbm"],
     "query": None, "defaults": [2.0, 2.1, 2.3, 2.6, 2.9, None]},
    {"key": "ex_hbm_die_per_wafer", "label": "Ex-HBM DRAM die per wafer", "unit": "dies/wafer",
     "type": "input", "group": "DRAM wafer conversion", "tabs": ["hbm"],
     "query": None, "defaults": [1400, 1400, 1400, 1400, 1400, None]},
    {"key": "ex_hbm_wafer_yield", "label": "Ex-HBM DRAM wafer yield", "unit": "%",
     "type": "input", "group": "DRAM wafer conversion", "tabs": ["hbm"],
     "query": None, "defaults": [90, 90, 90, 90, 90, None]},
    {"key": "ex_hbm_good_die_per_wafer", "label": "Ex-HBM good die per wafer", "unit": "dies/wafer",
     "type": "formula", "group": "DRAM wafer conversion", "tabs": ["hbm"],
     "expr": "ex_hbm_die_per_wafer * (ex_hbm_wafer_yield > 1 ? ex_hbm_wafer_yield / 100 : ex_hbm_wafer_yield)"},
    {"key": "total_gpus_mn", "label": "Base total GPU packages", "unit": "mn",
     "type": "formula", "group": "Rack scenario summary", "tabs": ALL_TABS,
     "expr": "rack_total_gpus_mn", "highlight": True},
    {"key": "foundry_kwspm_required", "label": "Base foundry capacity required", "unit": "kWSPM",
     "type": "formula", "group": "Rack scenario summary", "tabs": ["hbm", "capex"],
     "expr": "rack_foundry_kwspm_required", "highlight": True},
    {"key": "hbm_demand", "label": "Base HBM demand", "unit": "bn Gb",
     "type": "formula", "group": "DRAM market bridge", "tabs": ["hbm"],
     "expr": "rack_hbm_demand", "highlight": True},
    {"key": "system_dram_demand", "label": "Base DDR5 / CPU memory demand", "unit": "bn Gb",
     "type": "formula", "group": "DRAM market bridge", "tabs": ["hbm"],
     "expr": "rack_system_dram_demand"},
    {"key": "base_gddr_dram_demand", "label": "Base GDDR DRAM demand", "unit": "bn Gb",
     "type": "formula", "group": "DRAM market bridge", "tabs": ["hbm"],
     "expr": "rack_gddr_dram_demand"},
    {"key": "total_server_dram_demand", "label": "Base total server DRAM demand", "unit": "bn Gb",
     "type": "formula", "group": "DRAM market bridge", "tabs": ["hbm"],
     "expr": "rack_total_server_dram_demand", "highlight": True},
    {"key": "total_dram_demand", "label": "Base total DRAM demand", "unit": "bn Gb",
     "type": "formula", "group": "DRAM market bridge", "tabs": ["hbm"],
     "expr": "rack_total_dram_demand", "highlight": True},
    {"key": "implied_pc_mobile_dram_demand", "label": "Base implied PC, mobile and other DRAM demand", "unit": "bn Gb",
     "type": "formula", "group": "DRAM market bridge", "tabs": ["hbm"],
     "expr": "rack_implied_pc_mobile_dram_demand"},
    {"key": "total_dram_demand_yoy", "label": "Base total DRAM demand YoY", "unit": "%",
     "type": "formula", "group": "DRAM market bridge", "tabs": ["hbm"],
     "expr": "rack_total_dram_demand_yoy"},
    {"key": "hbm_share_total_dram_demand", "label": "Base HBM / total DRAM demand", "unit": "%",
     "type": "formula", "group": "DRAM market bridge", "tabs": ["hbm"],
     "expr": "rack_hbm_share_total_dram_demand", "highlight": True},
    {"key": "ex_hbm_dram_demand", "label": "Base ex-HBM DRAM demand", "unit": "bn Gb",
     "type": "formula", "group": "DRAM market bridge", "tabs": ["hbm"],
     "expr": "rack_ex_hbm_dram_demand"},
    {"key": "hbm_core_kwspm", "label": "Base HBM core kWSPM", "unit": "kWSPM",
     "type": "formula", "group": "DRAM wafer demand", "tabs": ["hbm"],
     "expr": "rack_hbm_core_kwspm", "highlight": True},
    {"key": "hbm_effective_kwspm", "label": "Base HBM effective kWSPM", "unit": "kWSPM",
     "type": "formula", "group": "DRAM wafer demand", "tabs": ["hbm"],
     "expr": "rack_hbm_effective_kwspm", "highlight": True},
    {"key": "ex_hbm_kwspm", "label": "Base ex-HBM DRAM kWSPM", "unit": "kWSPM",
     "type": "formula", "group": "DRAM wafer demand", "tabs": ["hbm"],
     "expr": "rack_ex_hbm_kwspm", "highlight": True},
    {"key": "dram_kwspm_required", "label": "Base total DRAM kWSPM demand", "unit": "kWSPM",
     "type": "formula", "group": "DRAM wafer demand", "tabs": ["hbm"],
     "expr": "rack_dram_kwspm_required", "highlight": True},
    {"key": "hbm_share_dram_capacity", "label": "HBM / total DRAM kWSPM", "unit": "%",
     "type": "formula", "group": "DRAM wafer demand", "tabs": ["hbm"],
     "expr": "rack_hbm_share_dram_capacity", "highlight": True},
    {"key": "tsmc_leading_edge_capacity_kwspm", "label": "TSMC leading-edge capacity", "unit": "kWSPM",
     "type": "input", "group": "Foundry capacity", "tabs": ["hbm", "capex"],
     "query": "TSMC leading edge foundry capacity kWSPM advanced node forecast",
     "defaults": [300, 335, 370, 400, 440]},
    {"key": "foundry_capacity_share", "label": "Foundry requirement / TSMC capacity", "unit": "x",
     "type": "formula", "group": "Foundry capacity", "tabs": ["hbm", "capex"],
     "expr": "foundry_kwspm_required / tsmc_leading_edge_capacity_kwspm", "highlight": True},
    {"key": "hbm_asp", "label": "HBM ASP", "unit": "$/Gb", "type": "input",
     "group": "HBM revenue cross-check", "tabs": ["hbm"],
     "query": "HBM ASP price per Gb blended forecast HBM3e HBM4",
     "defaults": [1.3, 1.52, 1.73, 2.77, 2.5]},
    {"key": "hbm_tam", "label": "Industry HBM TAM", "unit": "$bn", "type": "formula",
     "group": "HBM revenue cross-check", "tabs": ["hbm"], "expr": "hbm_demand * hbm_asp"},
    {"key": "skh_hbm_share", "label": "SKH HBM share", "unit": "x", "type": "input",
     "group": "HBM revenue cross-check", "tabs": ["hbm"],
     "query": "SK Hynix HBM market share percent",
     "defaults": [0.45, 0.50, 0.52, 0.50, 0.48]},
    {"key": "skh_hbm_revenue", "label": "SKH HBM AI revenue", "unit": "$bn",
     "type": "formula", "group": "HBM revenue cross-check", "tabs": ["hbm"],
     "expr": "hbm_tam * skh_hbm_share", "highlight": True},
    {"key": "skh_hbm_ebit_margin", "label": "SKH HBM EBIT margin", "unit": "x",
     "type": "input", "group": "HBM revenue cross-check", "tabs": ["hbm"],
     "query": "SK Hynix HBM EBIT operating margin",
     "defaults": [0.52, 0.50, 0.47, 0.45, 0.43]},
    {"key": "skh_hbm_ebit", "label": "SKH HBM EBIT", "unit": "$bn", "type": "formula",
     "group": "HBM revenue cross-check", "tabs": ["hbm"],
     "expr": "skh_hbm_revenue * skh_hbm_ebit_margin", "highlight": True},
    {"key": "ubs_hbm_demand", "label": "UBS HBM demand (check)", "unit": "bn Gb",
     "type": "input", "group": "HBM bank checks", "tabs": ["hbm"],
     "query": "UBS total HBM demand bn Gb forecast",
     "defaults": [None, 17.48, 32.89, 57.97, None]},
    {"key": "ms_hbm_demand", "label": "MS HBM demand (check)", "unit": "bn Gb",
     "type": "input", "group": "HBM bank checks", "tabs": ["hbm"],
     "query": "Morgan Stanley HBM demand forecast bits",
     "defaults": [9.059, 19.516, 34.205, 56.085, None]},
    {"key": "gs_hbm_tam", "label": "GS HBM TAM (check)", "unit": "$bn", "type": "input",
     "group": "HBM bank checks", "tabs": ["hbm"],
     "query": "Goldman Sachs HBM TAM market size forecast",
     "defaults": [None, None, 56.0, 116.0, 168.0]},
    {"key": "jpm_hbm_tam", "label": "JPM HBM TAM (check)", "unit": "$bn", "type": "input",
     "group": "HBM bank checks", "tabs": ["hbm"],
     "query": "JPMorgan HBM TAM market size forecast",
     "defaults": [16.935, 33.541, 64.948, 129.092, 225.191]},
    {"key": "var_vs_jpm", "label": "Our TAM vs JPM", "unit": "$bn", "type": "formula",
     "group": "HBM bank checks", "tabs": ["hbm"], "expr": "hbm_tam - jpm_hbm_tam"},
    {"key": "var_vs_gs", "label": "Our TAM vs GS", "unit": "$bn", "type": "formula",
     "group": "HBM bank checks", "tabs": ["hbm"], "expr": "hbm_tam - gs_hbm_tam"},
    {"key": "essd_tb_per_gpu", "label": "eSSD TB / GPU package", "unit": "TB", "type": "input",
     "group": "NAND / eSSD demand", "tabs": ["nand"],
     "query": "enterprise SSD storage TB per AI accelerator server GPU TPU custom ASIC",
     "defaults": [8, 12, 16, 24, 32]},
    {"key": "nand_demand", "label": "eSSD / NAND demand", "unit": "bn GB",
     "type": "formula", "group": "NAND / eSSD demand", "tabs": ["nand"],
     "expr": "total_gpus_mn * essd_tb_per_gpu * 1024 / 1000"},
    {"key": "nand_asp", "label": "eSSD / NAND ASP", "unit": "$/GB", "type": "input",
     "group": "NAND revenue", "tabs": ["nand"],
     "query": "enterprise SSD NAND ASP price per GB forecast",
     "defaults": [0.06, 0.07, 0.21, 0.27, 0.25]},
    {"key": "skh_nand_share", "label": "SKH eSSD / NAND share", "unit": "x",
     "type": "input", "group": "NAND revenue", "tabs": ["nand"],
     "query": "SK Hynix Solidigm enterprise SSD NAND market share",
     "defaults": [0.10, 0.12, 0.15, 0.15, 0.15]},
    {"key": "skh_nand_revenue", "label": "SKH eSSD / NAND AI revenue", "unit": "$bn",
     "type": "formula", "group": "NAND revenue", "tabs": ["nand"],
     "expr": "nand_demand * nand_asp * skh_nand_share", "highlight": True},
    {"key": "skh_total_ai_revenue", "label": "Tracked SKH AI memory revenue", "unit": "$bn",
     "type": "formula", "group": "Total", "tabs": ALL_TABS,
     "expr": "skh_hbm_revenue + skh_nand_revenue",
     "highlight": True},
]

def _period_year(period: str) -> str:
    match = re.search(r"CY(\d{2})", period)
    if not match:
        return ""
    year = int(match.group(1))
    return str(2000 + year)


_PERIOD_HINTS = {period: _period_year(period) for period in PERIODS}


async def _ensure_table() -> None:
    db = await get_db()
    await db.execute(
        """CREATE TABLE IF NOT EXISTS model_cells (
               model TEXT NOT NULL,
               row_key TEXT NOT NULL,
               period TEXT NOT NULL,
               value REAL,
               source TEXT NOT NULL DEFAULT 'rag',
               quote TEXT NOT NULL DEFAULT '',
               updated_at TEXT NOT NULL,
               PRIMARY KEY (model, row_key, period)
           )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS model_rack_rows (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               model TEXT NOT NULL,
               vendor TEXT NOT NULL,
               name TEXT NOT NULL DEFAULT '',
               display_order INTEGER NOT NULL DEFAULT 0,
               active INTEGER NOT NULL DEFAULT 1,
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL
           )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS model_rack_cells (
               model TEXT NOT NULL,
               row_id INTEGER NOT NULL,
               field_key TEXT NOT NULL,
               period TEXT NOT NULL,
               value REAL,
               source TEXT NOT NULL DEFAULT 'user',
               formula TEXT NOT NULL DEFAULT '',
               updated_at TEXT NOT NULL,
               PRIMARY KEY (model, row_id, field_key, period)
           )"""
    )
    cur = await db.execute("PRAGMA table_info(model_rack_cells)")
    rack_cell_columns = {str(row["name"]) for row in await cur.fetchall()}
    if "formula" not in rack_cell_columns:
        await db.execute("ALTER TABLE model_rack_cells ADD COLUMN formula TEXT NOT NULL DEFAULT ''")
    cur = await db.execute("SELECT COUNT(*) AS n FROM model_rack_rows WHERE model=?", (RACK_MODEL,))
    existing = await cur.fetchone()
    if not existing or int(existing["n"] or 0) == 0:
        now = datetime.now().isoformat(timespec="seconds")
        for vendor in RACK_VENDORS:
            await db.execute(
                """INSERT INTO model_rack_rows
                   (model, vendor, name, display_order, active, created_at, updated_at)
                   VALUES (?, ?, '', 0, 1, ?, ?)""",
                (RACK_MODEL, vendor, now, now),
            )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS model_news_cache (
               company TEXT PRIMARY KEY,
               data_json TEXT NOT NULL,
               updated_at TEXT NOT NULL
           )"""
    )
    await db.commit()


def _empty_news(status: str = "empty", message: str = "") -> dict:
    return {
        "company": NEWS_COMPANY,
        "columns": NEWS_COLUMNS,
        "rows": [],
        "status": status,
        "message": message or "Press Refresh to pull the latest public news and guidance.",
        "last_updated": None,
    }


def _news_date_int(value: str | None) -> int:
    s = str(value or "").strip()
    if not s or s.upper() == "N/A":
        return 0
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return int(m.group(1)) * 10000 + int(m.group(2)) * 100 + int(m.group(3))
    m = re.match(r"^(\d{4})-(\d{1,2})$", s)
    if m:
        return int(m.group(1)) * 10000 + int(m.group(2)) * 100
    m = re.match(r"^(\d{4})$", s)
    if m:
        return int(m.group(1)) * 10000
    return 0


def _sort_news_rows(rows: list[dict]) -> list[dict]:
    return [
        row for _, row in sorted(
            enumerate(rows or []),
            key=lambda item: (-_news_date_int(item[1].get("date") or item[1].get("published_date")), item[0]),
        )
    ]


async def _load_news_cache() -> dict:
    await _ensure_table()
    db = await get_db()
    cur = await db.execute(
        "SELECT data_json, updated_at FROM model_news_cache WHERE company=?",
        (NEWS_COMPANY,),
    )
    row = await cur.fetchone()
    if not row:
        return _empty_news()
    try:
        data = json.loads(row["data_json"])
        data["rows"] = _sort_news_rows(data.get("rows") or [])
        data["last_updated"] = row["updated_at"]
        return data
    except Exception:
        return _empty_news("cache_error", "Cached news table could not be read. Press Refresh.")


async def _save_news_cache(data: dict) -> dict:
    await _ensure_table()
    now = datetime.now().isoformat(timespec="seconds")
    data = {**data, "last_updated": now}
    data["rows"] = _sort_news_rows(data.get("rows") or [])
    db = await get_db()
    await db.execute(
        """INSERT OR REPLACE INTO model_news_cache (company, data_json, updated_at)
           VALUES (?, ?, ?)""",
        (NEWS_COMPANY, json.dumps(data, ensure_ascii=False), now),
    )
    await db.commit()
    return data


def _get_tavily_key() -> str:
    return os.getenv("TAVILY_API_KEY", "").strip()


def _tavily_search_sync(query: str, api_key: str, max_results: int = 6) -> list[dict]:
    payload = {
        "api_key": api_key,
        "query": query,
        "search_depth": "advanced",
        "topic": "news",
        "max_results": max_results,
        "include_answer": False,
        "include_raw_content": False,
    }
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        body = resp.read().decode("utf-8", errors="ignore")
    data = json.loads(body or "{}")
    return data.get("results") or []


async def _fetch_tavily_news() -> tuple[list[dict], str | None]:
    api_key = _get_tavily_key()
    if not api_key:
        return [], "TAVILY_API_KEY is not configured."
    today = date.today().isoformat()
    queries = [
        f"SK Hynix latest news guidance HBM DRAM NAND wafer capex revenue {today}",
        f"SK hynix expects plans said announced guidance HBM DRAM NAND wafer capex {today}",
        f"SK Hynix earnings guidance HBM supply AI memory wafer capacity investment {today}",
        f"SK hynix management said plans expects HBM demand DRAM NAND wafer latest announcement {today}",
    ]
    seen: set[str] = set()
    out: list[dict] = []
    for q in queries:
        try:
            results = await asyncio.to_thread(_tavily_search_sync, q, api_key, 6)
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 402, 403}:
                return out, f"Tavily unavailable: HTTP {exc.code}. Check API key, quota, or billing."
            return out, f"Tavily search failed: HTTP {exc.code}."
        except Exception as exc:
            return out, f"Tavily search failed: {exc}"
        for r in results:
            url = (r.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            out.append({
                "title": r.get("title", ""),
                "url": url,
                "content": r.get("content", ""),
                "published_date": r.get("published_date", ""),
                "score": r.get("score", 0),
            })
            if len(out) >= 14:
                return out, None
    return out, None


def _fallback_news_rows(results: list[dict]) -> list[dict]:
    rows = []
    for r in _sort_news_rows(results)[:6]:
        rows.append({
            "company": NEWS_COMPANY,
            "topic": "Recent update",
            "news_guidance": (r.get("title") or r.get("content") or "N/A")[:180],
            "key_data": "N/A",
            "source": r.get("title") or "Source",
            "url": r.get("url") or "",
            "date": r.get("published_date") or "",
        })
    return rows


def _vendor_order(vendor: str) -> int:
    try:
        return RACK_VENDORS.index(vendor)
    except ValueError:
        return len(RACK_VENDORS)


async def _load_rack_sections() -> list[dict]:
    await _ensure_table()
    db = await get_db()
    cur = await db.execute(
        """SELECT id, vendor, name, display_order
           FROM model_rack_rows
           WHERE model=? AND active=1
           ORDER BY display_order ASC, id ASC""",
        (RACK_MODEL,),
    )
    rows = [dict(r) for r in await cur.fetchall()]
    rows.sort(key=lambda r: (_vendor_order(str(r["vendor"])), int(r["display_order"] or 0), int(r["id"])))

    row_ids = [int(r["id"]) for r in rows]
    cell_map: dict[int, dict[str, dict[str, dict]]] = {rid: {} for rid in row_ids}
    if row_ids:
        placeholders = ",".join("?" for _ in row_ids)
        cur = await db.execute(
            f"""SELECT row_id, field_key, period, value, source, formula
                FROM model_rack_cells
                WHERE model=? AND row_id IN ({placeholders})""",
            (RACK_MODEL, *row_ids),
        )
        for c in await cur.fetchall():
            rid = int(c["row_id"])
            field_key = str(c["field_key"])
            period = str(c["period"])
            cell_map.setdefault(rid, {}).setdefault(field_key, {})[period] = {
                "value": c["value"],
                "source": c["source"] or "user",
                "formula": c["formula"] or "",
                "quote": "",
            }

    sections: dict[str, dict] = {
        vendor: {"vendor": vendor, "label": vendor, "rows": []}
        for vendor in RACK_VENDORS
    }
    for row in rows:
        rid = int(row["id"])
        cells: dict[str, dict[str, dict]] = {}
        for field in RACK_FIELDS:
            field_cells: dict[str, dict] = {}
            stored = cell_map.get(rid, {}).get(field["key"], {})
            if str(field.get("period_mode") or "period") == "single":
                field_cells[RACK_SPEC_PERIOD] = _single_field_fallback(stored, field)
            else:
                for period in PERIODS:
                    field_cells[period] = stored.get(period, {"value": None, "source": "default", "quote": "", "formula": ""})
            cells[field["key"]] = field_cells
        vendor = str(row["vendor"]).upper()
        if vendor not in sections:
            sections[vendor] = {"vendor": vendor, "label": vendor, "rows": []}
        sections[vendor]["rows"].append({
            "id": rid,
            "vendor": vendor,
            "name": row["name"] or "",
            "display_order": int(row["display_order"] or 0),
            "cells": cells,
        })

    return list(sections.values())


async def _summarize_news_rows(results: list[dict]) -> list[dict]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or not results:
        return _fallback_news_rows(results)
    from ...services.openai_research import AsyncResearchClient
    client = AsyncResearchClient(api_key=api_key)
    payload = json.dumps(results[:14], ensure_ascii=False)[:12000]
    prompt = f"""You are building a compact SK Hynix model dashboard table.

Use only the public web/news search results below. Focus on announced news,
management guidance, earnings commentary, HBM/DRAM/NAND/capex data, and any
numbers that are useful for an SK Hynix memory revenue model.

Return STRICT JSON only:
{{
  "rows": [
    {{
      "company": "SK Hynix",
      "topic": "HBM guidance | DRAM | NAND | Capex | Earnings | Customer demand",
      "news_guidance": "one concise sentence",
      "key_data": "numbers/guidance only, or N/A",
      "source": "publisher or article title",
      "url": "https://...",
      "date": "YYYY-MM-DD or N/A"
    }}
  ]
}}

Rules:
- 4-7 rows maximum.
- Sort rows newest first by date.
- Do not invent numbers.
- If a result is vague, keep key_data as "N/A".
- Prefer the newest and most concrete sources.

SEARCH RESULTS:
{payload}
"""
    try:
        async with client.messages.stream(
            model=_HAIKU_MODEL,
            max_tokens=1600,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            resp = await stream.get_final_message()
        text = "".join(getattr(b, "text", "") for b in resp.content).strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
        data = json.loads(re.search(r"\{.*\}", text, re.DOTALL).group(0))
        rows = data.get("rows") or []
        return _sort_news_rows(rows)[:7] if rows else _fallback_news_rows(results)
    except Exception as exc:
        logger.warning("SKH news summarization failed: %s", exc)
        return _fallback_news_rows(results)


@router.get("/news")
async def get_model_news():
    """Cached SK Hynix public news/guidance table for the model tab."""
    return await _load_news_cache()


@router.post("/news/refresh")
async def refresh_model_news():
    """Refresh SK Hynix public news/guidance via Tavily, then summarize with Haiku."""
    results, error = await _fetch_tavily_news()
    if error:
        cached = await _load_news_cache()
        return {
            **cached,
            "status": "tavily_error",
            "message": error,
        }
    if not results:
        data = _empty_news("no_results", "Tavily returned no relevant SK Hynix results.")
        return await _save_news_cache(data)
    rows = _sort_news_rows(await _summarize_news_rows(results))
    data = {
        "company": NEWS_COMPANY,
        "columns": NEWS_COLUMNS,
        "rows": rows,
        "status": "ok",
        "message": f"Summarized {len(results)} Tavily results.",
    }
    return await _save_news_cache(data)


@router.get("/hbm")
async def get_model():
    """Model structure + current cell values (user override > RAG > default)."""
    await _ensure_table()
    db = await get_db()
    cur = await db.execute(
        "SELECT row_key, period, value, source, quote FROM model_cells WHERE model=?",
        (STATIC_MODEL,),
    )
    stored = {(r["row_key"], r["period"]): r for r in await cur.fetchall()}

    rows = []
    for row in MODEL_ROWS:
        cells = {}
        for i, p in enumerate(PERIODS):
            s = stored.get((row["key"], p))
            if s is None and not p.endswith("E"):
                s = stored.get((row["key"], f"{p}E"))
            if s is not None and (s["value"] is not None or s["source"] == "formula"):
                cells[p] = {"value": s["value"], "source": s["source"],
                            "quote": "" if s["source"] == "formula" else s["quote"],
                            "formula": s["quote"] if s["source"] == "formula" else ""}
            else:
                d = _row_default(row, i)
                cells[p] = {"value": d, "source": "default", "quote": "", "formula": ""}
        rows.append({
            "key": row["key"], "label": row["label"], "unit": row["unit"],
            "type": row["type"], "group": row["group"],
            "tabs": row.get("tabs", ["hbm"]),
            "expr": row.get("expr"), "highlight": bool(row.get("highlight")),
            "has_query": bool(row.get("query")), "cells": cells,
        })
    rack_fields = [
        {
            "key": f["key"],
            "label": f["label"],
            "unit": f["unit"],
            "type": f["type"],
            "group": f["group"],
            "expr": f.get("expr"),
            "highlight": bool(f.get("highlight")),
            "period_mode": f.get("period_mode", "period"),
            "display": bool(f.get("display", True)),
        }
        for f in RACK_FIELDS
    ]
    return {
        "periods": PERIODS,
        "tabs": MODEL_TABS,
        "rows": rows,
        "rack_fields": rack_fields,
        "rack_sections": await _load_rack_sections(),
    }


@router.get("/workbook")
async def get_workbook_model():
    """Read the configured Excel workbook without modifying it."""
    from ...services.workbook_model import load_workbook_model

    try:
        return await asyncio.to_thread(load_workbook_model)
    except FileNotFoundError as exc:
        logger.warning("Memory model workbook not found: %s", exc.filename)
        raise HTTPException(503, "Memory model workbook not found.") from exc
    except (OSError, ValueError, BadZipFile, InvalidFileException, KeyError) as exc:
        logger.warning("Memory model workbook could not be read: %s", exc)
        raise HTTPException(503, "Memory model workbook could not be read.") from exc


class CellEdit(BaseModel):
    row_key: str
    period: str
    value: float | None  # None clears the override (falls back to RAG/default)
    formula: str | None = None


class RackRowCreate(BaseModel):
    vendor: str


class RackRowPatch(BaseModel):
    name: str | None = None
    vendor: str | None = None
    display_order: int | None = None


class RackCellEdit(BaseModel):
    row_id: int
    field_key: str
    period: str
    value: float | None
    formula: str | None = None


def _normalize_vendor(vendor: str) -> str:
    v = (vendor or "").strip().upper()
    if v not in RACK_VENDORS:
        raise HTTPException(400, f"vendor must be one of: {', '.join(RACK_VENDORS)}")
    return v


async def _require_active_rack_row(row_id: int) -> dict:
    await _ensure_table()
    db = await get_db()
    cur = await db.execute(
        """SELECT id, vendor, name, display_order
           FROM model_rack_rows
           WHERE model=? AND id=? AND active=1""",
        (RACK_MODEL, row_id),
    )
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Rack row not found")
    return dict(row)


@router.post("/hbm/cell")
async def set_cell(edit: CellEdit):
    await _ensure_table()
    if edit.period not in PERIODS:
        raise HTTPException(400, "Invalid period")
    row_def = _model_row_def(edit.row_key)
    if not row_def:
        raise HTTPException(400, "Invalid row")
    db = await get_db()
    formula = _normalize_formula_text(edit.formula)
    if formula is not None:
        if row_def["type"] != "formula":
            raise HTTPException(400, "Only formula rows can store formula overrides")
        if not formula:
            await db.execute(
                """DELETE FROM model_cells WHERE model=? AND row_key=? AND period=?
                   AND source IN ('user', 'formula')""", (STATIC_MODEL, edit.row_key, edit.period))
        else:
            await db.execute(
                """INSERT OR REPLACE INTO model_cells
                   (model, row_key, period, value, source, quote, updated_at)
                   VALUES (?, ?, ?, NULL, 'formula', ?, ?)""",
                (STATIC_MODEL, edit.row_key, edit.period, formula, datetime.now().isoformat()))
    elif edit.value is None:
        await db.execute(
            """DELETE FROM model_cells WHERE model=? AND row_key=? AND period=?
               AND source IN ('user', 'formula')""", (STATIC_MODEL, edit.row_key, edit.period))
    else:
        await db.execute(
            """INSERT OR REPLACE INTO model_cells
               (model, row_key, period, value, source, quote, updated_at)
               VALUES (?, ?, ?, ?, 'user', '', ?)""",
            (STATIC_MODEL, edit.row_key, edit.period, edit.value, datetime.now().isoformat()))
    await db.commit()
    return {"ok": True}


@router.post("/hbm/rack-row")
async def create_rack_row(edit: RackRowCreate):
    await _ensure_table()
    vendor = _normalize_vendor(edit.vendor)
    db = await get_db()
    cur = await db.execute(
        """SELECT COALESCE(MAX(display_order), -1) AS max_order
           FROM model_rack_rows
           WHERE model=? AND vendor=? AND active=1""",
        (RACK_MODEL, vendor),
    )
    row = await cur.fetchone()
    display_order = int(row["max_order"] if row and row["max_order"] is not None else -1) + 1
    now = datetime.now().isoformat(timespec="seconds")
    cur = await db.execute(
        """INSERT INTO model_rack_rows
           (model, vendor, name, display_order, active, created_at, updated_at)
           VALUES (?, ?, '', ?, 1, ?, ?)""",
        (RACK_MODEL, vendor, display_order, now, now),
    )
    await db.commit()
    return {"ok": True, "row_id": cur.lastrowid}


@router.patch("/hbm/rack-row/{row_id}")
async def update_rack_row(row_id: int, edit: RackRowPatch):
    row = await _require_active_rack_row(row_id)
    vendor = _normalize_vendor(edit.vendor) if edit.vendor is not None else row["vendor"]
    name = row["name"] if edit.name is None else edit.name.strip()[:120]
    display_order = row["display_order"] if edit.display_order is None else int(edit.display_order)
    db = await get_db()
    await db.execute(
        """UPDATE model_rack_rows
           SET vendor=?, name=?, display_order=?, updated_at=?
           WHERE model=? AND id=?""",
        (vendor, name, display_order, datetime.now().isoformat(timespec="seconds"), RACK_MODEL, row_id),
    )
    await db.commit()
    return {"ok": True}


@router.delete("/hbm/rack-row/{row_id}")
async def delete_rack_row(row_id: int):
    await _require_active_rack_row(row_id)
    db = await get_db()
    await db.execute(
        """UPDATE model_rack_rows
           SET active=0, updated_at=?
           WHERE model=? AND id=?""",
        (datetime.now().isoformat(timespec="seconds"), RACK_MODEL, row_id),
    )
    await db.commit()
    return {"ok": True}


@router.post("/hbm/rack-cell")
async def set_rack_cell(edit: RackCellEdit):
    await _require_active_rack_row(edit.row_id)
    field_key = (edit.field_key or "").strip()
    if field_key not in RACK_FIELD_KEYS:
        raise HTTPException(400, "Invalid rack field")
    field_def = _rack_field_def(field_key)
    period_mode = _rack_field_period_mode(field_key)
    period = RACK_SPEC_PERIOD if period_mode == "single" else edit.period
    if period_mode != "single" and period not in PERIODS:
        raise HTTPException(400, "Invalid period")
    db = await get_db()
    if field_key == "racks_per_gw" and edit.formula is None and edit.value is not None:
        raise HTTPException(400, "Number of racks is formula-driven; edit the formula or rack power instead")
    if period_mode == "single":
        await db.execute(
            """DELETE FROM model_rack_cells
               WHERE model=? AND row_id=? AND field_key=?""",
            (RACK_MODEL, edit.row_id, field_key),
        )
    formula = _normalize_formula_text(edit.formula)
    if formula is not None:
        if not field_def or field_def["type"] != "formula":
            raise HTTPException(400, "Only formula fields can store formula overrides")
        if not formula:
            await db.execute(
                """DELETE FROM model_rack_cells
                   WHERE model=? AND row_id=? AND field_key=? AND period=?""",
                (RACK_MODEL, edit.row_id, field_key, period),
            )
        else:
            await db.execute(
                """INSERT OR REPLACE INTO model_rack_cells
                   (model, row_id, field_key, period, value, source, formula, updated_at)
                   VALUES (?, ?, ?, ?, NULL, 'formula', ?, ?)""",
                (RACK_MODEL, edit.row_id, field_key, period, formula,
                 datetime.now().isoformat(timespec="seconds")),
            )
    elif edit.value is None:
        await db.execute(
            """DELETE FROM model_rack_cells
               WHERE model=? AND row_id=? AND field_key=? AND period=?""",
            (RACK_MODEL, edit.row_id, field_key, period),
        )
    else:
        await db.execute(
            """INSERT OR REPLACE INTO model_rack_cells
               (model, row_id, field_key, period, value, source, formula, updated_at)
               VALUES (?, ?, ?, ?, ?, 'user', '', ?)""",
            (RACK_MODEL, edit.row_id, field_key, period, edit.value,
             datetime.now().isoformat(timespec="seconds")),
        )
    await db.commit()
    return {"ok": True}


def _build_cell_extraction_prompt(row: dict, chunks: list[dict]) -> str:
    ctx = "\n\n".join(
        f"[S{i + 1} | {c.get('broker','?')} {c.get('date','?')} | "
        f"{c.get('filename','?')} P.{c.get('page') or '?'}]\n{c.get('text','')[:900]}"
        for i, c in enumerate(chunks))
    prompt_override = get_tab_prompt("model")
    if prompt_override:
        return render_prompt_template(prompt_override, {
            "metric_label": row["label"],
            "metric_unit": row["unit"],
            "query": row["query"],
            "ctx": ctx,
            "min_year": min(_PERIOD_HINTS.values()),
            "max_year": max(_PERIOD_HINTS.values()),
        })
    return f"""From the research excerpts below, extract values for this metric across calendar years.

METRIC: {row['label']} ({row['unit']})
Search intent: {row['query']}

EXCERPTS:
{ctx}

Return values for years {min(_PERIOD_HINTS.values())}-{max(_PERIOD_HINTS.values())} where the excerpts STATE them (estimates count;
skip years with no stated figure - do NOT extrapolate). Values must be in {row['unit']}.
Ratios/shares/margins as decimals (55% -> 0.55) when the unit is "x".
Percent rows should be whole percentages (55% -> 55) when the unit is "%".
For each value include the source id (S1/S2/S3).

STRICT JSON only:
{{"values": [{{"year": 2026, "value": 0.0, "source": "S1"}}]}}"""


async def _extract_row_values(row: dict, retrieve) -> dict[str, dict]:
    """RAG-retrieve (small k, memory_semiconductors scope) + Haiku-extract one
    input row's yearly values."""
    retrieval = get_retrieval_settings()
    chunks_get = int(retrieval.get("chunks_get") or 8)
    rerank_candidates = get_rerank_candidates(retrieval, 3)
    source_options = get_tab_sources("model")
    chunks = await retrieve(
        row["query"],
        k=chunks_get,
        top_n=chunks_get,
        rerank_candidates=rerank_candidates,
        source_options=source_options,
        scope="memory",
    )
    if not chunks:
        return {}
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return {}
    from ...services.openai_research import AsyncResearchClient
    client = AsyncResearchClient(api_key=api_key)
    try:
        async with client.messages.stream(
            model=_HAIKU_MODEL, max_tokens=1200, temperature=0,
            messages=[{"role": "user",
                       "content": _build_cell_extraction_prompt(row, chunks)}],
        ) as stream:
            resp = await stream.get_final_message()
        text = "".join(getattr(b, "text", "") for b in resp.content).strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
        data = json.loads(re.search(r"\{.*\}", text, re.DOTALL).group(0))
    except Exception as exc:
        logger.warning("Model cell extraction failed for %s: %s", row["key"], exc)
        return {}
    out = {}
    year_to_period = {v: k for k, v in _PERIOD_HINTS.items()}
    source_map = {f"S{i + 1}": c for i, c in enumerate(chunks)}
    for v in data.get("values", []):
        try:
            period = year_to_period.get(str(int(v.get("year"))))
            val = float(v.get("value"))
            if period:
                src = source_map.get(str(v.get("source") or "").strip(), {})
                filename = src.get("filename") or "source"
                page = src.get("page") or 0
                loc = f"{filename} P.{page}" if page else filename
                out[period] = {"value": val, "quote": loc[:260]}
        except (TypeError, ValueError):
            continue
    return out


@router.post("/hbm/refresh")
async def refresh_model():
    """Re-pull all RAG-backed input cells (user overrides are preserved)."""
    await _ensure_table()
    from ...services.fact_pool import retrieve_for_section
    db = await get_db()
    cur = await db.execute(
        "SELECT row_key, period FROM model_cells WHERE model=? AND source='user'",
        (STATIC_MODEL,),
    )
    user_locked = {(r["row_key"], r["period"]) for r in await cur.fetchall()}

    now = datetime.now().isoformat()
    pulled = 0
    for row in MODEL_ROWS:
        if row["type"] != "input" or not row.get("query"):
            continue
        values = await _extract_row_values(row, retrieve_for_section)
        for period, v in values.items():
            if (row["key"], period) in user_locked:
                continue
            await db.execute(
                """INSERT OR REPLACE INTO model_cells
                   (model, row_key, period, value, source, quote, updated_at)
                   VALUES (?, ?, ?, ?, 'rag', ?, ?)""",
                (STATIC_MODEL, row["key"], period, v["value"], v["quote"], now))
            pulled += 1
    await db.commit()
    logger.info("Model refresh: %d cells pulled from RAG", pulled)
    return {"ok": True, "cells_pulled": pulled}
