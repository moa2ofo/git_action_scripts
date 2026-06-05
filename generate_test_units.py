#!/usr/bin/env python3
"""
generate_test_units_ast_first.py

AST-first redesign of generate_test_units.py.

Main idea:
- libclang is the source of truth for functions, types, parameters, variables,
  call expressions, source ranges and include graph.
- classic text parsing is used only as best effort for areas that libclang exposes
  poorly in the Python bindings, mainly local #define preservation, Doxygen fallback,
  and limited source cleanup for generated files.

This file intentionally keeps the generated package concept compatible with the
previous script, while making the analysis/generation flow more modular.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from clang.cindex import (
    CompilationDatabase,
    CompilationDatabaseError,
    Config,
    Cursor,
    CursorKind,
    Diagnostic,
    Index,
    SourceLocation,
    SourceRange,
    StorageClass,
    TranslationUnit,
    Type,
    TypeKind,
)

from path_config_loader import load_paths


DOXY_BLOCK_START = "/**"
DOXY_BLOCK_END = "*/"
DOXY_LINE_PREFIXES = ("///", "//!")

TEXT_PROTECTED_PATTERN = re.compile(
    r"""
    /\*.*?\*/            |
    //.*?$               |
    "(?:\\.|[^"\\])*"    |
    '(?:\\.|[^'\\])*'
    """,
    re.DOTALL | re.MULTILINE | re.VERBOSE,
)


# =============================================================================
# Data model
# =============================================================================


@dataclass(frozen=True)
class CliOptions:
    workspace_root: Path
    out_root: Optional[Path]
    compile_db: Optional[Path]
    clang_args: Tuple[str, ...]
    force: bool
    dry_run: bool
    fail_on_clang_errors: bool
    verbose: bool


@dataclass(frozen=True)
class ProjectConfig:
    workspace_root: Path
    out_root: Path
    scan_roots: Tuple[Path, ...]


@dataclass(frozen=True)
class DiagnosticReport:
    diagnostics: Tuple[Diagnostic, ...]

    @property
    def errors(self) -> Tuple[Diagnostic, ...]:
        return tuple(d for d in self.diagnostics if d.severity >= Diagnostic.Error)

    @property
    def warnings(self) -> Tuple[Diagnostic, ...]:
        return tuple(d for d in self.diagnostics if d.severity == Diagnostic.Warning)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)


@dataclass(frozen=True)
class SourceIdentity:
    path: Path
    relative_key: str

    @property
    def safe_key(self) -> str:
        base = sanitize_identifier(self.relative_key.replace(".c", ""))
        short_hash = hashlib.sha1(str(self.path).encode("utf-8")).hexdigest()[:8]
        return f"{base}_{short_hash}"


@dataclass(frozen=True)
class PackageNames:
    package_dir_name: str
    symbol_prefix: str
    header_name: str
    helper_header_name: str
    source_name: str
    test_name: str
    include_guard: str
    helper_include_guard: str


@dataclass(frozen=True)
class ParameterModel:
    name: str
    type_spelling: str


@dataclass(frozen=True)
class VariableModel:
    usr: str
    name: str
    type_spelling: str
    cursor: Cursor
    source_file: Path
    storage_class: StorageClass
    is_static: bool
    is_const: bool
    is_array: bool
    array_element_type: Optional[str]
    array_count: Optional[int]
    source_text: str


@dataclass(frozen=True)
class FunctionModel:
    cursor: Cursor
    name: str
    source_file: Path
    source_identity: SourceIdentity
    return_type: str
    parameters: Tuple[ParameterModel, ...]
    is_variadic: bool
    storage_class: StorageClass
    source_text: str
    body_text: str
    prototype: str
    raw_comment: Optional[str]
    package_names: PackageNames


@dataclass(frozen=True)
class FunctionAnalysis:
    called_functions: Tuple[str, ...]
    used_globals: Tuple[VariableModel, ...]
    used_static_globals: Tuple[VariableModel, ...]
    used_define_texts: Tuple[str, ...]


@dataclass(frozen=True)
class HeaderCopy:
    source_path: Path
    output_name: str


@dataclass(frozen=True)
class HeaderCopyPlan:
    headers: Tuple[HeaderCopy, ...]
    include_rewrite_map: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TranslationUnitContext:
    c_path: Path
    tu: TranslationUnit
    diagnostics: DiagnosticReport
    globals_by_usr: Dict[str, VariableModel]
    local_defines: Dict[str, str]
    needed_headers: Tuple[Path, ...]


@dataclass(frozen=True)
class GenerationContext:
    project: ProjectConfig
    options: CliOptions
    tu_context: TranslationUnitContext
    function: FunctionModel
    analysis: FunctionAnalysis
    header_plan: HeaderCopyPlan


# =============================================================================
# Filesystem and string helpers
# =============================================================================


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def write_text(path: Path, text: str, *, dry_run: bool = False) -> None:
    if dry_run:
        print(f"[DRY-RUN] would write {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def sanitize_identifier(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-z_]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    if not value:
        value = "unit"
    if value[0].isdigit():
        value = f"_{value}"
    return value


def is_in_test_dir(path: Path) -> bool:
    return any(part.startswith("TEST_") for part in path.parts)


def is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def is_under_any(path: Path, roots: Sequence[Path]) -> bool:
    return any(is_under(path, root) for root in roots)


def relative_key_for(path: Path, roots: Sequence[Path]) -> str:
    resolved = path.resolve()
    for root in roots:
        try:
            return ""#resolved.relative_to(root.resolve()).as_posix()
        except Exception:
            continue
    return ""#resolved.name


def text_from_extent(extent: SourceRange) -> str:
    if not extent.start.file:
        return ""
    src_path = Path(extent.start.file.name)
    src = read_text(src_path)
    lines = src.splitlines(keepends=True)

    def absolute_index(loc: SourceLocation) -> int:
        line_idx = max(loc.line - 1, 0)
        col_idx = max(loc.column - 1, 0)
        return sum(len(lines[i]) for i in range(min(line_idx, len(lines)))) + col_idx

    start = absolute_index(extent.start)
    end = absolute_index(extent.end)
    return src[start:end]


def list_c_files(roots: Sequence[Path]) -> List[Path]:
    files: List[Path] = []
    for root in roots:
        if root.is_dir():
            files.extend(p for p in root.rglob("*.c") if p.is_file() and not is_in_test_dir(p))
    return sorted(files)


# =============================================================================
# CLI/configuration
# =============================================================================


def parse_cli(argv: Optional[Sequence[str]] = None) -> CliOptions:
    parser = argparse.ArgumentParser(
        description="Generate C unit-test packages using libclang AST analysis first, text parsing only as best effort."
    )
    parser.add_argument("root", help="Workspace root path used by the project path configuration.")
    parser.add_argument("--out-root", default=None, help="Output unit-test root. Defaults to path_config_loader value.")
    parser.add_argument("--compile-db", default=None, help="Path to compile_commands.json or its directory.")
    parser.add_argument("--force", action="store_true", help="Regenerate src files even when package already exists.")
    parser.add_argument("--dry-run", action="store_true", help="Print intended writes without creating/modifying files.")
    parser.add_argument("--fail-on-clang-errors", action="store_true", help="Skip generation for a TU when libclang reports errors.")
    parser.add_argument("--verbose", action="store_true", help="Print additional analysis details.")

    args, extra_clang = parser.parse_known_args(argv)
    return CliOptions(
        workspace_root=Path(args.root).resolve(),
        out_root=Path(args.out_root).resolve() if args.out_root else None,
        compile_db=Path(args.compile_db).resolve() if args.compile_db else None,
        clang_args=tuple(extra_clang),
        force=args.force,
        dry_run=args.dry_run,
        fail_on_clang_errors=args.fail_on_clang_errors,
        verbose=args.verbose,
    )


def load_project_config(options: CliOptions) -> ProjectConfig:
    paths = load_paths(__file__)
    out_root = options.out_root if options.out_root else Path(paths.unit_test_root).resolve()
    scan_roots = (
        Path(paths.sw_cmp_repo_pltf_dir).resolve(),
        Path(paths.sw_cmp_repo_cfg_dir).resolve(),
    )
    return ProjectConfig(
        workspace_root=options.workspace_root,
        out_root=out_root,
        scan_roots=scan_roots,
    )


# =============================================================================
# libclang parsing and AST extraction
# =============================================================================


def build_default_clang_args(project: ProjectConfig, extra: Sequence[str]) -> List[str]:
    args = ["-std=c11"]
    args.extend(f"-I{root}" for root in project.scan_roots)
    args.append(f"-I{project.workspace_root}")
    args.extend(extra)
    return args


def load_compile_database(path: Optional[Path]) -> Optional[CompilationDatabase]:
    if not path:
        return None
    db_dir = path if path.is_dir() else path.parent
    try:
        return CompilationDatabase.fromDirectory(str(db_dir))
    except CompilationDatabaseError as exc:
        print(f"[WARN] unable to load compile database from {db_dir}: {exc}")
        return None


def clang_args_for_file(
    c_path: Path,
    project: ProjectConfig,
    options: CliOptions,
    compile_db: Optional[CompilationDatabase],
) -> List[str]:
    default_args = build_default_clang_args(project, options.clang_args)
    if not compile_db:
        return default_args

    try:
        commands = list(compile_db.getCompileCommands(str(c_path)))
    except Exception as exc:
        print(f"[WARN] compile database lookup failed for {c_path}: {exc}")
        return default_args

    if not commands:
        return default_args

    # command.arguments usually contains compiler executable and source file too.
    # Keep flags, remove executable and the current source path when obvious.
    raw = list(commands[0].arguments)
    filtered: List[str] = []
    for idx, arg in enumerate(raw):
        if idx == 0:
            continue
        if Path(arg).name == c_path.name or arg == str(c_path):
            continue
        if arg in {"-c", "-o"}:
            continue
        if filtered and filtered[-1] == "-o":
            continue
        filtered.append(arg)

    # User-provided extra args should win by being appended last.
    filtered.extend(options.clang_args)
    return filtered or default_args


def parse_translation_unit(index: Index, c_path: Path, clang_args: Sequence[str]) -> TranslationUnit:
    return index.parse(
        str(c_path),
        args=list(clang_args),
        options=TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
    )


def diagnostic_report(tu: TranslationUnit) -> DiagnosticReport:
    return DiagnosticReport(tuple(tu.diagnostics))


def print_diagnostics(c_path: Path, report: DiagnosticReport) -> None:
    for diag in report.diagnostics:
        prefix = "ERROR" if diag.severity >= Diagnostic.Error else "WARN"
        print(f"[{prefix}] {c_path}: {diag}")


def walk(cursor: Cursor) -> Iterator[Cursor]:
    yield cursor
    for child in cursor.get_children():
        yield from walk(child)


def cursor_file(cursor: Cursor) -> Optional[Path]:
    try:
        if cursor.location and cursor.location.file:
            return Path(cursor.location.file.name).resolve()
    except Exception:
        return None
    return None


def cursor_usr(cursor: Cursor) -> str:
    usr = cursor.get_usr()
    if usr:
        return usr
    file_part = cursor_file(cursor) or Path("<unknown>")
    return f"{cursor.spelling}@{file_part}:{cursor.location.line}:{cursor.location.column}"


def is_translation_unit_global_var(cursor: Cursor) -> bool:
    return (
        cursor.kind == CursorKind.VAR_DECL
        and cursor.semantic_parent is not None
        and cursor.semantic_parent.kind == CursorKind.TRANSLATION_UNIT
    )


def is_const_qualified(clang_type: Type) -> bool:
    try:
        return bool(clang_type.is_const_qualified())
    except Exception:
        return False


def is_array_type(clang_type: Type) -> bool:
    return clang_type.kind in {
        TypeKind.CONSTANTARRAY,
        TypeKind.INCOMPLETEARRAY,
        TypeKind.VARIABLEARRAY,
        TypeKind.DEPENDENTSIZEDARRAY,
    }


def array_count_or_none(clang_type: Type) -> Optional[int]:
    try:
        if clang_type.kind == TypeKind.CONSTANTARRAY:
            return int(clang_type.element_count)
    except Exception:
        return None
    return None


def make_variable_model(cursor: Cursor) -> VariableModel:
    source_file = cursor_file(cursor) or Path("<unknown>")
    clang_type = cursor.type
    is_arr = is_array_type(clang_type)
    elem = clang_type.element_type.spelling if is_arr else None
    return VariableModel(
        usr=cursor_usr(cursor),
        name=cursor.spelling,
        type_spelling=clang_type.spelling,
        cursor=cursor,
        source_file=source_file,
        storage_class=cursor.storage_class,
        is_static=cursor.storage_class == StorageClass.STATIC,
        is_const=is_const_qualified(clang_type),
        is_array=is_arr,
        array_element_type=elem,
        array_count=array_count_or_none(clang_type),
        source_text=text_from_extent(cursor.extent).strip(),
    )


def collect_translation_unit_globals(tu_cursor: Cursor) -> Dict[str, VariableModel]:
    globals_by_usr: Dict[str, VariableModel] = {}
    for child in tu_cursor.get_children():
        if is_translation_unit_global_var(child):
            var = make_variable_model(child)
            globals_by_usr[var.usr] = var
    return globals_by_usr


def function_prototype_from_ast(fn: Cursor) -> str:
    ret = fn.result_type.spelling if fn.result_type else "void"
    params: List[str] = []
    for index, param in enumerate(fn.get_arguments()):
        p_name = param.spelling or f"param{index}"
        params.append(f"{param.type.spelling} {p_name}")
    if fn.type.kind == TypeKind.FUNCTIONPROTO and fn.type.is_function_variadic():
        params.append("...")
    param_str = ", ".join(params) if params else "void"
    return f"{ret} {fn.spelling}({param_str});"


def source_identity_for(c_path: Path, project: ProjectConfig) -> SourceIdentity:
    return SourceIdentity(path=c_path.resolve(), relative_key=relative_key_for(c_path, project.scan_roots))


def package_names_for(fn_name: str, identity: SourceIdentity) -> PackageNames:
    prefix = sanitize_identifier(f"{identity.safe_key}_{fn_name}")
    upper = prefix.upper()
    return PackageNames(
        package_dir_name=f"TEST_{prefix}",
        symbol_prefix=prefix,
        header_name=f"{prefix}.h",
        helper_header_name=f"{prefix}_help.h",
        source_name=f"{prefix}.c",
        test_name=f"test_{prefix}.c",
        include_guard=f"TEST_{upper}_H",
        helper_include_guard=f"TEST_{upper}_HELP_H",
    )


def get_doxygen_comment(fn: Cursor) -> Optional[str]:
    raw = getattr(fn, "raw_comment", None)
    if raw:
        return raw
    return get_doxygen_comment_text_fallback(fn)


def make_function_model(fn: Cursor, c_path: Path, project: ProjectConfig) -> FunctionModel:
    identity = source_identity_for(c_path, project)
    params = tuple(
        ParameterModel(name=p.spelling or f"param{i}", type_spelling=p.type.spelling)
        for i, p in enumerate(fn.get_arguments())
    )
    source_text = text_from_extent(fn.extent)
    return FunctionModel(
        cursor=fn,
        name=fn.spelling,
        source_file=c_path.resolve(),
        source_identity=identity,
        return_type=fn.result_type.spelling if fn.result_type else "void",
        parameters=params,
        is_variadic=fn.type.kind == TypeKind.FUNCTIONPROTO and fn.type.is_function_variadic(),
        storage_class=fn.storage_class,
        source_text=source_text,
        body_text=extract_compound_body_text(fn),
        prototype=function_prototype_from_ast(fn),
        raw_comment=get_doxygen_comment(fn),
        package_names=package_names_for(fn.spelling, identity),
    )


def collect_functions_in_file(tu: TranslationUnit, c_path: Path, project: ProjectConfig) -> Tuple[FunctionModel, ...]:
    result: List[FunctionModel] = []
    resolved_c = c_path.resolve()
    for cur in tu.cursor.get_children():
        if cur.kind != CursorKind.FUNCTION_DECL or not cur.is_definition():
            continue
        if cursor_file(cur) != resolved_c:
            continue
        result.append(make_function_model(cur, resolved_c, project))
    return tuple(result)


def extract_compound_body_text(fn: Cursor) -> str:
    for child in fn.get_children():
        if child.kind == CursorKind.COMPOUND_STMT:
            return text_from_extent(child.extent)
    return ""


def classify_referenced_global(ref: Cursor, globals_by_usr: Dict[str, VariableModel]) -> Optional[VariableModel]:
    if ref is None or ref.kind != CursorKind.VAR_DECL:
        return None
    usr = cursor_usr(ref)
    if usr in globals_by_usr:
        return globals_by_usr[usr]
    # Fallback when USR is unstable across cursors.
    for model in globals_by_usr.values():
        if model.name == ref.spelling and model.source_file == cursor_file(ref):
            return model
    return None


def analyze_function_ast(
    fn: FunctionModel,
    globals_by_usr: Dict[str, VariableModel],
    local_defines: Dict[str, str],
) -> FunctionAnalysis:
    calls: Set[str] = set()
    used_globals: Dict[str, VariableModel] = {}
    used_static: Dict[str, VariableModel] = {}

    for node in walk(fn.cursor):
        if node.kind == CursorKind.CALL_EXPR:
            target = called_function_target(node)
            if target is not None and target.spelling:
                calls.add(target.spelling)

        if node.kind == CursorKind.DECL_REF_EXPR and node.referenced:
            var = classify_referenced_global(node.referenced, globals_by_usr)
            if var is not None:
                if var.is_static:
                    used_static[var.usr] = var
                else:
                    used_globals[var.usr] = var

    used_define_texts = tuple(collect_used_defines_in_function(fn.source_text, local_defines))
    return FunctionAnalysis(
        called_functions=tuple(sorted(calls)),
        used_globals=tuple(sorted(used_globals.values(), key=lambda v: v.name)),
        used_static_globals=tuple(sorted(used_static.values(), key=lambda v: v.name)),
        used_define_texts=used_define_texts,
    )


def called_function_target(call_expr: Cursor) -> Optional[Cursor]:
    if call_expr.referenced is not None and call_expr.referenced.kind == CursorKind.FUNCTION_DECL:
        return call_expr.referenced
    for child in call_expr.get_children():
        if child.referenced is not None and child.referenced.kind == CursorKind.FUNCTION_DECL:
            return child.referenced
    return None


# =============================================================================
# Best-effort textual support
# =============================================================================


def collect_local_defines(c_path: Path) -> Dict[str, str]:
    """Best-effort #define collector.

    libclang's Python bindings are less ergonomic for faithfully preserving original
    macro text. This function intentionally stays text-based, but only for local
    macro preservation, not for C semantic analysis.
    """
    lines = read_text(c_path).splitlines()
    defines: Dict[str, str] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"^\s*#\s*define\b", line):
            block = [line]
            while block[-1].rstrip().endswith("\\") and i + 1 < len(lines):
                i += 1
                block.append(lines[i])
            match = re.match(r"^\s*#\s*define\s+([A-Za-z_]\w*)\b", block[0])
            if match:
                defines[match.group(1)] = "\n".join(block)
        i += 1
    return defines


def extract_define_dependencies(define_text: str, all_names: Set[str], self_name: str) -> Set[str]:
    deps: Set[str] = set()
    protected, chunks = protect_text_literals_and_comments(define_text)
    for name in all_names:
        if name == self_name:
            continue
        if re.search(rf"\b{re.escape(name)}\b", protected):
            deps.add(name)
    _ = chunks
    return deps


def collect_used_defines_in_function(function_text: str, local_defines: Dict[str, str]) -> List[str]:
    all_names = set(local_defines.keys())
    protected, chunks = protect_text_literals_and_comments(function_text)
    _ = chunks

    directly_used = {
        name for name in local_defines if re.search(rf"\b{re.escape(name)}\b", protected)
    }
    needed = set(directly_used)
    changed = True
    while changed:
        changed = False
        for name in list(needed):
            deps = extract_define_dependencies(local_defines[name], all_names, name)
            new_deps = deps - needed
            if new_deps:
                needed.update(new_deps)
                changed = True

    return [text for name, text in local_defines.items() if name in needed]


def protect_text_literals_and_comments(text: str) -> Tuple[str, List[str]]:
    chunks: List[str] = []

    def repl(match: re.Match[str]) -> str:
        chunks.append(match.group(0))
        return f"__PROTECTED_{len(chunks) - 1}__"

    return TEXT_PROTECTED_PATTERN.sub(repl, text), chunks


def get_doxygen_comment_text_fallback(fn: Cursor) -> Optional[str]:
    loc = fn.extent.start
    if not loc.file:
        return None

    src_path = Path(loc.file.name)
    try:
        lines = read_text(src_path).splitlines()
    except Exception:
        return None

    i = loc.line - 2
    if i < 0:
        return None

    j = i
    while j >= 0 and lines[j].strip() == "":
        j -= 1

    if j >= 0 and lines[j].rstrip().endswith(DOXY_BLOCK_END):
        k = j
        while k >= 0:
            if DOXY_BLOCK_START in lines[k]:
                block = lines[k : j + 1]
                return "\n".join(block)
            k -= 1

    if j >= 0 and lines[j].lstrip().startswith(DOXY_LINE_PREFIXES):
        block: List[str] = []
        while j >= 0 and lines[j].lstrip().startswith(DOXY_LINE_PREFIXES):
            block.append(lines[j])
            j -= 1
        return "\n".join(reversed(block))

    return None


def remove_leading_function_storage_qualifiers(function_text: str) -> str:
    """Best-effort source cleanup for copied function definitions.

    The decision that this text is a function is AST-based. This helper only removes
    leading static/inline tokens from the already-selected function extent.
    """
    return re.sub(r"^\s*(?:(?:static|inline)\s+)+", "", function_text, count=1)


def remove_function_prototype_text_best_effort(header_text: str, func_name: str) -> str:
    """Best-effort fallback for copied headers.

    Prefer not to understand C with regex. This only removes simple prototypes of
    the function-under-test from copied project headers to avoid duplicate symbols.
    """
    pattern = re.compile(
        r"(^|\n)\s*(?:[A-Za-z_]\w*\s+)*(?:[A-Za-z_]\w*\s*[\*\s]+)?"
        + re.escape(func_name)
        + r"\s*\([^;{}]*\)\s*;\s*(?=\n|$)",
        re.DOTALL,
    )
    return pattern.sub(r"\1", header_text)


def strip_function_keywords_in_copied_header_best_effort(header_text: str) -> str:
    """Best-effort cleanup for copied headers only.

    This is intentionally narrower than the old implementation: it only touches
    simple one-line function declarations/definitions that start with static/inline.
    """
    protected, chunks = protect_text_literals_and_comments(header_text)
    pattern = re.compile(
        r"""
        ^(?P<prefix>[ \t]*)
        (?P<quals>(?:(?:static|inline)[ \t]+)+)
        (?P<rest>
            [A-Za-z_][\w\s\*\(\)]*?
            [ \t]+[A-Za-z_]\w*
            [ \t]*\([^;{}]*\)
            [ \t]*(?:;|\{)
        )
        """,
        re.MULTILINE | re.VERBOSE,
    )
    cleaned = pattern.sub(lambda m: f"{m.group('prefix')}{m.group('rest')}", protected)

    def restore(match: re.Match[str]) -> str:
        return chunks[int(match.group(1))]

    return re.sub(r"__PROTECTED_(\d+)__", restore, cleaned)


# =============================================================================
# Include graph and header-copy plan
# =============================================================================


def collect_needed_project_headers(
    tu: TranslationUnit,
    start_c_path: Path,
    project_roots: Sequence[Path],
) -> Tuple[Path, ...]:
    start = start_c_path.resolve()
    roots = tuple(root.resolve() for root in project_roots)
    adjacency: Dict[Path, Set[Path]] = {}

    for inc in tu.get_includes():
        try:
            src = Path(inc.source.name).resolve()
            dst = Path(inc.include.name).resolve()
        except Exception:
            continue
        if not is_under_any(dst, roots):
            continue
        if src != start and not is_under_any(src, roots):
            continue
        adjacency.setdefault(src, set()).add(dst)

    needed: Set[Path] = set()
    stack = list(adjacency.get(start, set()))
    seen: Set[Path] = set()
    while stack:
        header = stack.pop()
        if header in seen:
            continue
        seen.add(header)
        needed.add(header)
        stack.extend(h for h in adjacency.get(header, set()) if h not in seen)

    return tuple(sorted(needed))


def build_header_copy_plan(headers: Sequence[Path], project_roots: Sequence[Path]) -> HeaderCopyPlan:
    basename_counts: Dict[str, int] = {}
    for header in headers:
        basename_counts[header.name] = basename_counts.get(header.name, 0) + 1

    copies: List[HeaderCopy] = []
    rewrite_map: Dict[str, str] = {}
    for header in headers:
        if basename_counts[header.name] == 1:
            out_name = header.name
        else:
            rel = relative_key_for(header, project_roots)
            out_name = f"{sanitize_identifier(rel.replace('.h', ''))}.h"
        copies.append(HeaderCopy(source_path=header, output_name=out_name))
        rewrite_map[header.name] = out_name
        rewrite_map[header.as_posix()] = out_name

    return HeaderCopyPlan(headers=tuple(copies), include_rewrite_map=rewrite_map)


def rewrite_local_includes_best_effort(text: str, rewrite_map: Dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        prefix, name, suffix = match.group(1), match.group(2), match.group(3)
        return f'{prefix}"{rewrite_map.get(name, name)}"{suffix}'

    return re.sub(r'(^\s*#\s*include\s*)"([^"]+)"(.*$)', repl, text, flags=re.MULTILINE)


# =============================================================================
# Code generation
# =============================================================================


def package_dir(ctx: GenerationContext) -> Path:
    return ctx.project.out_root / ctx.function.package_names.package_dir_name


def package_src_dir(ctx: GenerationContext) -> Path:
    return package_dir(ctx) / "src"


def package_test_dir(ctx: GenerationContext) -> Path:
    return package_dir(ctx) / "test"


def generated_test_path(ctx: GenerationContext) -> Path:
    return package_test_dir(ctx) / ctx.function.package_names.test_name


def package_src_exists_and_nonempty(ctx: GenerationContext) -> bool:
    src = package_src_dir(ctx)
    return src.exists() and any(src.iterdir())


def generate_function_header(ctx: GenerationContext) -> str:
    fn = ctx.function
    analysis = ctx.analysis
    names = fn.package_names

    needs_stddef = any(var.is_array for var in analysis.used_static_globals)
    needs_string = any(
        var.is_array and not var.is_const and var.array_count is not None
        for var in analysis.used_static_globals
    )

    lines: List[str] = [
        f"#ifndef {names.include_guard}",
        f"#define {names.include_guard}",
        "",
    ]

    for copied_header in ctx.header_plan.headers:
        lines.append(f'#include "{copied_header.output_name}"')
    if ctx.header_plan.headers:
        lines.append("")

    if needs_stddef:
        lines.append("#include <stddef.h>")
    if needs_string:
        lines.append("#include <string.h>")
    if needs_stddef or needs_string:
        lines.append("")

    if analysis.used_define_texts:
        lines.append("/* local macros used by the function under test - best effort */")
        lines.extend(analysis.used_define_texts)
        lines.append("")

    if fn.raw_comment:
        lines.append(fn.raw_comment)
    lines.append(fn.prototype)
    lines.append("")

    static_accessor_lines = generate_static_accessor_declarations(analysis.used_static_globals)
    if static_accessor_lines:
        lines.append("/* accessors for copied static globals */")
        lines.extend(static_accessor_lines)
        lines.append("")

    lines.append(f"#endif /* {names.include_guard} */")
    lines.append("")
    return "\n".join(lines)


def generate_static_accessor_declarations(vars_: Sequence[VariableModel]) -> List[str]:
    lines: List[str] = []
    for var in vars_:
        if var.is_array:
            elem_t = var.array_element_type or "void"
            const_prefix = "const " if var.is_const else ""
            lines.append(f"{const_prefix}{elem_t}* get_{var.name}_ptr(void);")
            lines.append(f"size_t get_{var.name}_size(void);")
            if not var.is_const and var.array_count is not None:
                lines.append(f"void set_{var.name}(const {elem_t}* src, size_t n);")
        else:
            lines.append(f"{var.type_spelling} get_{var.name}(void);")
            if not var.is_const:
                lines.append(f"void set_{var.name}({var.type_spelling} val);")
    return lines


def generate_helper_header(ctx: GenerationContext) -> str:
    fn = ctx.function
    analysis = ctx.analysis
    names = fn.package_names

    lines: List[str] = [
        f"#ifndef {names.helper_include_guard}",
        f"#define {names.helper_include_guard}",
        "",
        f'#include "{names.header_name}"',
        "#include <stddef.h>",
        "#include <string.h>",
        "",
    ]

    if analysis.used_globals:
        lines.append("/* non-static globals used by the function under test */")
        for var in analysis.used_globals:
            declaration = var.source_text
            declaration = re.sub(r"^\s*extern\s+", "", declaration)
            if not declaration.endswith(";"):
                declaration += ";"
            lines.append(declaration)
        lines.append("")

    if analysis.used_static_globals:
        lines.append("/* static globals copied from the original translation unit */")
        for var in analysis.used_static_globals:
            declaration = var.source_text
            if not declaration.endswith(";"):
                declaration += ";"
            lines.append(declaration)
            lines.extend(generate_static_accessor_definitions(var))
            lines.append("")

    lines.append(f"#endif /* {names.helper_include_guard} */")
    lines.append("")
    return "\n".join(lines)


def generate_static_accessor_definitions(var: VariableModel) -> List[str]:
    lines: List[str] = []
    if var.is_array:
        elem_t = var.array_element_type or "void"
        const_prefix = "const " if var.is_const else ""
        lines.append(f"{const_prefix}{elem_t}* get_{var.name}_ptr(void) {{ return {var.name}; }}")
        if var.array_count is not None:
            lines.append(f"size_t get_{var.name}_size(void) {{ return (size_t){var.array_count}; }}")
        else:
            lines.append(f"size_t get_{var.name}_size(void) {{ return 0U; }}")
        if not var.is_const and var.array_count is not None:
            lines.append(
                f"void set_{var.name}(const {elem_t}* src, size_t n) {{\n"
                f"    if (src == NULL || n == 0U) {{\n"
                f"        return;\n"
                f"    }}\n"
                f"    size_t m = (n < (size_t){var.array_count}) ? n : (size_t){var.array_count};\n"
                f"    memcpy({var.name}, src, m * sizeof({elem_t}));\n"
                f"}}"
            )
    else:
        lines.append(f"{var.type_spelling} get_{var.name}(void) {{ return {var.name}; }}")
        if not var.is_const:
            lines.append(f"void set_{var.name}({var.type_spelling} val) {{ {var.name} = val; }}")
    return lines


def generate_function_source(ctx: GenerationContext) -> str:
    fn = ctx.function
    clean_fn_text = remove_leading_function_storage_qualifiers(fn.source_text)
    return "\n".join(
        [
            f'#include "{fn.package_names.helper_header_name}"',
            "",
            "/* FUNCTION TO TEST */",
            clean_fn_text,
            "",
        ]
    )


def generate_test_stub(ctx: GenerationContext) -> str:
    fn = ctx.function
    lines: List[str] = [
        f'#include "{fn.package_names.header_name}"',
        '#include "unity.h"',
        "",
    ]

    for copied_header in ctx.header_plan.headers:
        lines.append(f'#include "mock_{copied_header.output_name}"')
    if ctx.header_plan.headers:
        lines.append("")

    lines.extend(
        [
            "void setUp(void) {}",
            "void tearDown(void) {}",
            "",
            f"void test_{fn.package_names.symbol_prefix}(void)",
            "{",
            '    TEST_IGNORE_MESSAGE("Auto-generated stub test");',
            "}",
            "",
        ]
    )
    return "\n".join(lines)


def copy_cleaned_project_headers(ctx: GenerationContext) -> None:
    for item in ctx.header_plan.headers:
        text = read_text(item.source_path)
        text = rewrite_local_includes_best_effort(text, ctx.header_plan.include_rewrite_map)
        text = remove_function_prototype_text_best_effort(text, ctx.function.name)
        text = strip_function_keywords_in_copied_header_best_effort(text)
        write_text(package_src_dir(ctx) / item.output_name, text, dry_run=ctx.options.dry_run)


def generate_package(ctx: GenerationContext) -> None:
    src_dir = package_src_dir(ctx)
    test_dir = package_test_dir(ctx)
    test_file = generated_test_path(ctx)

    if not ctx.options.dry_run:
        test_dir.mkdir(parents=True, exist_ok=True)

    if package_src_exists_and_nonempty(ctx) and not ctx.options.force:
        if not test_file.exists():
            write_text(test_file, generate_test_stub(ctx), dry_run=ctx.options.dry_run)
            print(f"[OK] Created missing test stub: {test_file}")
        print(f"[SKIP] {package_dir(ctx).name}: src/ exists and --force was not provided")
        return

    if not ctx.options.dry_run:
        src_dir.mkdir(parents=True, exist_ok=True)

    write_text(src_dir / ctx.function.package_names.header_name, generate_function_header(ctx), dry_run=ctx.options.dry_run)
    write_text(src_dir / ctx.function.package_names.helper_header_name, generate_helper_header(ctx), dry_run=ctx.options.dry_run)
    write_text(src_dir / ctx.function.package_names.source_name, generate_function_source(ctx), dry_run=ctx.options.dry_run)
    copy_cleaned_project_headers(ctx)

    if not test_file.exists() or ctx.options.force:
        write_text(test_file, generate_test_stub(ctx), dry_run=ctx.options.dry_run)

    print(f"[OK] Generated {package_dir(ctx).name} -> {package_dir(ctx)}")


# =============================================================================
# Orchestration
# =============================================================================


def make_tu_context(
    c_path: Path,
    tu: TranslationUnit,
    project: ProjectConfig,
) -> TranslationUnitContext:
    return TranslationUnitContext(
        c_path=c_path.resolve(),
        tu=tu,
        diagnostics=diagnostic_report(tu),
        globals_by_usr=collect_translation_unit_globals(tu.cursor),
        local_defines=collect_local_defines(c_path),
        needed_headers=collect_needed_project_headers(tu, c_path, project.scan_roots),
    )


def generation_contexts_for_tu(
    tu_ctx: TranslationUnitContext,
    project: ProjectConfig,
    options: CliOptions,
) -> Tuple[GenerationContext, ...]:
    functions = collect_functions_in_file(tu_ctx.tu, tu_ctx.c_path, project)
    contexts: List[GenerationContext] = []
    for fn in functions:
        analysis = analyze_function_ast(fn, tu_ctx.globals_by_usr, tu_ctx.local_defines)
        plan = build_header_copy_plan(tu_ctx.needed_headers, project.scan_roots)
        contexts.append(
            GenerationContext(
                project=project,
                options=options,
                tu_context=tu_ctx,
                function=fn,
                analysis=analysis,
                header_plan=plan,
            )
        )
    return tuple(contexts)


def run(options: CliOptions) -> int:
    project = load_project_config(options)
    compile_db = load_compile_database(options.compile_db)
    index = Index.create()

    c_files = list_c_files(project.scan_roots)
    if not c_files:
        print("[WARN] no C files found in configured scan roots")
        return 0

    for c_path in c_files:
        clang_args = clang_args_for_file(c_path, project, options, compile_db)
        if options.verbose:
            print(f"[INFO] parsing {c_path}")
            print(f"[INFO] clang args: {' '.join(clang_args)}")

        try:
            tu = parse_translation_unit(index, c_path, clang_args)
        except Exception as exc:
            print(f"[ERROR] unable to parse {c_path}: {exc}")
            continue

        tu_ctx = make_tu_context(c_path, tu, project)
        if tu_ctx.diagnostics.diagnostics:
            print_diagnostics(c_path, tu_ctx.diagnostics)
        if tu_ctx.diagnostics.has_errors and options.fail_on_clang_errors:
            print(f"[SKIP] {c_path}: clang errors detected")
            continue

        contexts = generation_contexts_for_tu(tu_ctx, project, options)
        if options.verbose:
            print(f"[INFO] found {len(contexts)} function definition(s) in {c_path}")

        for ctx in contexts:
            generate_package(ctx)

    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    options = parse_cli(argv)
    return run(options)


if __name__ == "__main__":
    raise SystemExit(main())
