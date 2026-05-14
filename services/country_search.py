"""Country master + multi-stage search algorithm."""
from __future__ import annotations

import json
import logging
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from rapidfuzz import fuzz

log = logging.getLogger(__name__)

COUNTRIES_PATH = Path(__file__).parent.parent / "i18n" / "countries.json"


@dataclass
class Country:
    name_ja: str
    name_en: str
    iso2: str
    iso3: str
    flag: str
    region: str
    block: Optional[str]
    aliases: list[str]
    excluded: bool = False
    reason_ja: str = ""
    reason_en: str = ""

    def display(self, lang: str = "ja") -> str:
        if lang == "en":
            return f"{self.flag} {self.name_en} ({self.name_ja})"
        return f"{self.flag} {self.name_ja} ({self.name_en})"


def _normalize(s: str) -> str:
    return unicodedata.normalize("NFKC", s).lower().strip()


class CountryRegistry:
    def __init__(self) -> None:
        self.countries: list[Country] = []
        self.regions: dict[str, dict] = {}
        self.block_map: dict[str, str] = {}  # alias -> actual block label
        self._load()

    def _load(self) -> None:
        data = json.loads(COUNTRIES_PATH.read_text("utf-8"))
        self.regions = data.get("regions", {})
        self.block_map = data.get("blocks", {}).get("_aliases", {})
        for raw in data.get("countries", []):
            self.countries.append(Country(
                name_ja=raw["name_ja"],
                name_en=raw["name_en"],
                iso2=raw.get("iso2", ""),
                iso3=raw.get("iso3", ""),
                flag=raw.get("flag", "🏳️"),
                region=raw.get("region", ""),
                block=raw.get("block"),
                aliases=raw.get("aliases", []),
                excluded=raw.get("excluded", False),
                reason_ja=raw.get("reason_ja", ""),
                reason_en=raw.get("reason_en", ""),
            ))
        log.info("Loaded %d countries, %d blocks aliases", len(self.countries), len(self.block_map))

    def reload(self) -> None:
        self.countries.clear()
        self.regions.clear()
        self.block_map.clear()
        self._load()

    def block_header(self, country: Country) -> Optional[str]:
        if country.block is None:
            return None
        return self.block_map.get(country.block, country.block)

    def by_region(self, region: str, exclude_excluded: bool = True) -> list[Country]:
        out = [c for c in self.countries if c.region == region]
        if exclude_excluded:
            out = [c for c in out if not c.excluded]
        out.sort(key=lambda c: c.name_ja)
        return out

    def all_regions(self) -> dict[str, dict]:
        return self.regions

    def find_exact(self, query: str) -> Optional[Country]:
        """Quick exact lookup. Returns None if not found."""
        q = _normalize(query)
        for c in self.countries:
            if q in (_normalize(c.name_ja), _normalize(c.name_en),
                     _normalize(c.iso2), _normalize(c.iso3)):
                return c
            for alias in c.aliases:
                if q == _normalize(alias):
                    return c
        return None

    def search(self, query: str, max_results: int = 25) -> tuple[list[Country], str]:
        """Multi-stage fuzzy search. Returns (matches, stage)."""
        if not query.strip():
            return [], "empty"
        q = _normalize(query)

        # Stage 1: exact match
        exact = self.find_exact(query)
        if exact:
            return [exact], "exact"

        # Stage 2: prefix
        prefix = []
        for c in self.countries:
            fields = [c.name_ja, c.name_en, c.iso2, c.iso3] + c.aliases
            if any(_normalize(f).startswith(q) for f in fields if f):
                prefix.append(c)
        if prefix:
            prefix = list({c.iso3 or c.name_en: c for c in prefix}.values())
            return prefix[:max_results], "prefix"

        # Stage 3: substring
        substr = []
        for c in self.countries:
            fields = [c.name_ja, c.name_en, c.iso2, c.iso3] + c.aliases
            if any(q in _normalize(f) for f in fields if f):
                substr.append(c)
        if substr:
            substr = list({c.iso3 or c.name_en: c for c in substr}.values())
            return substr[:max_results], "substring"

        # Stage 4: fuzzy
        scored: list[tuple[int, Country]] = []
        for c in self.countries:
            fields = [c.name_ja, c.name_en, c.iso2, c.iso3] + c.aliases
            best = max(fuzz.WRatio(q, _normalize(f)) for f in fields if f)
            if best >= 60:
                scored.append((int(best), c))
        if scored:
            scored.sort(key=lambda x: -x[0])
            seen: dict[str, Country] = {}
            for _, c in scored:
                key = c.iso3 or c.name_en
                if key not in seen:
                    seen[key] = c
                if len(seen) >= max_results:
                    break
            return list(seen.values()), "fuzzy"

        return [], "no_match"
