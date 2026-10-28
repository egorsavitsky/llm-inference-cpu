"""Local YAML/structural checks; real API validation is done in Minikube."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
documents = {}
for path in (ROOT / "k8s").glob("*.yaml"):
    documents[path.name] = list(yaml.safe_load_all(path.read_text()))
kustomization = yaml.safe_load((ROOT / "kustomization.yaml").read_text())
deployment = documents["deployment.yaml"][0]
spec = deployment["spec"]["template"]["spec"]
assert deployment["spec"]["replicas"] == 1
assert deployment["spec"]["strategy"]["type"] == "Recreate"
assert deployment["spec"]["selector"]["matchLabels"] == deployment["spec"]["template"]["metadata"]["labels"]
assert spec["securityContext"]["runAsNonRoot"] and spec["securityContext"]["runAsUser"] == 10001
assert spec["automountServiceAccountToken"] is False
init, server = spec["initContainers"][0], spec["containers"][0]
assert init["env"][0]["valueFrom"]["secretKeyRef"]["name"] == "hf-credentials"
assert server["env"][0]["valueFrom"]["secretKeyRef"]["name"] == "api-credentials"
assert not any(item["name"] == "HF_TOKEN" for item in server["env"])
assert next(item for item in server["volumeMounts"] if item["name"] == "models")["readOnly"]
for container in (init, server):
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert container["imagePullPolicy"] == "Never"
    assert all("value" not in item for item in container.get("env", []))
assert "k8s/offline-policy.yaml" not in kustomization["resources"]
assert kustomization["configMapGenerator"][0]["files"] == ["config/server.json"]
assert documents["offline-policy.yaml"][0]["spec"]["egress"] == []
assert documents["offline-policy.yaml"][0]["spec"]["podSelector"]["matchLabels"] == {"app": "cpu-llm"}
assert documents["service.yaml"][0]["spec"]["selector"] == deployment["spec"]["selector"]["matchLabels"]
assert json.loads((ROOT / "config/server.json").read_text())["port"] == server["ports"][0]["containerPort"]
print(json.dumps({"yaml_documents": sum(len(items) for items in documents.values()), "structural_checks": "passed",
                  "scope": "Static YAML/security/reference checks only; Kustomize rendering and API validation require kubectl/Minikube."}))
