"""Statistical summaries, explicit cost assumptions, and a buildless report."""
from collections import Counter
from datetime import datetime, timezone
import csv
import gzip
import html
import json
import math
from pathlib import Path
import shutil
import statistics

from .runner import MEMORIES, VARIANTS, SCENARIOS, dump
from .telemetry import selected

METRICS = ["http_ms", "sdk_ms", "handler_ms", "billed_ms", "init_ms", "restore_ms", "billed_restore_ms", "max_memory_mb"]
SCENARIO_LABELS = {"health": "Framework · GET /health", "validate": "Validation · valid", "validate_invalid": "Validation · invalid",
                   "users": "DynamoDB · POST /users", "files": "S3 · PUT /files (1 KB)"}
MIX = {"health": .25, "validate": .25, "users": .25, "files": .25}


def percentile(values, percent):
    index = (len(values) - 1) * percent / 100
    low, high = math.floor(index), math.ceil(index)
    return values[low] + (values[high] - values[low]) * (index - low)


def stats(observations):
    values = sorted(float(v) for v in observations if v is not None and math.isfinite(float(v)))
    if not values:
        return None
    result = {"count": len(values), "mean": statistics.mean(values), "min": values[0], "max": values[-1],
              "stddev": statistics.stdev(values) if len(values) > 1 else 0.0}
    result.update({f"p{p}": percentile(values, p) for p in [50, 90, 95, 99]})
    result["outliers_iqr"] = 0
    q1, q3 = percentile(values, 25), percentile(values, 75)
    result["outliers_iqr"] = sum(v < q1-1.5*(q3-q1) or v > q3+1.5*(q3-q1) for v in values)
    # Outliers remain in statistics and downloadable raw results.
    return result


def costs(groups, metadata, pricing):
    if not pricing:
        return []
    rates = {key: value["usd"] for key, value in pricing["rates"].items()}
    rows = []
    for memory in MEMORIES:
        for variant in VARIANTS:
            relevant = [g for g in groups if g["variant"] == variant and g["memory_mb"] == memory]
            by_key = {(g["scenario"], g["phase"]): g for g in relevant}
            if not all((endpoint, "warm") in by_key and by_key[endpoint, "warm"]["metrics"]["billed_ms"]
                       for endpoint in MIX):
                continue
            if not all((endpoint, "cold") in by_key and by_key[endpoint, "cold"]["metrics"]["billed_ms"]
                       for endpoint in ["health", "users", "files"]):
                continue
            warm_ms = sum(MIX[e]*by_key[e,"warm"]["metrics"]["billed_ms"]["mean"] for e in MIX)
            cold_ms = sum(MIX[e]*by_key["health" if e == "validate" else e,"cold"]["metrics"]["billed_ms"]["mean"] for e in MIX)
            # Lambda REPORT Billed Duration is the invocation duration billed by
            # Lambda. Do not add Billed Restore Duration again; it is diagnostic.
            initialization = [i["init_ms"] for i in metadata.get("initializations", [])
                              if i["variant"] == variant and i.get("memory_mb") == memory and i.get("init_ms") is not None]
            init_ms = statistics.mean(initialization) if initialization else 0
            for rate in [0, .01, .05, .2]:
                for requests in [10000, 100000, 1000000, 10000000]:
                    gb = memory / 1024
                    compute = requests*((1-rate)*warm_ms+rate*cold_ms)/1000*gb*rates["lambda_gb_s"]
                    request_cost = requests*rates["lambda_request"]
                    # One active production version, retained 730 hours/month.
                    # Minimum snapshot cache charge: 3 h per published version.
                    snap = variant == "python-snapstart"
                    caching = gb*max(730, 3)*3600*rates["snap_cache_gb_s"] if snap else 0
                    restoring = requests*rate*gb*rates["snap_restore_gb"] if snap else 0
                    # One publication/month and three initialization passes is
                    # an explicit model assumption, not measured AWS lifecycle.
                    init = 3*math.ceil(init_ms)/1000*gb*rates["lambda_gb_s"] if snap else 0
                    service = {"api_gateway": requests*rates["http_request"],
                               "dynamodb_writes": requests*MIX["users"]*rates["ddb_write"],
                               "s3_puts": requests*MIX["files"]*rates["s3_put"]}
                    # Each user record is <1KB. Assume 30-day retention and
                    # unique file keys; storage average = half end-month size.
                    service["dynamodb_storage"] = requests*MIX["users"]*256/(1024**3)/2*rates["ddb_storage_gb_month"]
                    service["s3_storage"] = requests*MIX["files"]*1024/(1024**3)/2*rates["s3_storage_gb_month"]
                    total = compute+request_cost+caching+restoring+init
                    rows.append({"variant":variant, "memory_mb":memory, "cold_fraction":rate, "requests":requests,
                        "lambda_duration":compute, "lambda_requests":request_cost, "snapshot_caching":caching,
                        "snapshot_restoration":restoring, "snapshot_initialization":init, "lambda_total":total,
                        "lambda_per_request":total/requests, "lambda_per_million":total/requests*1e6,
                        "services":service, "grand_total":total+sum(service.values()),
                        "warm_billed_ms":warm_ms, "cold_billed_ms":cold_ms})
    return rows


def summary(samples, metadata, pricing=None):
    groups = []
    for memory in MEMORIES:
        for scenario in SCENARIOS:
            for phase in ["cold", "warm"]:
                if phase == "cold" and scenario.startswith("validate"):
                    continue
                for variant in VARIANTS:
                    all_samples = [s for s in samples if s["memory_mb"] == memory and s["variant"] == variant
                                   and s["scenario"] == scenario and s["phase"] == phase]
                    accepted = [s for s in all_samples if selected(s)]
                    required = 100 if phase == "cold" or scenario == "validate_invalid" else 1000
                    metrics = {field: stats(s.get(field) for s in accepted) for field in METRICS}
                    group = {"memory_mb":memory, "scenario":scenario, "phase":phase, "variant":variant,
                        "observed":len(all_samples), "accepted":len(accepted), "required":required,
                        "complete":len(accepted)>=required, "metrics":metrics,
                        "errors":sum(bool(s.get("error")) or s.get("status") != s["expected_status"] for s in all_samples),
                        "throttles":sum(s.get("status")==429 for s in all_samples),
                        "exclusions":dict(Counter(s.get("exclude_reason") or "phase_mismatch" for s in all_samples if not selected(s))),
                        "new_environments":len({s.get("environment_id") for s in all_samples if s.get("classification")=="cold"}),
                        "warm_reuses":sum(s.get("classification")=="warm" for s in all_samples)}
                    groups.append(group)
    metadata = {**metadata, "initializations":metadata.get("initializations", [])}
    skipped = {(c["variant"],c["memory_mb"]):c for c in metadata.get("cells",[]) if c["status"]=="unsupported"}
    incomplete = [f'{g["variant"]}/{g["memory_mb"]}/{g["phase"]}/{g["scenario"]}: {g["accepted"]}/{g["required"]}'
                  for g in groups if not g["complete"] and (g["variant"],g["memory_mb"]) not in skipped]
    functional = [f for f in metadata.get("functional",[]) if (f["variant"],f["memory_mb"]) not in skipped]
    functional_ok = bool(functional) and all(all(check["passed"] for check in f["checks"]) for f in functional)
    expected_cells = {(v,m) for v in VARIANTS for m in MEMORIES} - set(skipped)
    checked_cells = {(f["variant"],f["memory_mb"]) for f in functional}
    functional_ok = functional_ok and expected_cells <= checked_cells
    price_ok = bool(pricing) and pricing.get("region") == metadata.get("region")
    complete = not incomplete and functional_ok and price_ok and metadata.get("status") == "complete" and metadata.get("client",{}).get("region") == metadata.get("region")
    result = {"groups":groups, "metadata":metadata, "pricing":pricing, "costs":costs(groups,metadata,pricing),
              "audit":{"complete":complete, "incomplete":incomplete, "functional_ok":functional_ok,
                       "pricing_ok":price_ok, "unsupported":list(skipped.values())},
              "assumptions": {"endpoint_mix":MIX, "snapshot_active_versions":1, "snapshot_hours_per_month":730,
                  "snapshot_minimum_hours":3, "snapshot_init_passes_per_month":3,
                  "validation_cold_proxy":"health cold billed duration", "ddb_record_storage_bytes":256,
                  "storage_retention_days":30, "free_tier":False, "discounts":False,
                  "excluded_costs":["CloudWatch", "EC2 benchmark client", "data transfer", "cleanup", "SDK retry request charges"],
                  "thresholds":{"cold_p95_ms":25,"cold_p95_percent":20,"warm_p95_percent":10,
                                "memory_mb":128,"monthly_cost_percent":20}}}
    result["recommendations"] = recommend(result)
    result["publish_initialization"] = [{"memory_mb": memory, "variant": variant,
        "statistics": stats(i.get("init_ms") for i in metadata.get("initializations", [])
                            if i["variant"] == variant and i.get("memory_mb") == memory)}
        for memory in MEMORIES for variant in VARIANTS]
    return result


def recommend(data):
    if not data["audit"]["complete"]:
        return ["The full matrix is incomplete. Runtime and memory recommendations are deferred until all supported cells meet the specified sample targets."]
    groups = data["groups"]
    candidates = {(g["variant"],g["memory_mb"]) for g in groups if g["complete"]}
    projected = [c for c in data["costs"] if c["requests"]==1000000 and c["cold_fraction"]==.01]
    findings = []
    for label, scenario, phase in [("Latency-sensitive APIs", "health", "cold"), ("DynamoDB-heavy backends", "users", "warm"),
                                    ("S3-heavy backends", "files", "warm")]:
        rows = [g for g in groups if g["scenario"]==scenario and g["phase"]==phase and g["complete"]]
        best = min(rows,key=lambda g:g["metrics"]["http_ms"]["p95"])
        findings.append(f'{label}: {best["variant"]} at {best["memory_mb"]} MB has the lowest measured HTTP p95 ({best["metrics"]["http_ms"]["p95"]:.2f} ms).')
    for label, volume in [("Low-traffic REST APIs",10000),("Cost-sensitive platforms",1000000)]:
        rows = [c for c in data["costs"] if c["requests"]==volume and c["cold_fraction"]==.01]
        if rows:
            best = min(rows,key=lambda c:c["lambda_total"])
            findings.append(f'{label}: {best["variant"]} at {best["memory_mb"]} MB has the lowest modeled Lambda cost (${best["lambda_total"]:.4f}/month at {volume:,} requests and 1% cold starts/restores).')
    for reference in ["python", "python-snapstart"]:
        comparisons = []
        for memory in MEMORIES:
            baseline = next((g for g in groups if g["variant"]==reference and g["memory_mb"]==memory and g["scenario"]=="health" and g["phase"]=="cold" and g["complete"]),None)
            rust = next((g for g in groups if g["variant"]=="rust" and g["memory_mb"]==memory and g["scenario"]=="health" and g["phase"]=="cold" and g["complete"]),None)
            if rust and baseline:
                b,r = baseline["metrics"]["http_ms"]["p95"],rust["metrics"]["http_ms"]["p95"]
                if b-r >= 25 and (b-r)/b >= .2:
                    comparisons.append(str(memory))
        findings.append(f'Rust vs {reference}: '+(f'framework cold HTTP p95 meets the stated migration threshold (at least 25 ms and 20% faster) at {", ".join(comparisons)} MB.' if comparisons else 'framework cold HTTP p95 does not meet the stated migration threshold at any comparable tested memory.'))
        warm_wins=[]
        for memory in MEMORIES:
            for endpoint in MIX:
                baseline = next((g for g in groups if g["variant"]==reference and g["memory_mb"]==memory and g["scenario"]==endpoint and g["phase"]=="warm" and g["complete"]),None)
                rust = next((g for g in groups if g["variant"]=="rust" and g["memory_mb"]==memory and g["scenario"]==endpoint and g["phase"]=="warm" and g["complete"]),None)
                if baseline and rust and rust["metrics"]["http_ms"]["p95"] <= .9*baseline["metrics"]["http_ms"]["p95"]:
                    warm_wins.append(f"{endpoint} at {memory} MB")
        findings.append(f'Rust vs {reference}: '+('warm HTTP p95 is at least 10% lower for '+', '.join(warm_wins)+'.' if warm_wins else 'no matched warm scenario meets the 10% p95 improvement threshold.'))
        successful = {v: sorted(c["memory_mb"] for c in data["metadata"].get("cells",[]) if c["variant"]==v and c["status"]=="complete")
                      for v in ["rust",reference]}
        if all(successful.values()):
            difference = min(successful[reference])-min(successful["rust"])
            findings.append(f'Minimum successful allocation: Rust {min(successful["rust"])} MB versus {reference} {min(successful[reference])} MB; {difference} MB less for Rust.')
        cost_wins=[]
        for memory in MEMORIES:
            base = next((c for c in projected if c["variant"]==reference and c["memory_mb"]==memory),None)
            rust = next((c for c in projected if c["variant"]=="rust" and c["memory_mb"]==memory),None)
            if base and rust and rust["lambda_total"]<=.8*base["lambda_total"]:
                cost_wins.append(str(memory))
        findings.append(f'Rust vs {reference}: '+(f'modeled Lambda cost is at least 20% lower at {", ".join(cost_wins)} MB, assuming one million requests/month and 1% cold starts/restores.' if cost_wins else 'no matched memory setting meets the 20% Lambda cost reduction threshold at one million monthly requests and 1% cold starts/restores.'))
    findings.append("These are descriptive rankings from one regional run, with no claim of statistical significance. Inspect standard deviations, outliers and per-endpoint costs. Python preserves FastAPI tooling and ecosystem familiarity; Rust adds compilation and AWS SDK binary size to deployment work. A migration is justified only where the measured savings meet your API requirements and outweigh that work.")
    return findings


def render(directory, output):
    samples = json.loads((directory / "samples.json").read_text())
    metadata = json.loads((directory / "metadata.json").read_text())
    metadata["initializations"] = json.loads((directory / "initializations.json").read_text())
    pricing_file = directory / "pricing.json"
    pricing = json.loads(pricing_file.read_text()) if pricing_file.exists() else None
    data = summary(samples,metadata,pricing)
    from .audit import verify
    # Exploratory reports remain useful; final recommendations require the raw
    # request records to agree with the retained CloudWatch evidence.
    evidence = verify(directory) if (directory / "cloudwatch.jsonl.gz").exists() else None
    data["evidence_audit"] = evidence
    if evidence is not None and not evidence["complete"]:
        data["audit"]["complete"] = False
        data["recommendations"] = recommend(data)
    output.mkdir(parents=True,exist_ok=True)
    dump(output / "summary.json",data)
    if evidence is not None:
        dump(output / "acceptance.json",evidence)
    with gzip.open(output / "samples.jsonl.gz","wt") as file:
        for sample in samples:
            file.write(json.dumps(sample,separators=(",",":"))+"\n")
    fields = sorted(set().union(*(s.keys() for s in samples))) if samples else []
    with gzip.open(output / "samples.csv.gz","wt",newline="") as file:
        writer = csv.DictWriter(file,fieldnames=fields)
        writer.writeheader()
        writer.writerows(samples)
    for name in ["metadata.json","initializations.json","pricing.json","cloudwatch.jsonl.gz","deployment.json","cleanup.json"]:
        source = directory / name
        if source.exists():
            shutil.copy(source,output / name)
    (output / ".nojekyll").touch()
    for name in ["style.css","app.js"]:
        shutil.copy(Path(__file__).parent / "web" / name,output / name)
    init = stats(i.get("init_ms") for i in metadata["initializations"])
    # All content in the initial HTML is usable without JavaScript. JS adds
    # memory/metric filtering and interactive cost controls.
    recommendations = "".join(f"<li>{html.escape(text)}</li>" for text in data["recommendations"])
    state = "Complete matrix" if data["audit"]["complete"] else "Incomplete measurements"
    initial = "".join(f'<tr><td>{g["variant"]}</td><td>{g["memory_mb"]} MB</td><td>{g["phase"]} · {html.escape(SCENARIO_LABELS[g["scenario"]])}</td>'
                    f'<td>{g["accepted"]}/{g["required"]}</td><td>{g["metrics"]["http_ms"]["p50"]:.2f}</td><td>{g["metrics"]["http_ms"]["p95"]:.2f}</td><td>{g["metrics"]["http_ms"]["p99"]:.2f}</td></tr>'
                    for g in data["groups"] if g["accepted"])
    template = (Path(__file__).parent / "web/index.html").read_text()
    replacements = {"TITLE":state,"REGION":html.escape(metadata["region"]),"DATE":html.escape(metadata["started_at"]),
                    "REQUESTS":f'{len(samples):,}',"RECOMMENDATIONS":recommendations,"INITIAL_ROWS":initial,
                    "DATA":json.dumps(data,separators=(",",":"),ensure_ascii=True).replace("<","\\u003c"),
                    "INIT":f'{init["mean"]:.2f} ms mean over {init["count"]} records' if init else "No INIT_REPORT records captured"}
    for key,value in replacements.items():
        template = template.replace("{{"+key+"}}",value)
    (output / "index.html").write_text(template)
