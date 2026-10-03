#!/usr/bin/env python3
"""scripts/fetch-v1/push_tree.py — push a fixture tree into a live OCI registry.

    scripts/fetch-v1/push_tree.py --registry registry.test:5443 --repo chtypes/v1 \
        tests/fixtures/fetch-v1/trees/<tree> [<tree> ...]

The registry-transport conformance cases name different fixture trees (the
`tree` field of cases.json), all against ONE base URL, so the v1-network job
must serve every such tree from one repository. A tree is the file layout the
scripted server serves (v2/<repo>/blobs, manifests, ...): this script uploads
every blob, then every manifest, byte for byte, so digests are unchanged.

- A manifest file named `sha256:<hex>` is pushed by digest; any other name
  (a tag such as `26.8`, or a referrers fallback tag such as `sha256-<hex>`)
  is pushed under that name. Content-Type is the manifest's own `mediaType`.
- Manifests are pushed in dependency order by retrying until no progress, since
  an index is refused until its child manifests exist.
- TLS uses the default context, i.e. the OS trust store; nothing is disabled.
- Stdlib only. Exits nonzero if anything cannot be pushed, naming it.
"""
import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def put(url: str, data: bytes, ctype: str, method: str) -> None:
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 — operator-supplied registry
            resp.read()
    except urllib.error.HTTPError as e:
        # The registry's own error body names the cause (missing blob, bad media type, ...).
        raise urllib.error.URLError(f"HTTP {e.code} {e.read()[:300]!r}") from e


def push_tree(registry: str, repo: str, tree: Path) -> list[str]:
    base = f"https://{registry}/v2/{repo}"
    root = tree / "v2" / repo
    problems: list[str] = []
    blobs = sorted((root / "blobs").iterdir())
    for blob in blobs:
        data = blob.read_bytes()
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        if blob.name != digest:
            problems.append(f"{blob}: file name is not its own digest ({digest})")
            continue
        try:
            put(f"{base}/blobs/uploads/?digest={digest}", data, "application/octet-stream", "POST")
        except urllib.error.URLError as e:
            problems.append(f"blob {digest}: {e}")
    pending = [p for p in sorted((root / "manifests").iterdir()) if p.is_file()]
    last_err: dict[str, str] = {}
    while pending:
        remaining = []
        for m in pending:
            data = m.read_bytes()
            try:
                ctype = json.loads(data)["mediaType"]
                put(f"{base}/manifests/{m.name}", data, ctype, "PUT")
            except (urllib.error.URLError, KeyError, ValueError) as e:
                last_err[m.name] = str(e)
                remaining.append(m)
        if len(remaining) == len(pending):
            problems += [f"manifest {m.name}: {last_err[m.name]}" for m in remaining]
            break
        pending = remaining
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--registry", required=True, help="host:port of the registry (https)")
    ap.add_argument("--repo", required=True)
    ap.add_argument("trees", nargs="+", type=Path)
    args = ap.parse_args()
    bad = []
    for t in args.trees:
        probs = push_tree(args.registry, args.repo, t)
        print(f"push_tree: {t.name}: {'ok' if not probs else str(len(probs)) + ' problem(s)'}")
        bad += [f"{t.name}: {p}" for p in probs]
    for p in bad:
        print(f"push_tree: {p}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
