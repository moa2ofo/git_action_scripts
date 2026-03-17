import argparse
import re
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional
from clang.cindex import Index, Cursor, CursorKind, StorageClass, TypeKind


from path_config_loader import load_paths

DOXY_BLOCK_START = "/**"
DOXY_BLOCK_END = "*/"
DOXY_LINE_PREFIXES = ("///", "//!")


def strip_function_keywords_in_header(text: str) -> str:
    """
    Rimuove 'static' e 'inline' solo dalle dichiarazioni/definizioni di funzione
    negli header, senza toccare le variabili.
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
        return f"__PROTECTED_{len(protected_chunks)-1}__"

    masked = protected_pattern.sub(protect, text)

    # match abbastanza conservativo:
    # prende righe che sembrano firme di funzione, non variabili
    func_pattern = re.compile(
        r"""
        ^(?P<prefix>\s*)
        (?P<quals>(?:(?:static|inline)\s+)+)
        (?P<rest>
            [A-Za-z_][\w\s\*\(\)]*      # tipo ritorno
            \s+
            [A-Za-z_]\w*                # nome funzione
            \s*\([^;{}]*\)              # parametri
            \s*(?:;|\{)                 # prototipo o definizione
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

def extract_define_dependencies(define_text: str, all_define_names: Set[str], self_name: str) -> Set[str]:
    deps = set()
    for name in all_define_names:
        if name == self_name:
            continue
        if re.search(rf"\b{re.escape(name)}\b", define_text):
            deps.add(name)
    return deps


def collect_used_defines_in_function(fn_text: str, local_defines: Dict[str, str]) -> List[str]:
    """
    Restituisce le #define usate dalla funzione, includendo anche
    le dipendenze transitive tra macro, in ordine stabile.
    """
    all_names = set(local_defines.keys())

    directly_used = {
        name for name in local_defines
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

    ordered = [local_defines[name] for name in local_defines if name in needed]
    return ordered

def get_doxygen_comment_for_function(fn: Cursor) -> Optional[str]:
    """
    Ritorna il commento Doxygen (testo originale) immediatamente sopra alla funzione,
    se presente. Prova prima con libclang (raw_comment), poi fa fallback leggendo il file.
    """
    # 1) Tentativo diretto via libclang
    raw = getattr(fn, "raw_comment", None)
    if raw:
        return raw

    # 2) Fallback: cerca nel sorgente sopra l'inizio della funzione
    loc = fn.extent.start
    if not loc.file:
        return None

    src_path = Path(loc.file.name)
    try:
        lines = read_text(src_path).splitlines()
    except Exception:
        return None

    i = loc.line - 2  # indice dell'ultima riga prima della funzione (0-based)
    if i < 0:
        return None

    # 2a) Cerca un blocco /** ... */ che termini immediatamente sopra
    j = i
    # Salta eventuali righe vuote finali
    while j >= 0 and lines[j].strip() == "":
        j -= 1
    if j >= 0 and lines[j].rstrip().endswith(DOXY_BLOCK_END):
        # risali finché trovi l'inizio "/**"
        k = j
        found_start = False
        while k >= 0:
            if DOXY_BLOCK_START in lines[k]:
                found_start = True
                break
            k -= 1
        if found_start:
            block = lines[k:j+1]
            # Verifica che sia davvero un blocco Doxygen (/** ... */)
            if any(DOXY_BLOCK_START in ln for ln in block):
                return "\n".join(block)

    # 2b) In alternativa, raccogli un gruppo contiguo di linee '///' o '//!'
    j = i
    # salta righe vuote
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


# ---------------------- FS helpers ----------------------

def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="ignore")


def write_text(p: Path, s: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s, encoding="utf-8")


def _is_in_test_dir(p: Path) -> bool:
    return any(part.startswith("TEST_") for part in p.parts)


# ---------------------- Discovery limited to ../pltf and ../cfg ----------------------

def list_c_files(roots: List[Path]) -> List[Path]:
    out: List[Path] = []
    for r in roots:
        if r.is_dir():
            out.extend([p for p in r.rglob("*.c") if p.is_file() and not _is_in_test_dir(p)])
    return sorted(out)


# ---------------------- Clang helpers ----------------------

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
    ret = fn.result_type.spelling if fn.result_type else "void"
    params = []
    for p in fn.get_arguments():
        t = p.type.spelling
        name = p.spelling or "param"
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
    if ref is None or ref.kind != CursorKind.VAR_DECL:
        return (False, False)
    if ref.semantic_parent and ref.semantic_parent.kind == CursorKind.TRANSLATION_UNIT:
        is_static = (ref.storage_class == StorageClass.STATIC)
        return (True, is_static)
    return (False, False)


def analyze_function(fn: Cursor, tu_globals: Dict[str, Cursor]):
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


def remove_function_proto_from_header(text: str, func_name: str) -> str:
    pattern = re.compile(
        r'(^|\n)\s*([A-Za-z_][\w\s\*\(\),\[\]:]+?\s+)?'
        + re.escape(func_name)
        + r'\s*\([^;{]*\)\s*;\s*(?=\n|$)',
        re.DOTALL,
    )
    return re.sub(pattern, r"\1", text)


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


# ---------------------- Project include graph helpers ----------------------

def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def _is_under_any(path: Path, roots: List[Path]) -> bool:
    return any(_is_under(path, r) for r in roots)


def collect_needed_project_headers(tu, start_c_path: Path, project_roots: List[Path]) -> List[Path]:
    """
    Using TU include info, collect direct + transitive headers that are under project_roots.
    Traverse only across project headers (stop on non-project includes).
    """
    start_c_path = start_c_path.resolve()
    project_roots = [r.resolve() for r in project_roots]

    # Build adjacency: source file -> set(included project headers)
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


# ---------------------- Main ----------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", help="workspace path (this script folder)")
    # allow overriding output dir if needed
    ap.add_argument("--out-root", default=None, help="path to /unitTest (default: sibling of root)")
    # pass-through extra clang args after '--'
    args, extra_clang = ap.parse_known_args()

    workspace_root = Path(args.root).resolve()
    paths = load_paths(__file__)

    root = Path(args.root).resolve()         # e.g., /workspace
    parent = root.parent                     # common parent of /workspace, /pltf, /cfg, /unitTest
    out_root = Path(args.out_root).resolve() if args.out_root else paths.unit_test_root

    # Scan roots from YAML config instead of assuming ../pltf and ../cfg
    scan_roots: List[Path] = [paths.sw_cmp_repo_pltf_dir, paths.sw_cmp_repo_cfg_dir]

    # Clang args: std + includes from YAML (+ workspace root) + extras
    clang_args: List[str] = ["-std=c11"]
    for inc in scan_roots:
        clang_args.append(f"-I{inc}")
    clang_args.append(f"-I{workspace_root}")
    clang_args.extend(extra_clang)

    index = Index.create()

    c_files = list_c_files(scan_roots)

    for c_path in c_files:
        tu = index.parse(str(c_path), args=clang_args)
        tu_globals = collect_tu_globals(tu.cursor)
        local_defines = collect_local_defines(c_path)

        # Compute per-TU needed project headers (direct + transitive across project headers)
        needed_headers: List[Path] = collect_needed_project_headers(tu, c_path, scan_roots)


        for fn in tu.cursor.get_children():
            if fn.kind != CursorKind.FUNCTION_DECL or not fn.is_definition():
                continue
            if Path(str(fn.location.file)).resolve() != c_path:
                continue

            fn_name = fn.spelling

            # TEST PACKAGE under ../unitTest
            test_pkg_dir = out_root / f"TEST_{fn_name}"
            src_dir = test_pkg_dir / "src"
            test_dir = test_pkg_dir / "test"

            src_exists = src_dir.exists()
            src_empty = (not src_exists) or (not any(src_dir.iterdir()))
            test_file_path = test_dir / f"test_{fn_name}.c"

            # assicura sempre che la cartella test/ esista
            test_dir.mkdir(parents=True, exist_ok=True)

            # se src esiste già ed è piena, non rigenerare src
            # ma crea comunque il file di test se manca
            if src_exists and not src_empty:
                if not test_file_path.exists():
                    test_c = [
                        f'#include "{fn_name}.h"',
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
                        f"void test_{fn_name}(void)",
                        "{",
                        '    TEST_IGNORE_MESSAGE("Auto-generated stub test");',
                        "}",
                        "",
                    ]

                    write_text(test_file_path, "\n".join(test_c))
                    print(f"[OK] Created missing test file for TEST_{fn_name}")

                print(f"[SKIP] TEST_{fn_name} exists and src/ not empty")
                continue

            # CASE 1 e CASE 2: rigenera src
            src_dir.mkdir(parents=True, exist_ok=True)

            _calls, used_glob_usr, used_stat_usr = analyze_function(fn, tu_globals)
            

            # ================== src/<fn>.h ==================
            header_lines = [
                f"#ifndef TEST_{fn_name.upper()}_H",
                f"#define TEST_{fn_name.upper()}_H",
                "",
            ]

            fn_text = text_from_extent(fn.extent)
            proto = function_prototype(fn)
            used_define_texts = collect_used_defines_in_function(fn_text, local_defines)

            need_stddef = False
            need_string = False

            for usr in sorted(used_stat_usr):
                v = tu_globals[usr]

                # copia solo static dichiarati nel file .c
                if Path(str(v.location.file)).resolve() != c_path:
                    continue

                t = v.type
                if is_array_type(t):
                    need_stddef = True
                    if not is_const_qualified(t) and array_count_or_none(t) is not None:
                        need_string = True

            # --- include solo gli header necessari (diretti + transitivi) ---
            for h in needed_headers:
                header_lines.append(f'#include "{h.name}"')
            header_lines.append("")

            # --- include standard necessari ---
            if need_stddef:
                header_lines.append("#include <stddef.h>")
            if need_string:
                header_lines.append("#include <string.h>")
            if need_stddef or need_string:
                header_lines.append("")

            # --- define locali usate dalla funzione ---
            if used_define_texts:
                for d in used_define_texts:
                    header_lines.append(d)
                header_lines.append("")

            # --- commento DOXYGEN (prima del prototipo) ---
            doxy = get_doxygen_comment_for_function(fn)
            if doxy:
                header_lines.append(doxy)

            # --- prototipo funzione (solo UNA volta) ---
            header_lines.append(proto)
            header_lines.append("")

            # --- accessor per variabili statiche ---
            for usr in sorted(used_stat_usr):
                v = tu_globals[usr]
                t = v.type
                vname = v.spelling
                v_is_const = is_const_qualified(t)
                v_is_array = is_array_type(t)

                if v_is_array:
                    elem_t = array_elem_type_spelling(t)
                    cnt = array_count_or_none(t)

                    header_lines.append(
                        f"{'const ' if v_is_const else ''}{elem_t}* get_{vname}_ptr(void);"
                    )
                    header_lines.append(f"size_t get_{vname}_size(void);")

                    if (not v_is_const) and (cnt is not None):
                        header_lines.append(f"void set_{vname}(const {elem_t}* src, size_t n);")

                else:
                    tname = t.spelling
                    header_lines.append(f"{tname} get_{vname}(void);")
                    if not v_is_const:
                        header_lines.append(f"void set_{vname}({tname} val);")

            header_lines.append("")
            header_lines.append(f"#endif /* TEST_{fn_name.upper()}_H */\n")

            clean_h = strip_function_keywords_in_header("\n".join(header_lines))
            write_text(src_dir / f"{fn_name}.h", clean_h)

            # ================== src/<fn>_help.h ==================
            help_lines = [
                f"#ifndef TEST_{fn_name.upper()}_HELP_H",
                f"#define TEST_{fn_name.upper()}_HELP_H",
                "",
                f'#include "{fn_name}.h"',
                "#include <stddef.h>",
                "#include <string.h>",
                "",
            ]

            # Dichiara solo le globali non statiche usate dalla funzione
            if used_glob_usr:
                help_lines.append("/* non-static globals used by this function */")
                for usr in sorted(used_glob_usr):
                    v = tu_globals[usr]
                    orig = text_from_extent(v.extent).strip()
                    orig = re.sub(r"^\s*extern\s+", "", orig)
                    if not orig.endswith(";"):
                        orig += ";"
                    help_lines.append(orig)
                help_lines.append("")

            if used_stat_usr:
                help_lines.append("/* static globals (copied) */")
                for usr in sorted(used_stat_usr):
                    v = tu_globals[usr]
                    t = v.type
                    vname = v.spelling
                    static_src = text_from_extent(v.extent).strip()
                    if not static_src.endswith(";"):
                        static_src += ";"
                    help_lines.append(static_src)

                    v_is_const = is_const_qualified(t)
                    v_is_array = is_array_type(t)

                    if v_is_array:
                        elem_t = array_elem_type_spelling(t)
                        cnt = array_count_or_none(t)

                        help_lines.append(
                            f"{'const ' if v_is_const else ''}{elem_t}* get_{vname}_ptr(void) {{ return {vname}; }}"
                        )

                        if cnt is not None:
                            help_lines.append(
                                f"size_t get_{vname}_size(void) {{ return (size_t){cnt}; }}"
                            )
                        else:
                            help_lines.append("size_t get_{vname}_size(void) { return 0; }")

                        if (not v_is_const) and (cnt is not None):
                            help_lines.append(
                                f"void set_{vname}(const {elem_t}* src, size_t n) {{\n"
                                f"    size_t m = (n < (size_t){cnt}) ? n : (size_t){cnt};\n"
                                f"    memcpy({vname}, src, m * sizeof({elem_t}));\n"
                                f"}}"
                            )

                    else:
                        tname = t.spelling
                        help_lines.append(f"{tname} get_{vname}(void) {{ return {vname}; }}")
                        if not v_is_const:
                            help_lines.append(f"void set_{vname}({tname} val) {{ {vname} = val; }}")

                help_lines.append("")

            help_lines.append(f"#endif /* TEST_{fn_name.upper()}_HELP_H */\n")

            clean_help = strip_function_keywords_in_header("\n".join(help_lines))
            write_text(src_dir / f"{fn_name}_help.h", clean_help)

            # ================== src/<fn>.c ==================
            impl = [
                f'#include "{fn_name}_help.h"',
                "",
                "/* FUNCTION TO TEST */",
                fn_text,
            ]

            clean_c = strip_function_keywords_in_header("\n".join(impl))
            write_text(src_dir / f"{fn_name}.c", clean_c)


            # ================== copy cleaned headers (needed project headers only) ==================
            for h in needed_headers:
                cleaned = remove_function_proto_from_header(read_text(h), fn_name)
                cleaned = strip_function_keywords_in_header(cleaned)
                write_text(src_dir / h.name, cleaned)



            # ================== create test/<fn>.c only if test didn't exist ==================
            if not test_file_path.exists():
                test_c = [
                    f'#include "{fn_name}.h"',
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
                    f"void test_{fn_name}(void)",
                    "{",
                    '    TEST_IGNORE_MESSAGE("Auto-generated stub test");',
                    "}",
                    "",
                ]

                write_text(test_file_path, "\n".join(test_c))

            print(f"[OK] Generated TEST_{fn_name} (src regenerated) -> {test_pkg_dir}")

if __name__ == "__main__":
    main()