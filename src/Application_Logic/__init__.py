"""
Application_Logic package — the Qt-free logic layer (Phase 0 of the pywebview
migration complete: no module in this package imports PyQt6). Qt controllers,
widgets, and dialogs live in the UI package, which is legacy and retires with
the PyQt app in Phase 4.

The names below load on first use, not with the package: the Sync Worker
imports a few small modules from here and mustn't pay for the ELF parsers,
fuzzy matching and crypto they would pull in.
"""

import importlib

_LAZY = {
    "Signaller": ".Logging_Handler",
    "SymbolMatcher": ".Logic_Symbol_Matcher",
    "SecurityManager": ".Logic_Security",
    "HistoryManager": ".Logic_History",
}


def __getattr__(name):
    if name in _LAZY:
        return getattr(importlib.import_module(_LAZY[name], __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
