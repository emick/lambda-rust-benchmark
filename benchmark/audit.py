"""Verify retained measurement evidence against SPEC, without contacting AWS."""
import base64
from collections import Counter
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path

from .runner import EXPECTED, MEMORIES, VARIANTS, SCENARIOS
from .telemetry import parse_report, selected


def verify(directory):
    metadata = json.loads((directory / "metadata.json").read_text())
    samples = json.loads((directory / "samples.json").read_text())
    pricing_path = directory / "pricing.json"
    pricing = json.loads(pricing_path.read_text()) if pricing_path.exists() else None
    failures = []
    checks = []

    def check(name, valid, evidence):
        checks.append({"requirement": name, "passed": bool(valid), "evidence": evidence})
        if not valid:
            failures.append(name)

    reports, markers = {}, {}
    events = 0
    logs_path = directory / "cloudwatch.jsonl.gz"
    if logs_path.exists():
        with gzip.open(logs_path, "rt") as file:
            for line in file:
                event = json.loads(line)
                events += 1
                message = event["message"].strip()
                variant = event["variant"]
                if message.startswith("REPORT ") or message.startswith("REPORT\t"):
                    report = parse_report(message)
                    if report.get("request_id"):
                        reports[variant, report["request_id"]] = report
                elif message.startswith("{"):
                    try:
                        marker = json.loads(message)
                    except ValueError:
                        continue
                    if marker.get("type") == "benchmark":
                        markers[variant, marker["request_id"]] = marker

    bad_records = []
    accepted = [s for s in samples if selected(s)]
    counts = Counter()
    cold_environments = set()
    warm_predecessors = {}
    successful_operations = {}
    for sample in samples:
        environment = (sample["variant"], sample["version"], sample.get("environment_id"))
        counter = sample.get("invocation_counter")
        if counter is not None:
            warm_predecessors[environment] = min(warm_predecessors.get(environment, counter), counter)
        if sample.get("service") and sample.get("status") in [201, 204] and not sample.get("error") and counter is not None:
            key = (*environment, sample["service"])
            successful_operations[key] = min(successful_operations.get(key, counter), counter)

    for sample in accepted:
        reasons = []
        variant, scenario = sample["variant"], sample["scenario"]
        key = (variant, sample["request_id"])
        report, marker = reports.get(key), markers.get(key)
        environment = (variant, sample["version"], sample["environment_id"])
        if not report or not marker:
            reasons.append("missing raw application/REPORT record")
        else:
            for field in ["environment_id", "invocation_counter", "status"]:
                if marker.get(field) != sample.get(field):
                    reasons.append(f"application {field} mismatch")
            if marker.get("version") != sample["version"]:
                reasons.append("application version mismatch")
            for field in ["handler_ms", "billed_ms", "max_memory_mb", "memory_mb"]:
                if field not in report or report[field] != sample.get(field):
                    reasons.append(f"REPORT {field} mismatch")
            if report.get("platform_status") != "success":
                reasons.append("platform failure")
        if sample["actual_version"] != sample["version"]:
            reasons.append("API alias version mismatch")
        if sample["status"] != EXPECTED[scenario] or sample.get("error"):
            reasons.append("unexpected response")
        if not math.isfinite(sample["http_ms"]) or sample["http_ms"] < 0:
            reasons.append("invalid HTTP timing")
        if sample["phase"] == "cold":
            metric = "restore_ms" if sample["snapstart"] else "init_ms"
            if sample["invocation_counter"] != 1 or marker is None or marker.get("first_invocation") is not True:
                reasons.append("first invocation unproven")
            if report is None or metric not in report or report[metric] != sample.get(metric):
                reasons.append(f"missing/mismatched {metric}")
            if sample["snapstart"] and (report is None or "billed_restore_ms" not in report):
                reasons.append("missing billed restore telemetry")
            if environment in cold_environments:
                reasons.append("duplicate cold environment")
            cold_environments.add(environment)
        else:
            if warm_predecessors.get(environment, sample["invocation_counter"]) >= sample["invocation_counter"]:
                reasons.append("warm environment has no observed predecessor")
            if report and ("init_ms" in report or "restore_ms" in report):
                reasons.append("warm record contains startup telemetry")
        if scenario in ["users", "files"]:
            service = "dynamodb" if scenario == "users" else "s3"
            if sample.get("service") != service or marker is None or marker.get("service") != service:
                reasons.append("service marker mismatch")
            if marker is None or marker.get("sdk_ms") != sample.get("sdk_ms") or sample.get("sdk_ms") is None:
                reasons.append("SDK timing missing/mismatched")
            if sample.get("service_first_use") != (sample["phase"] == "cold"):
                reasons.append("SDK first-use classification mismatch")
            if marker is None or marker.get("service_first_use") != sample.get("service_first_use"):
                reasons.append("application SDK first-use marker mismatch")
            if sample["phase"] == "warm" and successful_operations.get((*environment,service),sample["invocation_counter"]) >= sample["invocation_counter"]:
                reasons.append("no prior successful SDK operation")
        if reasons:
            bad_records.append({"request_id": sample["request_id"], "variant": variant, "reasons": reasons})
        else:
            counts[variant, sample["memory_mb"], scenario, sample["phase"]] += 1

    check("Run finished successfully", metadata.get("status") == "complete", metadata.get("status"))
    check("Regional EC2 client", metadata.get("client",{}).get("region") == metadata["region"], metadata.get("client"))
    check("Accepted records match raw telemetry", bool(accepted) and not bad_records,
          {"accepted": len(accepted), "raw_log_events": events, "invalid_records": bad_records})
    ids = [s["request_id"] for s in samples if s.get("request_id")]
    check("Unique Lambda request identifiers", len(ids) == len(set(ids)), {"records": len(ids), "unique": len(set(ids))})

    cells = {(c["variant"],c["memory_mb"]):c for c in metadata.get("cells",[])}
    functional = {(f["variant"],f["memory_mb"]):f for f in metadata.get("functional",[])}
    fixture_path = Path(__file__).resolve().parents[1] / "fixtures/validation.json"
    expected_fixtures = {case["name"] for case in json.loads(fixture_path.read_text())} | {"dynamodb_persistence", "s3_binary_and_content_type"}
    matrix = []
    for variant in VARIANTS:
        for memory in MEMORIES:
            cell = cells.get((variant,memory),{})
            unsupported = cell.get("status") == "unsupported"
            if unsupported:
                check(f"Unsupported {variant}/{memory} documented", bool(cell.get("reason")), cell)
                continue
            check(f"Cell {variant}/{memory} complete", cell.get("status") == "complete", cell.get("status"))
            f = functional.get((variant,memory),{})
            passed = {c["fixture"] for c in f.get("checks",[]) if c["passed"]}
            check(f"Functional equivalence {variant}/{memory}", expected_fixtures <= passed,
                  {"passed":len(passed),"required":len(expected_fixtures),"missing": sorted(expected_fixtures-passed)})
            for scenario in SCENARIOS:
                for phase in ["cold", "warm"]:
                    if phase == "cold" and scenario.startswith("validate"):
                        continue
                    required = 100 if phase == "cold" or scenario == "validate_invalid" else 1000
                    observed = counts[variant,memory,scenario,phase]
                    entry = {"variant":variant,"memory_mb":memory,"scenario":scenario,"phase":phase,"samples":observed,"target":required}
                    matrix.append(entry)
                    check(f"Samples {variant}/{memory}/{scenario}/{phase}", observed >= required, entry)

    deployed = metadata.get("deployed",{})
    for variant in VARIANTS:
        config = deployed.get(variant,{})
        check(f"Runtime configuration {variant}", config.get("Architectures") == ["arm64"] and config.get("Timeout") == 15
              and config.get("Runtime") == ("provided.al2023" if variant == "rust" else "python3.13")
              and (config.get("SnapStart",{}).get("ApplyOn") == "PublishedVersions") == (variant == "python-snapstart"),config)
        artifact = metadata.get("build",{}).get("artifacts",{}).get("rust" if variant == "rust" else "python",{})
        digest = base64.b64decode(config["CodeSha256"]).hex() if config.get("CodeSha256") else None
        check(f"Artifact identity {variant}", bool(digest) and digest == artifact.get("sha256") and config.get("CodeSize") == artifact.get("bytes"),artifact)
    check("Identical Python artifacts", bool(deployed.get("python",{}).get("CodeSha256")) and
          deployed.get("python",{}).get("CodeSha256") == deployed.get("python-snapstart",{}).get("CodeSha256"),"CodeSha256 equality")
    inventory_path = directory / "deployment.json"
    if inventory_path.exists():
        from .inventory import invariants
        inventory = json.loads(inventory_path.read_text())
        problems = invariants(inventory)
        check("Deployed isolation, routing, IAM and Lambda invariants", not problems and inventory["region"] == metadata["region"],problems)
        for variant, entry in inventory["variants"].items():
            check(f"Stable artifact throughout run {variant}",entry["function"]["CodeSha256"] == deployed.get(variant,{}).get("CodeSha256"),entry["function"]["CodeSha256"])
            check(f"Table predates measurements {variant}",datetime.fromisoformat(entry["table"]["created_at"]) <= datetime.fromisoformat(metadata["started_at"]),entry["table"]["created_at"])
            date = entry["bucket"].get("created_at")
            check(f"Bucket predates measurements {variant}",bool(date) and datetime.fromisoformat(date) <= datetime.fromisoformat(metadata["started_at"]),date)
    else:
        check("Deployed isolation, routing, IAM and Lambda invariants",False,"deployment.json is missing; run bench.py inventory before cleanup")
    rates = ["lambda_request","lambda_gb_s","snap_cache_gb_s","snap_restore_gb","http_request","ddb_write",
             "ddb_storage_gb_month","s3_put","s3_storage_gb_month"]
    check("Dated regional pricing", bool(pricing) and pricing.get("region") == metadata["region"] and bool(pricing.get("date"))
          and all(pricing.get("rates",{}).get(r,{}).get("usd",0) > 0 for r in rates), pricing.get("date") if pricing else None)
    hashes = {}
    for name in ["metadata.json","samples.json","cloudwatch.jsonl.gz","initializations.json","pricing.json","deployment.json"]:
        file = directory / name
        if file.exists():
            hashes[name] = hashlib.sha256(file.read_bytes()).hexdigest()
    return {"complete":not failures,"checked_at":datetime.now(timezone.utc).isoformat(),"run_id":metadata.get("run_id"),
            "checks":checks,"failed_requirements":failures,"matrix":matrix,"source_sha256":hashes}
