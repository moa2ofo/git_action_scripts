#!/usr/bin/env python3
import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Optional

IMG_TAG_DEFAULT = "llvm-c-parser:latest"
WORKDIR_IN_CONTAINER = "/workspace"


def sh(cmd: list[str], check=True, capture_output=False, text=True):
    return subprocess.run(cmd, check=check, capture_output=capture_output, text=text)


def docker_available():
    try:
        sh(["docker", "version"], check=True, capture_output=True)
        return True
    except Exception:
        return False


def build_image(tag: str, context: str = ".", dockerfile: Optional[str] = None):
    cmd = ["docker", "build", "-t", tag]
    if dockerfile:
        cmd += ["-f", dockerfile]
    cmd.append(context)
    print(f"[BUILD] {' '.join(cmd)}")
    sh(cmd)


def docker_run(tag: str, cmd: str, host_dir: str, interactive: bool = False):
    args = [
        "docker", "run", "--rm",
        "-w", WORKDIR_IN_CONTAINER,
        "-v", f"{host_dir}:{WORKDIR_IN_CONTAINER}"
    ]

    # Only add -it when interactive=True
    if interactive:
        args.append("-it")

    args += [tag, "bash", "-lc", cmd]

    print(f"[RUN] {' '.join(args)}")
    sh(args)


def main():
    parser = argparse.ArgumentParser(add_help=True)
    parser.set_defaults(action=None)

    sub = parser.add_subparsers(dest="action")

    # testgen: generate_test_units.py inside Docker
    p_testgen = sub.add_parser("testgen")
    p_testgen.add_argument("root", help="cartella progetto")
    p_testgen.add_argument("--tag", default=IMG_TAG_DEFAULT)
    p_testgen.add_argument("--host-dir", default=str(Path.cwd()))
    p_testgen.add_argument("--script", default="generate_test_units.py")
    p_testgen.add_argument("clang_args", nargs=argparse.REMAINDER)

    # build
    p_build = sub.add_parser("build")
    p_build.add_argument("--tag", default=IMG_TAG_DEFAULT)
    p_build.add_argument("--context", default=".")
    p_build.add_argument("--file", help="Dockerfile path/name", default=None)

    # bash interactive
    p_bash = sub.add_parser("bash")
    p_bash.add_argument("--tag", default=IMG_TAG_DEFAULT)
    p_bash.add_argument("--host-dir", default=str(Path.cwd()))

    # cmd (pass-through)
    p_cmd = sub.add_parser("cmd")
    p_cmd.add_argument("--tag", default=IMG_TAG_DEFAULT)
    p_cmd.add_argument("--host-dir", default=str(Path.cwd()))
    p_cmd.add_argument("command", nargs=argparse.REMAINDER)

    # clang-ast
    p_ast = sub.add_parser("clang-ast")
    p_ast.add_argument("file")
    p_ast.add_argument("--tag", default=IMG_TAG_DEFAULT)
    p_ast.add_argument("--host-dir", default=str(Path.cwd()))
    p_ast.add_argument("--std", default="c11")
    p_ast.add_argument("-I", dest="includes", action="append", default=[])
    p_ast.add_argument("-D", dest="defines", action="append", default=[])

    # libclang-ast
    p_lib = sub.add_parser("libclang-ast")
    p_lib.add_argument("file")
    p_lib.add_argument("--tag", default=IMG_TAG_DEFAULT)
    p_lib.add_argument("--host-dir", default=str(Path.cwd()))
    p_lib.add_argument("--std", default="c11")
    p_lib.add_argument("-I", dest="includes", action="append", default=[])
    p_lib.add_argument("-D", dest="defines", action="append", default=[])
    p_lib.add_argument("--script", default="parse_ast.py")

    args, unknown = parser.parse_known_args()

    if not docker_available():
        print("ERRORE: Docker non disponibile.", file=sys.stderr)
        sys.exit(1)

    if platform.system().lower().startswith("win") and " " in str(
            args.__dict__.get("host_dir", "")):
        print("[WARN] Attenzione: path con spazi.", file=sys.stderr)

    #
    # ACTIONS
    #
    if args.action == "build":
        build_image(args.tag, args.context, args.file)
        return

    if args.action == "bash":
        # ONLY this is interactive
        docker_run(args.tag, "bash", args.host_dir, interactive=True)
        return

    if args.action == "cmd":
        if not args.command:
            print("Uso: python run_docker.py cmd -- <comando>")
            sys.exit(2)

        cmd_tokens = list(args.command)
        if cmd_tokens and cmd_tokens[0] == "--":
            cmd_tokens = cmd_tokens[1:]

        if not cmd_tokens:
            print("Nessun comando dopo '--'")
            sys.exit(2)

        full_cmd = " ".join(cmd_tokens)

        # FIX: NON‑INTERACTIVE IN CI
        docker_run(args.tag, full_cmd, args.host_dir, interactive=False)
        return

    if args.action == "clang-ast":
        incs = " ".join(f'-I"{p}"' for p in args.includes)
        defs = " ".join(f'-D{d}' for d in args.defines)
        cmd = f'clang -std={args.std} {incs} {defs} -Xclang -ast-dump -fsyntax-only "{args.file}"'
        docker_run(args.tag, cmd, args.host_dir, interactive=False)
        return

    if args.action == "libclang-ast":
        incs = " ".join(f'-I"{p}"' for p in args.includes)
        defs = " ".join(f'-D{d}' for d in args.defines)
        cmd = f'python3 "{args.script}" "{args.file}" -- -std={args.std} {incs} {defs}'
        docker_run(args.tag, cmd, args.host_dir, interactive=False)
        return

    if args.action == "testgen":
        clang_args = list(args.clang_args)
        if clang_args and clang_args[0] == "--":
            clang_args = clang_args[1:]

        clang_str = " ".join(clang_args)
        cmd = f'python3 "{args.script}" "{args.root}" -- {clang_str}'

        # FIX: NON‑INTERACTIVE IN CI
        docker_run(args.tag, cmd, args.host_dir, interactive=False)
        return

    parser.print_help()


if __name__ == "__main__":
    main()