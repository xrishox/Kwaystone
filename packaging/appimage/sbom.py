#!/usr/bin/env python3
from __future__ import annotations

import importlib.metadata
import hashlib
import json
import pathlib
import re
import sys


def package(name: str, version: str, source: str, qualifier: str = "") -> dict:
    safe_name = re.sub(r"[^A-Za-z0-9.-]+", "-", name).strip("-") or "unknown"
    identity = hashlib.sha256(
        f"{source}\0{name}\0{version}\0{qualifier}".encode()
    ).hexdigest()[:16]
    return {
        "SPDXID": f"SPDXRef-{source}-{safe_name}-{identity}",
        "name": name,
        "versionInfo": version,
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": False,
        "licenseConcluded": "NOASSERTION",
        "licenseDeclared": "NOASSERTION",
        "supplier": "NOASSERTION",
    }


output = pathlib.Path(sys.argv[1])
node_lock_paths = [pathlib.Path(arg) for arg in sys.argv[2:-1]]
runtime = sys.argv[-1]

packages = [
    package(dist.metadata["Name"] or "unknown", dist.version, "Python")
    for dist in importlib.metadata.distributions()
]
for lock_path in node_lock_paths:
    node_lock = json.loads(lock_path.read_text())
    for location, details in sorted(node_lock.get("packages", {}).items()):
        if not location or not isinstance(details, dict):
            continue
        name = details.get("name") or location.rsplit("node_modules/", 1)[-1]
        version = details.get("version")
        if name and version:
            packages.append(package(str(name), str(version), "Node", f"{lock_path}:{location}"))

packages.sort(key=lambda item: (item["name"].lower(), item["versionInfo"], item["SPDXID"]))
content_id = hashlib.sha256(
    json.dumps(packages, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
document = {
    "spdxVersion": "SPDX-2.3",
    "dataLicense": "CC0-1.0",
    "SPDXID": "SPDXRef-DOCUMENT",
    "name": f"Kwaystone-{runtime}",
    "documentNamespace": (
        f"https://github.com/kriskruse/waystone/sbom/{runtime}/{content_id}"
    ),
    "creationInfo": {
        "created": "1970-01-01T00:00:00Z",
        "creators": ["Tool: Kwaystone packaging/appimage/sbom.py"],
    },
    "packages": packages,
}
output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
