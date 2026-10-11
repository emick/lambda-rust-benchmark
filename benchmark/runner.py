import asyncio
import base64
from collections import Counter
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import time
import uuid

import boto3
from botocore.exceptions import ClientError
import httpx
from loguru import logger

from .telemetry import join, selected

VARIANTS = ["python", "python-snapstart", "rust"]
MEMORIES = [128, 256, 512, 1024]
SCENARIOS = ["health", "validate", "validate_invalid", "users", "files"]
EXPECTED = {"health": 200, "validate": 200, "validate_invalid": 422, "users": 201, "files": 204}


def now():
    return datetime.now(timezone.utc).isoformat()


def dump(path, value):
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, indent=2, default=str) + "\n")
    temporary.replace(path)


class Runner:
    def __init__(self, region, outputs, directory, cold=100, warm=1000, invalid=100, max_batches=100,
                 memories=None, variants=None, bursts=(1, 25, 100)):
        self.region, self.directory = region, directory
        self.cold, self.warm, self.invalid, self.max_batches = cold, warm, invalid, max_batches
        self.memories, self.variants, self.bursts = memories or MEMORIES, variants or VARIANTS, bursts
        self.resources = {v: outputs[f"Benchmark-{v}"] for v in self.variants}
        self.directory.mkdir(parents=True, exist_ok=False)
        self.session = boto3.Session(region_name=region)
        self.lambda_client = self.session.client("lambda")
        self.logs = self.session.client("logs")
        self.cw = self.session.client("cloudwatch")
        self.s3 = self.session.client("s3")
        self.ddb = self.session.client("dynamodb")
        self.run_id = uuid.uuid4().hex
        self.samples, self.events, self.event_ids, self.cells, self.publications = [], [], set(), [], []
        self.initializations, self.functional = [], []
        self.log_watermarks = {}
        self.validations = json.loads(Path("fixtures/validation.json").read_text())
        self.payload = self.validations[0]["input"]
        self.file = Path("fixtures/file.bin").read_bytes()
        assert len(self.file) == 1024
        self.journal = (directory / "traffic.jsonl").open("a", buffering=1)
        self.started_ms = int(time.time() * 1000)
        self.meta = {"schema_version": 1, "run_id": self.run_id, "region": region, "started_at": now(),
            "settings": {"cold_target": cold, "warm_target": warm, "invalid_target": invalid,
                         "memories": self.memories, "variants": self.variants, "bursts": bursts,
                         "max_batches": max_batches, "http": {"http2": False, "timeout_s": 30, "retries": 0,
                                                               "max_connections": 100}},
            "build": json.loads(Path("build/manifest.json").read_text()),
            "cells": self.cells, "publications": self.publications, "functional": self.functional,
            "notes": ["No artificial holds or service prefetching.",
                      "Warm traffic is sequential. Variant order rotates per scenario and memory.",
                      "REPORT max memory is the execution environment high-water mark.",
                      "Suppressed initialization without telemetry is excluded conservatively.",
                      "Invalid validation 422 responses are expected successes."]}

    def preflight(self):
        account = self.lambda_client.get_account_settings()
        self.meta["concurrency"] = account
        if account["AccountLimit"]["ConcurrentExecutions"] < max(self.bursts):
            raise RuntimeError("Account concurrency quota cannot accommodate the requested burst")
        # Prove region of the measurement client through EC2 IMDS, without exposing
        # instance credentials. Outside EC2 is permitted only for exploratory runs.
        try:
            token = httpx.put("http://169.254.169.254/latest/api/token", headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
                              timeout=1, trust_env=False).text
            document = httpx.get("http://169.254.169.254/latest/dynamic/instance-identity/document",
                                 headers={"X-aws-ec2-metadata-token": token}, timeout=1, trust_env=False).json()
            self.meta["client"] = {k: document[k] for k in ["region", "instanceType", "availabilityZone"]}
            if document["region"] != self.region:
                raise RuntimeError("Client must run in the configured AWS region")
        except (httpx.HTTPError, ValueError, KeyError):
            self.meta["client"] = {"region": "unverified", "warning": "Exploratory client; regional location unverified"}
            if self.cold >= 100 and self.warm >= 1000 and self.invalid >= 100:
                raise RuntimeError("Full benchmark must run on an EC2 client in AWS_REGION; use the Runner stack")
        configurations = {}
        for variant, resource in self.resources.items():
            name = resource["FunctionName"]
            config = self.lambda_client.get_function_configuration(FunctionName=name)
            snap = config.get("SnapStart", {}).get("ApplyOn", "None")
            assert (snap == "PublishedVersions") == (variant == "python-snapstart"), "Incorrect SnapStart configuration"
            assert config["Architectures"] == ["arm64"] and config["Timeout"] == 15
            assert config["Runtime"] == ("provided.al2023" if variant == "rust" else "python3.13")
            artifact = self.meta["build"]["artifacts"]["rust" if variant == "rust" else "python"]
            assert base64.b64decode(config["CodeSha256"]).hex() == artifact["sha256"], "Deployed artifact does not match build manifest"
            assert config["CodeSize"] == artifact["bytes"], "Deployed artifact size does not match manifest"
            assert not config.get("VpcConfig", {}).get("VpcId") and not config.get("Layers")
            assert config.get("EphemeralStorage", {}).get("Size", 512) == 512
            provisioned = self.lambda_client.list_provisioned_concurrency_configs(FunctionName=name)
            assert not provisioned["ProvisionedConcurrencyConfigs"], "Provisioned concurrency must be disabled"
            concurrency = self.lambda_client.get_function_concurrency(FunctionName=name)
            limit = concurrency.get("ReservedConcurrentExecutions")
            if limit is not None and limit < max(self.bursts):
                raise RuntimeError(f"{variant} reserved concurrency {limit} is below burst size")
            configurations[variant] = {k: config.get(k) for k in ["Runtime", "Architectures", "CodeSha256", "CodeSize",
                "Timeout", "MemorySize", "SnapStart", "RuntimeVersionConfig"]}
        if "python" in configurations and "python-snapstart" in configurations:
            assert configurations["python"]["CodeSha256"] == configurations["python-snapstart"]["CodeSha256"], "Python artifacts differ"
        self.meta["deployed"] = configurations
        dump(self.directory / "metadata.json", self.meta)

    def publish(self, variant, memory, scenario):
        resource = self.resources[variant]
        name = resource["FunctionName"]
        config = self.lambda_client.get_function_configuration(FunctionName=name)
        environment = config["Environment"]["Variables"]
        environment["BENCHMARK_BATCH"] = uuid.uuid4().hex
        self.lambda_client.update_function_configuration(FunctionName=name, MemorySize=memory,
                                                       Environment={"Variables": environment})
        self.lambda_client.get_waiter("function_updated_v2").wait(FunctionName=name,
            WaiterConfig={"Delay": 2, "MaxAttempts": 150})
        publication = {"variant": variant, "memory_mb": memory, "scenario": scenario, "published_at": now()}
        version = self.lambda_client.publish_version(FunctionName=name,
            CodeSha256=self.meta["deployed"][variant]["CodeSha256"])["Version"]
        publication["version"] = version
        self.publications.append(publication)
        self.lambda_client.get_waiter("published_version_active").wait(FunctionName=name, Qualifier=version,
            WaiterConfig={"Delay": 2, "MaxAttempts": 300})
        config = self.lambda_client.get_function_configuration(FunctionName=name, Qualifier=version)
        if variant == "python-snapstart" and config.get("SnapStart", {}).get("OptimizationStatus") != "On":
            raise RuntimeError("SnapStart snapshot is not optimized")
        publication["active_at"] = now()
        publication["runtime_version"] = config.get("RuntimeVersionConfig")
        self.lambda_client.update_alias(FunctionName=name, Name=resource["Alias"], FunctionVersion=version)
        # Alias changes propagate asynchronously. Short setup delay is outside
        # all measured timings, with no HTTP request to prewarm the new version.
        time.sleep(2)
        return version

    async def request(self, client, variant, memory, version, scenario, phase, fixture=None):
        resource = self.resources[variant]
        request_id = uuid.uuid4().hex
        key = f"benchmark/{self.run_id}/{request_id}.bin"
        if scenario == "health":
            method, path, kwargs = "GET", "/health", {}
        elif scenario == "files":
            method, path, kwargs = "PUT", f"/files/{key}", {"content": self.file, "headers": {"content-type": "application/octet-stream"}}
        else:
            method, path = "POST", "/users" if scenario == "users" else "/validate"
            fixture = fixture or (self.validations[1] if scenario == "validate_invalid" else self.validations[0])
            kwargs = {"content": fixture.get("raw", json.dumps(fixture.get("input"))).encode(),
                      "headers": {"content-type": "application/json"}}
        sample = {"timestamp": now(), "timestamp_ms": int(time.time() * 1000), "runtime": "rust" if variant == "rust" else "python",
                  "variant": variant, "snapstart": variant == "python-snapstart", "version": version,
                  "scenario": scenario, "memory_mb": memory, "phase": phase, "expected_status": EXPECTED[scenario],
                  "request_id": None, "environment_id": None, "invocation_counter": None,
                  "service_first_use": None, "sdk_ms": None, "status": None, "error": None,
                  "fixture": fixture["name"] if fixture else None}
        start = time.perf_counter_ns()
        try:
            response = await client.request(method, resource["ApiUrl"].rstrip("/") + path, **kwargs)
            sample["http_ms"] = (time.perf_counter_ns() - start) / 1e6
            sample["status"] = response.status_code
            headers = response.headers
            sample["request_id"] = headers.get("x-benchmark-request-id")
            sample["environment_id"] = headers.get("x-benchmark-environment-id")
            sample["actual_version"] = headers.get("x-benchmark-version")
            if sample["actual_version"] != version:
                sample["error"] = "alias_version_mismatch"
            sample["invocation_counter"] = int(headers["x-benchmark-invocation"]) if "x-benchmark-invocation" in headers else None
            if "x-benchmark-service-first-use" in headers:
                sample["service_first_use"] = headers["x-benchmark-service-first-use"] == "true"
                sample["service"] = "dynamodb" if scenario == "users" else "s3"
                sample["sdk_ms"] = float(headers["x-benchmark-sdk-ms"])
            # Check endpoint semantics for every timed response, after latency
            # has been recorded. Parsing the response is outside HTTP timing.
            if response.status_code == EXPECTED[scenario]:
                if scenario == "health" and response.json() != {"status": "ok"}:
                    sample["error"] = "incorrect_health_response"
                elif scenario == "validate" and response.json() != {"tags": [], **fixture["input"]}:
                    sample["error"] = "incorrect_validation_response"
                elif scenario == "validate_invalid" and response.json() != {"error": "validation_failed"}:
                    sample["error"] = "incorrect_rejection_response"
                elif scenario == "users":
                    value = response.json()
                    uuid.UUID(value["id"])
                    sample["user_id"] = value.pop("id")
                    if value != {"tags": [], **self.payload}:
                        sample["error"] = "incorrect_user_response"
                elif scenario == "files":
                    sample["s3_key"] = key
                    if response.content:
                        sample["error"] = "nonempty_upload_response"
        except (httpx.HTTPError, ValueError, KeyError) as error:
            sample["http_ms"] = (time.perf_counter_ns() - start) / 1e6
            sample["error"] = f"{type(error).__name__}: {error}"
        self.samples.append(sample)
        self.journal.write(json.dumps(sample, separators=(",", ":")) + "\n")
        return sample

    def telemetry(self, variant, deadline=90):
        relevant = [s for s in self.samples if s["variant"] == variant]
        missing = [s["timestamp_ms"] for s in relevant if s.get("request_id") and
                   ("billed_ms" not in s or s.get("exclude_reason") == "missing_cloudwatch_records")]
        start_ms = min(missing) if missing else self.log_watermarks.get(variant,self.started_ms)
        wanted = {s["request_id"] for s in relevant if s.get("request_id")}
        until = time.monotonic() + deadline
        while True:
            pages = self.logs.get_paginator("filter_log_events").paginate(logGroupName=self.resources[variant]["LogGroup"],
                startTime=start_ms - 120000, endTime=int(time.time() * 1000))
            for page in pages:
                for event in page.get("events", []):
                    if event["eventId"] not in self.event_ids:
                        self.event_ids.add(event["eventId"])
                        self.events.append({"variant": variant, **event})
            events = [e for e in self.events if e["variant"] == variant]
            self.log_watermarks[variant] = int(time.time()*1000)-10000
            init = join(relevant, events)
            got = {s["request_id"] for s in relevant if "billed_ms" in s and s.get("exclude_reason") != "missing_cloudwatch_records"}
            if wanted <= got or time.monotonic() >= until:
                self.initializations = [i for i in self.initializations if i["variant"] != variant]
                for i in init:
                    publication = next((p for p in self.publications if p["variant"] == variant and p["version"] == i["version"]), {})
                    self.initializations.append({"variant": variant, "memory_mb": publication.get("memory_mb"), **i})
                return
            time.sleep(3)

    def count(self, variant, memory, scenario, phase):
        return sum(selected(s) for s in self.samples if s["variant"] == variant and s["memory_mb"] == memory
                   and s["scenario"] == scenario and s["phase"] == phase)

    async def smoke(self, client, variant, memory, version):
        checks = []
        for case in self.validations:
            scenario = "validate" if case["valid"] else "validate_invalid"
            sample = await self.request(client, variant, memory, version, scenario, "setup", case)
            checks.append({"fixture": case["name"], "passed": sample["status"] == EXPECTED[scenario] and not sample["error"]})
        user = await self.request(client, variant, memory, version, "users", "setup")
        if user.get("user_id"):
            persisted = await asyncio.to_thread(self.ddb.get_item, TableName=self.resources[variant]["TableName"],
                Key={"id": {"S": user["user_id"]}}, ConsistentRead=True)
            item = persisted.get("Item", {})
            checks.append({"fixture": "dynamodb_persistence", "passed": item == {
                "id": {"S": user["user_id"]}, "name": {"S": self.payload["name"]},
                "email": {"S": self.payload["email"]}, "age": {"N": str(self.payload["age"])},
                "tags": {"L": [{"S": t} for t in self.payload["tags"]]}}})
        else:
            checks.append({"fixture": "dynamodb_persistence", "passed": False})
        upload = await self.request(client, variant, memory, version, "files", "setup")
        if upload.get("s3_key"):
            persisted = await asyncio.to_thread(self.s3.get_object, Bucket=self.resources[variant]["BucketName"], Key=upload["s3_key"])
            checks.append({"fixture": "s3_binary_and_content_type", "passed": persisted["Body"].read() == self.file
                           and persisted["ContentType"] == "application/octet-stream"})
        else:
            checks.append({"fixture": "s3_binary_and_content_type", "passed": False})
        self.functional.append({"variant": variant, "memory_mb": memory, "checks": checks})
        if not all(check["passed"] for check in checks):
            raise RuntimeError(f"Functional equivalence failed for {variant} at {memory} MB")

    async def cold_scenario(self, client, variant, memory, scenario):
        for batch in range(self.max_batches):
            version = await asyncio.to_thread(self.publish, variant, memory, scenario)
            for concurrency in self.bursts:
                await asyncio.gather(*(self.request(client, variant, memory, version, scenario, "cold")
                                       for _ in range(concurrency)))
            await asyncio.to_thread(self.telemetry, variant)
            count = self.count(variant, memory, scenario, "cold")
            self.export()
            logger.info("{} {} MB {}: {}/{} verified cold", variant, memory, scenario, count, self.cold)
            # Export precedes removal of unused snapshots. Alias version remains.
            await asyncio.to_thread(self.prune_versions, variant)
            if count >= self.cold:
                return
        raise RuntimeError(f"Cold sample target unmet: {variant}/{memory}/{scenario}; max_batches reached")

    async def warm_scenario(self, client, variant, memory, scenario):
        version = self.lambda_client.get_alias(FunctionName=self.resources[variant]["FunctionName"],
                                              Name=self.resources[variant]["Alias"])["FunctionVersion"]
        target = self.invalid if scenario == "validate_invalid" else self.warm
        attempts = 0
        while self.count(variant, memory, scenario, "warm") < target:
            need = target - self.count(variant, memory, scenario, "warm")
            for i in range(min(need + 1, 250)):
                fixture = None
                if scenario == "validate_invalid":
                    invalid = [case for case in self.validations if not case["valid"]]
                    fixture = invalid[attempts % len(invalid)]
                await self.request(client, variant, memory, version, scenario, "warm", fixture)
                attempts += 1
            await asyncio.to_thread(self.telemetry, variant)
            if attempts > target * 3 + 100:
                raise RuntimeError(f"Warm target unmet: {variant}/{memory}/{scenario}")
        self.export()
        logger.info("{} {} MB {}: {} verified warm", variant, memory, scenario,
                    self.count(variant, memory, scenario, "warm"))

    def prune_versions(self, variant):
        resource = self.resources[variant]
        active = self.lambda_client.get_alias(FunctionName=resource["FunctionName"], Name=resource["Alias"])["FunctionVersion"]
        for publication in self.publications:
            if publication["variant"] != variant or publication.get("deleted_at") or publication["version"] == active:
                continue
            self.lambda_client.delete_function(FunctionName=resource["FunctionName"], Qualifier=publication["version"])
            publication["deleted_at"] = now()

    def cleanup_data(self, variant):
        resource = self.resources[variant]
        ids = {s["user_id"] for s in self.samples if s["variant"] == variant and s.get("user_id")}
        # Include timed-out writes that succeeded but whose HTTP response was
        # lost. This is a dedicated benchmark table, never application data.
        for page in self.ddb.get_paginator("scan").paginate(TableName=resource["TableName"], ProjectionExpression="id"):
            ids.update(item["id"]["S"] for item in page.get("Items", []))
        pending = [{"DeleteRequest": {"Key": {"id": {"S": uid}}}} for uid in ids]
        for offset in range(0, len(pending), 25):
            batch = {resource["TableName"]: pending[offset:offset+25]}
            while batch:
                batch = self.ddb.batch_write_item(RequestItems=batch).get("UnprocessedItems", {})
                if batch:
                    time.sleep(0.1)
        keys = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=resource["BucketName"], Prefix=f"benchmark/{self.run_id}/"):
            keys.extend({"Key": obj["Key"]} for obj in page.get("Contents", []))
        for offset in range(0, len(keys), 1000):
            response = self.s3.delete_objects(Bucket=resource["BucketName"], Delete={"Objects": keys[offset:offset+1000], "Quiet": True})
            if response.get("Errors"):
                raise RuntimeError("S3 cleanup failed")
        self.meta.setdefault("cleanup", []).append({"at": now(), "variant": variant, "users": len(ids), "files": len(keys)})

    def platform_metrics(self):
        self.meta["platform_metrics"] = {}
        for variant, resource in self.resources.items():
            metrics = {}
            for metric in ["Throttles", "Errors", "Invocations"]:
                response = self.cw.get_metric_statistics(Namespace="AWS/Lambda", MetricName=metric,
                    Dimensions=[{"Name": "FunctionName", "Value": resource["FunctionName"]}],
                    StartTime=datetime.fromtimestamp(self.started_ms / 1000, timezone.utc), EndTime=datetime.now(timezone.utc),
                    Period=60, Statistics=["Sum"])
                metrics[metric] = sum(d["Sum"] for d in response["Datapoints"])
            self.meta["platform_metrics"][variant] = metrics

    def export(self):
        self.meta["traffic"] = {"actual_http_requests": len(self.samples), "by_phase": dict(Counter(s["phase"] for s in self.samples)),
            "unexpected_errors": sum(bool(s["error"]) or s["status"] != s["expected_status"] for s in self.samples),
            "http_429": sum(s["status"] == 429 for s in self.samples),
            "excluded_reasons": dict(Counter(s.get("exclude_reason") for s in self.samples if s.get("exclude_reason"))),
            "verified_cold": sum(s.get("classification") == "cold" for s in self.samples),
            "warm_reuses": sum(s.get("classification") == "warm" for s in self.samples)}
        dump(self.directory / "metadata.json", self.meta)
        dump(self.directory / "samples.json", self.samples)
        dump(self.directory / "initializations.json", self.initializations)
        with gzip.open(self.directory / "cloudwatch.jsonl.gz", "wt") as file:
            for event in self.events:
                file.write(json.dumps(event, separators=(",", ":")) + "\n")

    async def run(self):
        self.preflight()
        limits = httpx.Limits(max_connections=100, max_keepalive_connections=100, keepalive_expiry=60)
        rotation = 0
        async with httpx.AsyncClient(timeout=30, limits=limits, http2=False, trust_env=False) as client:
            try:
                for memory in self.memories:
                    viable = []
                    for variant in self.variants:
                        cell = {"variant": variant, "memory_mb": memory, "status": "running", "started_at": now()}
                        self.cells.append(cell)
                        try:
                            version = await asyncio.to_thread(self.publish, variant, memory, "smoke")
                            probe = await self.request(client, variant, memory, version, "health", "setup")
                            try:
                                if probe["status"] != 200:
                                    raise RuntimeError("Health initialization failed")
                                await self.smoke(client, variant, memory, version)
                            except RuntimeError:
                                await asyncio.to_thread(self.telemetry, variant)
                                evidence = [event for event in self.events if event["variant"] == variant
                                            and f"[{version}]" in event.get("logStreamName", "")
                                            and ("OutOfMemory" in event["message"] or "signal: killed" in event["message"])]
                                if not evidence:
                                    raise
                                cell.update(status="unsupported", reason="out_of_memory", evidence=evidence)
                                logger.warning("Skipping {} {} MB: CloudWatch confirms memory exhaustion", variant, memory)
                                continue
                            viable.append(variant)
                        except ClientError as error:
                            # Service refusal is an unsupported configuration;
                            # correctness failure remains fatal rather than skipped.
                            code = error.response["Error"]["Code"]
                            if code != "InvalidParameterValueException":
                                raise
                            cell.update(status="unsupported", reason=str(error))
                    for phase, scenarios in [("cold", ["health", "users", "files"]), ("warm", SCENARIOS)]:
                        for scenario in scenarios:
                            order = self.variants[rotation % len(self.variants):] + self.variants[:rotation % len(self.variants)]
                            rotation += 1
                            self.meta.setdefault("batch_order", []).append({"memory_mb": memory, "phase": phase, "scenario": scenario, "variants": order})
                            for variant in order:
                                if variant in viable:
                                    if phase == "cold":
                                        await self.cold_scenario(client, variant, memory, scenario)
                                    else:
                                        await self.warm_scenario(client, variant, memory, scenario)
                    for variant in viable:
                        for cell in self.cells:
                            if cell["variant"] == variant and cell["memory_mb"] == memory:
                                cell.update(status="complete", finished_at=now())
                        await asyncio.to_thread(self.cleanup_data, variant)
                        self.export()
                await asyncio.to_thread(self.platform_metrics)
                self.meta["finished_at"] = now()
                self.meta["status"] = "complete"
            except Exception as error:
                self.meta["status"] = "failed"
                self.meta["error"] = f"{type(error).__name__}: {error}"
                raise
            finally:
                self.export()
                self.journal.close()
