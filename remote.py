#!/usr/bin/env -S uv run --quiet --env-file=.env
# /// script
# requires-python = ">=3.13,<3.14"
# dependencies = ["boto3==1.43.111", "typer==0.27.3", "loguru==0.7.3"]
# ///
"""Package, run, and retrieve the in-region benchmark through AWS SSM."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import zipfile
from datetime import datetime, timezone
from botocore.exceptions import ClientError
import typer
import boto3
from loguru import logger

ROOT = Path(__file__).resolve().parent
app = typer.Typer(add_completion=False)


def session():
    return boto3.Session(region_name=os.environ["AWS_REGION"])


def outputs():
    return json.loads((ROOT / "infra/runner-outputs.json").read_text())["Benchmark-Runner"]


@app.command()
def prepare():
    """Package sources after backend deployment, for the CDK runner asset."""
    destination = ROOT / "build/runner"
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    for name in ["benchmark", "fixtures"]:
        shutil.copytree(ROOT / name, destination / name, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ["bench.py", "bench.py.lock", "pyproject.toml", "uv.lock"]:
        shutil.copy(ROOT / name, destination / name)
    (destination / "infra").mkdir()
    shutil.copy(ROOT / "infra/outputs.json", destination / "infra/outputs.json")
    (destination / "build").mkdir()
    shutil.copy(ROOT / "build/manifest.json", destination / "build/manifest.json")
    logger.info("Packaged {}", destination)


@app.command()
def start(name: str = typer.Option("full", help="Unique run name for local/remote exports."),
          cold: int = typer.Option(100, min=1, help="Verified cold target."),
          warm: int = typer.Option(1000, min=1, help="Verified warm valid target."),
          invalid: int = typer.Option(100, min=1, help="Verified warm invalid target."),
          memories: str = typer.Option("128,256,512,1024", help="Comma-separated memory settings.")):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
        raise typer.BadParameter("Run name must contain only letters, digits, hyphens and underscores")
    info = outputs()
    state_file = ROOT / "results/remote.json"
    if state_file.exists():
        previous = json.loads(state_file.read_text())
        ssm = session().client("ssm")
        try:
            active = ssm.get_command_invocation(CommandId=previous["command_id"],InstanceId=previous["instance_id"])
            if active["Status"] in ["Pending","InProgress","Delayed","Cancelling"]:
                raise typer.BadParameter(f"Benchmark {previous['name']} is still {active['Status']}; wait for the existing command")
        except ssm.exceptions.InvocationDoesNotExist:
            pass
    prepare()
    # Deliver the current tooling and build manifest even if the runner host
    # was initially provisioned while backend compilation was still running.
    archive = ROOT / "build/runner-source.zip"
    with zipfile.ZipFile(archive,"w",compression=zipfile.ZIP_DEFLATED) as file:
        for source in sorted((ROOT / "build/runner").rglob("*")):
            if source.is_file():
                file.write(source,str(source.relative_to(ROOT / "build/runner")))
    session().client("s3").upload_file(str(archive),info["ExportBucket"],"input/source.zip")
    # No benchmark traffic until cloud-init has completely installed the runner.
    command = ["set -eu", "for i in $(seq 1 180); do test -f /opt/benchmark/ready && break; sleep 5; done",
               "test -f /opt/benchmark/ready",
               f"aws s3 cp s3://{info['ExportBucket']}/input/source.zip /tmp/current-source.zip --only-show-errors",
               "unzip -qo /tmp/current-source.zip -d /opt/benchmark", "cd /opt/benchmark", "chmod +x bench.py",
               "export PATH=/root/.local/bin:$PATH", "set +e",
               f"./bench.py run --directory results/{name} --cold {cold} --warm {warm} --invalid {invalid} --memories {shlex.quote(memories)} > /var/log/benchmark-{name}.log 2>&1",
               "benchmark_status=$?", "set -e",
               f"./bench.py pricing --directory results/{name}",
               f"aws s3 sync results/{name}/ s3://{info['ExportBucket']}/{name}/ --only-show-errors",
               f"aws s3 cp /var/log/benchmark-{name}.log s3://{info['ExportBucket']}/{name}/runner.log --only-show-errors",
               "exit $benchmark_status"]
    response = session().client("ssm").send_command(InstanceIds=[info["InstanceId"]], DocumentName="AWS-RunShellScript",
        Parameters={"commands":command, "executionTimeout":["21600"]}, TimeoutSeconds=600,
        Comment=f"Lambda benchmark {name}")
    state = {"command_id":response["Command"]["CommandId"], "instance_id":info["InstanceId"], "name":name, "export_bucket":info["ExportBucket"]}
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results/remote.json").write_text(json.dumps(state,indent=2)+"\n")
    logger.info("Started {}: SSM command {}", name, state["command_id"])


@app.command()
def status():
    state = json.loads((ROOT / "results/remote.json").read_text())
    response = session().client("ssm").get_command_invocation(CommandId=state["command_id"], InstanceId=state["instance_id"])
    logger.info("{}: {}", state["name"], response["Status"])
    for key in ["StandardOutputContent", "StandardErrorContent"]:
        if response.get(key):
            print(response[key][-5000:])
    if response["Status"] == "InProgress":
        progress = session().client("ssm").send_command(InstanceIds=[state["instance_id"]],DocumentName="AWS-RunShellScript",
            Parameters={"commands":[f"tail -n 5 /var/log/benchmark-{state['name']}.log 2>/dev/null || true"]})
        import time
        time.sleep(2)
        result = session().client("ssm").get_command_invocation(CommandId=progress["Command"]["CommandId"],InstanceId=state["instance_id"])
        if result.get("StandardOutputContent"):
            print(result["StandardOutputContent"])


@app.command()
def fetch():
    state = json.loads((ROOT / "results/remote.json").read_text())
    destination = ROOT / "results" / state["name"]
    destination.mkdir(parents=True,exist_ok=True)
    s3 = session().client("s3")
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=state["export_bucket"], Prefix=state["name"]+"/"):
        for item in page.get("Contents",[]):
            name = item["Key"].removeprefix(state["name"]+"/")
            file = destination / name
            file.parent.mkdir(parents=True,exist_ok=True)
            s3.download_file(state["export_bucket"], item["Key"], str(file))
    logger.info("Retrieved {}", destination)


@app.command()
def cleanup():
    """After fetching exports, delete benchmark data and all benchmark stacks."""
    state = json.loads((ROOT / "results/remote.json").read_text())
    destination = ROOT / "results" / state["name"]
    if not all((destination / name).is_file() for name in ["samples.json", "metadata.json", "cloudwatch.jsonl.gz", "pricing.json"]):
        raise typer.BadParameter("Fetch raw exports before cleanup")
    resources = json.loads((ROOT / "infra/outputs.json").read_text())
    aws = session()
    command = aws.client("ssm").get_command_invocation(CommandId=state["command_id"],InstanceId=state["instance_id"])
    if command["Status"] in ["Pending","InProgress","Delayed","Cancelling"]:
        raise typer.BadParameter("The benchmark command is still live; wait before cleanup")
    cloudformation = aws.client("cloudformation")
    stacks = {name:cloudformation.describe_stacks(StackName=name)["Stacks"][0]["StackId"]
              for name in ["Benchmark-Runner", *resources]}
    s3 = aws.client("s3")
    for resource in resources.values():
        bucket = resource["BucketName"]
        tags = s3.get_bucket_tagging(Bucket=bucket)["TagSet"]
        if {"Key":"Project","Value":"lambda-rust-benchmark"} not in tags:
            raise RuntimeError("Refusing to empty a bucket without the benchmark Project tag")
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            keys = [{"Key":o["Key"]} for o in page.get("Contents",[])]
            if keys:
                response = s3.delete_objects(Bucket=bucket,Delete={"Objects":keys,"Quiet":True})
                if response.get("Errors"):
                    raise RuntimeError("Bucket cleanup failed")
        # CDK-created version plus dynamically published versions. CloudFormation
        # owns only its initial version, so remove the dynamic versions explicitly.
        lam = aws.client("lambda")
        active = lam.get_alias(FunctionName=resource["FunctionName"], Name=resource["Alias"])["FunctionVersion"]
        for page in lam.get_paginator("list_versions_by_function").paginate(FunctionName=resource["FunctionName"]):
            for version in page["Versions"]:
                if version["Version"] not in ["$LATEST",active]:
                    lam.delete_function(FunctionName=resource["FunctionName"], Qualifier=version["Version"])
    # Export bucket is owned by the temporary runner stack and contains copies
    # of the already-fetched local evidence.
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=state["export_bucket"]):
        keys = [{"Key":o["Key"]} for o in page.get("Contents",[])]
        if keys:
            response = s3.delete_objects(Bucket=state["export_bucket"], Delete={"Objects":keys,"Quiet":True})
            if response.get("Errors"):
                raise RuntimeError("Export bucket cleanup failed")
    subprocess.run(["node","node_modules/aws-cdk/bin/cdk","destroy","Benchmark-Runner","--force","-c","runnerOnly=true"],cwd=ROOT,check=True)
    subprocess.run(["node","node_modules/aws-cdk/bin/cdk","destroy","--all","--force"],cwd=ROOT,check=True)
    receipt = {"region":aws.region_name,"completed_at":datetime.now(timezone.utc).isoformat(),
               "stacks":{name:cloudformation.describe_stacks(StackName=arn)["Stacks"][0]["StackStatus"]
                         for name,arn in stacks.items()},"functions":{},"buckets":{}}
    for resource in resources.values():
        name = resource["FunctionName"]
        try:
            aws.client("lambda").get_function(FunctionName=name)
            receipt["functions"][name] = "present"
        except aws.client("lambda").exceptions.ResourceNotFoundException:
            receipt["functions"][name] = "deleted"
    for bucket in [r["BucketName"] for r in resources.values()] + [state["export_bucket"]]:
        try:
            s3.head_bucket(Bucket=bucket)
            receipt["buckets"][bucket] = "present"
        except ClientError as error:
            if error.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
                raise
            receipt["buckets"][bucket] = "deleted"
    instances = aws.client("ec2").describe_instances(InstanceIds=[state["instance_id"]])
    receipt["runner"] = {"id":state["instance_id"],"state":instances["Reservations"][0]["Instances"][0]["State"]["Name"]}
    receipt["verified"] = (all(s=="DELETE_COMPLETE" for s in receipt["stacks"].values())
                           and all(s=="deleted" for s in receipt["functions"].values())
                           and all(s=="deleted" for s in receipt["buckets"].values())
                           and receipt["runner"]["state"]=="terminated")
    (destination / "cleanup.json").write_text(json.dumps(receipt,indent=2)+"\n")
    metadata_path = destination / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["final_cleanup"] = receipt
    metadata_path.write_text(json.dumps(metadata,indent=2)+"\n")
    if not receipt["verified"]:
        raise RuntimeError("Cleanup finished but AWS deletion evidence is incomplete; inspect cleanup.json")
    logger.info("Removed benchmark stacks; local evidence remains in {}", destination)


if __name__ == "__main__":
    app()
