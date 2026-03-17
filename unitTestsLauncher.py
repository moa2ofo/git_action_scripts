# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import re
import sys
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
from typing import Optional

from common_utils import (
    info, warn, error, fatal,
    require_python, require_command, require_dir,
    require_docker_running,
    run_cmd, docker_mount_path,
    preflight_check,
    clear_folder,
    copy_entire_folder,
    copy_folder_contents
)

from path_config_loader import load_paths

UNIT_TEST_PREFIX = "TEST_"

PATHS = load_paths(__file__)
SCRIPT_PATH = PATHS.script_path
PROJECT_ROOT = PATHS.project_root

GIT_RESULT = PATHS.git_result
UNIT_EXECUTION_FOLDER = PATHS.unit_execution_folder
UNIT_EXECUTION_FOLDER_TEST = PATHS.unit_execution_folder_test
UNIT_EXECUTION_FOLDER_BUILD = PATHS.unit_execution_folder_build
UNIT_RESULT_FOLDER = PATHS.unit_result_folder
RESULT_REPORT = "total_result_report.txt"

DOCKER_MOUNT = docker_mount_path(PATHS.docker_mount_source)

DOCKER_BASE = [
    "docker",
    "run",
    "-it",
    "--rm",
    "-v",
    f"{DOCKER_MOUNT}:/home/dev/project",
    "throwtheswitch/madsciencelab-plugins:1.0.1b",
]

CEEDLING_CLEAN = ["ceedling", "clobber"]
DOCKER_CLEAN = DOCKER_BASE + CEEDLING_CLEAN


def preflight_checks(project_root: Path):
    info("Performing preflight checks...")
    require_python(3, 8)
    require_command("docker")
    require_dir(project_root, "Project root")
    # require_dir(project_root / "code", "Code directory ('code')")
    require_docker_running()
    info("Preflight checks OK.")


@dataclass
class TestResultRow:
    module_function_name: str   # C function under test
    test_name: str              # Unity test function name
    status: str
    linesCvrg: str
    branchesCvrg: str


    def to_csv_line(self) -> str:
        return (
            f"{self.module_function_name},"
            f"{self.test_name},"
            f"{self.status},"
            f"{self.linesCvrg},"
            f"{self.branchesCvrg},"
        )

@dataclass
class UnitModule:
    module_name: str
    function_name: str
    source_dir: Path
    test_root: Path

    @property
    def test_case_folder(self) -> Path:
        return self.test_root / f"{UNIT_TEST_PREFIX}{self.function_name}"

    @property
    def test_c_path(self) -> Path:
        return self.test_case_folder / "src" / f"{self.function_name}.c"


def split_unity_tests(relative_dir):
    import os
    import re

    # Regex aggiornata:
    # trova funzioni void test_XXX(void) oppure void testXXX(void)
    FUNC_RE = re.compile(
        r'\bvoid\s+(test_[A-Za-z0-9_]*|test[A-Za-z0-9_]*)\s*\(\s*void\s*\)\s*{',
        re.MULTILINE
    )

    def _match_brace(text, idx):
        depth = 0
        i = idx
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        return -1

    def _find_function(text, name):
        pat = re.compile(r'\bvoid\s+' + re.escape(name) + r'\s*\(\s*void\s*\)\s*{')
        m = pat.search(text)
        if not m:
            return None
        start = m.start()
        brace = text.find("{", m.end() - 1)
        end = _match_brace(text, brace)
        return text[start:end+1]

    script_dir = os.path.abspath(os.path.dirname(__file__))
    input_dir = os.path.abspath(os.path.join(script_dir, relative_dir))

    if not os.path.isdir(input_dir):
        raise ValueError(f"Directory does not exist: {input_dir}")

    c_files = [f for f in os.listdir(input_dir) if f.startswith("test_") and f.endswith(".c")]
    if not c_files:
        raise RuntimeError("No test_*.c files found.")

    original_files = [os.path.join(input_dir, f) for f in c_files]

    for c_file in c_files:
        c_path = os.path.join(input_dir, c_file)
        with open(c_path, "r", encoding="utf-8") as f:
            text = f.read()

        setup = _find_function(text, "setUp")
        teardown = _find_function(text, "tearDown")

        tests = []
        for m in FUNC_RE.finditer(text):
            name = m.group(1)  # già cattura test_X o testX
            brace = text.find("{", m.end() - 1)
            end = _match_brace(text, brace)
            tests.append((name, text[m.start():end+1]))

        if not tests:
            continue

        indices = []
        if setup:
            indices.append(text.index(setup))
        if teardown:
            indices.append(text.index(teardown))

        if indices:
            preamble_end = min(indices)
        else:
            first_test_start = min(text.index(snippet) for _, snippet in tests)
            preamble_end = first_test_start

        preamble = text[:preamble_end].rstrip() + "\n\n"

        for name, body in tests:
            base_no_ext = os.path.splitext(c_file)[0]
            base_core = base_no_ext[5:] if base_no_ext.startswith("test_") else base_no_ext

            # Rimuove prefisso test_ o test
            if name.startswith("test_"):
                func_core = name[5:]
            elif name.startswith("test"):
                func_core = name[4:]
            else:
                func_core = name

            prefix_to_strip = base_core + "_"

            if func_core.startswith(prefix_to_strip):
                output_name = func_core[len(prefix_to_strip):]
            else:
                output_name = func_core

            out_path = os.path.join(input_dir, f"test_{output_name}.c")

            if os.path.exists(out_path):
                counter = 1
                while os.path.exists(os.path.join(input_dir, f"test_{output_name}_{counter}.c")):
                    counter += 1
                out_path = os.path.join(input_dir, f"test_{output_name}_{counter}.c")

            with open(out_path, "w", encoding="utf-8") as out:
                out.write(preamble)
                if setup:
                    out.write(setup + "\n\n")
                if teardown:
                    out.write(teardown + "\n\n")
                out.write(body + "\n")

    for orig in original_files:
        try:
            os.remove(orig)
        except:
            pass


def find_function_definition(root: Path, func_name: str):
    results = []
    pattern = re.compile(rf"\b{func_name}\s*\([^)]*\)", re.IGNORECASE)

    for c_file in root.rglob("*.c"):
        try:
            with c_file.open("r", encoding="utf-8", errors="ignore") as f:
                for i, line in enumerate(f, start=1):
                    if pattern.search(line):
                        results.append((c_file, i, line.strip()))
        except Exception as e:
            warn(f"Error reading '{c_file}': {e}")
    return results


def build_modules(root: Path):
    modules = []

    for test_dir in root.rglob("*"):
        if not test_dir.is_dir():
            continue
        if not test_dir.name.startswith(UNIT_TEST_PREFIX):
            continue

        func_name = test_dir.name.replace(UNIT_TEST_PREFIX, "", 1)
        test_root = test_dir.parent
        search_root = test_root.parent

        matches = find_function_definition(search_root, func_name)

        if matches:
            file_path, _, _ = matches[0]
            module_name = file_path.name
            source_dir = file_path.parent
        else:
            warn(f"No source file found for test folder '{test_dir}'")
            continue

        modules.append(UnitModule(module_name, func_name, source_dir, test_root))

    return modules


def find_and_extract_function(file_name: str, function_name: str, unit_path: Path):
    file_path: Path | None = None

    for candidate in unit_path.rglob(file_name):
        if candidate.is_file():
            file_path = candidate
            break

    if file_path is None:
        error(f"File '{file_name}' not found in directory '{unit_path}'.")
        return None

    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")

        function_pattern = re.compile(
            rf"""
            (?P<header>
                ^[ \t]*
                (?P<before>[^\n]*?)
                \b{function_name}\b
                \s*
                (?P<params>\([^)]*\))
                (?P<post_attr>
                    (?:\s*__attribute__\s*\(\([^)]*\)\))*
                )
            )
            \s*\{{
            """,
            re.MULTILINE | re.VERBOSE
        )

        match = function_pattern.search(content)
        if not match:
            warn(f"Function '{function_name}' not found in '{file_name}'.")
            return None

        brace_index = content.find("{", match.end("header"))
        if brace_index == -1:
            warn(f"Opening brace for function '{function_name}' not found in '{file_name}'.")
            return None

        open_braces = 0
        end_index = None
        for i in range(brace_index, len(content)):
            char = content[i]
            if char == "{":
                open_braces += 1
            elif char == "}":
                open_braces -= 1
                if open_braces == 0:
                    end_index = i
                    break

        if end_index is None:
            warn(f"Closing brace for function '{function_name}' not found in '{file_name}'.")
            return None

        before = match.group("before") or ""
        params = match.group("params")

        before_clean = re.sub(r'__attribute__\s*\(\([^)]*\)\)\s*', ' ', before)
        before_clean = re.sub(
            r'\b(static|inline|INLINE|extern|constexpr|volatile|register|__inline__|__forceinline)\b',
            ' ',
            before_clean
        )
        before_clean = before_clean.replace('\t', ' ')
        return_type = ' '.join(before_clean.split()) or "void"

        clean_header = f"{return_type} {function_name}{params}"
        body_part = content[brace_index:end_index + 1]
        return f"\n\n{clean_header} {body_part}"

    except Exception as e:
        error(f"Error reading file '{file_name}': {e}")
        return None


def modify_file_after_marker(file_path: Path, new_content: str):
    marker = "/* FUNCTION TO TEST */"
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
        marker_index = content.find(marker)
        if marker_index == -1:
            fatal(f"Marker '{marker}' not found in file: {file_path}")

        modified_content = content[:marker_index + len(marker)] + "\n" + new_content + "\n"
        file_path.write_text(modified_content, encoding="utf-8")
        info(f"Updated file: {file_path}")
    except FileNotFoundError:
        fatal(f"File not found: {file_path}")
    except Exception as e:
        fatal(f"Error modifying file '{file_path}': {e}")


def extract_function_name(path_str: str) -> str:
    filename = Path(path_str).name
    name_no_ext = Path(filename).stem
    if name_no_ext.startswith(UNIT_TEST_PREFIX):
        return name_no_ext[len(UNIT_TEST_PREFIX):]
    return name_no_ext


def update_unit_under_test(module: UnitModule, unit_name: str):
    extracted_body = find_and_extract_function(module.module_name, module.function_name, module.source_dir)
    if extracted_body is None:
        fatal(f"Cannot extract body for function '{module.function_name}' in module '{module.module_name}'")

    modify_file_after_marker(module.test_c_path, extracted_body)
    clear_folder(UNIT_EXECUTION_FOLDER)
    copy_folder_contents(module.test_case_folder, UNIT_EXECUTION_FOLDER)


def load_result_rows(summary_file: Path) -> dict[str, TestResultRow]:
    rows: dict[str, TestResultRow] = {}
    if not summary_file.exists() or summary_file.stat().st_size == 0:
        return rows

    text = summary_file.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    if not lines:
        return rows

    header_csv = "function_name,test_name,status,linesCvrg,branchesCvrg"

    header_line = lines[0].strip()
    if "function_name" not in header_line:
        header_line = header_csv

    headers = [h.strip() for h in header_line.split(",")]
    hmap = {name: i for i, name in enumerate(headers)}

    def hget(row_parts, name, default=""):
        i = hmap.get(name, -1)
        return row_parts[i].strip() if 0 <= i < len(row_parts) else default

    for line in lines[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        parts = [p.strip() for p in stripped.split(",")]

        tn = hget(parts, "test_name")
        if not tn:
            continue

        key = f"{hget(parts, 'function_name')}:{tn}"
        rows[key] = TestResultRow(
            module_function_name=hget(parts, "function_name"),
            test_name=tn,
            status=hget(parts, "status"),
            linesCvrg=hget(parts, "linesCvrg"),
            branchesCvrg=hget(parts, "branchesCvrg"),
        )

    return rows


def update_total_result_report(build_folder: Path, function_name: str, report_folder: Path):
    results_dir = build_folder / "gcov" / "results"
    coverage_dir = build_folder / "artifacts" / "gcov" / "gcovr"
    coverage_file = None

    now_str = datetime.now().strftime("%d/%m/%y %H:%M")

    # Cerca tutti gli HTML che iniziano con "GcovCoverageResults." ma NON contengono "_help"
    html_candidates = sorted(coverage_dir.glob("GcovCoverageResults*.html"))
    for cand in html_candidates:
        if "_help" not in cand.name:
            coverage_file = cand
            break

    if coverage_file is None:
        warn("Nessun file HTML di coverage valido trovato (tutti contenevano '_help').")


    now_str = datetime.now().strftime("%d/%m/%y %H:%M")

    report_folder.mkdir(parents=True, exist_ok=True)
    summary_file = report_folder / RESULT_REPORT
    rows = load_result_rows(summary_file)

    # ---------------------------------------------------------------------
    # Collect all .pass and .fail files inside gcov/results/
    # ---------------------------------------------------------------------
    test_files = list(results_dir.glob("*.pass")) + list(results_dir.glob("*.fail"))





    # Extract function name from file name (remove extension)
    # E.g. "test_myFunc.pass" → "myFunc"
    def extract_func_name(path: Path) -> str:
        name = path.stem  # "test_myFunc"
        if name.startswith("test_"):
            return name[5:]
        return name

    # ---------------------------------------------------------------------
    # Extract coverage from HTML if available
    # ---------------------------------------------------------------------
    def extract_coverage(html: str, label: str) -> Optional[str]:
        row_re = re.compile(
            rf"<tr>\s*<th[^>]*scope=\"row\"[^>]*>\s*{re.escape(label)}\s*</th>\s*"
            rf"<td[^>]*>.*?</td>\s*<td[^>]*>.*?</td>\s*<td[^>]*>(?P<pct>[^<]+)</td>\s*</tr>",
            re.IGNORECASE | re.DOTALL,
        )
        m = row_re.search(html)
        return m.group("pct").strip() if m else None

    html = None
    linesCvrg, branchesCvrg = None, None

    if coverage_file is not None and coverage_file.exists():
        try:
            html = coverage_file.read_text(encoding="utf-8", errors="ignore")
            linesCvrg = extract_coverage(html, "Lines:")
            branchesCvrg = extract_coverage(html, "Branches:")
        except Exception as e:
            warn(f"Error reading coverage file '{coverage_file}': {e}")
    else:
        warn("Coverage file does not exist or è stato ignorato perché conteneva '_help'.")



    # Se non ci sono test, segnala errore e aggiungi riga "NOT EXC"
    if not test_files:
        warn("Nessun test trovato in gcov/results/.")

        key = f"{function_name}:NO_TESTS"
        rows[key] = TestResultRow(
            module_function_name=function_name,
            test_name="NO_TESTS",
            status="NOT EXC",
            linesCvrg="-",
            branchesCvrg="-",
        )
    else:  
        # ---------------------------------------------------------------------
        # Process every test file found
        # ---------------------------------------------------------------------
        for f in test_files:
            test_name = extract_func_name(f)
            passed = f.suffix == ".pass"


            key = f"{function_name}:{test_name}"
            rows[key] = TestResultRow(
                module_function_name=function_name,   # real C FUNCTION under test
                test_name=test_name,                  # Unity test function name
                status="PASSED" if passed else "FAILED",
                linesCvrg=linesCvrg or "-",
                branchesCvrg=branchesCvrg or "-",
            )
            
    # ---------------------------------------------------------------------
    # Write CSV (no Tester column)
    # ---------------------------------------------------------------------
    header = "function_name,test_name,status,linesCvrg,branchesCvrg"
    lines_out = [header] + [row.to_csv_line() for row in rows.values()]
    summary_file.write_text("\n".join(lines_out) + "\n", encoding="utf-8")
    print(rows)
    info(f"Updated summary for {len(test_files)} test results → {summary_file}")


def format_total_result_report(report_folder: Path):
    summary_file = report_folder / RESULT_REPORT
    if not summary_file.exists():
        warn(f"Summary file does not exist: {summary_file}")
        return

    lines = summary_file.read_text(encoding="utf-8", errors="ignore").splitlines()
    if not lines:
        warn(f"Summary file is empty: {summary_file}")
        return

    header_parts = [h.strip() for h in lines[0].split(",") if h.strip()]
    if not header_parts:
        warn(f"Invalid header in summary file: {summary_file}")
        return

    data_rows: list[list[str]] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < len(header_parts):
            parts.extend([""] * (len(header_parts) - len(parts)))
        data_rows.append(parts[:len(header_parts)])

    if not data_rows:
        warn(f"No data rows to format: {summary_file}")
        return

    col_widths: list[int] = []
    for col_idx, header in enumerate(header_parts):
        max_data_len = max(len(row[col_idx]) for row in data_rows)
        col_widths.append(max(len(header), max_data_len))

    header_line = "| " + " | ".join(header_parts[i].ljust(col_widths[i]) for i in range(len(header_parts))) + " |\n"
    separator_line = "|" + "|".join("-" * (col_widths[i] + 2) for i in range(len(col_widths))) + "|\n"

    row_lines = ""
    for row in data_rows:
        row_lines += "| " + " | ".join(row[i].ljust(col_widths[i]) for i in range(len(row))) + " |\n"

    summary_file.write_text(header_line + separator_line + row_lines, encoding="utf-8")
    info(f"Formatted summary report: {summary_file}")


def run_and_collect_results(module: UnitModule):
    function_name = module.function_name
    update_unit_under_test(module, function_name)
    split_unity_tests(UNIT_EXECUTION_FOLDER_TEST)
    run_cmd(DOCKER_CLEAN, check=True)
    run_cmd(DOCKER_BASE + ["ceedling", f"gcov:all"], check=True, stopScript=False)
    update_total_result_report(UNIT_EXECUTION_FOLDER_BUILD, function_name, UNIT_RESULT_FOLDER)
    copy_folder_contents(UNIT_EXECUTION_FOLDER_BUILD, UNIT_RESULT_FOLDER / f"{function_name}Results")


def print_help():
    script_name = Path(sys.argv[0]).name
    print(f"""
Usage:
  python {script_name} <function_name|all>
  python {script_name} -h | --help | help
""".strip())


if __name__ == "__main__":
    preflight_check(
        script_dir=PROJECT_ROOT,
        min_python=(3, 8),
        require_docker=True,
        check_docker_daemon=True,
        # required_dirs=[(PROJECT_ROOT, 'Project root'), (PROJECT_ROOT / 'code', "Code directory ('code')")],
    )

    if len(sys.argv) < 2:
        print_help()
        sys.exit(1)

    if sys.argv[1] in ("-h", "--help", "help"):
        print_help()
        sys.exit(0)

    clear_folder(GIT_RESULT)
    clear_folder(UNIT_EXECUTION_FOLDER)
    clear_folder(UNIT_RESULT_FOLDER)

    unit_to_test = extract_function_name(sys.argv[1])
    info(f"Selected argument (function to test): {unit_to_test}")

    modules = build_modules(PROJECT_ROOT)

    if unit_to_test == "all":
        for module in modules:
            info(f"Processing unit: {module.function_name}")
            try:
                run_and_collect_results(module)
            except subprocess.CalledProcessError:
                fatal(f"Unit test failed for '{module.function_name}'. See error details above.")
    else:
        unit_metadata = [m for m in modules if m.function_name == unit_to_test]
        if not unit_metadata:
            fatal(f"No module found for function '{unit_to_test}'")

        try:
            run_and_collect_results(unit_metadata[0])
        except subprocess.CalledProcessError:
            fatal(f"Unit test failed for '{unit_to_test}'. See error details above.")

    format_total_result_report(UNIT_RESULT_FOLDER)

    copy_entire_folder(UNIT_RESULT_FOLDER, GIT_RESULT)
    clear_folder(UNIT_EXECUTION_FOLDER)
    clear_folder(UNIT_RESULT_FOLDER)
    info("Done.")
