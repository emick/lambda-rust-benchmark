import base64
import json
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.update(AWS_DEFAULT_REGION="eu-north-1", AWS_ACCESS_KEY_ID="test", AWS_SECRET_ACCESS_KEY="test",
                  USERS_TABLE="test", FILES_BUCKET="test", AWS_EC2_METADATA_DISABLED="true")
import app
from botocore.stub import Stubber, ANY
import pytest

CASES = json.loads(Path("fixtures/validation.json").read_text())


def invoke(path, method="GET", body=b"", content_type="application/json"):
    event = {"version": "2.0", "routeKey": "$default", "rawPath": path, "rawQueryString": "",
             "headers": {"content-type": content_type, "host": "example.com"},
             "requestContext": {"http": {"method": method, "path": path, "sourceIp": "127.0.0.1",
                                "protocol": "HTTP/1.1"}, "requestId": "api-id"},
             "body": base64.b64encode(body).decode(), "isBase64Encoded": True}
    return app.handler(event, SimpleNamespace(aws_request_id="lambda-id"))


@pytest.mark.parametrize("case", CASES, ids=lambda x: x["name"])
def test_validation(case):
    body = case.get("raw", json.dumps(case.get("input"))).encode()
    response = invoke("/validate", "POST", body)
    assert response["statusCode"] == (200 if case["valid"] else 422)
    if case["valid"]:
        assert json.loads(response["body"]) == {"tags": [], **case["input"]}


def test_health_and_restore():
    first_id = app.environment_id
    app.after_restore()
    assert app.environment_id != first_id
    response = invoke("/health")
    assert json.loads(response["body"]) == {"status": "ok"}
    assert response["headers"]["x-benchmark-invocation"] == "1"
    assert invoke("/health")["headers"]["x-benchmark-invocation"] == "2"


def test_storage_contract():
    app.after_restore()
    with Stubber(app.dynamodb) as stub:
        stub.add_response("put_item", {}, {"TableName": "test", "Item": ANY,
                                           "ConditionExpression": "attribute_not_exists(id)"})
        response = invoke("/users", "POST", json.dumps(CASES[0]["input"]).encode())
        assert response["statusCode"] == 201
        assert response["headers"]["x-benchmark-service-first-use"] == "true"
        assert json.loads(response["body"])["id"]
        stub.assert_no_pending_responses()
    body = bytes(range(256)) * 4
    with Stubber(app.s3) as stub:
        stub.add_response("put_object", {}, {"Bucket": "test", "Key": "benchmark/file", "Body": body,
                                            "ContentType": "application/octet-stream"})
        response = invoke("/files/benchmark/file", "PUT", body, "application/octet-stream")
        assert response["statusCode"] == 204
        assert response["headers"]["x-benchmark-service-first-use"] == "true"
        stub.assert_no_pending_responses()


def test_generic_storage_error():
    with Stubber(app.dynamodb) as stub:
        stub.add_client_error("put_item", "InternalServerError")
        response = invoke("/users", "POST", json.dumps(CASES[0]["input"]).encode())
        assert response["statusCode"] == 500
        assert "InternalServerError" not in response["body"]
