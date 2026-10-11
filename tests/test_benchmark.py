from copy import deepcopy
import json
from pathlib import Path
import pytest
from benchmark.telemetry import parse_report, join, selected
from benchmark.report import stats, summary, costs


def sample(request="r1", counter=1, snapstart=False):
    return {"request_id":request, "environment_id":"env", "invocation_counter":counter,
            "snapstart":snapstart, "version":"1", "variant":"python-snapstart" if snapstart else "python",
            "status":200, "expected_status":200, "phase":"cold" if counter==1 else "warm", "scenario":"health",
            "memory_mb":512, "http_ms":100, "error":None}


def events(s, report, time=1):
    marker = {"type":"benchmark", "request_id":s["request_id"], "environment_id":s["environment_id"],
              "invocation_counter":s["invocation_counter"], "first_invocation":s["invocation_counter"]==1}
    return [{"timestamp":time,"eventId":str(time),"logStreamName":"stream", "message":json.dumps(marker)},
            {"timestamp":time+1,"eventId":str(time+1),"logStreamName":"stream", "message":report}]


def report(request="r1", extra=""):
    return f"REPORT RequestId: {request}\tDuration: 3.20 ms\tBilled Duration: 104 ms\tMemory Size: 512 MB\tMax Memory Used: 112 MB\t{extra}"


def test_report_field_disambiguation():
    parsed = parse_report(report(extra="Init Duration: 100.0 ms\tRestore Duration: 80 ms\tBilled Restore Duration: 2 ms"))
    assert parsed["handler_ms"] == 3.2
    assert parsed["billed_ms"] == 104
    assert parsed["restore_ms"] == 80
    assert parsed["billed_restore_ms"] == 2
    assert parsed["init_ms"] == 100


def test_first_marker_requires_platform_evidence():
    first = sample()
    join([first],events(first,report()))
    assert first["classification"] == "excluded"
    assert first["exclude_reason"] == "unverified_initialization"
    join([first],events(first,report(extra="Init Duration: 100 ms")))
    assert selected(first)


def test_snapstart_requires_restore_and_warm_reuse():
    first, second = sample(snapstart=True), sample("r2",2,True)
    logs = events(first,report(extra="Restore Duration: 20 ms\tBilled Restore Duration: 2 ms"))
    logs += events(second,report("r2"),3)
    join([first,second],logs)
    assert first["classification"]=="cold" and second["classification"]=="warm"
    assert "init_ms" not in first


def test_storage_first_use_is_independent_of_warm_environment():
    first, second = sample(), sample("r2",2)
    second.update(service="s3",service_first_use=True)
    join([first,second],events(first,report(extra="Init Duration: 100 ms"))+events(second,report("r2"),3))
    assert second["exclude_reason"] == "service_warmup"


def test_reset_after_failure_is_excluded():
    failed, reset = sample(), sample("r2")
    reset["environment_id"] = "reset-env"
    logs = events(failed,report(extra="Status: error"))+events(reset,report("r2"),3)
    join([failed,reset],logs)
    assert reset["exclude_reason"] == "suspected_suppressed_initialization"


def test_expected_422_is_success_and_errors_retained():
    first, invalid = sample(), sample("r2",2)
    invalid.update(status=422,expected_status=422,scenario="validate_invalid")
    join([first,invalid],events(first,report(extra="Init Duration: 100 ms"))+events(invalid,report("r2"),3))
    assert selected(invalid)


def test_statistics_keep_outliers():
    s = stats([1]*99+[1000])
    assert s["count"]==100 and s["max"]==1000 and s["outliers_iqr"]==1
    assert s["p99"] == pytest.approx(10.99)
    assert s["stddev"]>0


def test_partial_run_cannot_produce_recommendations():
    data = summary([], {"region":"eu-north-1", "cells":[]})
    assert not data["audit"]["complete"]
    assert len(data["audit"]["incomplete"]) == 96
    assert "deferred" in data["recommendations"][0]


def test_snapstart_cost_does_not_double_count_restore_duration():
    groups=[]
    for phase,endpoints in [("cold",["health","users","files"]),("warm",["health","validate","users","files"])]:
        for endpoint in endpoints:
            groups.append({"variant":"python-snapstart","memory_mb":512,"scenario":endpoint,"phase":phase,
                "metrics":{"billed_ms":{"mean":10},"billed_restore_ms":{"mean":999}}})
    pricing={"rates":{k:{"usd":1} for k in ["lambda_request","lambda_gb_s","snap_cache_gb_s","snap_restore_gb",
                "http_request","ddb_write","ddb_storage_gb_month","s3_put","s3_storage_gb_month"]}}
    row=next(c for c in costs(groups,{},pricing) if c["requests"]==10000 and c["cold_fraction"]==.01)
    assert row["lambda_duration"]==50
    assert row["snapshot_restoration"]==50
    assert row["snapshot_caching"]==730*3600*.5
