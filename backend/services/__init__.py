"""Shared utilities for memory_chart services."""

from __future__ import annotations

import re

# Canonical display broker names for Memory Chart.
#
# This local table applies Memory Tracker's preferred display labels for the
# gallery/dashboard (BofA, Citi, DB, Arete Research). Keep aliases lowercase
# because `normalize_broker` lowers incoming metadata before lookup.
_BROKER_ALIASES = {
    "goldman sachs": "GS",
    "goldman": "GS",
    "gs": "GS",
    "j.p. morgan": "JPM",
    "jp morgan": "JPM",
    "jpmorgan": "JPM",
    "jpm": "JPM",
    "morgan stanley": "MS",
    "ms": "MS",
    "bank of america": "BofA",
    "bank of america merrill": "BofA",
    "bofa": "BofA",
    "bac": "BofA",
    "baml": "BofA",
    "boa": "BofA",
    "bofa securities": "BofA",
    "bof a": "BofA",
    "citigroup": "Citi",
    "citibank": "Citi",
    "citi research": "Citi",
    "citi": "Citi",
    "barclays": "Barclays",
    "barc": "Barclays",
    "deutsche": "DB",
    "deutsche bank": "DB",
    "db": "DB",
    "ubs": "UBS",
    "clsa": "CLSA",
    "nomura": "Nomura",
    "trendforce": "TrendForce",
    "yole": "Yole",
    "hsbc": "HSBC",
    "bernstein": "Bernstein",
    "bern": "Bernstein",
    "alliancebernstein": "Bernstein",
    "sanford bernstein": "Bernstein",
    "jefferies": "Jefferies",
    "jef": "Jefferies",
    "jeff": "Jefferies",
    "jeffries": "Jefferies",
    "wells": "Wells Fargo",
    "wells fargo": "Wells Fargo",
    "wf": "Wells Fargo",
    "keybanc": "KeyBanc",
    "mizuho": "Mizuho",
    "daiwa": "Daiwa",
    "macquarie": "Macquarie",
    "macq": "Macquarie",
    "evercore": "Evercore",
    "evr": "Evercore",
    "evercore isi": "Evercore",
    "cowen": "Cowen",
    "td cowen": "Cowen",
    "stifel": "Stifel",
    "piper": "Piper",
    "piper sandler": "Piper",
    "raymond james": "Raymond James",
    "rjf": "Raymond James",
    "truist": "Truist",
    "oppenheimer": "Oppenheimer",
    "opp": "Oppenheimer",
    "wedbush": "Wedbush",
    "loop capital": "Loop",
    "loop": "Loop",
    "argus": "Argus",
    "needham": "Needham",
    "canaccord": "Canaccord",
    "baird": "Baird",
    "guggenheim": "Guggenheim",
    "gugg": "Guggenheim",
    "susquehanna": "Susquehanna",
    "susq": "Susquehanna",
    "stephens": "Stephens",
    "benchmark": "Benchmark",
    "rosenblatt": "Rosenblatt",
    "rbc": "RBC",
    "bmo": "BMO",
    "bmo capital": "BMO",
    "cibc": "CIBC",
    "berenberg": "Berenberg",
    "redburn": "Redburn",
    "exane": "Exane",
    "exane bnp": "Exane",
    "kepler": "Kepler",
    "kepler cheuvreux": "Kepler",
    "oddo": "Oddo",
    "oddo bhf": "Oddo",
    "liberum": "Liberum",
    "peel hunt": "Peel Hunt",
    "numis": "Numis",
    "socgen": "SocGen",
    "sg": "SocGen",
    "societe generale": "SocGen",
    "société générale": "SocGen",
    "bnp paribas": "BNP",
    "bnp": "BNP",
    "nikko": "SMBC Nikko",
    "smbc": "SMBC Nikko",
    "smbc nikko": "SMBC Nikko",
    "cicc": "CICC",
    "citics": "CITICS",
    "citic securities": "CITICS",
    "haitong": "Haitong",
    "huatai": "Huatai",
    "guojun": "Guojun",
    "guotai": "Guotai",
    "guosen": "Guosen",
    "bocom": "BOCOM",
    "atlantic equities": "Atlantic",
    "lincoln international": "Lincoln",
    "gartner": "Gartner",
    "arete": "Arete Research",
    "arete research": "Arete Research",
}


def _broker_key(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (raw or "").lower()).strip()


_BROKER_ALIAS_LOOKUP = {
    _broker_key(alias): display
    for alias, display in _BROKER_ALIASES.items()
    if _broker_key(alias)
}


def _known_broker(raw: str) -> str:
    return _BROKER_ALIAS_LOOKUP.get(_broker_key(raw), "")


def normalize_broker(raw: str) -> str:
    """Normalize broker name to canonical short form."""
    if not raw:
        return ""
    return _known_broker(raw) or raw.strip()


def known_brokers() -> list[str]:
    """Canonical display broker names accepted by the alias table."""
    return sorted(set(_BROKER_ALIAS_LOOKUP.values()), key=str.lower)


_BRACKET_BROKER_RE = re.compile(r"\[([A-Za-z][A-Za-z0-9 &.'+-]{1,60})\]")


def broker_from_filename(filename: str, explicit_broker: str = "") -> str:
    """Resolve a broker from a filename using the ES-style alias table.

    Explicit broker input is trusted and normalized. Filename detection is
    stricter: it only returns a broker when a known alias is present.
    """
    explicit = normalize_broker(explicit_broker)
    if explicit:
        return explicit

    raw = filename or ""
    for m in _BRACKET_BROKER_RE.finditer(raw):
        bracket_text = m.group(1)
        direct = _known_broker(bracket_text)
        if direct:
            return direct
        bracket_key = _broker_key(bracket_text)
        for alias_key, display in sorted(
            _BROKER_ALIAS_LOOKUP.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            if re.search(rf"(^| ){re.escape(alias_key)}( |$)", bracket_key):
                return display

    filename_key = _broker_key(raw.replace("_", " "))
    for alias_key, display in sorted(
        _BROKER_ALIAS_LOOKUP.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if re.search(rf"(^| ){re.escape(alias_key)}( |$)", filename_key):
            return display
    return ""
