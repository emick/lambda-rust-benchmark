"""Capture current region-specific public AWS price dimensions, with provenance."""
from datetime import datetime, timezone
import json
import urllib.request
from .runner import dump


def fetch(region, directory):
    captured = {"region": region, "date": datetime.now(timezone.utc).isoformat(), "currency": "USD",
                "free_tier": False, "discounts": False, "rates": {}, "sources": []}
    rules = {
        "AWSLambda": {"lambda_request": ("Request-ARM", None), "lambda_gb_s": ("Lambda-GB-Second-ARM", None),
                      "snap_cache_gb_s": ("Lambda-SnapStart-Cached-GB-S", None),
                      "snap_restore_gb": ("Lambda-SnapStart-Restored-GB", None)},
        "AmazonApiGateway": {"http_request": ("ApiGatewayHttpRequest", "ApiGatewayHttpApi")},
        "AmazonDynamoDB": {"ddb_write": ("WriteRequestUnits", "PayPerRequestThroughput"),
                           "ddb_storage_gb_month": ("TimedStorage-ByteHrs", "")},
        "AmazonS3": {"s3_put": ("Requests-Tier1", ""), "s3_storage_gb_month": ("TimedStorage-ByteHrs", "")},
    }
    for service, selectors in rules.items():
        url = f"https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/{service}/current/{region}/index.json"
        with urllib.request.urlopen(url, timeout=60) as response:
            offer = json.load(response)
        captured["sources"].append({"service": service, "url": url, "publication_date": offer["publicationDate"]})
        for name, (usage, operation) in selectors.items():
            matches = []
            for sku, product in offer["products"].items():
                attributes = product["attributes"]
                actual_usage = attributes.get("usagetype", "")
                if actual_usage.split("-", 1)[-1] != usage or actual_usage.startswith("Global-"):
                    continue
                if operation is not None and attributes.get("operation", "") != operation:
                    continue
                for term in offer["terms"]["OnDemand"].get(sku, {}).values():
                    for dimension in term["priceDimensions"].values():
                        # Storage catalogs can encode free-tier ranges as a zero
                        # tier. Select the first paid tier; discounts are excluded.
                        if float(dimension["pricePerUnit"]["USD"]) > 0:
                            matches.append({"sku": sku, "attributes": attributes, "dimension": dimension,
                                            "usd": float(dimension["pricePerUnit"]["USD"])})
            if matches:
                first = min(float(m["dimension"]["beginRange"]) for m in matches)
                matches = [m for m in matches if float(m["dimension"]["beginRange"]) == first]
            if len(matches) != 1:
                raise RuntimeError(f"Expected one price for {name}; found {len(matches)}. Review current AWS catalog selectors.")
            captured["rates"][name] = matches[0]
    dump(directory / "pricing.json", captured)
    return captured
