"""Generate the strict CRD schema from the checksum-pinned Kubernetes OpenAPI document."""

import hashlib
import json
import sys


SOURCE_SHA256 = "1c7dd621bece6661867bcc29471f774a46b36c02567d59815c7c72f6d08aa512"
API = "io.k8s.apiextensions-apiserver.pkg.apis.apiextensions.v1."
ROOT_TYPE = API + "CustomResourceDefinition"


def convert(value, referenced):
    if isinstance(value, list):
        return [convert(item, referenced) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: convert(item, referenced) for key, item in value.items()}
    ref = result.get("$ref")
    if isinstance(ref, str):
        if not ref.startswith("#/components/schemas/"):
            raise ValueError("unexpected external OpenAPI reference")
        name = ref.removeprefix("#/components/schemas/")
        referenced.add(name)
        result["$ref"] = "#/definitions/" + name
    if result.get("type") == "object" and "properties" in result:
        result.setdefault("additionalProperties", False)
    return result


def main():
    source = sys.stdin.buffer.read(1024 * 1024)
    if hashlib.sha256(source).hexdigest() != SOURCE_SHA256:
        raise SystemExit("Kubernetes v1.36.2 OpenAPI source checksum mismatch")
    upstream = json.loads(source)["components"]["schemas"]
    pending = {ROOT_TYPE}
    definitions = {}
    while pending:
        name = pending.pop()
        if name not in definitions:
            definitions[name] = convert(upstream[name], pending)

    # The Go wire types express unions that kube-openapi publishes as descriptions.
    props = {"$ref": "#/definitions/" + API + "JSONSchemaProps"}
    alternatives = {
        "OrArray": {"type": "array", "items": props},
        "OrBool": {"type": "boolean"},
        "OrStringArray": {"type": "array", "items": {"type": "string"}},
    }
    for suffix, alternative in alternatives.items():
        definitions[API + "JSONSchemaProps" + suffix]["oneOf"] = [props, alternative]
    # Kubeconform lowercases kind names for file lookup. Bind the JSON itself
    # to its exact native GVK, as the standard Kubernetes schema registry does.
    root = definitions[ROOT_TYPE]
    [gvk] = root["x-kubernetes-group-version-kind"]
    root["properties"]["apiVersion"]["enum"] = [f"{gvk['group']}/{gvk['version']}"]
    root["properties"]["kind"]["enum"] = [gvk["kind"]]
    # metav1.Time marshals a zero time as null and accepts it on unmarshal.
    definitions["io.k8s.apimachinery.pkg.apis.meta.v1.Time"]["type"] = ["string", "null"]
    schema = {"$schema": "http://json-schema.org/draft-07/schema#",
              "$ref": "#/definitions/" + ROOT_TYPE, "definitions": definitions}
    sys.stdout.buffer.write((json.dumps(schema, sort_keys=True, indent=2) + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
