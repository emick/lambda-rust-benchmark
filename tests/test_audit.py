import gzip
import json
from benchmark.audit import verify


def test_audit_checks_raw_billing_instead_of_trusting_classification(tmp_path):
    sample={"variant":"python","version":"1","actual_version":"1","request_id":"r1","environment_id":"e1",
            "invocation_counter":1,"phase":"cold","classification":"cold","exclude_reason":None,"status":201,
            "expected_status":201,"scenario":"users","memory_mb":512,"snapstart":False,"http_ms":20,
            "service":"dynamodb","service_first_use":True,"sdk_ms":1,
            "handler_ms":1.5,"billed_ms":12,"max_memory_mb":100,"init_ms":10,"error":None}
    metadata={"region":"eu-north-1","started_at":"2026-10-11T00:00:00+00:00","run_id":"test","status":"complete"}
    for name,value in [("samples.json",[sample]),("metadata.json",metadata)]:
        (tmp_path/name).write_text(json.dumps(value))
    marker={"type":"benchmark",**{k:sample[k] for k in ["request_id","environment_id","invocation_counter","status","version","service","service_first_use","sdk_ms"]},"first_invocation":True}
    def write_logs():
        with gzip.open(tmp_path/"cloudwatch.jsonl.gz","wt") as file:
            for message in [json.dumps(marker),"REPORT RequestId: r1\tDuration: 1.5 ms\tBilled Duration: 12 ms\tMemory Size: 512 MB\tMax Memory Used: 100 MB\tInit Duration: 10 ms"]:
                file.write(json.dumps({"variant":"python","message":message})+"\n")
    write_logs()
    result=verify(tmp_path)
    check=next(c for c in result["checks"] if c["requirement"]=="Accepted records match raw telemetry")
    assert check["passed"]
    assert not result["complete"]  # One real record cannot satisfy the matrix.
    sample["billed_ms"]=1
    (tmp_path/"samples.json").write_text(json.dumps([sample]))
    result=verify(tmp_path)
    check=next(c for c in result["checks"] if c["requirement"]=="Accepted records match raw telemetry")
    assert not check["passed"]
    assert check["evidence"]["invalid_records"][0]["reasons"]==["REPORT billed_ms mismatch"]
    sample["billed_ms"]=12
    (tmp_path/"samples.json").write_text(json.dumps([sample]))
    marker["service_first_use"]=False
    write_logs()
    check=next(c for c in verify(tmp_path)["checks"] if c["requirement"]=="Accepted records match raw telemetry")
    assert not check["passed"]
    assert check["evidence"]["invalid_records"][0]["reasons"]==["application SDK first-use marker mismatch"]
