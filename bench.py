#!/usr/bin/env -S uv run --quiet --env-file=.env
# /// script
# requires-python = ">=3.13,<3.14"
# dependencies = ["boto3==1.43.111", "httpx==0.28.1", "typer==0.27.3", "loguru==0.7.3"]
# ///
"""Run the benchmark, capture pricing, or render the static report."""
import asyncio
import json
import os
from pathlib import Path
import typer
from loguru import logger
from benchmark.runner import Runner, MEMORIES, VARIANTS

ROOT = Path(__file__).resolve().parent
app = typer.Typer(add_completion=False)


@app.command()
def run(outputs: Path = typer.Option(ROOT / "infra/outputs.json", help="CDK deployment outputs."),
        directory: Path = typer.Option(ROOT / "results/run", help="New directory for all raw observations."),
        cold: int = typer.Option(100, min=1, help="Verified cold samples per scenario."),
        warm: int = typer.Option(1000, min=1, help="Successful warm samples per valid scenario."),
        invalid: int = typer.Option(100, min=1, help="Successful warm validation rejections."),
        max_batches: int = typer.Option(100, min=1, help="Maximum fresh versions per cold scenario."),
        memories: str = typer.Option("128,256,512,1024", help="Comma-separated memory matrix."),
        variants: str = typer.Option("python,python-snapstart,rust", help="Comma-separated variant matrix."),
        bursts: str = typer.Option("1,25,100", help="Cold burst sizes; each must be 1, 25, or 100.")):
    memory_values = [int(m) for m in memories.split(",")]
    variant_values = variants.split(",")
    burst_values = [int(b) for b in bursts.split(",")]
    if not set(memory_values) <= set(MEMORIES) or not set(variant_values) <= set(VARIANTS) or not set(burst_values) <= {1,25,100}:
        raise typer.BadParameter("Invalid matrix or burst values")
    region = os.environ.get("AWS_REGION")
    if not region:
        raise typer.BadParameter("Set AWS_REGION in .env")
    runner = Runner(region, json.loads(outputs.read_text()), directory, cold, warm, invalid, max_batches,
                    memory_values, variant_values, burst_values)
    asyncio.run(runner.run())
    logger.info("Raw results written to {}", directory)


@app.command()
def pricing(directory: Path = typer.Option(ROOT / "results/run", help="Result directory for pricing snapshot.")):
    from benchmark.pricing import fetch
    directory.mkdir(parents=True, exist_ok=True)
    fetch(os.environ["AWS_REGION"], directory)
    logger.info("Captured current AWS pricing in {}", directory)


@app.command()
def inventory(directory: Path = typer.Option(ROOT / "results/run", help="Result directory for deployment evidence."),
              outputs: Path = typer.Option(ROOT / "infra/outputs.json", help="CDK deployment outputs.")):
    from benchmark.inventory import capture, invariants
    directory.mkdir(parents=True,exist_ok=True)
    value = capture(os.environ["AWS_REGION"],json.loads(outputs.read_text()),directory)
    problems = invariants(value)
    if problems:
        logger.error("Deployment invariants failed: {}",problems)
        raise typer.Exit(1)
    logger.info("Verified deployment isolation and settings; wrote {}",directory / "deployment.json")


@app.command()
def audit(directory: Path = typer.Option(ROOT / "results/run", help="Raw measurement directory to verify against SPEC.")):
    from benchmark.audit import verify
    from benchmark.runner import dump
    result = verify(directory)
    dump(directory / "acceptance.json",result)
    if result["complete"]:
        logger.info("All {} measurement evidence checks passed",len(result["checks"]))
    else:
        logger.warning("{} requirements remain incomplete; see {}",len(result["failed_requirements"]),directory / "acceptance.json")
        raise typer.Exit(1)


@app.command()
def report(directory: Path = typer.Option(ROOT / "results/run", help="Directory containing real benchmark results."),
           output: Path = typer.Option(ROOT / "report", help="Static GitHub Pages output directory.")):
    from benchmark.report import render
    render(directory, output)
    logger.info("Wrote {}", output / "index.html")


if __name__ == "__main__":
    app()
