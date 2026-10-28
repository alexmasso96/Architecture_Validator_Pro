"""
Symbol Matcher Module
=====================
Handles fuzzy matching between architecture port names and ELF symbols.
Uses rapidfuzz for scoring and ranking matches.
"""

from __future__ import annotations

from typing import List, Tuple, Dict, Optional
from functools import lru_cache

from rapidfuzz import fuzz, process
import logging

from core.elf_parser import ELFParser

logger = logging.getLogger(__name__)


class SymbolMatcher:
    """
    Handles fuzzy matching between architecture port names and ELF symbols.
    Loads only symbol *names* into RAM — avoids the 7 GB spike from full objects.
    """

    def __init__(self, parser: ELFParser, db=None, elf_hash=None,
                 macros: Optional[Dict[str, List[str]]] = None) -> None:
        self.parser: ELFParser = parser
        # Names that exist only as source #define macros (no ELF symbol), and
        # names only seen in the Code Map's call tree (no symbol, no #define
        # found — e.g. a macro from a header that wasn't imported).
        self.macro_names: set = set()
        self.call_target_names: set = set()

        if db is not None and (elf_hash or macros):
            # DB-backed path — load only name strings, not full objects. A list
            # of hashes (a release's component symbol files) is one union pool;
            # a name defined in several components is offered once.
            self.all_function_names: List[str] = db.get_function_names(elf_hash)
            self.all_variable_names: List[str] = db.get_variable_names(elf_hash)
            # #6D: functions that exist only as inline expansions (no symbol,
            # no address) still belong in the match pool — an architecture
            # port whose function was inlined is NOT missing from the code.
            self.inlined_names = set(
                db.get_inlined_only_function_names(elf_hash))
            self.all_function_names = (
                self.all_function_names + sorted(self.inlined_names))
            # Macros from the release's source (Logic_Source_Macros): generated
            # accessors like `#define cfgGet…_u16()` never reach the ELF but are
            # real call targets. Function-like → function pool, object-like →
            # variable pool; an ELF symbol of the same name takes precedence.
            if macros:
                known = set(self.all_function_names) | set(self.all_variable_names)
                fmac = [m for m in macros.get("functions", []) if m not in known]
                omac = [m for m in macros.get("objects", []) if m not in known]
                self.all_function_names = self.all_function_names + fmac
                self.all_variable_names = self.all_variable_names + omac
                self.macro_names = set(fmac) | set(omac)
                # Anything visible in the call tree is a call target, so it
                # joins the function pool even when no definition was found.
                known |= self.macro_names
                calls = [n for n in macros.get("call_targets", []) if n not in known]
                self.all_function_names = self.all_function_names + calls
                self.call_target_names = set(calls)
        else:
            # Legacy in-memory path
            self.all_function_names = [f.name for f in parser.functions]
            self.all_variable_names = list(parser.global_vars_dwarf.keys())
            self.inlined_names = set()

        self.search_pool: List[str] = self.all_function_names + self.all_variable_names

    def find_best_match(self, target_name: str,
                        threshold: int = 70) -> Tuple[Optional[str], int]:
        if not target_name or not self.search_pool:
            return None, 0
        return self._cached_best_match(target_name, threshold)

    @lru_cache(maxsize=512)
    def _cached_best_match(self, target_name: str,
                           threshold: int) -> Tuple[Optional[str], int]:
        result = process.extractOne(
            target_name, self.search_pool, scorer=fuzz.token_sort_ratio
        )
        if result:
            matched_name, score, _ = result
            score = int(score)
            if score >= threshold:
                return matched_name, score
        return None, 0

    def find_top_matches(self, target_name: str,
                         limit: int = 20) -> List[Tuple[str, int]]:
        if not target_name or not self.search_pool:
            return []
        return self._cached_top_matches(target_name, limit)

    @lru_cache(maxsize=512)
    def _cached_top_matches(self, target_name: str,
                            limit: int) -> List[Tuple[str, int]]:
        raw = process.extract(
            target_name, self.search_pool,
            scorer=fuzz.token_sort_ratio,
            limit=limit
        )
        return [(m, int(s)) for m, s, _ in raw]

    @lru_cache(maxsize=512)
    def find_top_function_matches(self, target_name: str, limit: int = 10) -> List[Tuple[str, int]]:
        if not target_name or not getattr(self, 'all_function_names', None):
            return []
        raw = process.extract(
            target_name, self.all_function_names,
            scorer=fuzz.token_sort_ratio,
            limit=limit
        )
        return [(m, int(s)) for m, s, _ in raw]

    @lru_cache(maxsize=512)
    def find_top_variable_matches(self, target_name: str, limit: int = 10) -> List[Tuple[str, int]]:
        if not target_name or not getattr(self, 'all_variable_names', None):
            return []
        raw = process.extract(
            target_name, self.all_variable_names,
            scorer=fuzz.token_sort_ratio,
            limit=limit
        )
        return [(m, int(s)) for m, s, _ in raw]

    def get_matches_for_list(self, port_list: List[str],
                             threshold: int = 70) -> Dict[str, Tuple[Optional[str], int]]:
        results = {}
        for port in port_list:
            results[port] = self.find_best_match(port, threshold)
        return results


# Column logic_key → matcher method. "Port Search" matches against both pools
# (functions + variables); the typed search columns match their own pool.
_SEARCH_KIND_BY_LOGIC = {
    "Port Search": "any",
    "Function Search": "function",
    "Variable Search": "variable",
}


def search_specs_from_layout(layout) -> "List[Tuple[str, str, str]]":
    """From a column layout ``[(name, logic_key, visible, width), …]`` derive the
    ``(search_col, match_col, kind)`` specs: each search column's fuzzy result
    lives in the immediately-following column. Mirrors the table's eager-match.
    """
    specs = []
    for i, col in enumerate(layout):
        logic_key = col[1]
        kind = _SEARCH_KIND_BY_LOGIC.get(logic_key)
        if kind and i + 1 < len(layout):
            specs.append((col[0], layout[i + 1][0], kind))
    return specs


def rematch_rows(rows, specs, matcher, limit: int = 10) -> int:
    """Re-run fuzzy matching for every search column in ``rows`` and write the
    best ``"Name (NN%)"`` into the adjacent (Match) cell's ``widget_text``.
    Pure (Qt-free); returns the number of cells updated. Mirrors the Qt
    ``_match_model_data_inplace`` minus the widget styling.

    A Match cell the user picked by hand (``user_changed``) is kept: re-runs
    happen automatically (e.g. after a source import) and must never replace
    a tester's choice.
    """
    changed = 0
    for row in rows:
        for search_col, match_col, kind in specs:
            existing = row.get(match_col)
            if isinstance(existing, dict) and existing.get("user_changed"):
                continue
            cell = row.get(search_col)
            text = (cell.get("text", "") if isinstance(cell, dict) else "").strip()
            if not text:
                continue
            if kind == "function":
                matches = matcher.find_top_function_matches(text, limit=limit)
            elif kind == "variable":
                matches = matcher.find_top_variable_matches(text, limit=limit)
            else:
                matches = matcher.find_top_matches(text, limit=limit)
            if not matches:
                continue
            best_name, best_score = matches[0]
            target = row.setdefault(match_col, {"text": ""})
            target["widget_text"] = f"{best_name} ({best_score}%)"
            changed += 1
    return changed
