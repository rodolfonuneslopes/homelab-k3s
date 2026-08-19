#!/usr/bin/env python3
"""Collect every container image this repository puts on the cluster.

Three sources, because none of them sees the whole picture alone: the Flux
overlays, the generated `gotk-components.yaml` that no overlay references, and
the chart behind each HelmRelease. 

Anything that cannot be resolved exits non-zero rather than warning

Prints a JSON array on stdout for the scan matrix, an inventory on stderr.

  1. Read the overlay list from the Flux entry points   -- overlays()
  2. Render each overlay and harvest its images         -- images_in()
  3. Add the Flux controllers                           -- main()
  4. Template each HelmRelease and harvest those too    -- helm_images()
  5. Print the sorted, de-duplicated result             -- main()
"""

import json
import pathlib
import subprocess
import sys
import tempfile

import yaml

# 0. What the script reads. The Flux entry points own the overlay list, so
#    adding or moving an overlay needs no change here.
CLUSTER = pathlib.Path("clusters/staging")
GOTK_COMPONENTS = CLUSTER / "flux-system" / "gotk-components.yaml"
KUSTOMIZE_GROUP = "kustomize.toolkit.fluxcd.io/"


def die(message):
    """Fail loudly -- every caller marks a would-be gap in the scan set."""
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def run(*cmd):
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return result.stdout


def load(text):
    return [doc for doc in yaml.safe_load_all(text) if isinstance(doc, dict)]


# 1. Which overlays to render.
def overlays():
    """Overlay paths, read from the Flux Kustomizations that own them.

    Globbing only `clusters/staging/*.yaml` leaves out the flux-system
    bootstrap Kustomization, which renders no workloads.
    """
    paths = []
    for entry in sorted(CLUSTER.glob("*.yaml")):
        for doc in load(entry.read_text()):
            # Flux and kustomize.config.k8s.io share the kind "Kustomization",
            # so the API group has to be checked too.
            if doc.get("kind") != "Kustomization":
                continue
            if not doc.get("apiVersion", "").startswith(KUSTOMIZE_GROUP):
                continue
            path = doc.get("spec", {}).get("path")
            if not path:
                die(f"Kustomization {doc['metadata']['name']} has no spec.path")
            paths.append(path.removeprefix("./"))

    if not paths:
        die(f"no Flux Kustomizations found in {CLUSTER}/*.yaml")
    return paths


# 2 + 4. Pulling images out of rendered manifests.
def images_in(docs):
    """Every image in a containers/initContainers list, plus a top-level
    `spec.image`.

    Walking for the enclosing list rather than bare `image:` keys keeps
    ConfigMap payloads out; the `spec.image` case catches the Prometheus and
    Alertmanager CRs, which declare no container.
    """
    found = set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("containers", "initContainers") and isinstance(value, list):
                    for container in value:
                        if isinstance(container, dict) and isinstance(container.get("image"), str):
                            found.add(container["image"])
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for doc in docs:
        walk(doc)
        spec = doc.get("spec")
        if isinstance(spec, dict) and isinstance(spec.get("image"), str):
            found.add(spec["image"])

    return found


# 4. Recovering the images that live inside Helm charts.
def is_test_hook(doc):
    """Helm test hooks never reach the cluster (nothing here enables
    `spec.test`) so their images would be findings against something that
    never runs."""
    annotations = doc.get("metadata", {}).get("annotations") or {}
    return "test" in annotations.get("helm.sh/hook", "")


def chart_reference(repository, chart_name, alias):
    """Where to point `helm template`: an OCI repository is addressed by URL, a
    classic HTTP one has to be registered first and used through its alias."""
    url = repository["url"].rstrip("/")
    if repository.get("type") == "oci":
        return f"{url}/{chart_name}"

    run("helm", "repo", "add", "--force-update", alias, url)
    return f"{alias}/{chart_name}"


def helm_images(docs):
    """Template every HelmRelease at the version it pins.

    `spec.values` is passed through so the result matches the cluster;
    `spec.valuesFrom` is not resolvable outside it and selects no images here.
    """
    # The whole spec is kept: both the URL and the `type: oci` flag are needed
    # to address a chart.
    repositories = {
        (doc["metadata"]["name"], doc["metadata"].get("namespace")): doc["spec"]
        for doc in docs
        if doc.get("kind") == "HelmRepository"
    }

    found = set()
    for release in [doc for doc in docs if doc.get("kind") == "HelmRelease"]:
        name = release["metadata"]["name"]
        namespace = release["metadata"].get("namespace")
        chart = release["spec"]["chart"]["spec"]
        source = chart["sourceRef"]

        # Only a HelmRepository can be templated from outside the cluster;
        # anything else has to fail rather than silently drop its images.
        kind = source.get("kind", "HelmRepository")
        if kind != "HelmRepository":
            die(
                f"HelmRelease {name} sources its chart from a {kind}, which this "
                f"script cannot template; its images would be missing from the scan"
            )

        repository = repositories.get((source["name"], source.get("namespace", namespace)))
        if repository is None:
            die(f"no HelmRepository found for HelmRelease {name}")

        reference = chart_reference(repository, chart["chart"], f"scan-{source['name']}")

        command = [
            "helm", "template", name, reference,
            "--namespace", namespace or "default",
        ]

        # Flux treats a missing version as "*", and so does helm without the flag.
        version = chart.get("version")
        if version:
            command += ["--version", version]

        values = release["spec"].get("values")
        if values:
            with tempfile.NamedTemporaryFile("w", suffix=".yaml") as handle:
                yaml.safe_dump(values, handle)
                handle.flush()
                rendered = load(run(*command, "--values", handle.name))
        else:
            rendered = load(run(*command))

        found |= images_in([doc for doc in rendered if not is_test_hook(doc)])

    return found


def main():
    # 1 + 2. Render every overlay Flux reconciles.
    docs = []
    for overlay in overlays():
        docs += load(run("kubectl", "kustomize", overlay))

    # 3. The Flux controllers. Nothing references this file, so rendering the
    #    overlays never reaches it.
    if not GOTK_COMPONENTS.exists():
        die(f"{GOTK_COMPONENTS} not found; the Flux controllers would go unscanned")
    docs += load(GOTK_COMPONENTS.read_text())

    # 4 + 5. Add the images hiding inside charts, then emit the union.
    images = sorted(images_in(docs) | helm_images(docs))

    print(f"{len(images)} images to scan:", file=sys.stderr)
    for image in images:
        print(f"  {image}", file=sys.stderr)

    print(json.dumps(images))


if __name__ == "__main__":
    main()
