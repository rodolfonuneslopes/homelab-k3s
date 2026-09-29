#!/usr/bin/env python3
"""Refuse any Secret that is not fully SOPS-encrypted.

Runs as a git hook, so a forgotten `sops -e` never reaches git history.
Structure only -- it needs no age key, and never decrypts anything.

  1. Every `kind: Secret` carries SOPS metadata (a `sops.mac`)
  2. Every value under `data`/`stringData` is SOPS ciphertext
  3. No kustomize `secretGenerator` (it would build a Secret from plaintext)

What it reads depends on how it is called:

  - pre-commit: the files given as arguments (pre-commit passes the staged
    ones, with unstaged changes stashed away)
  - pre-push:   every YAML file in every commit being pushed, read from git
    -- the working tree may no longer match what was committed
  - by hand:    every YAML file git tracks, as it is on disk
"""

import os
import subprocess
import sys

import yaml

# 0. Prefix of every value SOPS encrypts (see `.sops.yaml`).
CIPHERTEXT = "ENC[AES256_GCM,"


def git(*args):
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout


def pushed_files():
    """Yield (label, text) for each YAML file each pushed commit adds or changes."""
    to_ref = os.environ["PRE_COMMIT_TO_REF"]
    # Unset when the push shares no history with the remote: check it all.
    from_ref = os.environ.get("PRE_COMMIT_FROM_REF")
    span = f"{from_ref}..{to_ref}" if from_ref else to_ref
    for commit in git("rev-list", span).split():
        changed = git(
            "diff-tree", "--root", "--no-commit-id", "--name-only", "-r",
            "--diff-filter=ACMR", commit, "--", "*.yaml", "*.yml",
        ).split()
        for path in changed:
            yield f"{commit[:7]}:{path}", git("show", f"{commit}:{path}")


def disk_files(paths):
    """Yield (label, text) for each path as it is on disk."""
    for path in paths:
        with open(path) as fh:
            yield path, fh.read()


def problems(text):
    """Yield one message per violation in a YAML stream."""
    for doc in yaml.safe_load_all(text):
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
            yield f"Secret/{name} is not SOPS-encrypted"
        # 2. A plaintext key added to an already-encrypted file.
        for field in ("data", "stringData"):
            for key, value in (doc.get(field) or {}).items():
                if not str(value).startswith(CIPHERTEXT):
                    yield f"Secret/{name} {field}.{key} is plaintext"


def main():
    pushing = "PRE_COMMIT_TO_REF" in os.environ
    if pushing:
        files = pushed_files()
    else:
        files = disk_files(sys.argv[1:] or git("ls-files", "*.yaml", "*.yml").split())
    failed = False
    for label, text in files:
        try:
            found = list(problems(text))
        except yaml.YAMLError as err:
            found = [f"cannot parse YAML: {err.problem or err}"]
        for message in found:
            print(f"{label}: {message}", file=sys.stderr)
            failed = True
    if failed:
        print(
            "\nEncrypt with `sops -e -i <file>`."
            + (" The plaintext is already in a commit: amend or rebase it"
               " before pushing." if pushing else ""),
            file=sys.stderr,
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
