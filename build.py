#!/usr/bin/env -S uv run --quiet --env-file=.env
# /// script
# requires-python = ">=3.13,<3.14"
# dependencies = ["typer==0.27.3", "loguru==0.7.3"]
# ///
"""Build reproducible Lambda ZIP packages and an exact build manifest."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile
import tomllib
import typer
from loguru import logger

ROOT = Path(__file__).resolve().parent


def run(*args):
    subprocess.run(args, cwd=ROOT, check=True)


def archive(directory, destination):
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for file in sorted(directory.rglob("*")):
            if file.is_file():
                info = zipfile.ZipInfo(str(file.relative_to(directory)), (2020, 1, 1, 0, 0, 0))
                info.external_attr = (0o755 if file.name == "bootstrap" else 0o644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, file.read_bytes())


def main(python_only: bool = typer.Option(False, help="Only rebuild the shared Python artifact.")):
    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    target = build / "python"
    # Generated package only; clear stale dependencies before constructing it.
    if target.exists():
        shutil.rmtree(target)
    target.mkdir()
    run("uv", "export", "-q", "--frozen", "--project", "backends/python", "--no-dev", "--no-emit-project",
        "--output-file", str(build / "python-requirements.txt"))
    run("uv", "pip", "install", "-q", "--python-version", "3.13", "--python-platform", "aarch64-manylinux_2_28",
        "--only-binary", ":all:", "--require-hashes", "--target", str(target), "-r", str(build / "python-requirements.txt"))
    shutil.copy(ROOT / "backends/python/app.py", target / "app.py")
    archive(target, build / "python.zip")
    if not python_only:
        run("uv", "run", "--frozen", "--group", "build", "cargo", "lambda", "build", "--release", "--arm64",
            "--manifest-path", "backends/rust/Cargo.toml", "--locked")
        rust = build / "rust"
        rust.mkdir(exist_ok=True)
        cargo_target = Path(os.environ.get("CARGO_TARGET_DIR", ROOT / "backends/rust/target"))
        shutil.copy(cargo_target / "lambda/benchmark-rust/bootstrap", rust / "bootstrap")
        archive(rust, build / "rust.zip")
    manifest = {"architecture": "arm64", "python_runtime": "python3.13", "rust_runtime": "provided.al2023",
                "rustc": subprocess.check_output(["rustc", "--version"], text=True).strip(),
                "artifacts": {}}
    for runtime in ["python", "rust"]:
        file = build / f"{runtime}.zip"
        if file.exists():
            manifest["artifacts"][runtime] = {"bytes": file.stat().st_size, "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}
    for file in ["uv.lock", "backends/python/uv.lock", "backends/rust/Cargo.lock", "package-lock.json",
                 "bench.py.lock", "build.py.lock", "remote.py.lock"]:
        p = ROOT / file
        if p.exists():
            manifest[file] = hashlib.sha256(p.read_bytes()).hexdigest()
    manifest["dependencies"] = {}
    for name, lock in [("python", "backends/python/uv.lock"), ("rust", "backends/rust/Cargo.lock")]:
        manifest["dependencies"][name] = [{"name": p["name"], "version": p["version"]}
            for p in tomllib.loads((ROOT / lock).read_text())["package"]]
    (build / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    logger.info("Built artifacts; manifest: {}", build / "manifest.json")


if __name__ == "__main__":
    typer.run(main)
