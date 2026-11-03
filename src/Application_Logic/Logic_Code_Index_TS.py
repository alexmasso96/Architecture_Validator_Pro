"""
Code Index with tree-sitter
===========================
Fills the same ``CodeIndex`` as Logic_Code_Index, but finds functions, globals,
signatures, line ranges and calls with a real C parser instead of patterns.
The regex scan missed functions it couldn't recognise, such as one returning
``const T *``, and AUTOSAR declarations like ``FUNC(void, COM_CODE) Com_Init(...)``.

What a function does with variables (reads, writes, returns, conditions) still
comes from Logic_Code_Index's body analysis, run on the body tree-sitter found,
so the call-tree Test columns judge exactly as before.

Before parsing, "type macros" are expanded: macros whose body is only their own
arguments, ``*`` and C qualifiers, such as ``FUNC(rettype, memclass) rettype``
or ``STATIC static``. They're learned from the project and, when the project
doesn't carry its Compiler.h, taken from the AUTOSAR standard. Line numbers
never move. Other macros are left alone.

Files tree-sitter can't help with fall back to the regex scan: C++ files, and
C files whose parse had errors get the regex scan's functions added for any
function tree-sitter missed.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Dict, List, Optional, Set, Tuple

from . import Logic_Code_Index as ck
from .Logic_Code_Index import CodeIndex, FunctionInfo, GlobalVarInfo

logger = logging.getLogger(__name__)

C_EXTS = (".c", ".h")
CPP_EXTS = (".cpp", ".cc", ".cxx", ".hpp", ".hh")
HEADER_EXTS = (".h", ".hpp", ".hh")

# AUTOSAR Compiler Abstraction (SWS_CompilerAbstraction), for projects whose
# Compiler.h isn't part of the imported source.
AUTOSAR_TYPE_MACROS = {
    "FUNC": (["rettype", "memclass"], "rettype"),
    "FUNC_P2CONST": (["rettype", "ptrclass", "memclass"], "const rettype *"),
    "FUNC_P2VAR": (["rettype", "ptrclass", "memclass"], "rettype *"),
    "P2VAR": (["ptrtype", "memclass", "ptrclass"], "ptrtype *"),
    "P2CONST": (["ptrtype", "memclass", "ptrclass"], "const ptrtype *"),
    "CONSTP2VAR": (["ptrtype", "memclass", "ptrclass"], "ptrtype * const"),
    "CONSTP2CONST": (["ptrtype", "memclass", "ptrclass"], "const ptrtype * const"),
    "VAR": (["vartype", "memclass"], "vartype"),
    "CONST": (["consttype", "memclass"], "const consttype"),
    "STATIC": (None, "static"),
    "INLINE": (None, "inline"),
    "LOCAL_INLINE": (None, "static inline"),
}

_QUALIFIERS = {"const", "volatile", "static", "inline", "extern", "register",
               "restrict", "__inline", "__inline__", "*"}
_DEFINE_RE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)(\(([^)]*)\))?[ \t]*(.*?)[ \t]*$",
                        re.M)
_IDENT_RE = re.compile(r"\b[A-Za-z_]\w*\b")
_TOKEN_RE = re.compile(r"[A-Za-z_]\w*|\*|\S")

_language = None


def available() -> bool:
    """Whether tree-sitter and its C grammar can be loaded."""
    return _c_language() is not None


def _c_language():
    global _language
    if _language is None:
        try:
            import tree_sitter_c
            from tree_sitter import Language
            _language = Language(tree_sitter_c.language())
        except Exception:  # noqa: BLE001 — missing or broken install: use the regex scan
            logger.info("tree-sitter is not available; using the regex source scan",
                        exc_info=True)
            _language = False
    return _language or None


# ---------------------------------------------------------------------------
# Type macros
# ---------------------------------------------------------------------------

def _is_type_body(params: Optional[List[str]], body: str) -> bool:
    tokens = _TOKEN_RE.findall(body)
    allowed = _QUALIFIERS | set(params or ())
    return bool(params) and all(t in allowed for t in tokens) or \
        not params and all(t in _QUALIFIERS for t in tokens)


def learn_type_macros(texts: Dict[str, str]) -> Dict[str, Tuple[Optional[List[str]], str]]:
    """The project's type macros, over the AUTOSAR defaults."""
    macros = dict(AUTOSAR_TYPE_MACROS)
    for text in texts.values():
        if "define" not in text:
            continue
        for m in _DEFINE_RE.finditer(re.sub(r"\\\r?\n", " ", text)):
            name, has_params, params, body = m.groups()
            body = re.sub(r"/\*.*?\*/|//.*$", "", body or "").strip()
            args = [a.strip() for a in params.split(",")] if has_params else None
            if args == [""]:
                args = []
            if _is_type_body(args, body):
                macros[name] = (args, body)
            else:
                macros.pop(name, None)    # the project means something else by it
    return macros


def _split_args(text: str, start: int):
    depth, args, cur = 0, [], []
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "(":
            depth += 1
            if depth > 1:
                cur.append(ch)
        elif ch == ")":
            depth -= 1
            if depth == 0:
                args.append("".join(cur).strip())
                return args, i + 1
            cur.append(ch)
        elif ch == "," and depth == 1:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    return None, start


def expand_type_macros(text: str, macros) -> str:
    """``text`` with its type macros expanded. Each expansion keeps the newlines
    of what it replaced, so every line stays where it was."""
    if not macros or not any(name in text for name in macros):
        return text
    for _ in range(3):    # FUNC(P2VAR(...)) and the like
        out, pos, changed = [], 0, False
        for m in _IDENT_RE.finditer(text):
            name = m.group(0)
            if name not in macros or m.start() < pos:
                continue
            line_start = text.rfind("\n", 0, m.start()) + 1
            if text[line_start:m.start()].lstrip().startswith("#"):
                continue
            params, body = macros[name]
            if params is None:
                end, sub = m.end(), body
            else:
                j = m.end()
                while j < len(text) and text[j] in " \t":
                    j += 1
                if j >= len(text) or text[j] != "(":
                    continue
                args, end = _split_args(text, j)
                if args is None or len(args) != len(params):
                    continue
                sub = re.sub(r"\b[A-Za-z_]\w*\b",
                             lambda t: dict(zip(params, args)).get(t.group(0), t.group(0)), body)
            out.append(text[pos:m.start()])
            out.append(sub + "\n" * text.count("\n", m.start(), end))
            pos, changed = end, True
        out.append(text[pos:])
        text = "".join(out)
        if not changed:
            break
    return text


# ---------------------------------------------------------------------------
# Syntax helpers
# ---------------------------------------------------------------------------

def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _walk(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def _declarator_name(node, src) -> Optional[str]:
    """The name a declarator declares, through pointers, arrays and parentheses."""
    while node is not None:
        if node.type in ("identifier", "field_identifier"):
            return _text(node, src)
        nxt = node.child_by_field_name("declarator")
        if nxt is None:
            ids = [c for c in node.children if c.type == "identifier"]
            if ids:
                return _text(ids[0], src)
            inner = [c for c in node.named_children if c.type.endswith("declarator")]
            if not inner:
                return None
            nxt = inner[0]
        node = nxt
    return None


def _function_declarator(node):
    while node is not None and node.type != "function_declarator":
        node = node.child_by_field_name("declarator")
    return node


def _is_prototype(declarator) -> bool:
    top = declarator.child_by_field_name("declarator") \
        if declarator.type == "pointer_declarator" else declarator
    return top is not None and top.type == "function_declarator"


def _specifiers(node, src) -> Tuple[str, Set[str]]:
    """The type written before a declarator, and its storage classes."""
    words, storage = [], set()
    for c in node.children:
        if c.type == "storage_class_specifier":
            storage.add(_text(c, src))
        elif c.type in ("type_qualifier", "primitive_type", "type_identifier",
                        "sized_type_specifier", "struct_specifier", "enum_specifier",
                        "union_specifier", "macro_type_specifier"):
            words.append(" ".join(_text(c, src).split()))
        elif c.type in ("function_declarator", "pointer_declarator", "init_declarator",
                        "array_declarator", "identifier", "compound_statement", ";", ","):
            break
    return " ".join(words), storage


def _pointer_suffix(declarator, src) -> str:
    """The ``*``s between the type and the name, such as ``const T *f(void)``."""
    stars, node = "", declarator
    while node is not None and node.type == "pointer_declarator":
        stars += "*"
        node = node.child_by_field_name("declarator")
    return f" {stars}" if stars else ""


def _joined(typ: str, name: str) -> str:
    """``T *name`` the way C is usually written, not ``T * name``."""
    return f"{typ}{name}" if typ.endswith("*") else f"{typ} {name}".strip()


def _params(fdecl, src) -> List[Tuple[str, str]]:
    plist = fdecl.child_by_field_name("parameters")
    out = []
    for p in plist.named_children if plist is not None else ():
        if p.type != "parameter_declaration":
            continue
        typ, _storage = _specifiers(p, src)
        d = p.child_by_field_name("declarator")
        name = _declarator_name(d, src) if d is not None else ""
        if d is not None:
            typ += _pointer_suffix(d, src)
        if typ.strip() == "void" and not name:
            continue
        out.append((typ.strip(), name or ""))
    return out


# ---------------------------------------------------------------------------
# One file
# ---------------------------------------------------------------------------

def _file_level(root):
    for n in root.named_children:
        if n.type in ("preproc_ifdef", "preproc_if", "preproc_else", "preproc_elif",
                      "linkage_specification", "declaration_list"):
            yield from _file_level(n)
        else:
            yield n


def _collect_globals(tree, src: bytes, relpath: str) -> List[GlobalVarInfo]:
    found = []
    for node in _file_level(tree.root_node):
        if node.type != "declaration":
            continue
        typ, storage = _specifiers(node, src)
        if "typedef" in storage:
            continue
        for d in node.children_by_field_name("declarator"):
            if _is_prototype(d):
                continue
            name = _declarator_name(d, src)
            if not name:
                continue
            inner = d.child_by_field_name("declarator") if d.type == "init_declarator" else d
            value = d.child_by_field_name("value") if d.type == "init_declarator" else None
            found.append(GlobalVarInfo(
                name=name, var_type=(typ + _pointer_suffix(inner, src)).strip(),
                relpath=relpath, is_static="static" in storage,
                is_const="const" in typ.split(),
                is_array=inner is not None and (inner.type == "array_declarator"
                                                or "[" in _text(inner, src)),
                init_value=" ".join(_text(value, src).split()) if value is not None else ""))
    return found


def _calls(body, src, self_name: str, known_types: Set[str]) -> List[str]:
    """Called names in source order, without casts, keywords or the function itself."""
    calls = []
    for n in _walk(body):
        if n.type != "call_expression":
            continue
        fn = n.child_by_field_name("function")
        if fn is None or fn.type != "identifier":
            continue
        name = _text(fn, src)
        if (name == self_name or name in calls or name in ck.C_KEYWORDS
                or name in known_types or name.startswith("__")):
            continue
        calls.append(name)
    return calls


def _collect_functions(tree, src: bytes, expanded_lines: List[str], original_lines: List[str],
                       relpath: str, known_types: Set[str]) -> List[FunctionInfo]:
    functions = []
    for node in _walk(tree.root_node):
        if node.type != "function_definition":
            continue
        declarator = node.child_by_field_name("declarator")
        fdecl = _function_declarator(declarator)
        body = node.child_by_field_name("body")
        if fdecl is None or body is None:
            continue
        name = _declarator_name(fdecl, src)
        if not name:
            continue
        ret, storage = _specifiers(node, src)
        ret = (ret + _pointer_suffix(declarator, src)).strip() or "int"
        params = _params(fdecl, src)
        param_text = ", ".join(_joined(t, n) for t, n in params) or "void"
        name_line = fdecl.start_point[0]
        body_first, body_last = body.start_point[0], body.end_point[0]
        func = FunctionInfo(name=name)
        func.relpath = relpath
        func.return_type = ret
        func.is_static = "static" in storage
        func.signature = f"{_joined(ret, name)}({param_text})"
        func.params = params
        func.line_start = name_line + 1
        func.line_end = body_last + 1
        func.body = "\n".join(original_lines[body_first:body_last + 1])
        analysis = ck._strip_comments("\n".join(expanded_lines[body_first:body_last + 1]))
        ck._analyze_function_body(func, analysis, known_types)
        func.calls = _calls(body, src, name, known_types)
        functions.append(func)
    return functions


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------

def _parser():
    from tree_sitter import Parser
    return Parser(_c_language())


def file_function_list(content: str, relpath: str) -> List[FunctionInfo]:
    """The functions defined in one file, without building a whole index."""
    if not available() or relpath.lower().endswith(CPP_EXTS):
        return ck.file_function_list(content, relpath)
    macros = learn_type_macros({relpath: content})
    expanded = expand_type_macros(content, macros)
    src = expanded.encode("utf-8")
    tree = _parser().parse(src)
    found = _collect_functions(tree, src, expanded.split("\n"), content.split("\n"),
                               relpath, set())
    if tree.root_node.has_error:
        names = {f.name for f in found}
        found += [f for f in ck.file_function_list(content, relpath) if f.name not in names]
    return found


def build_index(source) -> CodeIndex:
    """Same contract as Logic_Code_Index.build_index: a path or a SourceProvider."""
    from .Logic_Source_Store import as_provider
    provider = as_provider(source)
    index = CodeIndex()
    files = provider.list_files()
    index.all_files = [{"relpath": sf.rel_path, "ext": sf.ext,
                        "name": os.path.basename(sf.rel_path), "size": sf.size}
                       for sf in files]

    # Each file is read once, then everything is learned before functions are
    # analysed, because the body analysis needs every type name.
    texts: Dict[str, str] = {}
    for finfo in index.all_files:
        try:
            content = provider.read_file(finfo["relpath"])
        except (IOError, OSError):
            continue
        if content is not None:
            texts[finfo["relpath"]] = content
    macros = learn_type_macros(texts)

    # Trees aren't kept between the passes: parsing again is cheap, and a large
    # project's trees together would take far more memory than its source.
    parser = _parser()
    expanded_texts: Dict[str, str] = {}
    for finfo in index.all_files:
        relpath, ext = finfo["relpath"], finfo["ext"]
        content = texts.get(relpath)
        if content is None:
            continue
        index.defines.update(ck._extract_defines(content))
        enums, enum_types = ck._extract_enums(content)
        index.enums.update(enums)
        index._known_types.update(enum_types)
        index._known_types.update(ck._extract_typedefs(content))
        if ext in CPP_EXTS:
            for gv in ck._extract_globals(content, relpath):
                _add_global(index, gv)
            continue
        expanded = expand_type_macros(content, macros)
        src = expanded.encode("utf-8")
        for gv in _collect_globals(parser.parse(src), src, relpath):
            _add_global(index, gv)
        expanded_texts[relpath] = expanded

    for finfo in index.all_files:
        relpath, ext = finfo["relpath"], finfo["ext"]
        content = texts.get(relpath)
        if content is None:
            continue
        if ext in CPP_EXTS:
            funcs = (ck._extract_functions(content, relpath, index._known_types)
                     if ext not in HEADER_EXTS else [])
        else:
            expanded = expanded_texts.pop(relpath)
            src = expanded.encode("utf-8")
            tree = parser.parse(src)
            funcs = _collect_functions(tree, src, expanded.split("\n"), content.split("\n"),
                                       relpath, index._known_types)
            if tree.root_node.has_error and ext not in HEADER_EXTS:
                names = {f.name for f in funcs}
                funcs += [f for f in ck._extract_functions(content, relpath, index._known_types)
                          if f.name not in names]
        for func in funcs:
            _add_function(index, func, relpath)
    return index


def _add_global(index: CodeIndex, gv: GlobalVarInfo) -> None:
    # A definition beats an extern declaration of the same name.
    existing = index.globals.get(gv.name)
    if existing is not None and existing.relpath.endswith(HEADER_EXTS) \
            and not gv.relpath.endswith(HEADER_EXTS):
        existing = None
    if existing is None:
        index.globals[gv.name] = gv
        index.file_globals.setdefault(gv.relpath, []).append(gv.name)


def _add_function(index: CodeIndex, func: FunctionInfo, relpath: str) -> None:
    # A header's inline function doesn't replace the definition in a .c file.
    existing = index.functions.get(func.name)
    if existing is not None and not existing.relpath.endswith(HEADER_EXTS) \
            and relpath.endswith(HEADER_EXTS):
        return
    index.functions[func.name] = func
    index.call_graph[func.name] = func.calls
    for callee in func.calls:
        callers = index.reverse_call_graph.setdefault(callee, [])
        if func.name not in callers:
            callers.append(func.name)
    index.file_functions.setdefault(relpath, []).append(func.name)
