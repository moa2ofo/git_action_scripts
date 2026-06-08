import argparse
import re
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional

from clang.cindex import Index, Cursor, CursorKind, StorageClass, TypeKind

from path_config_loader import load_paths


DOXY_BLOCK_START = "/**"
DOXY_BLOCK_END = "*/"
DOXY_LINE_PREFIXES = ("///", "//!")


# =============================================================================
# Naming helpers
# =============================================================================

def make_display_name(fn_name: str) -> str:
    """
    Converte il nome reale della funzione C in un nome più pulito
    da usare per cartelle e file di test.

    Esempi:
        unit_bf679415_AnalyzeArray_u32      -> AnalyzeArray_u32
        unit_bf679415_InternalHelper_u32    -> InternalHelper_u32
        unit_bf679415_main                  -> main
        unit_bf679415_MyLib_ProcessRecord   -> MyLib_ProcessRecord

    Se il nome non rispetta il pattern unit_<id>_<name>, viene lasciato invariato.
    """
    m = re.match(r"^unit_[A-Za-z0-9]+_(.+)$", fn_name)
    if m:
        return m.group(1)

    return fn_name


def remove_unit_prefix_from_all_symbols(text: str) -> str:
    """
    Rimuove il prefisso unit_<id>_ da tutti i simboli C presenti nel testo.

    Esempi:
        unit_bf679415_AnalyzeArray_u32      -> AnalyzeArray_u32
        unit_bf679415_InternalHelper_u32    -> InternalHelper_u32
        unit_bf679415_main                  -> main
        unit_bf679415_MyLib_ProcessRecord   -> MyLib_ProcessRecord
    """
    return re.sub(
        r"\bunit_[A-Za-z0-9]+_([A-Za-z_]\w*)\b",
        r"\1",
        text,
    )


def make_c_identifier(name: str) -> str:
    """
    Rende sicuro un nome da usare come identificatore C.
    """
    out = re.sub(r"\W+", "_", name)

    if not out:
        return "unnamed"

    if out[0].isdigit():
        out = "_" + out

    return out


def make_header_guard(name: str) -> str:
    """
    Genera una macro valida per include guard.
    """
    guard = re.sub(r"\W+", "_", name).upper()

    if not guard:
        guard = "UNNAMED"

    if guard[0].isdigit():
        guard = "_" + guard

    return guard


# =============================================================================
# Text cleanup helpers
# =============================================================================

def strip_function_keywords_in_header(text: str) -> str:
    """
    Rimuove 'static' e 'inline' solo dalle dichiarazioni/definizioni di funzione
    negli header, senza toccare variabili, commenti, stringhe o caratteri.
    """

    protected_pattern = re.compile(
        r"""
        /\*.*?\*/            |
        //.*?$               |
        "(?:\\.|[^"\\])*"    |
        '(?:\\.|[^'\\])*'
        """,
        re.DOTALL | re.MULTILINE | re.VERBOSE,
    )

    protected_chunks = []

    def protect(match):
        protected_chunks.append(match.group(0))
        return f"__PROTECTED_{len(protected_chunks) - 1}__"

    masked = protected_pattern.sub(protect, text)

    func_pattern = re.compile(
        r"""
        ^(?P<prefix>\s*)
        (?P<quals>(?:(?:static|inline)\s+)+)
        (?P<rest>
            [A-Za-z_][\w\s\*\(\)]*
            \s+
            [A-Za-z_]\w*
            \s*\([^;{}]*\)
            \s*(?:;|\{)
        )
        """,
        re.MULTILINE | re.VERBOSE,
    )

    def repl(match):
        prefix = match.group("prefix")
        rest = match.group("rest")
        return f"{prefix}{rest}"

    masked = func_pattern.sub(repl, masked)

    def restore(match):
        return protected_chunks[int(match.group(1))]

    return re.sub(r"__PROTECTED_(\d+)__", restore, masked)


def remove_function_proto_from_header(text: str, func_name: str) -> str:
    """
    Rimuove da un header il prototipo della funzione indicata.
    """
    pattern = re.compile(
        r"(^|\n)\s*([A-Za-z_][\w\s\*\(\),\[\]:]+?\s+)?"
        + re.escape(func_name)
        + r"\s*\([^;{]*\)\s*;\s*(?=\n|$)",
        re.DOTALL,
    )

    return re.sub(pattern, r"\1", text)


# =============================================================================
# Define helpers
# =============================================================================

def collect_local_defines(c_path: Path) -> Dict[str, str]:
    """
    Raccoglie le #define presenti nel file .c e restituisce:
        { NOME_MACRO : testo_completo_define }

    Supporta define mono-linea e multi-linea con backslash finale.
    """
    src = read_text(c_path)
    lines = src.splitlines()

    out: Dict[str, str] = {}
    i = 0

    while i < len(lines):
        line = lines[i]

        if re.match(r"^\s*#\s*define\b", line):
            block = [line]

            while block[-1].rstrip().endswith("\\") and i + 1 < len(lines):
                i += 1
                block.append(lines[i])

            full = "\n".join(block)
            m = re.match(r"^\s*#\s*define\s+([A-Za-z_]\w*)\b", block[0])

            if m:
                name = m.group(1)
                out[name] = full

        i += 1

    return out


def extract_define_dependencies(
    define_text: str,
    all_define_names: Set[str],
    self_name: str,
) -> Set[str]:
    deps = set()

    for name in all_define_names:
        if name == self_name:
            continue

        if re.search(rf"\b{re.escape(name)}\b", define_text):
            deps.add(name)

    return deps


def collect_used_defines_in_function(
    fn_text: str,
    local_defines: Dict[str, str],
) -> List[str]:
    """
    Restituisce le #define usate dalla funzione, includendo anche
    le dipendenze transitive tra macro, in ordine stabile.
    """
    all_names = set(local_defines.keys())

    directly_used = {
        name
        for name in local_defines
        if re.search(rf"\b{re.escape(name)}\b", fn_text)
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

    ordered = [
        local_defines[name]
        for name in local_defines
        if name in needed
    ]

    return ordered


# =============================================================================
# Doxygen helpers
# =============================================================================

def get_doxygen_comment_for_function(fn: Cursor) -> Optional[str]:
    """
    Ritorna il commento Doxygen immediatamente sopra alla funzione,
    se presente.

    Prova prima con libclang raw_comment, poi usa fallback testuale.
    """
    raw = getattr(fn, "raw_comment", None)
    if raw:
        return raw

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

    # Cerca blocco /** ... */ subito sopra la funzione.
    j = i
    while j >= 0 and lines[j].strip() == "":
        j -= 1

    if j >= 0 and lines[j].rstrip().endswith(DOXY_BLOCK_END):
        k = j
        found_start = False

        while k >= 0:
            if DOXY_BLOCK_START in lines[k]:
                found_start = True
                break
            k -= 1

        if found_start:
            block = lines[k:j + 1]

            if any(DOXY_BLOCK_START in ln for ln in block):
                return "\n".join(block)

    # Cerca gruppo contiguo di linee /// oppure //!.
    j = i
    while j >= 0 and lines[j].strip() == "":
        j -= 1

    if j >= 0 and lines[j].lstrip().startswith(DOXY_LINE_PREFIXES):
        buf = []

        while j >= 0 and lines[j].lstrip().startswith(DOXY_LINE_PREFIXES):
            buf.append(lines[j])
            j -= 1

        buf.reverse()
        return "\n".join(buf)

    return None


# =============================================================================
# File system helpers
# =============================================================================

def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="ignore")


def write_text(p: Path, s: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s, encoding="utf-8")


def _is_in_test_dir(p: Path) -> bool:
    return any(part.startswith("TEST_") for part in p.parts)


def list_c_files(roots: List[Path]) -> List[Path]:
    """
    Cerca ricorsivamente tutti i file .c nelle root indicate,
    escludendo cartelle TEST_*.
    """
    out: List[Path] = []

    for r in roots:
        if r.is_dir():
            out.extend(
                [
                    p
                    for p in r.rglob("*.c")
                    if p.is_file() and not _is_in_test_dir(p)
                ]
            )

    return sorted(out)


# =============================================================================
# Clang helpers
# =============================================================================

def text_from_extent(ext) -> str:
    src_path = Path(ext.start.file.name)
    src = read_text(src_path)
    lines = src.splitlines(keepends=True)

    def idx(loc):
        li = loc.line - 1
        ci = loc.column - 1
        return sum(len(lines[i]) for i in range(li)) + ci

    start = idx(ext.start)
    end = idx(ext.end)

    return src[start:end]


def function_prototype(fn: Cursor) -> str:
    """
    Genera il prototipo della funzione usando le informazioni di libclang.
    """
    ret = fn.result_type.spelling if fn.result_type else "void"
    params = []

    for idx, p in enumerate(fn.get_arguments()):
        t = p.type.spelling
        name = p.spelling or f"param{idx}"
        params.append(f"{t} {name}")

    if fn.type.kind == TypeKind.FUNCTIONPROTO and fn.type.is_function_variadic():
        params.append("...")

    param_str = ", ".join(params) if params else "void"

    return f"{ret} {fn.spelling}({param_str});"


def collect_tu_globals(tu_cursor: Cursor) -> Dict[str, Cursor]:
    out = {}

    for c in tu_cursor.get_children():
        if c.kind == CursorKind.VAR_DECL and c.semantic_parent.kind == CursorKind.TRANSLATION_UNIT:
            usr = c.get_usr() or f"{c.spelling}@{c.location.file}:{c.location.line}"
            out[usr] = c

    return out


def classify_var(ref: Cursor) -> Tuple[bool, bool]:
    """
    Classifica una variabile referenziata.

    Ritorna:
        (is_global, is_static)
    """
    if ref is None or ref.kind != CursorKind.VAR_DECL:
        return False, False

    if ref.semantic_parent and ref.semantic_parent.kind == CursorKind.TRANSLATION_UNIT:
        is_static = ref.storage_class == StorageClass.STATIC
        return True, is_static

    return False, False


def analyze_function(fn: Cursor, tu_globals: Dict[str, Cursor]):
    """
    Analizza il corpo funzione e raccoglie:
        - funzioni chiamate
        - variabili globali non statiche usate
        - variabili statiche usate
    """
    calls: Set[str] = set()
    used_globals: Set[str] = set()
    used_static: Set[str] = set()

    def walk(n: Cursor):
        if n.kind == CursorKind.CALL_EXPR:
            tgt = None

            for ch in n.get_children():
                if hasattr(ch, "referenced") and ch.referenced:
                    tgt = ch.referenced
                    break

            if tgt and tgt.kind == CursorKind.FUNCTION_DECL and tgt.spelling:
                calls.add(tgt.spelling)

        if n.kind == CursorKind.DECL_REF_EXPR and n.referenced:
            ref = n.referenced
            is_glob, is_stat = classify_var(ref)

            if is_glob:
                usr = ref.get_usr() or f"{ref.spelling}@{ref.location.file}:{ref.location.line}"

                if usr in tu_globals:
                    if is_stat:
                        used_static.add(usr)
                    else:
                        used_globals.add(usr)

        for ch in n.get_children():
            walk(ch)

    for ch in fn.get_children():
        if ch.kind == CursorKind.COMPOUND_STMT:
            walk(ch)

    return calls, used_globals, used_static


def is_const_qualified(t) -> bool:
    try:
        return bool(t.is_const_qualified())
    except Exception:
        return False


def is_array_type(t) -> bool:
    return t.kind in (
        TypeKind.CONSTANTARRAY,
        TypeKind.INCOMPLETEARRAY,
        TypeKind.VARIABLEARRAY,
        TypeKind.DEPENDENTSIZEDARRAY,
    )


def array_elem_type_spelling(t) -> str:
    return t.element_type.spelling


def array_count_or_none(t):
    try:
        if t.kind == TypeKind.CONSTANTARRAY:
            return int(t.element_count)
    except Exception:
        pass

    return None


# =============================================================================
# Project include graph helpers
# =============================================================================

def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def _is_under_any(path: Path, roots: List[Path]) -> bool:
    return any(_is_under(path, r) for r in roots)


def collect_needed_project_headers(
    tu,
    start_c_path: Path,
    project_roots: List[Path],
) -> List[Path]:
    """
    Usa le informazioni include della Translation Unit per raccogliere
    gli header di progetto diretti e transitivi.

    Vengono considerati solo gli header sotto project_roots.
    """
    start_c_path = start_c_path.resolve()
    project_roots = [r.resolve() for r in project_roots]

    adj: Dict[Path, Set[Path]] = {}

    for inc in tu.get_includes():
        try:
            src = Path(inc.source.name).resolve()
            incp = Path(inc.include.name).resolve()
        except Exception:
            continue

        if not _is_under_any(incp, project_roots):
            continue

        if not (src == start_c_path or _is_under_any(src, project_roots)):
            continue

        adj.setdefault(src, set()).add(incp)

    needed: Set[Path] = set()
    stack: List[Path] = list(adj.get(start_c_path, set()))
    seen: Set[Path] = set()

    while stack:
        h = stack.pop()

        if h in seen:
            continue

        seen.add(h)
        needed.add(h)

        for nxt in adj.get(h, set()):
            if nxt not in seen:
                stack.append(nxt)

    return sorted(needed)


# =============================================================================
# Generation helpers
# =============================================================================

def create_stub_test_file(
    test_file_path: Path,
    display_fn_name: str,
    needed_headers: List[Path],
):
    """
    Crea un file test Unity minimo.
    """
    test_identifier = make_c_identifier(display_fn_name)

    test_c = [
        f'#include "{display_fn_name}.h"',
        '#include "unity.h"',
        "",
    ]

    for h in needed_headers:
        test_c.append(f'#include "mock_{h.name}"')

    test_c += [
        "",
        "void setUp(void) {}",
        "void tearDown(void) {}",
        "",
        f"void test_{test_identifier}(void)",
        "{",
        '    TEST_IGNORE_MESSAGE("Auto-generated stub test");',
        "}",
        "",
    ]

    write_text(test_file_path, "\n".join(test_c))


# =============================================================================
# Main
# =============================================================================

def main():
    ap = argparse.ArgumentParser()

    ap.add_argument(
        "root",
        help="workspace path",
    )

    ap.add_argument(
        "--out-root",
        default=None,
        help="path to /unitTest. Default: value from path config",
    )

    ap.add_argument(
        "--force",
        action="store_true",
        help="regenerate src even if TEST_<function>/src already exists and is not empty",
    )

    ap.add_argument(
        "--show-clang-diagnostics",
        action="store_true",
        help="print clang diagnostics for each parsed .c file",
    )

    # pass-through extra clang args after '--'
    args, extra_clang = ap.parse_known_args()

    workspace_root = Path(args.root).resolve()
    paths = load_paths(__file__)

    out_root = Path(args.out_root).resolve() if args.out_root else paths.unit_test_root

    # Scan roots from YAML config.
    scan_roots: List[Path] = [
        paths.sw_cmp_repo_pltf_dir,
        paths.sw_cmp_repo_cfg_dir,
    ]

    # Clang args.
    clang_args: List[str] = ["-std=c11"]

    for inc in scan_roots:
        clang_args.append(f"-I{inc}")

    clang_args.append(f"-I{workspace_root}")
    clang_args.extend(extra_clang)

    index = Index.create()
    c_files = list_c_files(scan_roots)

    print(f"[INFO] Workspace root : {workspace_root}")
    print(f"[INFO] Output root    : {out_root}")
    print(f"[INFO] Scan roots     : {', '.join(str(r) for r in scan_roots)}")
    print(f"[INFO] C files found  : {len(c_files)}")

    for c_path in c_files:
        print(f"[INFO] Parsing {c_path}")

        tu = index.parse(str(c_path), args=clang_args)

        if args.show_clang_diagnostics:
            for d in tu.diagnostics:
                print(f"[CLANG][{c_path.name}] severity={d.severity}: {d.spelling}")

        tu_globals = collect_tu_globals(tu.cursor)
        local_defines = collect_local_defines(c_path)

        needed_headers: List[Path] = collect_needed_project_headers(
            tu,
            c_path,
            scan_roots,
        )

        for fn in tu.cursor.get_children():
            if fn.kind != CursorKind.FUNCTION_DECL or not fn.is_definition():
                continue

            if Path(str(fn.location.file)).resolve() != c_path:
                continue

            # -----------------------------------------------------------------
            # Nome reale e nome pulito
            # -----------------------------------------------------------------
            real_fn_name = fn.spelling
            display_fn_name = make_display_name(real_fn_name)
            test_identifier = make_c_identifier(display_fn_name)

            print(f"[INFO] Function found: {real_fn_name} -> {display_fn_name}")

            # -----------------------------------------------------------------
            # Test package
            # -----------------------------------------------------------------
            test_pkg_dir = out_root / f"TEST_{display_fn_name}"
            src_dir = test_pkg_dir / "src"
            test_dir = test_pkg_dir / "test"

            src_exists = src_dir.exists()
            src_empty = (not src_exists) or (not any(src_dir.iterdir()))

            test_file_path = test_dir / f"test_{display_fn_name}.c"

            # Assicura sempre che test/ esista.
            test_dir.mkdir(parents=True, exist_ok=True)

            # Se src esiste già ed è piena, non rigenerare src,
            # a meno che sia stato passato --force.
            # Crea comunque il file di test se manca.
            if src_exists and not src_empty and not args.force:
                if not test_file_path.exists():
                    create_stub_test_file(
                        test_file_path=test_file_path,
                        display_fn_name=display_fn_name,
                        needed_headers=needed_headers,
                    )
                    print(f"[OK] Created missing test file for TEST_{display_fn_name}")

                print(f"[SKIP] TEST_{display_fn_name} exists and src/ not empty")
                continue

            # Rigenera src.
            src_dir.mkdir(parents=True, exist_ok=True)

            _calls, used_glob_usr, used_stat_usr = analyze_function(fn, tu_globals)

            # -----------------------------------------------------------------
            # Testo funzione e prototipo con simboli ripuliti
            # -----------------------------------------------------------------
            fn_text_real = text_from_extent(fn.extent)
            fn_text = remove_unit_prefix_from_all_symbols(fn_text_real)

            proto_real = function_prototype(fn)
            proto = remove_unit_prefix_from_all_symbols(proto_real)

            used_define_texts_real = collect_used_defines_in_function(
                fn_text_real,
                local_defines,
            )

            used_define_texts = [
                remove_unit_prefix_from_all_symbols(d)
                for d in used_define_texts_real
            ]

            # -----------------------------------------------------------------
            # src/<display_fn_name>.h
            # -----------------------------------------------------------------
            header_guard = make_header_guard(f"TEST_{display_fn_name}_H")

            header_lines = [
                f"#ifndef {header_guard}",
                f"#define {header_guard}",
                "",
            ]

            need_stddef = False
            need_string = False

            for usr in sorted(used_stat_usr):
                v = tu_globals[usr]

                # Copia solo static dichiarati nel file .c corrente.
                if Path(str(v.location.file)).resolve() != c_path:
                    continue

                t = v.type

                if is_array_type(t):
                    need_stddef = True

                    if not is_const_qualified(t) and array_count_or_none(t) is not None:
                        need_string = True

            # Include degli header necessari.
            for h in needed_headers:
                header_lines.append(f'#include "{h.name}"')

            header_lines.append("")

            # Include standard necessari.
            if need_stddef:
                header_lines.append("#include <stddef.h>")

            if need_string:
                header_lines.append("#include <string.h>")

            if need_stddef or need_string:
                header_lines.append("")

            # Define locali usate dalla funzione.
            if used_define_texts:
                for d in used_define_texts:
                    header_lines.append(d)

                header_lines.append("")

            # Commento Doxygen prima del prototipo.
            doxy = get_doxygen_comment_for_function(fn)
            if doxy:
                doxy = remove_unit_prefix_from_all_symbols(doxy)
                header_lines.append(doxy)

            # Prototipo funzione con nome pulito.
            header_lines.append(proto)
            header_lines.append("")

            # Accessor per variabili statiche.
            for usr in sorted(used_stat_usr):
                v = tu_globals[usr]
                t = v.type
                vname = remove_unit_prefix_from_all_symbols(v.spelling)

                v_is_const = is_const_qualified(t)
                v_is_array = is_array_type(t)

                if v_is_array:
                    elem_t = remove_unit_prefix_from_all_symbols(array_elem_type_spelling(t))
                    cnt = array_count_or_none(t)

                    header_lines.append(
                        f"{'const ' if v_is_const else ''}{elem_t}* get_{vname}_ptr(void);"
                    )
                    header_lines.append(f"size_t get_{vname}_size(void);")

                    if (not v_is_const) and (cnt is not None):
                        header_lines.append(
                            f"void set_{vname}(const {elem_t}* src, size_t n);"
                        )

                else:
                    tname = remove_unit_prefix_from_all_symbols(t.spelling)

                    header_lines.append(f"{tname} get_{vname}(void);")

                    if not v_is_const:
                        header_lines.append(f"void set_{vname}({tname} val);")

            header_lines.append("")
            header_lines.append(f"#endif /* {header_guard} */\n")

            clean_h = strip_function_keywords_in_header("\n".join(header_lines))
            clean_h = remove_unit_prefix_from_all_symbols(clean_h)
            write_text(src_dir / f"{display_fn_name}.h", clean_h)

            # -----------------------------------------------------------------
            # src/<display_fn_name>_help.h
            # -----------------------------------------------------------------
            help_guard = make_header_guard(f"TEST_{display_fn_name}_HELP_H")

            help_lines = [
                f"#ifndef {help_guard}",
                f"#define {help_guard}",
                "",
                f'#include "{display_fn_name}.h"',
                "#include <stddef.h>",
                "#include <string.h>",
                "",
            ]

            # Dichiara solo le globali non statiche usate dalla funzione.
            if used_glob_usr:
                help_lines.append("/* non-static globals used by this function */")

                for usr in sorted(used_glob_usr):
                    v = tu_globals[usr]
                    orig = text_from_extent(v.extent).strip()
                    orig = re.sub(r"^\s*extern\s+", "", orig)
                    orig = remove_unit_prefix_from_all_symbols(orig)

                    if not orig.endswith(";"):
                        orig += ";"

                    help_lines.append(orig)

                help_lines.append("")

            # Copia static globals usate dalla funzione e genera accessors.
            if used_stat_usr:
                help_lines.append("/* static globals (copied) */")

                for usr in sorted(used_stat_usr):
                    v = tu_globals[usr]
                    t = v.type

                    vname_real = v.spelling
                    vname = remove_unit_prefix_from_all_symbols(vname_real)

                    static_src = text_from_extent(v.extent).strip()
                    static_src = remove_unit_prefix_from_all_symbols(static_src)

                    if not static_src.endswith(";"):
                        static_src += ";"

                    help_lines.append(static_src)

                    v_is_const = is_const_qualified(t)
                    v_is_array = is_array_type(t)

                    if v_is_array:
                        elem_t = remove_unit_prefix_from_all_symbols(array_elem_type_spelling(t))
                        cnt = array_count_or_none(t)

                        help_lines.append(
                            f"{'const ' if v_is_const else ''}{elem_t}* "
                            f"get_{vname}_ptr(void) {{ return {vname}; }}"
                        )

                        if cnt is not None:
                            help_lines.append(
                                f"size_t get_{vname}_size(void) "
                                f"{{ return (size_t){cnt}; }}"
                            )
                        else:
                            help_lines.append(
                                f"size_t get_{vname}_size(void) {{ return 0; }}"
                            )

                        if (not v_is_const) and (cnt is not None):
                            help_lines.append(
                                f"void set_{vname}(const {elem_t}* src, size_t n) {{\n"
                                f"    size_t m = (n < (size_t){cnt}) ? n : (size_t){cnt};\n"
                                f"    memcpy({vname}, src, m * sizeof({elem_t}));\n"
                                f"}}"
                            )

                    else:
                        tname = remove_unit_prefix_from_all_symbols(t.spelling)

                        help_lines.append(
                            f"{tname} get_{vname}(void) {{ return {vname}; }}"
                        )

                        if not v_is_const:
                            help_lines.append(
                                f"void set_{vname}({tname} val) {{ {vname} = val; }}"
                            )

                help_lines.append("")

            help_lines.append(f"#endif /* {help_guard} */\n")

            clean_help = strip_function_keywords_in_header("\n".join(help_lines))
            clean_help = remove_unit_prefix_from_all_symbols(clean_help)
            write_text(src_dir / f"{display_fn_name}_help.h", clean_help)

            # -----------------------------------------------------------------
            # src/<display_fn_name>.c
            # -----------------------------------------------------------------
            impl = [
                f'#include "{display_fn_name}_help.h"',
                "",
                "/* FUNCTION TO TEST */",
                fn_text,
            ]

            clean_c = strip_function_keywords_in_header("\n".join(impl))
            clean_c = remove_unit_prefix_from_all_symbols(clean_c)
            write_text(src_dir / f"{display_fn_name}.c", clean_c)

            # -----------------------------------------------------------------
            # Copy cleaned project headers.
            # -----------------------------------------------------------------
            for h in needed_headers:
                cleaned = read_text(h)

                # Rimuove eventuale prototipo con nome reale.
                cleaned = remove_function_proto_from_header(cleaned, real_fn_name)

                # Rimuove eventuale prototipo con nome pulito.
                cleaned = remove_function_proto_from_header(cleaned, display_fn_name)

                # Ripulisce tutti i simboli unit_<id>_ anche negli header copiati.
                cleaned = remove_unit_prefix_from_all_symbols(cleaned)

                cleaned = strip_function_keywords_in_header(cleaned)

                write_text(src_dir / h.name, cleaned)

            # -----------------------------------------------------------------
            # Create test/<display_fn_name>.c only if it does not exist.
            # -----------------------------------------------------------------
            if not test_file_path.exists():
                create_stub_test_file(
                    test_file_path=test_file_path,
                    display_fn_name=display_fn_name,
                    needed_headers=needed_headers,
                )

            print(
                f"[OK] Generated TEST_{display_fn_name} "
                f"from {real_fn_name} -> {test_pkg_dir}"
            )


if __name__ == "__main__":
    main()