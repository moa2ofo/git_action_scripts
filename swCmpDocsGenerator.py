#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations


import re
import shutil
import subprocess
from pathlib import Path
import sys
from typing import  List, Tuple
from path_config_loader import load_paths

from common_utils import (
    info, warn, error, fatal,
    require_python, require_command, require_dir, require_file,
    require_docker_running,
    run_cmd, docker_mount_path,
    safe_unlink, safe_restore,
    find_targets_with_subfolders,
    preflight_check,
    resolve_template,
    print_summary,
    exit_code_from_failures
)

IMAGE_NAME = "doxygen-plantuml"


DOCKERFILE = "Dockerfile"
DOXYFILE = "Doxyfile"


def patch_doxyfile(doxy_path: Path, project_name: str, has_pltf: bool, has_cfg: bool) -> None:
    # Read existing Doxyfile
    content = doxy_path.read_text(encoding="utf-8", errors="replace")

    # Force PROJECT_NAME to the target folder name
    if re.search(r"^\s*PROJECT_NAME\s*=", content, flags=re.MULTILINE):
        content = re.sub(
            r"^\s*PROJECT_NAME\s*=.*$",
            f'PROJECT_NAME           = "{project_name}"',
            content,
            flags=re.MULTILINE,
        )
    else:
        # If missing, prepend it
        content = f'PROJECT_NAME           = "{project_name}"\n' + content

    # Build INPUT path list based on available subfolders
    inputs = []
    if has_cfg:
        inputs.append("./cfg")
    if has_pltf:
        inputs.append("./pltf")

    # Override INPUT line
    input_line = "INPUT                  = " + " ".join(inputs)
    content = re.sub(r"^\s*INPUT\s*=.*$\n?", "", content, flags=re.MULTILINE)
    content = input_line + "\n" + content

    # Write patched file
    doxy_path.write_text(content, encoding="utf-8")

import time

def main():
    paths = load_paths(__file__)
    script_dir = paths.script_dir
    codebase_root = paths.sw_cmp_repo_root

    template_dockerfile = script_dir / DOCKERFILE
    template_doxyfile = script_dir / DOXYFILE

    info(f"Template Dockerfile : {template_dockerfile}")
    info(f"Template Doxyfile   : {template_doxyfile}")
    info(f"Scanning targets in : {codebase_root}")

    # Find all targets
    targets = list(find_targets_with_subfolders(codebase_root, ("pltf", "cfg")))
    # Filter out CMakeFiles
    targets = [t for t in targets if "CMakeFiles" not in t.parts]

    if not targets:
        warn("No folders found containing 'pltf' or 'cfg'. Nothing to do.")
        return

    total = len(targets)
    print(f"\n=== Found {total} valid targets ===\n")

    ok_targets = []
    fail_targets = []

    for idx, target_dir in enumerate(targets, start=1):
        print(f"\n>>> [{idx}/{total}] Processing target: {target_dir}")
        start_time = time.time()

        has_pltf = (target_dir / "pltf").is_dir()
        has_cfg = (target_dir / "cfg").is_dir()
        project_name = target_dir.parent.name

        dest_dockerfile = target_dir / DOCKERFILE
        dest_doxyfile = target_dir / DOXYFILE

        docker_backup = None
        doxy_backup = None

        try:
            # Backups
            if dest_dockerfile.exists():
                docker_backup = target_dir / (DOCKERFILE + ".bak")
                shutil.move(str(dest_dockerfile), str(docker_backup))

            if dest_doxyfile.exists():
                doxy_backup = target_dir / (DOXYFILE + ".bak")
                shutil.move(str(dest_doxyfile), str(doxy_backup))

            # Copy templates
            shutil.copy2(template_dockerfile, dest_dockerfile)
            shutil.copy2(template_doxyfile, dest_doxyfile)

            # Patch Doxyfile
            patch_doxyfile(dest_doxyfile, project_name, has_pltf, has_cfg)

            print("   - Building Docker image...")
            run_cmd(["docker", "build", "-t", IMAGE_NAME, "."], cwd=target_dir, check=True)

            mount = docker_mount_path(target_dir)
            print(f"   - Running Doxygen in Docker (mount: {mount})")

            run_cmd([
                "docker", "run", "--rm",
                "-v", f"{mount}:/workspace",
                IMAGE_NAME,
                "doxygen", "/workspace/Doxyfile"
            ], cwd=target_dir, check=True)

            info("   [OK] Documentation generated.")
            ok_targets.append(target_dir)

        except subprocess.CalledProcessError as e:
            msg = f"Command failed (exit={e.returncode})"
            error(f"   [FAIL] {target_dir}: {msg}")
            fail_targets.append((target_dir, msg))

        except Exception as e:
            msg = f"Unexpected error: {repr(e)}"
            error(f"   [FAIL] {target_dir}: {msg}")
            fail_targets.append((target_dir, msg))

        finally:
            # Cleanup
            safe_unlink(dest_dockerfile)
            safe_unlink(dest_doxyfile)
            safe_restore(docker_backup, dest_dockerfile)
            safe_restore(doxy_backup, dest_doxyfile)
            print("   - Cleanup done.")

        elapsed = time.time() - start_time
        print(f"<<< Completed [{idx}/{total}] in {elapsed:.1f}s")

    print_summary("FINAL SUMMARY", ok_targets, fail_targets)
    sys.exit(exit_code_from_failures(fail_targets))
if __name__ == "__main__":
    main()
