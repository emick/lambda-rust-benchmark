"""Conservative joins of application markers to CloudWatch Lambda records."""
import json
import re

FIELDS = {
    "handler_ms": "Duration", "billed_ms": "Billed Duration", "memory_mb": "Memory Size",
    "max_memory_mb": "Max Memory Used", "init_ms": "Init Duration",
    "restore_ms": "Restore Duration", "billed_restore_ms": "Billed Restore Duration",
}


def parse_report(message):
    result = {}
    for name, label in FIELDS.items():
        # Labels can be separated by spaces or tabs. The negative lookbehinds
        # keep handler/restore durations from matching inside longer labels.
        prefix = r"(?<!Billed )(?<!Restore )(?<!Init )" if label == "Duration" else r"(?<!Billed )" if label == "Restore Duration" else ""
        found = re.search(prefix + r"\b" + re.escape(label) + r":\s*([\d.]+)\s*(?:ms|MB)", message)
        if found:
            result[name] = float(found[1])
    request = re.search(r"RequestId:\s*([\w-]+)", message)
    if request:
        result["request_id"] = request[1]
    status = re.search(r"Status:\s*(\w+)", message)
    result["platform_status"] = status[1] if status else "success"
    return result


def join(samples, events):
    reports, application, initialization, failed_streams, suppressed = {}, {}, [], {}, set()
    for event in sorted(events, key=lambda x: (x["timestamp"], x.get("eventId", ""))):
        message = event["message"].strip()
        stream = event.get("logStreamName", "")
        if message.startswith("INIT_REPORT"):
            fields = parse_report(message)
            version = re.search(r"\[([^]]+)\]", stream)
            initialization.append({"timestamp_ms": event["timestamp"], "stream": stream,
                                   "version": version[1] if version else None, **fields, "message": message})
        elif message.startswith("REPORT ") or message.startswith("REPORT\t"):
            fields = parse_report(message)
            if "request_id" in fields:
                reports[fields["request_id"]] = {**fields, "log_stream": stream}
            if fields["platform_status"] != "success":
                failed_streams[stream] = event["timestamp"]
        elif message.startswith("{"):
            try:
                value = json.loads(message)
            except ValueError:
                continue
            if value.get("type") == "benchmark":
                application[value["request_id"]] = value
                if value.get("first_invocation") and stream in failed_streams:
                    suppressed.add(value["request_id"])
    seen = set()
    service_warmed = set()
    for sample in samples:
        request_id = sample.get("request_id")
        report, marker = reports.get(request_id), application.get(request_id)
        if report:
            sample.update(report)
        if marker:
            for key in ["sdk_ms", "service_first_use", "first_invocation", "service"]:
                if key in marker:
                    sample[key] = marker[key]
            if any(marker.get(k) != sample.get(k) for k in ["environment_id", "invocation_counter"]):
                sample["exclude_reason"] = "marker_mismatch"
        sample["classification"] = "excluded"
        environment = (sample.get("version"), sample.get("environment_id"))
        first = sample.get("invocation_counter") == 1
        if sample.get("error") or sample.get("status") != sample["expected_status"]:
            sample["exclude_reason"] = sample.get("exclude_reason") or "unexpected_response"
        elif not report or not marker:
            sample["exclude_reason"] = "missing_cloudwatch_records"
        elif report["platform_status"] != "success":
            sample["exclude_reason"] = "platform_failure"
        elif request_id in suppressed:
            sample["exclude_reason"] = "suspected_suppressed_initialization"
        elif first:
            # Environment marker alone is insufficient. Require ordinary init or
            # restore telemetry. Suppressed inits without telemetry stay excluded.
            metric = "restore_ms" if sample["snapstart"] else "init_ms"
            if metric not in sample:
                sample["exclude_reason"] = "unverified_initialization"
            elif environment in seen:
                sample["exclude_reason"] = "duplicate_environment_first_invocation"
            elif sample.get("service") and sample.get("service_first_use") is not True:
                sample["exclude_reason"] = "service_already_used"
            else:
                sample["classification"] = "cold"
                sample["exclude_reason"] = None
        elif environment not in seen:
            sample["exclude_reason"] = "unseen_warm_environment"
        elif sample.get("service") and (sample.get("service_first_use") is not False or
                (*environment, sample["service"]) not in service_warmed):
            sample["exclude_reason"] = "service_warmup"
        elif "init_ms" in sample or "restore_ms" in sample:
            sample["exclude_reason"] = "unexpected_initialization_on_warm_invocation"
        else:
            sample["classification"] = "warm"
            sample["exclude_reason"] = None
        # Track all observed execution environments, including setup traffic.
        if sample.get("environment_id"):
            seen.add(environment)
            if sample.get("service") and sample.get("status") in [201, 204] and not sample.get("error") and report and marker:
                service_warmed.add((*environment, sample["service"]))
    return initialization


def selected(sample):
    return (sample["phase"] == sample.get("classification") and sample.get("status") == sample["expected_status"]
            and not sample.get("exclude_reason"))
