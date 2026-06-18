#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
import sys
import time

from path_config_loader import load_paths

from common_utils import (
    info, warn, error,
    run_cmd, docker_mount_path,
    safe_unlink, safe_restore,
    print_summary,
    exit_code_from_failures
)

# This image is expected to be pulled from GHCR and locally tagged by the CI:
# docker pull ghcr.io/moa2ofo/git_action_scripts/llvm-c-parser:latest
# docker tag ghcr.io/moa2ofo/git_action_scripts/llvm-c-parser:latest llvm-c-parser:latest
IMAGE_NAME = "llvm-c-parser:latest"

DOXYFILE = "Doxyfile"


def patch_doxyfile(doxy_path: Path, project_name: str) -> None:
    """
    Patch only PROJECT_NAME.

    The INPUT paths are already defined inside the Doxyfile template,
    so this function must not modify INPUT.
    """
    content = doxy_path.read_text(encoding="utf-8", errors="replace")

    if re.search(r"^\s*PROJECT_NAME\s*=", content, flags=re.MULTILINE):
        content = re.sub(
            r"^\s*PROJECT_NAME\s*=.*$",
            f'PROJECT_NAME           = "{project_name}"',
            content,
            flags=re.MULTILINE,
        )
    else:
        content = f'PROJECT_NAME           = "{project_name}"\n' + content

    doxy_path.write_text(content, encoding="utf-8")


def main():
    paths = load_paths(__file__)
    script_dir = paths.script_dir
    codebase_root = paths.project_root

    template_doxyfile = script_dir / DOXYFILE
    dest_doxyfile = codebase_root / DOXYFILE

    project_name = codebase_root.name

    info(f"Template Doxyfile   : {template_doxyfile}")
    info(f"Codebase root       : {codebase_root}")
    info(f"Destination Doxyfile: {dest_doxyfile}")

    ok_targets = []
    fail_targets = []

    doxy_backup = None
    start_time = time.time()

    try:
        if not template_doxyfile.is_file():
            raise FileNotFoundError(f"Template Doxyfile not found: {template_doxyfile}")

        if not codebase_root.is_dir():
            raise NotADirectoryError(f"Codebase root not found: {codebase_root}")

        # Backup existing Doxyfile in codebase_root, if present
        if dest_doxyfile.exists():
            doxy_backup = codebase_root / (DOXYFILE + ".bak")
            shutil.move(str(dest_doxyfile), str(doxy_backup))

        # Copy template Doxyfile into codebase_root
        shutil.copy2(template_doxyfile, dest_doxyfile)

        # Patch only PROJECT_NAME.
        # INPUT is not modified because the Doxyfile already contains all paths.
        patch_doxyfile(dest_doxyfile, project_name)

        mount = docker_mount_path(codebase_root)

        print(f"   - Using prebuilt Docker image: {IMAGE_NAME}")
        print(f"   - Running Doxygen in Docker from codebase_root")
        print(f"   - Docker mount: {mount}")

        print("   - Checking doxygen version...")

        proc = subprocess.run(
            [
                "docker", "run", "--rm",
                IMAGE_NAME,
                "doxygen", "--version"
            ],
            cwd=codebase_root
        )

        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, proc.args)

        run_cmd(
            [
                "docker", "run", "--rm",
                "-v", f"{mount}:/workspace",
                IMAGE_NAME,
                "doxygen", "/workspace/Doxyfile"
            ],
            cwd=codebase_root,
            check=True
        )

        info("   [OK] Documentation generated.")
        ok_targets.append(codebase_root)

    except subprocess.CalledProcessError as e:
        msg = f"Command failed (exit={e.returncode})"
        error(f"   [FAIL] {codebase_root}: {msg}")
        fail_targets.append((codebase_root, msg))

    except Exception as e:
        msg = f"Unexpected error: {repr(e)}"
        error(f"   [FAIL] {codebase_root}: {msg}")
        fail_targets.append((codebase_root, msg))

    finally:
        # Cleanup temporary Doxyfile and restore original one, if it existed
        safe_unlink(dest_doxyfile)
        safe_restore(doxy_backup, dest_doxyfile)
        print("   - Cleanup done.")

    elapsed = time.time() - start_time
    print(f"<<< Completed in {elapsed:.1f}s")

    print_summary("FINAL SUMMARY", ok_targets, fail_targets)
    sys.exit(exit_code_from_failures(fail_targets))


if __name__ == "__main__":
    main()
