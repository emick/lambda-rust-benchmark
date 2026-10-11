"""Read-only evidence of deployed resources and benchmark invariants."""
from datetime import datetime, timezone
import json
import boto3
from .runner import VARIANTS, dump


def capture(region, outputs, directory):
    session = boto3.Session(region_name=region)
    clients = {name:session.client(name) for name in ["lambda","apigatewayv2","dynamodb","s3","iam","cloudformation"]}
    result = {"region":region,"observed_at":datetime.now(timezone.utc).isoformat(),"variants":{}}
    bucket_dates = {b["Name"]:b["CreationDate"].isoformat()
                    for page in clients["s3"].get_paginator("list_buckets").paginate()
                    for b in page.get("Buckets",[])}
    for variant in VARIANTS:
        resources = outputs[f"Benchmark-{variant}"]
        lam = clients["lambda"]
        alias = lam.get_alias(FunctionName=resources["FunctionName"],Name=resources["Alias"])
        config = lam.get_function_configuration(FunctionName=resources["FunctionName"],Qualifier=alias["FunctionVersion"])
        api_id = resources["ApiUrl"].split("//",1)[1].split(".",1)[0]
        api = clients["apigatewayv2"].get_api(ApiId=api_id)
        routes = clients["apigatewayv2"].get_routes(ApiId=api_id)["Items"]
        integrations = clients["apigatewayv2"].get_integrations(ApiId=api_id)["Items"]
        table = clients["dynamodb"].describe_table(TableName=resources["TableName"])["Table"]
        bucket = resources["BucketName"]
        versioning = clients["s3"].get_bucket_versioning(Bucket=bucket)
        bucket_tags = clients["s3"].get_bucket_tagging(Bucket=bucket)["TagSet"]
        table_tags = clients["dynamodb"].list_tags_of_resource(ResourceArn=table["TableArn"])["Tags"]
        function_tags = lam.list_tags(Resource=":".join(config["FunctionArn"].split(":")[:7]))["Tags"]
        role_name = config["Role"].rsplit("/",1)[-1]
        iam = clients["iam"]
        policies = [iam.get_role_policy(RoleName=role_name,PolicyName=name)["PolicyDocument"]
                    for name in iam.list_role_policies(RoleName=role_name)["PolicyNames"]]
        attached = iam.list_attached_role_policies(RoleName=role_name)["AttachedPolicies"]
        physical = [{k:r[k] for k in ["LogicalResourceId","PhysicalResourceId","ResourceType","ResourceStatus"] if k in r}
                    for page in clients["cloudformation"].get_paginator("list_stack_resources").paginate(StackName=f"Benchmark-{variant}")
                    for r in page["StackResourceSummaries"]]
        statement = [s for p in policies for s in p["Statement"]]
        service_actions = sorted({a for s in statement if s.get("Effect")=="Allow"
                                  for a in ([s["Action"]] if isinstance(s["Action"],str) else s["Action"])
                                  if a.startswith(("s3:","dynamodb:"))})
        expected_role_resources = {"dynamodb:PutItem":table["TableArn"],"s3:PutObject":f"arn:aws:s3:::{bucket}/*"}
        scoped = True
        for s in statement:
            actions = [s["Action"]] if isinstance(s["Action"],str) else s["Action"]
            targets = [s["Resource"]] if isinstance(s["Resource"],str) else s["Resource"]
            for action in actions:
                if action in expected_role_resources and (targets != [expected_role_resources[action]] or s["Effect"] != "Allow"):
                    scoped = False
        provisioned = lam.list_provisioned_concurrency_configs(FunctionName=resources["FunctionName"])["ProvisionedConcurrencyConfigs"]
        entry = {"resources":resources,"function":{k:config.get(k) for k in ["FunctionArn","Runtime","Handler","Architectures",
                 "MemorySize","Timeout","CodeSize","CodeSha256","Role","Environment","SnapStart","Layers","VpcConfig","EphemeralStorage"]},
                 "function_tags":function_tags,
                 "alias":{k:alias.get(k) for k in ["AliasArn","Name","FunctionVersion","RoutingConfig"]},
                 "api":{"id":api_id,"protocol":api["ProtocolType"],"tags":api.get("Tags",{}),"routes":routes,"integrations":integrations},
                 "table":{"arn":table["TableArn"],"created_at":table["CreationDateTime"].isoformat(),"key_schema":table["KeySchema"],
                          "attributes":table["AttributeDefinitions"],"billing":table["BillingModeSummary"]["BillingMode"],"tags":table_tags},
                 "bucket":{"name":bucket,"created_at":bucket_dates.get(bucket),"versioning":versioning.get("Status","Disabled"),"tags":bucket_tags},
                 "role":{"name":role_name,"policies":policies,"attached":attached,"service_actions":service_actions,"scoped":scoped},
                 "provisioned_concurrency":provisioned,"stack_resources":physical}
        result["variants"][variant] = entry
    dump(directory / "deployment.json",result)
    return result


def invariants(inventory):
    problems = []
    variants = inventory.get("variants",{})
    for variant in VARIANTS:
        entry = variants.get(variant)
        if not entry:
            problems.append(f"{variant}: missing inventory")
            continue
        config = entry["function"]
        if config["Architectures"] != ["arm64"] or config["Timeout"] != 15:
            problems.append(f"{variant}: unequal architecture/timeout")
        if config.get("Layers") or (config.get("VpcConfig") or {}).get("VpcId") or entry["provisioned_concurrency"]:
            problems.append(f"{variant}: VPC/layers/provisioned concurrency enabled")
        snap = config.get("SnapStart",{})
        if (snap.get("ApplyOn")=="PublishedVersions") != (variant=="python-snapstart") or (variant=="python-snapstart" and snap.get("OptimizationStatus")!="On"):
            problems.append(f"{variant}: incorrect snapshot configuration")
        api = entry["api"]
        if api["protocol"]!="HTTP" or len(api["routes"])!=1 or api["routes"][0]["RouteKey"]!="$default":
            problems.append(f"{variant}: API must have a single HTTP catch-all route")
        if len(api["integrations"])!=1 or api["integrations"][0].get("PayloadFormatVersion")!="2.0" or api["integrations"][0].get("IntegrationUri")!=entry["alias"]["AliasArn"]:
            problems.append(f"{variant}: API integration does not invoke the version alias with payload 2.0")
        table=entry["table"]
        if table["billing"]!="PAY_PER_REQUEST" or table["key_schema"]!=[{"AttributeName":"id","KeyType":"HASH"}] or table["attributes"]!=[{"AttributeName":"id","AttributeType":"S"}]:
            problems.append(f"{variant}: DynamoDB schema/billing mismatch")
        if entry["bucket"]["versioning"]!="Disabled":
            problems.append(f"{variant}: S3 versioning enabled")
        role=entry["role"]
        if role["service_actions"]!=["dynamodb:PutItem","s3:PutObject"] or not role["scoped"] or role["attached"]:
            problems.append(f"{variant}: backend storage permissions differ")
        project={"Key":"Project","Value":"lambda-rust-benchmark"}
        if project not in table["tags"] or project not in entry["bucket"]["tags"] or api["tags"].get("Project")!="lambda-rust-benchmark" or entry.get("function_tags",{}).get("Project")!="lambda-rust-benchmark":
            problems.append(f"{variant}: Project tag missing")
        env=config.get("Environment",{}).get("Variables",{})
        if env.get("USERS_TABLE")!=entry["resources"]["TableName"] or env.get("FILES_BUCKET")!=entry["bucket"]["name"]:
            problems.append(f"{variant}: storage resources are not the stack's dedicated resources")
    for field in ["FunctionName","TableName","BucketName","LogGroup","ApiUrl"]:
        values=[entry["resources"][field] for entry in variants.values()]
        if len(values)!=3 or len(set(values))!=3:
            problems.append(f"{field}: resources are not isolated")
    return problems
