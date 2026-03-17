#!/usr/bin/env python3
from __future__ import annotations

import getpass
from datetime import datetime
import xml.etree.ElementTree as ET
import html
import re
from pathlib import Path
from typing import Union, Dict
import sys
import subprocess
import shutil

from common_utils import (
    info, warn, error, fatal,
    require_python, require_command, require_file, require_dir,
    require_docker_running,
    run_cmd,
    find_targets_with_subfolders,
    preflight_check,
    copy_entire_folder,
    delete_folder,
    delete_file,
    move_file

)
from path_config_loader import load_paths

PATHS = load_paths(__file__)

# Default MISRA rules file path (configurable via YAML)
MISRA_RULES_PATH = PATHS.sw_cmp_misra_rules_path


def copy_into_workspace(src_dir: Path, workspace: Path, name: str) -> Path:
    """
    Copy folder `src_dir` into `workspace/name`.
    - Skips if source doesn't exist.
    - Overwrites destination if it exists.
    Returns destination path.
    """
    src_dir = Path(src_dir).resolve()
    workspace = Path(workspace).resolve()
    dst_dir = (workspace / name).resolve()

    if not src_dir.is_dir():
        warn(f"Skip copy: source folder not found: {src_dir}")
        return dst_dir

    # Ensure destination is within workspace
    try:
        dst_dir.relative_to(workspace)
    except ValueError:
        raise ValueError(f"Destination escapes workspace: {dst_dir}")

    if dst_dir.exists():
        shutil.rmtree(dst_dir)

    shutil.copytree(src_dir, dst_dir)
    return dst_dir


# -------------------------
# Requirements / preflight
# -------------------------
def preflight_checks(script_dir: Path) -> None:
    """Verify all required prerequisites before starting."""
    info("Performing preflight checks...")

    require_python(3, 8)
    require_command("docker")

    require_file(script_dir / "CMakeLists.txt", "Template CMakeLists.txt")
    # require_dir(script_dir / "code", "Code directory")

    if not MISRA_RULES_PATH.is_file():
        warn(
            f"MISRA rules file not found: {MISRA_RULES_PATH}. "
            "cppcheck severities will be used instead."
        )

    require_docker_running()
    info("Preflight checks OK.")


# ----------------------------------------------------
# Existing logic (cleaned paths)
# ----------------------------------------------------
def load_misra_rules(misra_rules_path: Union[str, Path]) -> Dict[str, str]:
    rules: Dict[str, str] = {}
    path = Path(misra_rules_path).resolve()

    if not path.is_file():
        warn(f"MISRA rules file not found: {path}. cppcheck severities will be used instead.")
        return rules

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or not line.startswith("Rule "):
                continue

            rest = line[5:].strip()
            parts = rest.split("\t")
            if len(parts) < 2:
                parts = rest.split(None, 1)
                if len(parts) < 2:
                    continue

            rule_id = parts[0].strip()
            severity = parts[1].strip()

            if rule_id:
                rules[rule_id] = severity

    info(f"Loaded {len(rules)} MISRA rules from {path}")
    return rules


MISRA_ID_REGEX = re.compile(r"misra-c2012-(\d+\.\d+)")




def generate_html_for_cppcheck_xml(xml_path: Union[str, Path], misra_rules_path: Union[str, Path]) -> str:
    xml_path = Path(xml_path).resolve()
    xml_dir = xml_path.parent

    tree = ET.parse(xml_path)
    root = tree.getroot()

    errors = root.findall(".//error")
    if not errors:
        info(f"No <error> elements found in {xml_path}")
        return ""

    misra_rules = load_misra_rules(misra_rules_path)

    attr_names = set()
    for err in errors:
        attr_names.update(err.attrib.keys())

    attr_names.discard("cwe")
    attr_names.discard("file0")
    attr_names.discard("verbose")

    preferred_order = ["id", "severity", "file1", "msg"]
    ordered_attrs = [a for a in preferred_order if a in attr_names]
    ordered_attrs.extend(sorted(attr_names - set(ordered_attrs)))
    columns = ordered_attrs + ["locations"]

    rows_html = []

    for err in errors:
        cells = []

        err_id = err.attrib.get("id", "")
        severity_val = ""

        m = MISRA_ID_REGEX.search(err_id)
        if m:
            rule_number = m.group(1)
            severity_val = misra_rules.get(rule_number, "")

        if not severity_val:
            severity_val = err.attrib.get("severity", "")

        severity_for_row = severity_val or ""
        sev_norm = severity_for_row.strip().lower()

        row_style = ""
        if "advisory" in sev_norm:
            row_style = ' style="background-color: #ffff99;"'
        elif "required" in sev_norm:
            row_style = ' style="background-color: #ffcc80;"'
        elif "mandatory" in sev_norm:
            row_style = ' style="background-color: #ff9999;"'

        for col in ordered_attrs:
            val = severity_for_row if col == "severity" else err.attrib.get(col, "")
            cells.append("<td>%s</td>" % html.escape(val))

        locations_html = []
        for loc in err.findall("location"):
            file_ = loc.attrib.get("file", "")
            line = loc.attrib.get("line", "")
            col_ = loc.attrib.get("column", "")
            info_txt = loc.attrib.get("info", "")

            # Build "file:line:column" (omit missing parts)
            parts = []
            if file_:
                parts.append(file_)
            if line:
                parts.append(line)
            if col_:
                parts.append(col_)

            pos_text = ":".join(parts) if parts else ""
            info_text = f" - {info_txt}" if info_txt else ""

            # Escape everything for HTML safety
            locations_html.append(f"{html.escape(pos_text)}{html.escape(info_text)}")


        loc_html = "<br>".join(locations_html)
        cells.append("<td>%s</td>" % loc_html)

        rows_html.append("<tr%s>%s</tr>" % (row_style, "".join(cells)))

    header_cells = "".join("<th>%s</th>" % html.escape(col) for col in columns)
    header_html = "<tr>%s</tr>" % header_cells

    css = """
table {
    border-collapse: collapse;
    width: 100%;
    font-family: Arial, sans-serif;
    font-size: 14px;
}
th, td {
    border: 1px solid #ccc;
    padding: 4px 8px;
}
th {
    background-color: #f2f2f2;
}
tbody tr:hover td {
    background-color: #e8f2ff;
}
.meta {
    margin: 6px 0 10px 0;
}
"""

    title = "Cppcheck MISRA Results"
    tester = getpass.getuser()
    now = datetime.now()
    meta_line = f"Tester: {html.escape(tester)}&nbsp;&nbsp;Date: {now:%d/%m/%y}&nbsp;&nbsp;Time: {now:%H:%M:%S}"

    html_doc = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<h1>{title}</h1>
<p class="meta"><strong>{meta_line}</strong></p>
<p>Source file: <code>{html.escape(str(xml_path))}</code></p>
<table>
<thead>{header_html}</thead>
<tbody>{''.join(rows_html)}</tbody>
</table>
</body>
</html>
"""

    html_path = xml_path.with_suffix(".html")
    html_path.write_text(html_doc, encoding="utf-8")

    cleaned = html_path.read_text(encoding="utf-8").replace("\\011", " ").replace("\\342\\200\\246", "…")
    html_path.write_text(cleaned, encoding="utf-8")

    info(f"Generated: {html_path}")

    try:
        xml_path.unlink()
        info(f"Deleted source XML: {xml_path}")
    except Exception as e:
        warn(f"Could not delete {xml_path}: {e}")

    return str(html_path)


def generate_cppcheck_html_reports(root_folder: Union[str, Path], misra_rules_path: Union[str, Path]) -> None:
    root = Path(root_folder).resolve()
    for xml_path in root.rglob("*"):
        if xml_path.is_file() and xml_path.name in ("cppcheck_misra_results.mxl", "cppcheck_misra_results.xml"):
            try:
                generate_html_for_cppcheck_xml(xml_path, misra_rules_path)
            except Exception as e:
                error(f"Failed to process {xml_path}: {e}")


def scan_components(codebase_root: Path, template_content: str) -> list[Path]:
    codebase_root = Path(codebase_root).resolve()
    info(f"Scanning for components under: {codebase_root}")
    created: list[Path] = []

    for target_dir in find_targets_with_subfolders(codebase_root, ("pltf", "cfg")):
        target_dir = Path(target_dir)

        # ✅ Skip anything inside build directories
        if "build" in target_dir.parts:
            continue

        cmake_path = target_dir / "CMakeLists.txt"

        project_name = target_dir.name
        component_content = template_content.replace("projectName", project_name)

        info(f"Creating CMakeLists.txt in: {target_dir} (projectName -> {project_name})")
        try:
            cmake_path.write_text(component_content, encoding="utf-8")
        except Exception as e:
            error(f"Failed to write {cmake_path}: {e}")
            continue

        created.append(cmake_path)

    if not created:
        warn(f"No new CMakeLists.txt files were created under {codebase_root}. Nothing to do.")
        sys.exit(0)

    info("CMakeLists.txt created in:")
    for p in created:
        print(f" - {p}")
    return created


def build_and_run_docker(script_dir: Path) -> None:
    script_dir = Path(script_dir).resolve()
    info("Building Docker image: cmake-misra-multi")
    run_cmd(["docker", "build", "-t", "cmake-misra-multi", "."], cwd=script_dir, check=True)

    cwd = str(script_dir)
    info(f"Running analysis container with workspace: {cwd}")
    run_cmd(
        [
            "docker", "run", "--rm",
            "-v", f"{cwd}:/workspace",
            "cmake-misra-multi",
            "bash", "-lc",
            "build-and-check-all.sh"
        ],
        check=True,
    )


def generate_reports(codebase_root: Path, misra_rules_path: Path) -> None:
    info(f"Using MISRA rules file: {misra_rules_path}")
    generate_cppcheck_html_reports(codebase_root, misra_rules_path)


def main():
    script_dir = PATHS.script_dir
    repo_root = PATHS.sw_cmp_repo_root

    preflight_check(
        script_dir=script_dir,
        min_python=(3, 8),
        require_docker=True,
        check_docker_daemon=True,
        optional_files=[(MISRA_RULES_PATH, "MISRA rules file")],
    )

    template_path = PATHS.sw_cmp_template_path
    template_content = template_path.read_text(encoding="utf-8", errors="replace")

    # Copy cfg/pltf from repo root into workspace (script_dir)
    delete_folder(PATHS.sw_cmp_workspace_build_dir)
    delete_folder(PATHS.sw_cmp_repo_build_dir)
    delete_file(PATHS.sw_cmp_workspace_report_file)
    delete_file(PATHS.sw_cmp_repo_report_file)
    copy_entire_folder(PATHS.sw_cmp_repo_cfg_dir, PATHS.sw_cmp_workspace_cfg_dir, overwrite=True)
    copy_entire_folder(PATHS.sw_cmp_repo_pltf_dir, PATHS.sw_cmp_workspace_pltf_dir, overwrite=True)
    created: list[Path] = []
    try:
        created = scan_components(repo_root, template_content)
        build_and_run_docker(script_dir)
    except subprocess.CalledProcessError:
        fatal("Docker build/run failed. See error details above.")

    # Generate HTML reports from XMLs under repo_root
    generate_reports(repo_root, MISRA_RULES_PATH)


    move_file(PATHS.sw_cmp_workspace_report_file, PATHS.sw_cmp_repo_report_file)

    # Copy build back to repo root (if it exists)

    copy_entire_folder(PATHS.sw_cmp_workspace_build_dir, PATHS.sw_cmp_repo_build_dir, overwrite=True)

    # Cleanup temporary cfg/pltf in workspace
    delete_folder(PATHS.sw_cmp_workspace_cfg_dir)
    delete_folder(PATHS.sw_cmp_workspace_pltf_dir)
    info("Done.")


if __name__ == "__main__":
    main()

