"""
Sync sections (#16)
===================
A section is the unit of change, of sync and of logging: a group of tables,
scoped to a release or a model where that applies. A scope is stored as
``project``, ``release:<id>`` or ``model:<id>``.

Triggers on the synced tables mark a section dirty, so every writer is covered
without changing each ``save_*`` method. They're added when a project is tracked,
so a project that isn't shared pays nothing for them. The ELF tables have no
triggers, because imports write huge numbers of rows and the tables are keyed by
hash: sync only compares which hashes each side has.
"""
from __future__ import annotations

from dataclasses import dataclass

SCOPE_PROJECT = "project"
SCOPE_RELEASE = "release"
SCOPE_MODEL = "model"

ELF_SECTION = "ELF Data"
ELF_INDEX = "elf_index"
ELF_TABLES = ("elf_functions", "elf_global_vars", "elf_inlined_functions",
              "elf_structures", "elf_symbols")

# project_meta keys that belong to one copy and never travel.
LOCAL_META_KEYS = ("activity_status",)
LOCAL_META_PREFIX = "sync_"
_META_FILTER = ("{a}.key NOT IN ('activity_status') AND {a}.key NOT LIKE 'sync\\_%' ESCAPE '\\'")

_RELEASE = "{a}.release_id = ?"
_MODEL = "{a}.model_id = ?"
_VIA_BUILD = "{a}.test_build_id IN (SELECT id FROM {db}.test_builds WHERE release_id = ?)"


@dataclass(frozen=True)
class Table:
    name: str
    filter: str = ""    # scoped tables only; {a} is the row alias, {db} the schema
    trigger_where: str = ""    # project_meta's local keys
    scope_via_build: bool = False


@dataclass(frozen=True)
class Section:
    kind: str
    name: str
    tables: tuple


def _release_tables(*names: str) -> tuple:
    return tuple(Table(n, _RELEASE) for n in names)


def _model_tables(*names: str) -> tuple:
    return tuple(Table(n, _MODEL) for n in names)


# Children come before parents: a row's filter can read its parent, so the parent
# has to outlast it while deletions are applied.
SECTIONS = (
    Section(SCOPE_RELEASE, "Table", _release_tables("release_rows", "release_column_metadata")),
    Section(SCOPE_RELEASE, "Results", _release_tables("release_results")),
    Section(SCOPE_RELEASE, "Source", _release_tables("release_source_files")),
    Section(SCOPE_RELEASE, "Objects", _release_tables(
        "release_object_config", "release_objects", "release_object_edges",
        "release_object_data_refs", "release_inlined_functions")),
    Section(SCOPE_RELEASE, "Symbols", _release_tables("release_symbol_files")),
    Section(SCOPE_RELEASE, "Code Map", _release_tables("release_code_maps")),
    Section(SCOPE_RELEASE, "AI Maps", _release_tables("ai_release_maps")),
    Section(SCOPE_RELEASE, "Test Builds", (
        Table("build_manifests", _VIA_BUILD, scope_via_build=True),
        Table("tracked_artifacts", _VIA_BUILD, "NEW_OR_OLD.test_build_id IS NOT NULL",
              scope_via_build=True),
        Table("clean_lock_overrides", _RELEASE),
        Table("test_builds", _RELEASE))),
    Section(SCOPE_MODEL, "Architecture", _model_tables(
        "architecture_rows", "model_metadata", "ai_model_mindmaps", "ai_code_diffs")),
    Section(SCOPE_PROJECT, "Settings", (
        Table("project_meta", _META_FILTER,
              "NEW_OR_OLD.key NOT IN ('activity_status') "
              "AND NEW_OR_OLD.key NOT LIKE 'sync\\_%' ESCAPE '\\'"),
        Table("column_layout"), Table("test_case_design"))),
    Section(SCOPE_PROJECT, "Structure", (Table("architecture_models"), Table("releases"))),
    Section(SCOPE_PROJECT, "Test Code", (
        Table("test_project_files"), Table("test_code_injections"), Table("test_projects"))),
    Section(SCOPE_PROJECT, "Test Runs", (
        Table("tracked_artifacts", "{a}.test_build_id IS NULL",
              "NEW_OR_OLD.test_build_id IS NULL"),
        Table("build_run_items"), Table("build_runs"))),
    Section(SCOPE_PROJECT, "History", (Table("history"),)),
)

_BY_NAME = {(s.kind, s.name): s for s in SECTIONS}


def make_scope(kind: str, scope_id=None) -> str:
    return kind if kind == SCOPE_PROJECT else f"{kind}:{scope_id}"


def parse_scope(scope: str) -> tuple:
    """Returns ``(kind, id)``; the id is None for the project scope."""
    if scope == SCOPE_PROJECT:
        return SCOPE_PROJECT, None
    kind, _, scope_id = scope.partition(":")
    return kind, int(scope_id)


def section_for(scope: str, name: str) -> Section:
    return _BY_NAME[(parse_scope(scope)[0], name)]


def _mark_sql(section: Section, table: Table, row: str) -> str:
    upsert = "ON CONFLICT(scope, section) DO UPDATE SET dirty=1"
    label = f"'{section.name}'"
    if table.scope_via_build:
        # A cascade has already removed the parent; test_builds' own trigger
        # covers that case.
        return (f"INSERT INTO sync_sections (scope, section, revision, dirty) "
                f"SELECT 'release:' || release_id, {label}, 0, 1 FROM test_builds "
                f"WHERE id = {row}.test_build_id {upsert};")
    if section.kind == SCOPE_PROJECT:
        scope = "'project'"
    elif section.kind == SCOPE_RELEASE:
        scope = f"'release:' || {row}.release_id"
    else:
        scope = f"'model:' || {row}.model_id"
    return (f"INSERT INTO sync_sections (scope, section, revision, dirty) "
            f"VALUES ({scope}, {label}, 0, 1) {upsert};")


def trigger_statements() -> list:
    statements = []
    for section in SECTIONS:
        for table in section.tables:
            for event, row in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
                name = f"sync_{section.name.replace(' ', '_')}_{table.name}_{event.lower()}"
                when = ""
                if table.trigger_where:
                    when = " WHEN " + table.trigger_where.replace("NEW_OR_OLD", row)
                statements.append(
                    f"CREATE TRIGGER IF NOT EXISTS {name} AFTER {event} ON {table.name}"
                    f"{when} BEGIN {_mark_sql(section, table, row)} END;")
    return statements


def install_triggers(conn) -> None:
    for statement in trigger_statements():
        conn.execute(statement)
    conn.commit()
