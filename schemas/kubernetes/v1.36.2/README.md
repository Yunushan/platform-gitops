# Kubernetes 1.36.2 CustomResourceDefinition schema

The local schema covers `apiextensions.k8s.io/v1/CustomResourceDefinition`. The committed strict JSON Schema derives from the Kubernetes 1.36.2 OpenAPI definition and its 27 transitive dependencies. Kubeconform selects it by the Kubernetes version, full API group, API version and kind. [Upstream OpenAPI source](https://github.com/kubernetes/kubernetes/blob/v1.36.2/api/openapi-spec/v3/apis__apiextensions.k8s.io__v1_openapi.json)

`generate.py` rewrites local references to JSON Schema draft-07 definitions and closes objects with declared fields. It preserves typed maps and arbitrary JSON values. It restores the documented schema-or-array, schema-or-boolean and schema-or-string-array wire types and permits `null` for a zero Kubernetes timestamp. Server-side checks still exercise CEL expressions, admission webhooks and controller behavior. [API extension types](https://github.com/kubernetes/apiextensions-apiserver/blob/v0.36.2/pkg/apis/apiextensions/v1/types_jsonschema.go) · [Timestamp encoding](https://github.com/kubernetes/apimachinery/blob/v0.36.2/pkg/apis/meta/v1/time.go)

Run these commands from this directory to reproduce the committed file.

```bash
curl --fail --location --proto '=https' --proto-redir '=https' \
  https://raw.githubusercontent.com/kubernetes/kubernetes/v1.36.2/api/openapi-spec/v3/apis__apiextensions.k8s.io__v1_openapi.json \
  --output /tmp/kubernetes-1.36.2-apiextensions-v1.json
python3 generate.py < /tmp/kubernetes-1.36.2-apiextensions-v1.json > /tmp/kubernetes-1.36.2-crd.json
cmp /tmp/kubernetes-1.36.2-crd.json apiextensions.k8s.io/v1/customresourcedefinition.json
```

The generator verifies source SHA-256 `1c7dd621bece6661867bcc29471f774a46b36c02567d59815c7c72f6d08aa512`. The generated schema SHA-256 is `a6a10a35fad1ba50bff9e7ce51bc775278d865e44dabd5cc6ce5bf66ec2a28c0`. Kubernetes distributes the upstream definitions under the Apache License 2.0. [License](LICENSE)
