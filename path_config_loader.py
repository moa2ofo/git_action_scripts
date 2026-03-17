from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class PathsConfig:
    script_path: Path
    script_dir: Path
    project_root: Path
    git_result: Path
    unit_execution_folder: Path
    unit_execution_folder_test: Path
    unit_execution_folder_build: Path
    unit_result_folder: Path
    docker_mount_source: Path
    unit_test_root: Path

    sw_cmp_repo_root: Path
    sw_cmp_misra_rules_path: Path
    sw_cmp_template_path: Path

    sw_cmp_workspace_build_dir: Path
    sw_cmp_repo_build_dir: Path
    sw_cmp_workspace_report_file: Path
    sw_cmp_repo_report_file: Path
    sw_cmp_workspace_cfg_dir: Path
    sw_cmp_repo_cfg_dir: Path
    sw_cmp_workspace_pltf_dir: Path
    sw_cmp_repo_pltf_dir: Path


_REQUIRED_TOP_LEVEL_KEYS = {
    "base",
    "project_root",
    "git_result",
    "unit_execution_folder",
    "unit_execution_folder_test",
    "unit_execution_folder_build",
    "unit_result_folder",
    "docker_mount_source",
    "unit_test_root",
    "sw_cmp",
}

_REQUIRED_BASE_KEYS = {
    "workspace",
    "repo",
}

_REQUIRED_SW_CMP_KEYS = {
    "repo_root",
    "misra_rules_path",
    "template_path",
    "build_dir",
    "report_file",
    "cfg_dir",
    "pltf_dir",
}


def _resolve_path(base_dir: Path, raw_value: str) -> Path:
    path = Path(raw_value)
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return path


def _require_keys(section_name: str, data: dict[str, Any], required: set[str]) -> None:
    missing_keys = sorted(required - set(data))
    if missing_keys:
        raise KeyError(f"Missing required keys in '{section_name}': {', '.join(missing_keys)}")


def _load_yaml(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raise FileNotFoundError(f"YAML config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    if not isinstance(data, dict):
        raise ValueError(f"Invalid YAML structure in {config_path}: expected a mapping at root level")

    paths_section = data.get("paths", data)
    if not isinstance(paths_section, dict):
        raise ValueError(f"Invalid 'paths' section in {config_path}: expected a mapping")

    _require_keys("paths", paths_section, _REQUIRED_TOP_LEVEL_KEYS)

    base_section = paths_section["base"]
    if not isinstance(base_section, dict):
        raise ValueError("Invalid 'base' section: expected a mapping")
    _require_keys("paths.base", base_section, _REQUIRED_BASE_KEYS)

    sw_cmp_section = paths_section["sw_cmp"]
    if not isinstance(sw_cmp_section, dict):
        raise ValueError("Invalid 'sw_cmp' section: expected a mapping")
    _require_keys("paths.sw_cmp", sw_cmp_section, _REQUIRED_SW_CMP_KEYS)

    return paths_section


def _resolve_dual_path(base_workspace: Path, base_repo: Path, relative_value: str) -> tuple[Path, Path]:
    return (
        (base_workspace / relative_value).resolve(),
        (base_repo / relative_value).resolve(),
    )


def load_paths(current_file: str | Path, config_name: str = "path_cfg.yml") -> PathsConfig:
    script_path = Path(current_file).resolve()
    script_dir = script_path.parent
    config_path = script_dir / config_name
    paths = _load_yaml(config_path)

    base = paths["base"]
    sw_cmp = paths["sw_cmp"]

    workspace_base = _resolve_path(script_dir, base["workspace"])
    repo_base = _resolve_path(script_dir, base["repo"])

    sw_cmp_workspace_build_dir, sw_cmp_repo_build_dir = _resolve_dual_path(
        workspace_base, repo_base, sw_cmp["build_dir"]
    )
    sw_cmp_workspace_report_file, sw_cmp_repo_report_file = _resolve_dual_path(
        workspace_base, repo_base, sw_cmp["report_file"]
    )
    sw_cmp_workspace_cfg_dir, sw_cmp_repo_cfg_dir = _resolve_dual_path(
        workspace_base, repo_base, sw_cmp["cfg_dir"]
    )
    sw_cmp_workspace_pltf_dir, sw_cmp_repo_pltf_dir = _resolve_dual_path(
        workspace_base, repo_base, sw_cmp["pltf_dir"]
    )

    return PathsConfig(
        script_path=script_path,
        script_dir=script_dir,
        project_root=_resolve_path(script_dir, paths["project_root"]),
        git_result=_resolve_path(script_dir, paths["git_result"]),
        unit_execution_folder=_resolve_path(script_dir, paths["unit_execution_folder"]),
        unit_execution_folder_test=_resolve_path(script_dir, paths["unit_execution_folder_test"]),
        unit_execution_folder_build=_resolve_path(script_dir, paths["unit_execution_folder_build"]),
        unit_result_folder=_resolve_path(script_dir, paths["unit_result_folder"]),
        docker_mount_source=_resolve_path(script_dir, paths["docker_mount_source"]),
        unit_test_root=_resolve_path(script_dir, paths["unit_test_root"]),
        sw_cmp_repo_root=_resolve_path(script_dir, sw_cmp["repo_root"]),
        sw_cmp_misra_rules_path=_resolve_path(script_dir, sw_cmp["misra_rules_path"]),
        sw_cmp_template_path=_resolve_path(script_dir, sw_cmp["template_path"]),
        sw_cmp_workspace_build_dir=sw_cmp_workspace_build_dir,
        sw_cmp_repo_build_dir=sw_cmp_repo_build_dir,
        sw_cmp_workspace_report_file=sw_cmp_workspace_report_file,
        sw_cmp_repo_report_file=sw_cmp_repo_report_file,
        sw_cmp_workspace_cfg_dir=sw_cmp_workspace_cfg_dir,
        sw_cmp_repo_cfg_dir=sw_cmp_repo_cfg_dir,
        sw_cmp_workspace_pltf_dir=sw_cmp_workspace_pltf_dir,
        sw_cmp_repo_pltf_dir=sw_cmp_repo_pltf_dir,
    )