#!/usr/bin/env python3
"""Refuse any Secret that is not fully SOPS-encrypted.

Runs as a pre-commit hook, so a forgotten `sops -e` never reaches git
history. Structure only -- it needs no age key, and never decrypts anything.

  1. Every `kind: Secret` carries SOPS metadata (a `sops.mac`)
  2. Every value under `data`/`stringData` is SOPS ciphertext
  3. No kustomize `secretGenerator` (it would build a Secret from plaintext)

Takes the files to check as arguments (pre-commit passes the staged ones);
with none, checks every YAML file git tracks.
"""

import subprocess
import sys

import yaml

# 0. Prefix of every value SOPS encrypts (see `.sops.yaml`).
CIPHERTEXT = "ENC[AES256_GCM,"


def problems(path):
    """Yield one message per violation in `path`."""
    with open(path) as fh:
        docs = list(yaml.safe_load_all(fh))
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        kind = doc.get("kind")
        # 3. secretGenerator literals/envs put plaintext straight into git.
        if kind == "Kustomization" and doc.get("secretGenerator"):
            yield "secretGenerator is not allowed; commit a SOPS-encrypted Secret instead"
        if kind != "Secret":
            continue
        name = doc.get("metadata", {}).get("name", "?")
        # 1. SOPS writes its metadata per document, so check per document.
        if "mac" not in (doc.get("sops") or {}):
            yield f"Secret/{name} is not SOPS-encrypted -- run: sops -e -i {path}"
        # 2. A plaintext key added to an already-encrypted file.
        for field in ("data", "stringData"):
            for key, value in (doc.get(field) or {}).items():
                if not str(value).startswith(CIPHERTEXT):
                    yield f"Secret/{name} {field}.{key} is plaintext"


def main():
    paths = sys.argv[1:] or subprocess.run(
        ["git", "ls-files", "*.yaml", "*.yml"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    failed = False
    for path in paths:
        for message in problems(path):
            print(f"{path}: {message}", file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
