# AWS Lambda: Python, Python SnapStart, and Rust

This experiment compares a full FastAPI + Mangum backend with a release-optimized Axum + lambda_http backend on ARM64 Lambda. A third deployment enables SnapStart on **the exact same Python ZIP**. The question is whether Rust improves startup latency, warm HTTP latency, memory allocation, or request cost enough to justify migrating an existing Python API.

The implementation follows [SPEC.md](SPEC.md). The [static report](report/index.html) contains interactive comparisons, dated regional prices, cost projections, raw downloads, and a completeness audit. It can be served directly or published with GitHub Pages.

The full experiment ran in `eu-north-1` on October 11, 2026, from 14:20 to 17:53 UTC, using a regional t3.small client. It retained **77,366 HTTP requests with zero unexpected failures and zero HTTP 429 responses**. All 12 variant/memory cells passed 42 live checks each, including validation equivalence, DynamoDB persistence, and binary S3 content/type preservation. All 96 measurement groups met their targets, and [142 raw-evidence checks](report/acceptance.json) passed. An earlier 1,347-request pilot is retained separately under `results/pilot/`. The four benchmark stacks were removed after export; the [cleanup receipt](report/cleanup.json) verifies deletion and termination of the temporary client.

## Findings

Rust materially reduced framework cold-start latency against both Python variants at every tested memory setting. At a matched 512 MB allocation, its framework cold HTTP p95 was **84% lower than baseline Python and 56% lower than SnapStart**. SnapStart substantially improved Python startup while keeping the same deployment ZIP.

The following values are client-observed HTTP p95 in milliseconds at **512 MB**; SDK rows contain the separately instrumented operation p95 instead. Cold scenarios each contain at least 100 verified new environments; valid warm scenarios contain at least 1,000 requests, and invalid validation contains at least 100.

| Scenario | Python | Python SnapStart | Rust |
| --- | ---: | ---: | ---: |
| Framework cold `/health` | 2,187.50 | 794.32 | 348.06 |
| DynamoDB first-use HTTP | 2,203.86 | 1,016.21 | 527.41 |
| S3 first-use HTTP | 2,270.77 | 1,029.77 | 578.48 |
| Warm `/health` | 21.06 | 22.39 | 22.11 |
| Warm valid `/validate` | 23.05 | 23.12 | 21.90 |
| Warm invalid `/validate` | 21.58 | 22.71 | 26.06 |
| Warm `/users` | 28.07 | 27.58 | 26.80 |
| Warm `/files` | 60.51 | 61.48 | 56.60 |
| First DynamoDB operation, SDK only | 79.24 | 231.21 | 209.49 |
| First S3 operation, SDK only | 114.23 | 280.22 | 244.57 |
| Highest reported memory, MB | 125 | 123 | 38 |

Rust’s total storage cold latency benefited from faster application startup, while **baseline boto3 had faster first SDK operations** at this allocation. Warm HTTP results at 512 MB did not meet the experiment’s 10% improvement threshold for Rust against either Python variant. Rust met that threshold for all four valid endpoints at 128 MB, and for DynamoDB writes at 256 MB. These differences are descriptive results from one regional run; invalid validation was slower in Rust at 512 MB.

All three variants successfully completed the matrix at **128 MB**, the smallest tested allocation. Rust used substantially less reported memory, but this experiment demonstrated **no reduction in configured minimum memory**. REPORT memory values are environment high-water marks, rather than exact per-request working sets.

At one million monthly requests, an equal mix of the four valid endpoints, and 1% cold starts/restores, the lowest-cost tested allocation was 128 MB for every variant:

| Variant at 128 MB | Modeled monthly Lambda cost | Including modeled API Gateway, DynamoDB, and S3 |
| --- | ---: | ---: |
| Python | $0.2772 | $2.7654 |
| Python SnapStart | $0.9308 | $3.4191 |
| Rust | $0.2280 | $2.7163 |

At that allocation Rust’s Lambda estimate was 17.7% lower than baseline Python and 75.5% lower than SnapStart. The absolute savings were approximately **$0.05 and $0.70 per million requests**, respectively; other AWS services dominated the modeled bill. At matched 512 and 1,024 MB allocations, Rust exceeded the 20% Lambda-cost improvement threshold against baseline Python. The report covers all requested volumes and cold fractions, with assumptions and dated price sources.

For this workload, choose **Rust at 128 MB for low request cost**, or **256 MB for framework startup latency** (the lowest measured cold framework p95 was 300.96 ms). The measured warm DynamoDB winner was Rust at 1,024 MB, while warm S3 favored Rust at 512 MB. Prefer retaining Python when ecosystem and maintenance costs outweigh startup requirements; SnapStart offers a substantial Python startup improvement with additional cache/restore charges. A migration motivated only by warm HTTP latency or the small absolute cost difference needs stronger workload-specific evidence.

## What is measured

| Endpoint | Experiment |
| --- | --- |
| `GET /health` | Full-backend initialization/restoration and minimal warm HTTP overhead |
| `POST /validate` | JSON parsing, strict Pydantic versus Serde/Garde validation, and serialization; valid and invalid traffic separately |
| `POST /users` | UUID creation and one conditional DynamoDB PutItem; fresh environment versus warm SDK client |
| `PUT /files/{key}` | Direct S3 PutObject with an identical 1,024-byte binary payload; fresh environment versus warm SDK client |

Python uses Python 3.13, FastAPI, Mangum, Pydantic v2, and boto3. Rust uses provided.al2023, Axum, lambda_http, Serde, Garde, Tokio, and the AWS SDK. Exact dependencies are locked in the Python, Cargo, npm, and standalone-script lockfiles; the build manifest records dependency versions, compiler version, artifact sizes, and hashes.

Every deployment has its own HTTP API, Lambda, DynamoDB table, S3 bucket, IAM role, and CloudWatch log group. All taggable resources carry `Project: lambda-rust-benchmark`. HTTP API uses one `$default` route and payload format 2.0. Functions use ARM64, a 15-second timeout, no VPC, no layers/extensions, no provisioned concurrency, and reserved concurrency of 100. SnapStart is enabled only on published Python SnapStart versions; the API invokes a version alias.

Both backends initialize and reuse their two SDK clients at startup, without issuing storage calls. Both use conditional user writes, identical DynamoDB attribute types, unique UUIDs, content-type-preserving uploads, and generic 422/500 errors. A shared fixture suite resolves validator differences: strict integers, unknown/duplicate field rejection, Unicode scalar lengths, omitted tags defaulting to `[]`, null tags rejected, and the same ASCII email grammar. Internationalized/quoted addresses and IP domains are deliberately rejected in both implementations. Unpaired surrogate escapes are rejected by both.

## Measurement design

The full matrix tests Python, Python SnapStart, and Rust at 128, 256, 512, and 1,024 MB. Per supported variant/memory cell it requires:

- 100 verified framework cold starts, 100 first-use DynamoDB writes, and 100 first-use S3 uploads.
- 1,000 successful **warm** requests for each of the four endpoints.
- 100 successful warm invalid-validation checks returning HTTP 422.

That is 52,800 planned measured requests. Actual traffic is larger because smoke checks, extra cold-generation bursts, environment reuses, warmups, and excluded/failed requests are retained separately.

A cold batch changes only a benchmark batch identifier, publishes a fresh version, waits for it to become active (and for SnapStart optimization), updates the alias, then sends bursts of 1, 25, and 100. The deployment ZIP remains unchanged. Response version markers detect alias propagation mismatches. The runner counts actual new execution environments, rather than requests, and publishes more versions until it reaches the target. There are no holds or artificial timing adjustments.

Application initialization creates a process-wide UUID and invocation counter. The shared Python artifact registers an after-restore hook that regenerates the UUID and resets invocation and independent service-first-use flags. A verified cold sample needs counter 1, a unique environment, matching application logs, and CloudWatch initialization or restore telemetry. SDK first use must also be the first application invocation. Warm storage samples require a previous successful operation in that environment. Unexpected marker resets, suspected suppressed initialization, missing logs, errors, and phase mismatches remain in raw data with exclusion reasons.

The HTTP client runs on a temporary EC2 instance in `AWS_REGION`, verified through IMDS. It uses HTTP/1.1, persistent connections, a 30-second timeout, and no HTTP retries. Warm traffic is sequential. Variant order rotates between scenarios and memories; different variants are never benchmarked concurrently. SDK calls use standard retries with three total attempts, and their separate monotonic timing includes retries. Publication, smoke checks, and cleanup are outside request timing. Tables and buckets remain provisioned and unchanged throughout the matrix.

The report includes p50/p90/p95/p99, mean, minimum, maximum, sample standard deviation, IQR outlier counts, memory high-water marks, billed duration, errors, throttles, sample targets, and absolute/percentage differences. Outliers are retained. Publish-time initialization is separate from request-time SnapStart restoration. Ordinary REPORT records cannot prove every suppressed initialization; ambiguous samples are excluded conservatively without adding an extension.

## Build and test

Prerequisites: Linux, [uv](https://docs.astral.sh/uv/), a current stable Rust toolchain, Node.js 22+, and AWS credentials for the configured account. The verified build uses rustc 1.99.0. Cargo Lambda and Zig are pinned in the build dependency group; Zig 0.14.1 is used for the Lambda-compatible ARM64 GNU build.

```sh
cp .env.example .env                 # new checkout only; set your AWS_REGION
uv sync --frozen --group build -q
npm ci --silent --no-audit --no-fund
rustup target add aarch64-unknown-linux-gnu
uv run --frozen pytest -q
cargo test --locked --manifest-path backends/rust/Cargo.toml -q
./build.py
npm run synth
node --test tests/infra.cjs
```

`build.py` exports the locked Python production dependencies, installs Python 3.13 ARM64 wheels, and creates one Python ZIP. It builds Rust in release mode with optimization, thin LTO, and stripping, then packages the executable as `bootstrap`. The tested binary is AArch64 and needs at most GLIBC 2.28, compatible with AL2023. Generated packages and compiler caches are ignored by Git. `build/manifest.json` is copied into measurement metadata.

`./build.py --python-only` rebuilds just Python. Tests need no AWS credentials: Python storage calls use botocore stubs, Rust checks the shared validation fixtures and framework routes, and CDK template tests check isolation, artifact identity, tags, and settings. The regional runner additionally performs live storage verification for every tested memory configuration.

## Deploy and run

Set `AWS_REGION` in `.env`; no deployment or benchmark command hard-codes the target region. AWS credentials use the normal credential chain. Use the existing regional CDK bootstrap stack, or bootstrap your account/region with CDK first.

```sh
npm run deploy
./remote.py prepare
node node_modules/aws-cdk/bin/cdk deploy Benchmark-Runner \
  -c runnerOnly=true --require-approval never \
  --outputs-file infra/runner-outputs.json
./remote.py start --name full
./remote.py status
```

The runner stack creates a temporary t3.small EC2 client in a dedicated public subnet, an export bucket, and a scoped instance role. It has no inbound security-group rules; control uses SSM. Startup installs uv/Python and the locked dependencies. `start` sends the current source bundle and build manifest, waits for bootstrap readiness, and launches an SSM command with a six-hour execution limit. It refuses to start a second benchmark while the previous command is live. Allow enough time for many fresh versions and snapshot publications; fast endpoints may create far fewer than 100 environments per burst.

Preflight verifies region, deployed artifact hashes/sizes, identical Python artifacts, runtime, architecture, timeout, no provisioned concurrency/layers/VPC, SnapStart settings, and concurrency quota/limits. Memory-exhausted configurations are skipped only with retained CloudWatch evidence. Functional mismatches and other unexpected failures stop the run. Avoid CDK updates or other traffic against these APIs while measurements are running.

For a smaller regional pilot:

```sh
./remote.py start --name pilot --cold 3 --warm 5 --invalid 3 --memories 512
./remote.py status
./remote.py fetch
```

For exploratory local traffic, after deployment:

```sh
./bench.py run --directory results/local-pilot \
  --cold 3 --warm 5 --invalid 3 --memories 512
```

Full-size runs require an EC2 client whose region matches `.env`. Small exploratory runs may use an unverified local client and are labeled accordingly. Output directories must be new, preventing accidental replacement of measurements. Sample targets, memory/variant lists, maximum batches, and allowed burst sizes are configurable with `./bench.py run --help`.

## Export, report, and GitHub Pages

After the SSM command reaches a terminal state:

```sh
./remote.py fetch
./bench.py inventory --directory results/full
./bench.py audit --directory results/full
./bench.py report --directory results/full
python3 -m http.server 8080 --directory report
```

SSM exports results even when the benchmark fails. `results/<name>/` holds metadata, raw request JSON, a traffic journal, raw CloudWatch logs, publication initialization records, the regional pricing snapshot, and runner logs. The generated `report/` has no server dependency or external JavaScript assets. It contains gzipped raw JSONL/CSV and CloudWatch downloads, compact statistics, metadata, pricing, HTML, CSS, and browser-local controls. Incomplete runs display missing targets and defer recommendations.

Commit `report/`, the source/fixtures, and lockfiles. In the GitHub repository settings, choose **Pages → Build and deployment → Source: GitHub Actions**. [pages.yml](.github/workflows/pages.yml) uploads `report/` and deploys it on changes to the report on `main`, or on manual dispatch. No AWS secrets are needed for Pages. [test.yml](.github/workflows/test.yml) runs local functional/template tests separately. The workflow publishes existing measurements; it does not provision AWS or run the paid benchmark.

The repository intentionally excludes `.env`, deployment output files, large uncompressed request journals, build artifacts, and temporary credentials. Compressed raw measurements are retained with the report. Current public regional prices can be refreshed with `./bench.py pricing --directory results/<name>`; fetching prices is separate from measurement timing.

To rebuild the included report after cloning, restore its raw JSON from the retained compressed download. This needs no AWS credentials or infrastructure:

```sh
uv run --frozen python - <<'PY'
import gzip, json
from pathlib import Path
with gzip.open("report/samples.jsonl.gz", "rt") as stream:
    samples = [json.loads(line) for line in stream]
Path("results/full/samples.json").write_text(json.dumps(samples, indent=2) + "\n")
PY
./bench.py audit --directory results/full
./bench.py report --directory results/full --output report-rebuilt
```

The restored JSON was verified to match the original export byte for byte. Keep the recorded pricing snapshot when reproducing these projections; refreshing prices produces a new cost scenario.

## Cost assumptions and interpreting results

Pricing comes from AWS’s current public regional offer files, with exact SKUs, dimensions, source URLs, publication dates, and retrieval timestamp retained. Estimates exclude Free Tier and discounts, use ARM64 duration rates, and show request and measured billed-duration charges separately. REPORT billed duration already includes billed restoration, which is not added a second time.

Monthly projections cover 10,000, 100,000, one million, and ten million requests at 0%, 1%, 5%, and 20% cold starts/restores. The traffic mix is 25% per valid endpoint. Validation cold billing uses health as a disclosed proxy because validation has no separate cold scenario. SnapStart assumes one active version cached for 730 hours/month, one publication/month, and three initialization passes. Every published version has a three-hour minimum cache charge. Benchmark snapshot churn is reported separately from production projections.

API Gateway, DynamoDB writes/storage, and S3 PUT/storage costs are separate. Storage assumes 256-byte user records, unique 1 KB files, 30-day retention, and average storage of half the end-month total. CloudWatch, the EC2 client, network transfer, cleanup, and extra SDK retry request charges are excluded. Initialization refresh frequency and production restore frequency are assumptions; adjust them for your workload.

Recommendations rank tested memory/runtime choices for low traffic, startup latency, DynamoDB-heavy, S3-heavy, and cost-sensitive workloads. The report’s explicit migration thresholds are 25 ms **and** 20% lower cold HTTP p95, 10% lower warm HTTP p95, 128 MB less configured memory, or 20% lower modeled monthly Lambda cost. Comparisons include both baseline Python and SnapStart. These are descriptive results from a regional run, not significance claims or universal requirements. A p99 from 100 observations is particularly sensitive to outliers. Fresh versions do not clear AWS-managed service caches. Rust’s native build and SDK binary footprint, versus Python/FastAPI ecosystem familiarity and SnapStart operations, belong in the migration decision.

## Cleanup

Fetch exports and generate the report before removing infrastructure:

```sh
./remote.py cleanup
```

The runner cleans user records and benchmark-prefixed objects between memory cells, outside timing. It exports before pruning its unused versions/snapshots. Final cleanup verifies that the SSM command has finished and local raw exports exist, empties tagged benchmark buckets, deletes leftover versions, empties the temporary export bucket, and destroys the runner and three variant stacks. `cleanup.json` records verified CloudFormation deletion, absent functions/buckets, and the terminated EC2 client. The existing CDK bootstrap stack is preserved. Generate the report again after cleanup to include its receipt and stop the snapshot cache estimate at the observed deletion upper bound. Local files and the static report remain available after cleanup.

AWS references: [SnapStart](https://docs.aws.amazon.com/lambda/latest/dg/snapstart.html), [Python restore hooks](https://docs.aws.amazon.com/lambda/latest/dg/snapstart-runtime-hooks-python.html), [monitoring](https://docs.aws.amazon.com/lambda/latest/dg/snapstart-monitoring.html), [Lambda pricing](https://aws.amazon.com/lambda/pricing/).
