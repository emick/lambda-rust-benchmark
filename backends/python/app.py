"""Shared application artifact for Python with and without SnapStart."""
import json
import logging
import os
import re
import time
import uuid
from contextvars import ContextVar

import boto3
from botocore.config import Config
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mangum import Mangum
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# Deliberately explicit common ASCII email grammar; see fixtures/validation.json.
EMAIL = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.[A-Za-z]{2,63}")


class User(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    name: str = Field(min_length=2, max_length=100)
    email: str
    age: int = Field(ge=18, le=120)
    tags: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("name", "email")
    @classmethod
    def unicode_scalars(cls, value):
        # JSON escapes can contain unpaired surrogates; Serde rejects these.
        # Require Unicode scalar values here as well.
        value.encode("utf-8")
        return value

    @field_validator("tags")
    @classmethod
    def unicode_tags(cls, values):
        for value in values:
            value.encode("utf-8")
        return values

    @field_validator("email")
    @classmethod
    def email_pattern(cls, value):
        if len(value) > 254 or len(value.split("@")[0]) > 64 or not EMAIL.fullmatch(value):
            raise ValueError("invalid email")
        return value


config = Config(retries={"mode": "standard", "total_max_attempts": 3}, connect_timeout=3, read_timeout=5)
dynamodb = boto3.client("dynamodb", config=config)
s3 = boto3.client("s3", config=config)
table = os.environ["USERS_TABLE"]
bucket = os.environ["FILES_BUCKET"]
environment_id = str(uuid.uuid4())
invocations = 0
service_used = {"dynamodb": False, "s3": False}
current = ContextVar("benchmark_invocation")
logging.getLogger().setLevel(logging.INFO)


def after_restore():
    global environment_id, invocations, service_used
    environment_id, invocations = str(uuid.uuid4()), 0
    service_used = {"dynamodb": False, "s3": False}
    # No service calls here. boto3's container credential provider refreshes its
    # credentials, and botocore reconnects stale HTTP connections on first use.


try:
    from snapshot_restore_py import register_after_restore
    register_after_restore(after_restore)
except ImportError:
    # Local tests lack Lambda's built-in runtime hook module. Never silently
    # disable hooks in an actual Lambda runtime.
    if os.environ.get("AWS_LAMBDA_RUNTIME_API"):
        raise

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(RequestValidationError)
async def validation_error(request, error):
    return JSONResponse({"error": "validation_failed"}, status_code=422)


@app.exception_handler(Exception)
async def unexpected_error(request, error):
    return JSONResponse({"error": "internal_error"}, status_code=500)


def no_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate field")
        value[key] = item
    return value


async def validated(request):
    try:
        value = json.loads(await request.body(), object_pairs_hook=no_duplicates,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite")))
        return User.model_validate(value)
    except (ValueError, UnicodeError, ValidationError):
        return None


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/validate")
async def validate(request: Request):
    user = await validated(request)
    if user is None:
        return JSONResponse({"error": "validation_failed"}, status_code=422)
    return user.model_dump()


def operation(service, call):
    meta = current.get()
    meta["service"] = service
    meta["service_first_use"] = not service_used[service]
    service_used[service] = True
    start = time.perf_counter_ns()
    try:
        return call()
    finally:
        meta["sdk_ms"] = (time.perf_counter_ns() - start) / 1e6


@app.post("/users")
async def users(request: Request):
    user = await validated(request)
    if user is None:
        return JSONResponse({"error": "validation_failed"}, status_code=422)
    value = {"id": str(uuid.uuid4()), **user.model_dump()}
    item = {"id": {"S": value["id"]}, "name": {"S": value["name"]},
            "email": {"S": value["email"]}, "age": {"N": str(value["age"])},
            "tags": {"L": [{"S": tag} for tag in value["tags"]]}}
    operation("dynamodb", lambda: dynamodb.put_item(TableName=table, Item=item,
              ConditionExpression="attribute_not_exists(id)"))
    return JSONResponse(value, status_code=201)


@app.put("/files/{key:path}")
async def files(key: str, request: Request):
    body = await request.body()
    operation("s3", lambda: s3.put_object(Bucket=bucket, Key=key, Body=body,
              ContentType=request.headers.get("content-type", "application/octet-stream")))
    return Response(status_code=204)


class Instrumented:
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        meta = current.get()
        meta["endpoint"] = scope["path"]
        status = 500
        started = False

        async def instrument(message):
            nonlocal status, started
            if message["type"] == "http.response.start":
                status, started = message["status"], True
                headers = {"x-benchmark-request-id": meta["request_id"],
                           "x-benchmark-environment-id": meta["environment_id"],
                           "x-benchmark-version": meta["version"],
                           "x-benchmark-invocation": str(meta["invocation_counter"])}
                if "service" in meta:
                    headers["x-benchmark-service-first-use"] = str(meta["service_first_use"]).lower()
                    headers["x-benchmark-sdk-ms"] = str(meta["sdk_ms"])
                message["headers"] += [(k.encode(), v.encode()) for k, v in headers.items()]
            await send(message)

        try:
            await app(scope, receive, instrument)
        except Exception:
            # Framework may have already sent its generic 500. Suppress detailed
            # exception bodies; identifiers correlate errors with raw logs.
            logging.exception("unexpected_backend_error")
            if not started:
                await JSONResponse({"error": "internal_error"}, status_code=500)(scope, receive, instrument)
        finally:
            print(json.dumps({"type": "benchmark", **meta, "status": status}, separators=(",", ":")))


adapter = Mangum(Instrumented(), lifespan="off")


def handler(event, context):
    global invocations
    invocations += 1
    token = current.set({"request_id": context.aws_request_id, "environment_id": environment_id,
                         "version": getattr(context, "function_version", os.environ.get("AWS_LAMBDA_FUNCTION_VERSION", "$LATEST")),
                         "invocation_counter": invocations, "first_invocation": invocations == 1})
    try:
        return adapter(event, context)
    finally:
        current.reset(token)
