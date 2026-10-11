# AWS Lambda Backend Benchmark — Python vs Rust

**Region:** Read from .env variable AWS_REGION
**Infrastructure:** AWS CDK\
**Objective:** Determine whether migrating FastAPI + Mangum backends to Rust + Axum meaningfully improves cold starts, warm HTTP latency, memory usage, or per-request cost on AWS Lambda.

## 1. Overview

Implement two functionally equivalent serverless REST backends:

- **Python:** FastAPI + Mangum + Pydantic + boto3
- **Rust:** Axum + lambda_http + Serde + Garde + AWS SDK for Rust

Compare three deployment variants: Python with SnapStart disabled, the same Python implementation with SnapStart enabled, and Rust with SnapStart disabled. Both Python variants must use identical application source, deployment artifacts, dependency versions, and endpoint behavior.

Each variant must have completely separate AWS resources, including API Gateway, Lambda, DynamoDB and S3.

Use existing AWS CDK infrastructure practices. CDK implementation details are outside the scope of this specification.

The backends must implement identical operations and data models to ensure that measured differences are attributable primarily to the application runtime and framework.

Limit the required benchmark to four focused tests:

| Test                  | Endpoint           | Primary objective                            |
| --------------------- | ------------------ | -------------------------------------------- |
| 1. Framework overhead | `GET /health`      | Compare cold starts and minimal HTTP overhead |
| 2. Validation         | `POST /validate`   | Compare Pydantic with Serde/Garde              |
| 3. DynamoDB           | `POST /users`      | Compare first-use and warm SDK performance    |
| 4. S3                 | `PUT /files/{key}` | Compare first-use and warm SDK performance    |

Prioritize representative runtime differences rather than comprehensive load testing of AWS-managed services. Dedicated DynamoDB reads, S3 downloads, 100 KB S3 payloads, and broad concurrency/throughput testing are outside the required scope. Concurrent bursts are used only to generate verified cold starts.

### Primary questions

1. How much faster does Rust initialize compared with FastAPI + Mangum?
2. What is the difference in warm-request latency?
3. How much memory does each implementation require?
4. How does AWS SDK initialization influence cold starts?
5. Is there a measurable difference in DynamoDB and S3 operations?
6. What is the cost difference per million requests?
7. Can Rust provide comparable request validation and REST functionality without SnapStart?
8. Does Rust offer meaningful benefits over the same Python backend with SnapStart enabled?

---

## 2. Technology requirements

| Component            | Python         | Rust             |
| -------------------- | -------------- | ---------------- |
| Runtime              | Python 3.13    | provided.al2023  |
| Architecture         | ARM64          | ARM64            |
| Web framework        | FastAPI        | Axum             |
| Lambda HTTP adapter  | Mangum         | lambda_http      |
| Deserialization      | Pydantic v2    | Serde            |
| Validation           | Pydantic v2    | Garde            |
| AWS client           | boto3          | AWS SDK for Rust |
| Async support        | asyncio        | Tokio            |
| HTTP request routing | FastAPI router | Axum Router      |
| Deployment           | CDK            | CDK              |

Use stable, current library versions and commit dependency lockfiles.

Rust must use a release-optimized native executable. Python should use a standard Lambda deployment package with production dependencies.

Enable SnapStart only for the dedicated Python SnapStart variant, on published versions. Keep it disabled for baseline Python and Rust. Provisioned Concurrency, Lambda extensions, and external monitoring agents must remain disabled for all variants. AWS documents the supported runtimes and deployment requirements in [Lambda SnapStart](https://docs.aws.amazon.com/lambda/latest/dg/snapstart.html).

---

## 3. Infrastructure isolation

Create three independently deployed variant stacks using the existing CDK isolation practices.

### Python stack

- `benchmark-python-api`: API Gateway HTTP API
- `benchmark-python-lambda`: Lambda function
- `benchmark-python-users`: DynamoDB table
- Dedicated S3 bucket
- Dedicated IAM execution role
- Dedicated CloudWatch log group

### Python SnapStart stack

- `benchmark-python-snapstart-api`: API Gateway HTTP API
- `benchmark-python-snapstart-lambda`: Lambda function using the same Python artifact
- `benchmark-python-snapstart-users`: DynamoDB table
- Dedicated S3 bucket
- Dedicated IAM execution role
- Dedicated CloudWatch log group
- SnapStart enabled for published versions

### Rust stack

- `benchmark-rust-api`: API Gateway HTTP API
- `benchmark-rust-lambda`: Lambda function
- `benchmark-rust-users`: DynamoDB table
- Dedicated S3 bucket
- Dedicated IAM execution role
- Dedicated CloudWatch log group

### Common infrastructure configuration

| Setting                 | Value                             |
| ----------------------- | --------------------------------- |
| AWS region              | Read from `.env` variable `AWS_REGION` |
| Lambda architecture     | ARM64                             |
| Initial memory          | 512 MB                            |
| Lambda timeout          | 15 seconds                        |
| SnapStart               | Enabled only for Python SnapStart |
| Provisioned Concurrency | 0                                 |
| Reserved Concurrency    | Optional, up to 100 for benchmark |
| API Gateway type        | HTTP API                          |
| API routing             | `$default` catch-all              |
| Payload format          | 2.0                               |
| DynamoDB billing        | On-demand                         |
| DynamoDB partition key  | `id` (String)                     |
| S3 bucket versioning    | Disabled                          |
| Lambda VPC              | None                              |
| API caching             | Disabled                          |
| Application caching     | Disabled                          |

All three functions must have equivalent IAM permissions for their respective storage resources. Use the same region from `.env` for all variants; do not hard-code the region in deployment or benchmark tooling.

### Resource lifecycle

**DynamoDB tables and S3 buckets must remain provisioned throughout the benchmark.**

Do not recreate, empty or replace them between cold-start samples.

Keep all variants deployed in the same region and test under comparable conditions.

Separate resources prevent direct application-level cache sharing but do not guarantee that AWS's underlying managed infrastructure is cold.

---

## 4. REST API specification

Each backend must implement the same four endpoints.

| Method | Path           | Function                              |
| ------ | -------------- | ------------------------------------- |
| GET    | `/health`      | Return health status                  |
| POST   | `/validate`    | Validate JSON input without AWS calls |
| POST   | `/users`       | Create DynamoDB record                |
| PUT    | `/files/{key}` | Upload file to S3                     |

All request routing must occur inside the application framework. API Gateway should forward requests through a single catch-all integration.

### 4.1 GET /health

Return HTTP 200:

```
{
  "status": "ok"
}
```

No DynamoDB or S3 calls.

No application-level computation beyond handling the request and serializing the response.

Load the same full application configuration as the other endpoints, including initializing both AWS SDK clients during startup. This endpoint must use the shared backend application, not a standalone minimal handler.

Measure Lambda initialization duration, first-request end-to-end HTTP latency, warm HTTP latency, and memory usage.

Purpose: compare realistic full-backend cold starts and minimal HTTP framework overhead.

### 4.2 POST /validate

Accept JSON:

```
{
  "name": "Alice Smith",
  "email": "alice@example.com",
  "age": 30,
  "tags": ["engineering", "backend"]
}
```

Validation constraints:

| Field | Type         | Constraints                   |
| ----- | ------------ | ----------------------------- |
| name  | String       | Required, 2–100 characters    |
| email | String       | Required, valid email address |
| age   | Integer      | Required, 18–120              |
| tags  | String array | Optional, maximum 10 elements |

Return HTTP 200 with validated input.

Invalid requests must return HTTP 422.

Both implementations must reject:

- Missing required fields
- Unexpected fields
- Invalid JSON types
- Invalid email addresses
- Invalid string lengths
- Out-of-range numerical values
- Arrays exceeding the maximum length

Use strict Pydantic typing and equivalent Serde/Garde validation.

Ensure the two implementations agree on accepted email patterns, Unicode string-length semantics, and handling of optional fields. Differences should be documented and resolved before performance testing.

No AWS SDK operations should occur inside this endpoint.

Measure valid and invalid requests separately, including rejection latency and correctness. The measured endpoint includes JSON parsing, Pydantic versus Serde/Garde validation, and response serialization.

Purpose: compare JSON processing and strict validation performance without AWS operations.

### 4.3 POST /users

Accept the same input schema as `/validate`.

Generate a UUID and write the record to DynamoDB.

Return HTTP 201:

```
{
  "id": "generated-uuid",
  "name": "Alice Smith",
  "email": "alice@example.com",
  "age": 30,
  "tags": ["engineering", "backend"]
}
```

Requirements:

- One DynamoDB PutItem operation
- Await successful persistence before returning
- No response caching
- Identical DynamoDB attribute types
- No extra read operation

Use conditional writes to avoid overwriting an existing ID.

Measure two scenarios:

- **First-use:** A fresh execution environment performs its first DynamoDB request.
- **Warm:** An initialized environment performs repeated DynamoDB writes after its first DynamoDB operation.

Record total client-observed HTTP latency and the DynamoDB SDK operation duration separately, using application instrumentation around `PutItem`.

Use unique user IDs and periodically clean up benchmark records outside timed measurements. Do not recreate the table between samples.

Purpose: measure validation, JSON processing, and first-use versus warm DynamoDB SDK performance.

### 4.4 PUT /files/{key}

Accept raw binary request body.

Upload to the respective S3 bucket.

Return HTTP 204 after successful upload.

Requirements:

- Use S3 PutObject
- Accept binary content
- Preserve content type
- Overwriting the same key is allowed for testing
- Use direct Lambda-to-S3 uploads
- Do not use presigned URLs for this benchmark

Use a small binary payload, initially 1 KB (1,024 bytes), identical for both runtimes.

Measure the first S3 request in a fresh execution environment, then repeated uploads from a warm environment after its first S3 operation.

Record total client-observed HTTP latency and the S3 SDK operation duration separately, using application instrumentation around `PutObject`.

Use a dedicated benchmark key prefix and clean up objects outside timed measurements. Do not recreate the bucket.

Purpose: compare first-use versus warm S3 SDK performance.

---

## 5. Application implementation requirements

### AWS client initialization

Both applications must initialize DynamoDB and S3 clients outside individual HTTP request handlers.

For Python, initialize boto3 clients at module scope.

For Rust, initialize AWS SDK configuration and clients during Lambda startup, before beginning the request-processing loop.

Reuse those clients across warm invocations.

Do not initialize clients on every request.

### Logging

Both implementations must use equivalent structured logging.

Log:

- Request identifier
- Endpoint
- HTTP status
- Execution environment identifier
- Whether the request is the first invocation in that environment
- DynamoDB `PutItem` or S3 `PutObject` SDK operation duration for storage endpoints
- Whether the operation is the first call to that service client in the environment

Do not log sensitive request bodies or credentials.

### Execution environment identification

Generate a unique identifier during application initialization and keep it in process memory.

Return or log this identifier for every invocation.

Maintain an invocation counter initialized to zero for each new execution environment.

A request using a previously unseen environment identifier and counter value 1 represents the first application invocation in that environment.

These markers are for benchmarking and must not be generated per request.

For the Python SnapStart variant, regenerate the environment identifier and reset invocation counters and service first-use flags in an after-restore hook. Otherwise, restored environments would inherit the same snapshotted identifier. Include this hook in the shared Python artifact so both Python variants use the same code. Generate user UUIDs inside request handlers. See [Python SnapStart runtime hooks](https://docs.aws.amazon.com/lambda/latest/dg/snapstart-runtime-hooks-python.html).

Track DynamoDB and S3 first-use status independently in each environment. A first-use storage sample must also be the first application invocation in a fresh environment; prior health checks or hold requests must not warm that sample's environment.

Use equivalent monotonic timing around SDK calls, including any SDK retries. Keep client-observed HTTP latency separate from SDK operation duration and correlate both with the Lambda request identifier.

### Error handling

Use equivalent status codes for comparable failures.

Avoid returning detailed AWS SDK internal errors to clients.

Log unexpected exceptions while returning standardized HTTP 500 responses.

### Initialization behavior

Do not prefetch DynamoDB data or S3 objects.

Initialize AWS SDK clients, but do not intentionally execute AWS service operations during startup.

This separates client initialization overhead from the first real service request.

For Python SnapStart, initialize both SDK clients before snapshotting without making DynamoDB or S3 calls. After restoration, preserve equivalent SDK behavior and recover stale connections or credentials when needed without prewarming either service. See [SnapStart compatibility considerations](https://docs.aws.amazon.com/lambda/latest/dg/snapstart.html#snapstart-compatibility).

---

## 6. Cold-start benchmark

### Objective

Collect **at least 100 verified cold-start samples for each cold-start scenario, deployment variant, and tested memory configuration**. For Python SnapStart, a cold-start sample means the first invocation in a newly restored execution environment.

Do not infer that every concurrent HTTP request is a cold start.

Do not rely on waiting 15 minutes between requests.

### 6.1 Test preparation

1. Deploy all three variants.
2. Verify all endpoints are functional.
3. Verify Provisioned Concurrency is disabled for all variants and SnapStart is enabled only for the dedicated Python SnapStart variant.
4. Confirm enough Lambda concurrency quota is available.
5. Configure function concurrency limits appropriately.
6. Start collecting CloudWatch initialization and invocation metrics.

A functional smoke test may warm an environment; use a fresh published version for the subsequent cold-start batch.

### 6.2 Generating cold starts

Use automated concurrent requests rather than long idle periods.

Recommended initial strategy:

1. Publish a new Lambda version.
2. Wait until the version is active; for Python SnapStart, confirm snapshot optimization is ready. Update the benchmark API integration or alias to that version.
3. Send request bursts of **1, 25, and 100 concurrent requests** to the endpoint for the current cold-start scenario.
4. Record the actual execution environment identifiers and request timings.
5. Count verified cold starts.
6. Collect Lambda initialization or SnapStart restore metrics from CloudWatch.
7. Repeat using another fresh version if fewer than 100 verified samples have been collected.

Keep the deployment artifact unchanged between batches. If another publication requires a configuration change, use a benchmark-only batch identifier with no effect on application behavior.

Use the same fresh-version and burst procedure for all three variants. Burst sizes are environment-creation controls, not separate throughput benchmarks or sample targets.

A newly published version provides an isolated execution environment pool for that version, but it does not clear AWS-managed infrastructure caches.

### Important concurrency consideration

A very fast endpoint can complete before Lambda scales to 100 simultaneous environments.

Therefore, 100 concurrent requests may produce considerably fewer than 100 cold starts.

The benchmark runner must count actual new execution environments rather than treating concurrent request count as cold-start count.

If necessary, use additional burst batches with fresh versions. An optional benchmark-only hold mechanism may encourage overlapping invocations, but it must not add artificial delay to measured endpoint timings. Retain unmodified client-observed HTTP timings; do not subtract artificial waiting from HTTP latency to present it as a normal sample. Keep hold-assisted setup traffic and billed duration separate and clearly report them. A hold request must not prewarm an environment counted as a verified cold or SDK first-use sample.

### 6.3 Cold-start measurements

Collect:

- Lambda initialization duration
- SnapStart restore duration and billed restore duration for the Python SnapStart variant
- Total client-observed HTTP latency
- Handler duration
- Billed duration
- Maximum memory used
- Number of new execution environments
- Number of warm environment reuses
- Lambda throttles and failed requests

Use CloudWatch REPORT records, platform initialization events where available, and application environment identifiers.

For Python SnapStart, verify new environments using restore telemetry and regenerated environment identifiers. Report publish-time initialization separately from request-time restore latency; initialization moved to publication must not be presented as an ordinary request-time cold start. See [Monitoring for Lambda SnapStart](https://docs.aws.amazon.com/lambda/latest/dg/snapstart-monitoring.html).

Handle suppressed initialization separately; a reset after an invocation failure should not be silently treated as an ordinary warm invocation.

### 6.4 Statistical requirements

Collect at least 100 verified cold-start observations per cold-start scenario, deployment variant, and memory setting.

Calculate:

- p50
- p90
- p95
- p99
- Mean
- Minimum
- Maximum
- Standard deviation

With only 100 samples, p99 estimates will be highly sensitive to individual outliers. Report raw results and sample counts alongside percentiles.

### 6.5 Cold-start scenarios

Run separate cold-start benchmarks for:

1. `GET /health` — full application startup or restoration with minimal HTTP processing
2. `POST /users` — first DynamoDB `PutItem`
3. `PUT /files/{key}` — first S3 `PutObject`, using a 1 KB payload

Each scenario should use fresh Lambda environments.

Do not mix initial SDK service-call overhead with results from already-warm environments.

For each SDK first-use sample, confirm that the operation is the first call to that respective service client in its fresh execution environment. Do not benchmark validation as a separate cold-start scenario.

---

## 7. Warm-request benchmark

### Objective

Compare runtime performance without Lambda initialization or SnapStart restoration overhead.

### Procedure

1. Invoke the backend to initialize an execution environment.
2. Verify subsequent requests reuse an existing environment identifier.
3. Execute at least 1,000 successful valid requests per endpoint, deployment variant, and memory configuration.
4. Record request latency and status.
5. Exclude cold-start requests from the warm-only statistics.
6. Repeat for each memory configuration.

Start with sequential requests and low concurrency to avoid accidentally measuring autoscaling.

For storage endpoints, complete the first operation for that service client before collecting warm samples. If a new environment appears, classify its first invocation separately and continue until the warm sample target is reached.

### Warm benchmark scenarios

| Scenario                 | Requests per variant and memory |
| ------------------------ | ------------------------------- |
| GET /health              | 1,000                           |
| POST /validate (valid)   | 1,000                           |
| POST /validate (invalid) | 100                             |
| POST /users              | 1,000                           |
| PUT /files/{key} (1 KB)  | 1,000                           |

Measure the 100 invalid validation requests separately from valid requests. Verify HTTP 422 and equivalent rejection behavior, and report rejection latency. Expected HTTP 422 responses are successful validation checks, not unexpected benchmark errors.

For write tests, use appropriate unique DynamoDB identifiers and S3 keys to avoid unintended duplicate-write behavior.

Do not recreate the DynamoDB tables or S3 buckets between tests.

---

## 8. Memory configurations

Benchmark all three deployment variants at:

| Memory  | Purpose                          |
| ------- | -------------------------------- |
| 128 MB  | Minimum-cost scenario            |
| 256 MB  | Low-memory scenario              |
| 512 MB  | Existing reference configuration |
| 1024 MB | Higher-CPU comparison            |

Skip configurations that cannot run successfully, and explicitly report the failure or resource limitation.

Perform the same cold-start and warm-request measurements at each supported memory configuration.

Keep timeout, architecture, API Gateway configuration and other relevant settings identical.

Publish fresh Lambda versions after configuration changes for cold-start testing.

---

## 9. Simplified execution matrix

Run the following matrix for Python, Python with SnapStart, and Rust at 128, 256, 512, and 1024 MB:

| Scenario                    | Samples per variant and memory |
| --------------------------- | ------------------------------ |
| Framework cold starts       | 100 verified                   |
| Validation warm requests    | 1,000 valid                    |
| DynamoDB first-use requests | 100 verified                   |
| DynamoDB warm requests      | 1,000                          |
| S3 first-use requests       | 100 verified                   |
| S3 warm requests            | 1,000                          |
| Framework warm requests     | 1,000                          |
| Invalid validation requests | 100                            |

This is 4,400 measured requests per variant and memory configuration: 300 verified cold/first-use samples, 4,000 valid warm requests, and 100 invalid validation requests. Across three variants and four memory configurations, planned traffic is **52,800 measured requests**. The two-variant Python/Rust baseline accounts for 35,200; Python SnapStart adds 17,600.

These totals exclude extra bursts needed to produce verified cold-start samples, setup, warmup, retries, and cleanup. Record actual traffic separately. Report unsupported memory configurations explicitly rather than counting them as completed measurements.

Use **1, 25, and 100 concurrent requests** only as cold-start burst sizes. Broad concurrency and throughput testing is outside the required scope. Verify account and function concurrency quotas before bursts. Reserved Concurrency may limit capacity, but does not pre-initialize environments.

---

## 10. Benchmark execution controls

To reduce measurement noise:

- Run tests from a machine in the region configured by `.env` variable `AWS_REGION`, preferably a dedicated benchmark EC2 instance.
- Use the same benchmark client and HTTP settings for all variants.
- Rotate the order of Python, Python SnapStart, and Rust test batches.
- Avoid concurrent benchmark runs against different variants.
- Use the same request payloads.
- Use identical data models and DynamoDB access patterns.
- Avoid intentionally warming one implementation more than the other.
- Record exact runtime, compiler and dependency versions.
- Record deployment artifact size.
- Record client-observed HTTP latency separately from instrumented AWS SDK operation timing.
- Do not include infrastructure deployment time in performance results.
- Include failed and throttled requests in a separate accounting rather than silently dropping them.

The benchmark runner must export raw results in JSON or CSV format with timestamps, runtime, deployment variant, SnapStart setting, version, scenario, memory, request identifier, environment identifier, invocation counter, service first-use status, cold/warm classification, HTTP status, HTTP latency, SDK duration where applicable, initialization or restore duration, billed duration, and maximum memory used. Retain errors, throttles, and excluded samples with reasons.

Retain payload fixtures, benchmark settings, dependency lockfiles, and commands needed to reproduce the measurements. Clean up benchmark records and objects outside timed tests, and remove unused published versions, snapshots, and benchmark infrastructure after exporting results.

---

## 11. Cost analysis

Calculate Lambda cost for each deployment variant and successful memory configuration.

Include:

1. Lambda invocation/request charges
2. Lambda duration charges
3. Memory allocation
4. ARM64 architecture pricing
5. Cold-start effects on billed duration
6. Python SnapStart snapshot caching, restoration, and separately billed initialization or hook work

Produce estimated monthly cost for:

| Requests/month | Purpose          |
| -------------- | ---------------- |
| 10,000         | Very low traffic |
| 100,000        | Low traffic      |
| 1,000,000      | Moderate traffic |
| 10,000,000     | Higher traffic   |

Use measured cold/warm durations and explicitly stated cold-start percentages, such as 0%, 1%, 5% and 20%. For Python SnapStart, state the assumed restore frequency. State the endpoint traffic mix and use equivalent assumptions for all variants rather than treating every request as cold.

Use current Lambda pricing for the region configured by `.env` variable `AWS_REGION` and state the pricing date and whether the AWS Free Tier or other discounts are included.

Report API Gateway, DynamoDB and S3 costs separately.

Include SnapStart caching and restoration charges only for the Python SnapStart variant. State assumed active-version counts and retention time, including minimum caching charges, and separate benchmark version churn from projected production costs. Avoid double-counting restore duration. Provisioned Concurrency charges are excluded for all variants. See [SnapStart pricing](https://docs.aws.amazon.com/lambda/latest/dg/snapstart.html#snapstart-pricing).

---

## 12. Results and reporting

Produce a final benchmark report containing:

### Four primary comparisons

| Comparison              | Required results                                                     |
| ----------------------- | -------------------------------------------------------------------- |
| Framework overhead      | Initialization or restore duration, first-request and warm HTTP latency |
| Validation              | Valid and invalid processing latency, strict validation correctness   |
| DynamoDB                | First-use versus warm HTTP latency and separate `PutItem` SDK timing   |
| S3                      | First-use versus warm HTTP latency and separate `PutObject` SDK timing |

For each comparison and memory configuration, show Python, Python SnapStart, and Rust side by side, with absolute and percentage differences. Include p50/p95/p99 latency, memory usage, unexpected errors, throttles, sample counts, and relevant Lambda billed duration. Report client-observed HTTP latency separately from SDK operation timing. Separate expected validation rejections from unexpected errors.

For framework results, distinguish baseline initialization from SnapStart request-time restoration and report snapshot initialization separately. Retain raw observations and explain exclusions and outliers.

### Resource usage and cost

Compare maximum memory used, configured memory, minimum successful allocation, and deployment artifact sizes. Report cost per request and per million requests at each successful memory configuration, plus projections for 10,000, 100,000, 1 million, and 10 million monthly requests using stated cold-start/restore percentages and traffic mixes. Include the Python SnapStart costs described in section 11.

### Functional differences

Document any behavioral or developer-experience differences between FastAPI/Pydantic and Axum/Serde/Garde.

### Recommendation

Identify the best runtime and memory configuration for:

- Low-traffic REST APIs
- Latency-sensitive REST APIs
- DynamoDB-heavy backends
- S3-heavy backends
- Cost-sensitive serverless platforms

Explain whether Rust's measured benefits justify migration compared with both baseline Python and enabling SnapStart on the same Python implementation. Judge meaningful improvements against stated latency, memory, or cost requirements, with sample variability and operational tradeoffs made explicit.

---

## 13. Deliverables

The completed work should contain:

**Python backend**

- FastAPI REST application
- Mangum integration
- Pydantic validation models
- DynamoDB and S3 operations
- Automated functional tests
- One shared deployment artifact for baseline and SnapStart variants
- SnapStart restore hooks for correct execution environment identification

**Rust backend**

- Axum REST application
- lambda_http integration
- Serde/Garde validation models
- DynamoDB and S3 operations
- Automated functional tests

**CDK integration**

- Separate Python, Python SnapStart, and Rust stacks
- Use Tag `Project: lambda-rust-benchmark` for all of the resources
- Independent API Gateway, Lambda, S3 and DynamoDB resources
- Equivalent IAM policies
- Configurable Lambda memory
- Support for version-based cold-start testing
- SnapStart configuration for the dedicated Python variant

**Benchmark tooling**

- Automated HTTP benchmark runner
- Configurable sample counts and cold-start burst sizes of 1, 25, and 100
- Cold-start detection and verification
- CloudWatch data collection
- SnapStart restore verification and SDK operation instrumentation
- Exportable results
- Statistical comparison

**Final report**

- Raw measurements
- Latency percentiles
- Initialization duration
- Memory usage
- Cost estimates
- Recommended runtime and memory configuration
- Comparison against Python with SnapStart enabled
- Reproducibility instructions and cleanup procedures

---

## 14. Acceptance criteria

The benchmark is complete when:

- All three variants expose the same four REST endpoints.
- Both Python variants use the same implementation and deployment artifact.
- All three variants enforce equivalent input validation.
- Each variant successfully uses its own DynamoDB table and S3 bucket.
- Infrastructure is isolated and implemented using CDK.
- DynamoDB tables and S3 buckets remain stable throughout all samples.
- SnapStart is enabled only for the Python SnapStart variant, and Provisioned Concurrency is disabled for all variants.
- At least 100 verified framework cold-start, DynamoDB first-use, and S3 first-use samples are collected separately per variant and supported memory configuration.
- At least 1,000 valid warm requests per endpoint, variant, and supported memory configuration are measured.
- At least 100 invalid validation requests per variant and supported memory configuration verify HTTP 422 behavior and measure rejection latency separately.
- AWS SDK first-use performance is measured separately.
- Lambda memory use and billed duration are compared.
- Cold-start and warm-request latency percentiles are reported.
- Estimated Lambda execution costs are calculated.
- Raw benchmark measurements are retained.
- Automated functional tests pass, measurements are reproducible, and cleanup occurs outside timed tests.
- A final Python vs Rust recommendation is documented.

**Primary success criterion:** Determine whether Rust offers a meaningful reduction in Lambda cold-start latency, warm HTTP processing latency, required memory allocation, or per-request cost for a realistic REST backend using strong input validation, DynamoDB, and S3, compared with both FastAPI + Mangum and the same Python implementation with SnapStart enabled.

Two AWS-specific details are particularly important when implementing the benchmark: reserved concurrency does not keep functions warm, and Lambda's concurrency scaling limit is separate from its concurrency quota. Also, suppressed initializations can require telemetry beyond ordinary CloudWatch REPORT entries to classify correctly.

For the API integrations, all three variants must use the same HTTP API payload format 2.0.
