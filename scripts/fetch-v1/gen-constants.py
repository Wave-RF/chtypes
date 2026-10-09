#!/usr/bin/env python3
r"""scripts/fetch-v1/gen-constants.py — one constants source, generated into
all four bindings plus the guide (the v1 fetch-layer plan's §3.1).

spec/fetch-v1/constants.json is the only place any of these values is
hand-written. Every binding reads its own generated file instead of
re-typing a media type, a key id or a retry number, so the four fetchers
cannot drift on the wire contract the way four independent implementations
otherwise would. This script is Python stdlib only, deliberately: it must
run in the `v1-constants` CI job with nothing but a checkout.

    scripts/fetch-v1/gen-constants.py --write      regenerate every output in place
    scripts/fetch-v1/gen-constants.py --check       regenerate into memory and diff
                                                     against what is on disk; exits 1
                                                     on any drift
    scripts/fetch-v1/gen-constants.py --selftest    prove --check actually catches
                                                     drift, on a throwaway copy —
                                                     never the real tree

OUTPUTS. Four binding constant files, each carrying a `GENERATED ... DO NOT
EDIT` header naming spec/fetch-v1/constants.json's own sha256, plus a table
between markers in docs/guides/fetch-v1.md:

    go/internal/ocifetch/constants_gen.go
    python/src/chtypes/_ocifetch/_constants.py
    ts/src/ocifetch/constants.gen.ts
    rust/src/ocifetch/constants.rs
    docs/guides/fetch-v1.md (between <!-- BEGIN/END GENERATED: fetch-v1 constants -->)

VALIDATION. Beyond spec/fetch-v1/schema/constants.schema.json (which a later
schema-validation script under scripts/fetch-v1/ checks, once it exists), this
script also asserts a structural invariant no JSON Schema keyword expresses: no
`test_keys` entry's key id may ever appear in `trust.release_keys` — a test
key must never be part of the default trust list a production build ships
with. That check runs on every `--write` and `--check`, not only on the
selftest's own fabricated violation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
SOURCE_RELPATH = "spec/fetch-v1/constants.json"
GUIDE_RELPATH = "docs/guides/fetch-v1.md"

OUTPUT_RELPATHS: dict[str, str] = {
    "go": "go/internal/ocifetch/constants_gen.go",
    "python": "python/src/chtypes/_ocifetch/_constants.py",
    "ts": "ts/src/ocifetch/constants.gen.ts",
    "rust": "rust/src/ocifetch/constants.rs",
}

GUIDE_BEGIN = "<!-- BEGIN GENERATED: fetch-v1 constants -->"
GUIDE_END = "<!-- END GENERATED: fetch-v1 constants -->"


# ------------------------------------------------------------------ loading


def load_constants(root: Path) -> tuple[dict[str, Any], str]:
    """Parse root/spec/fetch-v1/constants.json and return (data, sha256-hex
    of its exact bytes) — the hash generated files name in their header."""
    path = root / SOURCE_RELPATH
    text = path.read_text(encoding="utf-8")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return json.loads(text), sha


def validate(data: dict[str, Any]) -> list[str]:
    """Structural invariants beyond the JSON Schema. Pure, so --selftest can
    drive it with a fabricated violation."""
    problems: list[str] = []
    test_keyids = {k["keyid"] for k in data.get("test_keys", [])}
    release_keyids = {k["keyid"] for k in data.get("trust", {}).get("release_keys", [])}
    overlap = sorted(test_keyids & release_keyids)
    if overlap:
        problems.append(
            f"test key id(s) {overlap} appear in trust.release_keys — a test key must never be "
            "in the default trust list"
        )
    for k in data.get("test_keys", []):
        if k.get("trusted_by_default") is not False:
            problems.append(f"test key {k.get('keyid')!r} does not have trusted_by_default: false")
    return problems


# ------------------------------------------------------------------ renderers
#
# Each renderer is a pure function of (data, source_sha) -> file text. Field
# order follows spec/fetch-v1/constants.json's own key order throughout, so a
# diff of the generated output tracks a diff of the source.

GENERATED_NOTICE = "spec/fetch-v1/constants.json"


def _go_string(s: str) -> str:
    # A Go raw string literal when the value contains no backtick and no
    # literal CR, which is true of every regex and URL here; otherwise a
    # normal interpreted string via json.dumps (handles backslash escaping
    # the same way Go's double-quoted strings do for the characters we emit).
    if "`" not in s and "\r" not in s:
        return f"`{s}`"
    return json.dumps(s)


def render_go(data: dict[str, Any], sha: str) -> str:
    reg = data["registry"]
    env = data["env"]
    spelling = data["spelling"]
    mt = data["media_types"]
    dsse = data["dsse"]
    pt = data["predicate_types"]
    trust = data["trust"]
    retry = data["retry"]
    timeouts = data["timeouts"]
    limits = data["limits"]
    cache = data["cache"]
    lock = data["lock"]
    autofetch = data["autofetch"]

    lines: list[str] = []
    lines.append(f"// Code generated by scripts/fetch-v1/gen-constants.py from")
    lines.append(f"// {GENERATED_NOTICE} (sha256:{sha}). DO NOT EDIT.")
    lines.append("")
    lines.append("package ocifetch")
    lines.append("")
    lines.append("const (")
    lines.append(f"\tSchemaVersion = {data['schema']}")
    lines.append(f"\tABIGeneration = {data['abi_generation']}")
    lines.append("")
    lines.append(f"\tEnvBasesName         = {_go_string(reg['env_bases'])}")
    lines.append(f"\tBaseSeparator        = {_go_string(reg['base_separator'])}")
    lines.append(f"\tFixturesRepoSuffix   = {_go_string(reg['fixtures_repository_suffix'])}")
    lines.append(f"\tEnvCacheName         = {_go_string(env['cache'])}")
    lines.append(f"\tEnvTokenName         = {_go_string(env['token'])}")
    lines.append(f"\tEnvTrustedKeysName   = {_go_string(env['trusted_keys'])}")
    lines.append(f"\tEnvAllowUnsignedName = {_go_string(env['allow_unsigned'])}")
    lines.append(f"\tEnvAutofetchName     = {_go_string(env['autofetch'])}")
    lines.append(f"\tEnvTargetName        = {_go_string(env['target'])}")
    lines.append(f"\tEnvCacheStrictName   = {_go_string(env['strict_cache'])}")
    lines.append("")
    lines.append(f"\tSpellingRegex           = {_go_string(spelling['regex'])}")
    lines.append(f"\tSpellingRefuseHintRegex = {_go_string(spelling['refuse_hint_regex'])}")
    lines.append("")
    lines.append(f"\tMediaTypeIndex       = {_go_string(mt['index'])}")
    lines.append(f"\tMediaTypeManifest    = {_go_string(mt['manifest'])}")
    lines.append(f"\tMediaTypeEmptyConfig = {_go_string(mt['empty_config'])}")
    lines.append(f"\tMediaTypeConfig      = {_go_string(mt['config'])}")
    lines.append(f"\tMediaTypeLayer       = {_go_string(mt['layer'])}")
    lines.append(f"\tMediaTypeBundle      = {_go_string(mt['bundle'])}")
    lines.append(f"\tArtifactType         = {_go_string(mt['artifact_type'])}")
    lines.append(f"\tGoldensArtifactType  = {_go_string(mt['goldens_artifact_type'])}")
    lines.append(f"\tFixturesArtifactType = {_go_string(mt['fixtures_artifact_type'])}")
    lines.append(f"\tChannelArtifactType  = {_go_string(mt['channel_artifact_type'])}")
    lines.append("")
    lines.append(f"\tDSSEPayloadType   = {_go_string(dsse['payload_type'])}")
    lines.append(f"\tDSSEMaxSignatures = {dsse['max_signatures']}")
    lines.append("")
    lines.append(f"\tStatementType = {_go_string(data['statement_type'])}")
    lines.append("")
    lines.append(f"\tPredicateTypeArtifact = {_go_string(pt['artifact'])}")
    lines.append(f"\tPredicateTypeGoldens  = {_go_string(pt['goldens'])}")
    lines.append(f"\tPredicateTypeFixtures = {_go_string(pt['fixtures'])}")
    lines.append(f"\tPredicateTypeChannel  = {_go_string(pt['channel'])}")
    lines.append("")
    lines.append(f"\tKeyIDAlgorithm = {_go_string(trust['keyid_algorithm'])}")
    lines.append("")
    lines.append(f"\tRetryAttempts         = {retry['attempts']}")
    lines.append(f"\tRetryFirstWaitSeconds = {float(retry['first_wait_s'])}")
    lines.append(f"\tRetryMultiplier       = {float(retry['multiplier'])}")
    lines.append(f"\tRetryAfterOverBudget  = {_go_string(retry['retry_after_over_budget'])}")
    lines.append(f"\tDigest404Policy       = {_go_string(retry['digest_404'])}")
    lines.append(f"\tTag404Policy          = {_go_string(retry['tag_404'])}")
    lines.append("")
    lines.append(f"\tConnectTimeoutSeconds  = {float(timeouts['connect_s'])}")
    lines.append(f"\tIdleReadTimeoutSeconds = {float(timeouts['idle_read_s'])}")
    lines.append("")
    lines.append(f"\tManifestMaxBytes    = {limits['manifest_bytes']}")
    lines.append(f"\tBundleMaxBytes      = {limits['bundle_bytes']}")
    lines.append(f"\tTagsListMaxBytes    = {limits['tags_list_bytes']}")
    lines.append(f"\tLayoutIndexMaxBytes = {limits['layout_index_bytes']}")
    lines.append(f"\tMaxReferrers        = {limits['max_referrers']}")
    lines.append(f"\tMaxRedirects        = {limits['max_redirects']}")
    lines.append(f"\tMaxUnpackedBytes    = {limits['max_unpacked_bytes']}")
    lines.append(f"\tZstdWindowLogMax    = {limits['zstd_window_log_max']}")
    lines.append("")
    lines.append(f"\tRetiredBodyMaxBytes         = {limits['retired_body_bytes']}")
    lines.append(f"\tRetiredMessageMaxCodePoints = {limits['retired_message_code_points']}")
    lines.append("")
    lines.append(f"\tCacheRootTemplate     = {_go_string(cache['root_template'])}")
    lines.append(f"\tCacheUnpackedDir      = {_go_string(cache['unpacked_dir'])}")
    lines.append(f"\tCacheVerifiedRecord   = {_go_string(cache['verified_record'])}")
    lines.append(f"\tCacheAnnotationPrefix = {_go_string(cache['annotation_prefix'])}")
    lines.append("")
    lines.append(f"\tLockSchema      = {lock['schema']}")
    lines.append(f"\tLockDefaultFile = {_go_string(lock['default_file'])}")
    lines.append("")
    lines.append(f"\tAutofetchUnpublishedMemoSeconds = {float(autofetch['unpublished_memo_s'])}")
    lines.append(")")
    lines.append("")
    lines.append("// EnvRetired lists environment variables retired at v1; when one is set,")
    lines.append("// a fetch produces exactly one loud warning and otherwise ignores it.")
    go_list = ", ".join(_go_string(v) for v in env["retired"])
    lines.append(f"var EnvRetired = []string{{{go_list}}}")
    lines.append("")
    lines.append("// DefaultBases is the ordered, fallback base URL list used when")
    lines.append(f"// {env['target'] if False else reg['env_bases']} is unset.")
    go_list = ", ".join(_go_string(v) for v in reg["default_bases"])
    lines.append(f"var DefaultBases = []string{{{go_list}}}")
    lines.append("")
    lines.append("// AllowedSchemes is the set of URL schemes a configured base may use.")
    go_list = ", ".join(_go_string(v) for v in reg["allowed_schemes"])
    lines.append(f"var AllowedSchemes = []string{{{go_list}}}")
    lines.append("")
    lines.append("// Platform is one entry of the Platforms table.")
    lines.append("type Platform struct {")
    lines.append("\tKey          string")
    lines.append("\tOS           string")
    lines.append("\tArchitecture string")
    lines.append("}")
    lines.append("")
    lines.append("// Platforms is the v1 platform set (layout-v2 spec §0, A18): darwin-amd64 is")
    lines.append("// not built.")
    lines.append("var Platforms = []Platform{")
    for p in data["platforms"]:
        lines.append(
            f"\t{{Key: {_go_string(p['key'])}, OS: {_go_string(p['os'])}, "
            f"Architecture: {_go_string(p['architecture'])}}},"
        )
    lines.append("}")
    lines.append("")
    lines.append("// ReleaseKey is one trusted ed25519 public key, raw 32 bytes as lowercase hex.")
    lines.append("type ReleaseKey struct {")
    lines.append("\tKeyID      string")
    lines.append("\tEd25519Hex string")
    lines.append("}")
    lines.append("")
    lines.append("// ReleaseKeys is the default trust list: today's release key, unrotated for")
    lines.append("// v1 (layout-v2 spec §0/§4.2, D8).")
    lines.append("var ReleaseKeys = []ReleaseKey{")
    for k in trust["release_keys"]:
        lines.append(f"\t{{KeyID: {_go_string(k['keyid'])}, Ed25519Hex: {_go_string(k['ed25519_hex'])}}},")
    lines.append("}")
    lines.append("")
    lines.append("// RetryStatuses is the set of HTTP statuses that trigger a retry.")
    go_list = ", ".join(str(v) for v in retry["retry_statuses"])
    lines.append(f"var RetryStatuses = []int{{{go_list}}}")
    lines.append("")
    lines.append("// RetryAfterStatuses is the subset of RetryStatuses whose Retry-After header")
    lines.append("// (either the delta-seconds or the HTTP-date form) is honored.")
    go_list = ", ".join(str(v) for v in retry["retry_after_statuses"])
    lines.append(f"var RetryAfterStatuses = []int{{{go_list}}}")
    lines.append("")
    lines.append("// RetiredStatuses mean a retired repository: permanent, so never retried and")
    lines.append("// never a reason to try the next base (CHTYPES_SOURCE_RETIRED).")
    go_list = ", ".join(str(v) for v in retry["retired_statuses"])
    lines.append(f"var RetiredStatuses = []int{{{go_list}}}")
    lines.append("")
    lines.append("// SystemCacheDirs are read-only system directories searched after the user cache.")
    go_list = ", ".join(_go_string(v) for v in cache["system_dirs"])
    lines.append(f"var SystemCacheDirs = []string{{{go_list}}}")
    lines.append("")
    pv2 = data["prod_v2"]
    lines.append("// The production generation-2 channel (docs/guides/fetch-v1.md, \"Generation 2 after the lock\"):")
    lines.append("// the v1 contract with v2 values, trusting ReleaseKeys. Built, not yet the default.")
    lines.append("const (")
    pv2_consts = [
        ("ProdV2Name", _go_string(pv2["name"])),
        ("ProdV2ABIGeneration", str(pv2["abi_generation"])),
        ("ProdV2RecordSchema", str(pv2["record_schema"])),
        ("ProdV2CacheDir", _go_string(pv2["cache_leaf"])),
    ]
    pv2_w = max(len(n) for n, _ in pv2_consts)
    for n, v in pv2_consts:
        lines.append(f"\t{n.ljust(pv2_w)} = {v}")
    lines.append(")")
    lines.append("")
    lines.append(f"var ProdV2Bases = []string{{{', '.join(_go_string(v) for v in pv2['default_bases'])}}}")
    lines.append(f"var ProdV2SystemDirs = []string{{{', '.join(_go_string(v) for v in pv2['system_dirs'])}}}")
    lines.append("")
    lines.append("// TestKey is a fixture-only ed25519 public key, never in the default trust list.")
    lines.append("type TestKey struct {")
    field_names = ["KeyID", "Ed25519Hex", "TrustedByDefault"]
    field_types = ["string", "string", "bool"]
    field_w = max(len(n) for n in field_names)
    for n, t in zip(field_names, field_types):
        lines.append(f"\t{n.ljust(field_w)} {t}")
    lines.append("}")
    lines.append("")
    lines.append("// TestKeys are the fixture-generator's signing keys (tests/fixtures/fetch-v1/")
    lines.append("// test-key/). A conformance case trusts one only by explicitly naming it.")
    lines.append("var TestKeys = []TestKey{")
    for k in data["test_keys"]:
        lines.append(
            f"\t{{KeyID: {_go_string(k['keyid'])}, Ed25519Hex: {_go_string(k['ed25519_hex'])}, "
            f"TrustedByDefault: {str(k['trusted_by_default']).lower()}}},"
        )
    lines.append("}")
    lines.append("")
    lines.append("// ErrorExitCodes maps each shared error code to its process exit status")
    lines.append("// (docs/guides/fetch-v1.md §8). The six v0 codes carry v0's own real, measured")
    lines.append("// mapping (go/chtypes/fetch_errors.go ExitCode; Python, TypeScript and Rust")
    lines.append("// agree); the four new v1-only codes are assigned here, not inherited.")
    lines.append("var ErrorExitCodes = map[string]int{")
    key_w = max(len(f"{_go_string(code)}:") for code in data["errors"]) if data["errors"] else 0
    for code, exit_code in data["errors"].items():
        key = f"{_go_string(code)}:"
        lines.append(f"\t{key.ljust(key_w)} {exit_code},")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def _py_str(s: str) -> str:
    """A Python double-quoted string literal for `s`. `json.dumps` escapes
    exactly the characters every value in spec/fetch-v1/constants.json needs
    (backslash, and nothing more exotic), and always double-quotes — the
    default `ruff format` quote style this repository uses, so the generated
    file is already format-clean and `ruff format --check` never re-touches
    it."""
    return json.dumps(s)


def _py_tuple(reprs: list[str]) -> str:
    """A Python tuple literal from already-stringified elements, ruff-format-
    stable: a single element keeps its required trailing comma (`(x,)`); two
    or more never carry one, because a trailing comma there is ruff's (and
    Black's) "magic trailing comma" and forces one-element-per-line
    expansion, which would make --check's output disagree with what
    `ruff format` produces."""
    if len(reprs) == 1:
        return f"({reprs[0]},)"
    return f"({', '.join(reprs)})"


def render_python(data: dict[str, Any], sha: str) -> str:
    reg = data["registry"]
    env = data["env"]
    spelling = data["spelling"]
    mt = data["media_types"]
    dsse = data["dsse"]
    pt = data["predicate_types"]
    trust = data["trust"]
    retry = data["retry"]
    timeouts = data["timeouts"]
    limits = data["limits"]
    cache = data["cache"]
    lock = data["lock"]
    autofetch = data["autofetch"]

    lines: list[str] = []
    lines.append("# Code generated by scripts/fetch-v1/gen-constants.py from")
    lines.append(f"# {GENERATED_NOTICE}. DO NOT EDIT.")
    lines.append(f"# Source sha256: {sha}")
    lines.append('"""The v1 fetch constants, generated: see docs/guides/fetch-v1.md."""')
    lines.append("")
    lines.append("from __future__ import annotations")
    lines.append("")
    lines.append("from typing import Final")
    lines.append("")
    lines.append(f"SCHEMA_VERSION: Final[int] = {data['schema']}")
    lines.append(f"ABI_GENERATION: Final[int] = {data['abi_generation']}")
    lines.append("")
    lines.append(f"ENV_BASES_NAME: Final[str] = {_py_str(reg['env_bases'])}")
    lines.append(f"BASE_SEPARATOR: Final[str] = {_py_str(reg['base_separator'])}")
    lines.append(f"FIXTURES_REPO_SUFFIX: Final[str] = {_py_str(reg['fixtures_repository_suffix'])}")
    default_bases = _py_tuple([_py_str(v) for v in reg["default_bases"]])
    lines.append(f"DEFAULT_BASES: Final[tuple[str, ...]] = {default_bases}")
    allowed_schemes = _py_tuple([_py_str(v) for v in reg["allowed_schemes"]])
    lines.append(f"ALLOWED_SCHEMES: Final[tuple[str, ...]] = {allowed_schemes}")
    lines.append("")
    lines.append(f"ENV_CACHE_NAME: Final[str] = {_py_str(env['cache'])}")
    lines.append(f"ENV_TOKEN_NAME: Final[str] = {_py_str(env['token'])}")
    lines.append(f"ENV_TRUSTED_KEYS_NAME: Final[str] = {_py_str(env['trusted_keys'])}")
    lines.append(f"ENV_ALLOW_UNSIGNED_NAME: Final[str] = {_py_str(env['allow_unsigned'])}")
    lines.append(f"ENV_AUTOFETCH_NAME: Final[str] = {_py_str(env['autofetch'])}")
    lines.append(f"ENV_TARGET_NAME: Final[str] = {_py_str(env['target'])}")
    lines.append(f"ENV_CACHE_STRICT_NAME: Final[str] = {_py_str(env['strict_cache'])}")
    env_retired = _py_tuple([_py_str(v) for v in env["retired"]])
    lines.append(f"ENV_RETIRED: Final[tuple[str, ...]] = {env_retired}")
    lines.append("")
    lines.append(f"SPELLING_REGEX: Final[str] = (\n    {_py_str(spelling['regex'])}\n)")
    lines.append(f"SPELLING_REFUSE_HINT_REGEX: Final[str] = {_py_str(spelling['refuse_hint_regex'])}")
    lines.append("")
    lines.append("PLATFORMS: Final[tuple[dict[str, str], ...]] = (")
    for p in data["platforms"]:
        lines.append(
            f"    {{\"key\": {_py_str(p['key'])}, \"os\": {_py_str(p['os'])}, "
            f"\"architecture\": {_py_str(p['architecture'])}}},"
        )
    lines.append(")")
    lines.append("")
    lines.append(f"MEDIA_TYPE_INDEX: Final[str] = {_py_str(mt['index'])}")
    lines.append(f"MEDIA_TYPE_MANIFEST: Final[str] = {_py_str(mt['manifest'])}")
    lines.append(f"MEDIA_TYPE_EMPTY_CONFIG: Final[str] = {_py_str(mt['empty_config'])}")
    lines.append(f"MEDIA_TYPE_CONFIG: Final[str] = {_py_str(mt['config'])}")
    lines.append(f"MEDIA_TYPE_LAYER: Final[str] = {_py_str(mt['layer'])}")
    lines.append(f"MEDIA_TYPE_BUNDLE: Final[str] = {_py_str(mt['bundle'])}")
    lines.append(f"ARTIFACT_TYPE: Final[str] = {_py_str(mt['artifact_type'])}")
    lines.append(f"GOLDENS_ARTIFACT_TYPE: Final[str] = {_py_str(mt['goldens_artifact_type'])}")
    lines.append(f"FIXTURES_ARTIFACT_TYPE: Final[str] = {_py_str(mt['fixtures_artifact_type'])}")
    lines.append(f"CHANNEL_ARTIFACT_TYPE: Final[str] = {_py_str(mt['channel_artifact_type'])}")
    lines.append("")
    lines.append(f"DSSE_PAYLOAD_TYPE: Final[str] = {_py_str(dsse['payload_type'])}")
    lines.append(f"DSSE_MAX_SIGNATURES: Final[int] = {dsse['max_signatures']}")
    lines.append("")
    lines.append(f"STATEMENT_TYPE: Final[str] = {_py_str(data['statement_type'])}")
    lines.append("")
    lines.append(f"PREDICATE_TYPE_ARTIFACT: Final[str] = {_py_str(pt['artifact'])}")
    lines.append(f"PREDICATE_TYPE_GOLDENS: Final[str] = {_py_str(pt['goldens'])}")
    lines.append(f"PREDICATE_TYPE_FIXTURES: Final[str] = {_py_str(pt['fixtures'])}")
    lines.append(f"PREDICATE_TYPE_CHANNEL: Final[str] = {_py_str(pt['channel'])}")
    lines.append("")
    lines.append(f"KEYID_ALGORITHM: Final[str] = {_py_str(trust['keyid_algorithm'])}")
    lines.append("RELEASE_KEYS: Final[tuple[dict[str, str], ...]] = (")
    for k in trust["release_keys"]:
        lines.append("    {")
        lines.append(f'        "keyid": {_py_str(k["keyid"])},')
        lines.append(f'        "ed25519_hex": {_py_str(k["ed25519_hex"])},')
        lines.append("    },")
    lines.append(")")
    lines.append("")
    lines.append("# Fixture-only keys. A conformance case trusts one only by explicitly naming")
    lines.append("# it; TRUSTED_BY_DEFAULT is always False and never consulted for real trust.")
    lines.append("TEST_KEYS: Final[tuple[dict[str, object], ...]] = (")
    for k in data["test_keys"]:
        lines.append("    {")
        lines.append(f'        "keyid": {_py_str(k["keyid"])},')
        lines.append(f'        "ed25519_hex": {_py_str(k["ed25519_hex"])},')
        lines.append(f'        "trusted_by_default": {k["trusted_by_default"]!r},')
        lines.append("    },")
    lines.append(")")
    lines.append("")
    lines.append(f"RETRY_ATTEMPTS: Final[int] = {retry['attempts']}")
    lines.append(f"RETRY_FIRST_WAIT_S: Final[float] = {float(retry['first_wait_s'])}")
    lines.append(f"RETRY_MULTIPLIER: Final[float] = {float(retry['multiplier'])}")
    retry_statuses = _py_tuple([str(v) for v in retry["retry_statuses"]])
    lines.append(f"RETRY_STATUSES: Final[tuple[int, ...]] = {retry_statuses}")
    retry_after_statuses = _py_tuple([str(v) for v in retry["retry_after_statuses"]])
    lines.append(f"RETRY_AFTER_STATUSES: Final[tuple[int, ...]] = {retry_after_statuses}")
    lines.append(f"RETRY_AFTER_OVER_BUDGET: Final[str] = {_py_str(retry['retry_after_over_budget'])}")
    lines.append(f"DIGEST_404_POLICY: Final[str] = {_py_str(retry['digest_404'])}")
    lines.append(f"TAG_404_POLICY: Final[str] = {_py_str(retry['tag_404'])}")
    retired_statuses = _py_tuple([str(v) for v in retry["retired_statuses"]])
    lines.append(f"RETIRED_STATUSES: Final[tuple[int, ...]] = {retired_statuses}")
    lines.append("")
    lines.append(f"CONNECT_TIMEOUT_S: Final[float] = {float(timeouts['connect_s'])}")
    lines.append(f"IDLE_READ_TIMEOUT_S: Final[float] = {float(timeouts['idle_read_s'])}")
    lines.append("")
    lines.append(f"MANIFEST_MAX_BYTES: Final[int] = {limits['manifest_bytes']}")
    lines.append(f"BUNDLE_MAX_BYTES: Final[int] = {limits['bundle_bytes']}")
    lines.append(f"TAGS_LIST_MAX_BYTES: Final[int] = {limits['tags_list_bytes']}")
    lines.append(f"LAYOUT_INDEX_MAX_BYTES: Final[int] = {limits['layout_index_bytes']}")
    lines.append(f"MAX_REFERRERS: Final[int] = {limits['max_referrers']}")
    lines.append(f"MAX_REDIRECTS: Final[int] = {limits['max_redirects']}")
    lines.append(f"MAX_UNPACKED_BYTES: Final[int] = {limits['max_unpacked_bytes']}")
    lines.append(f"ZSTD_WINDOW_LOG_MAX: Final[int] = {limits['zstd_window_log_max']}")
    lines.append(f"RETIRED_BODY_MAX_BYTES: Final[int] = {limits['retired_body_bytes']}")
    lines.append(f"RETIRED_MESSAGE_MAX_CODE_POINTS: Final[int] = {limits['retired_message_code_points']}")
    lines.append("")
    lines.append(f"CACHE_ROOT_TEMPLATE: Final[str] = {_py_str(cache['root_template'])}")
    system_cache_dirs = _py_tuple([_py_str(v) for v in cache["system_dirs"]])
    lines.append(f"SYSTEM_CACHE_DIRS: Final[tuple[str, ...]] = {system_cache_dirs}")
    pv2 = data["prod_v2"]
    lines.append(f"PROD_V2_NAME: Final[str] = {_py_str(pv2['name'])}")
    lines.append(f"PROD_V2_ABI_GENERATION: Final[int] = {pv2['abi_generation']}")
    lines.append(f"PROD_V2_RECORD_SCHEMA: Final[int] = {pv2['record_schema']}")
    lines.append(f"PROD_V2_CACHE_DIR: Final[str] = {_py_str(pv2['cache_leaf'])}")
    lines.append(f"PROD_V2_BASES: Final[tuple[str, ...]] = {_py_tuple([_py_str(v) for v in pv2['default_bases']])}")
    lines.append(f"PROD_V2_SYSTEM_DIRS: Final[tuple[str, ...]] = {_py_tuple([_py_str(v) for v in pv2['system_dirs']])}")
    lines.append(f"CACHE_UNPACKED_DIR: Final[str] = {_py_str(cache['unpacked_dir'])}")
    lines.append(f"CACHE_VERIFIED_RECORD: Final[str] = {_py_str(cache['verified_record'])}")
    lines.append(f"CACHE_ANNOTATION_PREFIX: Final[str] = {_py_str(cache['annotation_prefix'])}")
    lines.append("")
    lines.append(f"LOCK_SCHEMA: Final[int] = {lock['schema']}")
    lines.append(f"LOCK_DEFAULT_FILE: Final[str] = {_py_str(lock['default_file'])}")
    lines.append("")
    lines.append(f"AUTOFETCH_UNPUBLISHED_MEMO_S: Final[float] = {float(autofetch['unpublished_memo_s'])}")
    lines.append("")
    lines.append("# docs/guides/fetch-v1.md §8: the six v0 codes carry v0's own real, measured")
    lines.append("# mapping (python/src/chtypes/__main__.py _EXIT_FOR_CODE; Go, TypeScript and")
    lines.append("# Rust agree); the four new v1-only codes are assigned here, not inherited.")
    lines.append("ERROR_EXIT_CODES: Final[dict[str, int]] = {")
    for code, exit_code in data["errors"].items():
        lines.append(f"    {_py_str(code)}: {exit_code},")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def render_python_init() -> str:
    return (
        '"""The private v1 OCI + zstd fetch layer (docs/guides/fetch-v1.md).\n\n'
        "Not part of this package's public API: every name here is subject to change\n"
        "without notice until the v1 switch. `_constants` is generated; the fetch\n"
        'logic lives in sibling modules added by the Python v1 lane.\n"""\n\n'
        "from __future__ import annotations\n"
    )


def render_ts(data: dict[str, Any], sha: str) -> str:
    reg = data["registry"]
    env = data["env"]
    spelling = data["spelling"]
    mt = data["media_types"]
    dsse = data["dsse"]
    pt = data["predicate_types"]
    trust = data["trust"]
    retry = data["retry"]
    timeouts = data["timeouts"]
    limits = data["limits"]
    cache = data["cache"]
    lock = data["lock"]
    autofetch = data["autofetch"]

    def arr(xs: list[str]) -> str:
        return "[" + ", ".join(json.dumps(x) for x in xs) + "]"

    lines: list[str] = []
    lines.append("// Code generated by scripts/fetch-v1/gen-constants.py from")
    lines.append(f"// {GENERATED_NOTICE} (sha256:{sha}). DO NOT EDIT.")
    lines.append("")
    lines.append("export const SCHEMA_VERSION = " + str(data["schema"]) + " as const;")
    lines.append("export const ABI_GENERATION = " + str(data["abi_generation"]) + " as const;")
    lines.append("")
    lines.append(f"export const ENV_BASES_NAME = {json.dumps(reg['env_bases'])} as const;")
    lines.append(f"export const BASE_SEPARATOR = {json.dumps(reg['base_separator'])} as const;")
    lines.append(f"export const FIXTURES_REPO_SUFFIX = {json.dumps(reg['fixtures_repository_suffix'])} as const;")
    lines.append(f"export const DEFAULT_BASES = {arr(reg['default_bases'])} as const;")
    lines.append(f"export const ALLOWED_SCHEMES = {arr(reg['allowed_schemes'])} as const;")
    lines.append("")
    lines.append(f"export const ENV_CACHE_NAME = {json.dumps(env['cache'])} as const;")
    lines.append(f"export const ENV_TOKEN_NAME = {json.dumps(env['token'])} as const;")
    lines.append(f"export const ENV_TRUSTED_KEYS_NAME = {json.dumps(env['trusted_keys'])} as const;")
    lines.append(f"export const ENV_ALLOW_UNSIGNED_NAME = {json.dumps(env['allow_unsigned'])} as const;")
    lines.append(f"export const ENV_AUTOFETCH_NAME = {json.dumps(env['autofetch'])} as const;")
    lines.append(f"export const ENV_TARGET_NAME = {json.dumps(env['target'])} as const;")
    lines.append(f"export const ENV_CACHE_STRICT_NAME = {json.dumps(env['strict_cache'])} as const;")
    lines.append(f"export const ENV_RETIRED = {arr(env['retired'])} as const;")
    lines.append("")
    lines.append(f"export const SPELLING_REGEX = {json.dumps(spelling['regex'])} as const;")
    lines.append(f"export const SPELLING_REFUSE_HINT_REGEX = {json.dumps(spelling['refuse_hint_regex'])} as const;")
    lines.append("")
    lines.append("export const PLATFORMS = [")
    for p in data["platforms"]:
        lines.append(
            f"  {{ key: {json.dumps(p['key'])}, os: {json.dumps(p['os'])}, "
            f"architecture: {json.dumps(p['architecture'])} }},"
        )
    lines.append("] as const;")
    lines.append("")
    lines.append(f"export const MEDIA_TYPE_INDEX = {json.dumps(mt['index'])} as const;")
    lines.append(f"export const MEDIA_TYPE_MANIFEST = {json.dumps(mt['manifest'])} as const;")
    lines.append(f"export const MEDIA_TYPE_EMPTY_CONFIG = {json.dumps(mt['empty_config'])} as const;")
    lines.append(f"export const MEDIA_TYPE_CONFIG = {json.dumps(mt['config'])} as const;")
    lines.append(f"export const MEDIA_TYPE_LAYER = {json.dumps(mt['layer'])} as const;")
    lines.append(f"export const MEDIA_TYPE_BUNDLE = {json.dumps(mt['bundle'])} as const;")
    lines.append(f"export const ARTIFACT_TYPE = {json.dumps(mt['artifact_type'])} as const;")
    lines.append(f"export const GOLDENS_ARTIFACT_TYPE = {json.dumps(mt['goldens_artifact_type'])} as const;")
    lines.append(f"export const FIXTURES_ARTIFACT_TYPE = {json.dumps(mt['fixtures_artifact_type'])} as const;")
    lines.append(f"export const CHANNEL_ARTIFACT_TYPE = {json.dumps(mt['channel_artifact_type'])} as const;")
    lines.append("")
    lines.append(f"export const DSSE_PAYLOAD_TYPE = {json.dumps(dsse['payload_type'])} as const;")
    lines.append(f"export const DSSE_MAX_SIGNATURES = {dsse['max_signatures']} as const;")
    lines.append("")
    lines.append(f"export const STATEMENT_TYPE = {json.dumps(data['statement_type'])} as const;")
    lines.append("")
    lines.append(f"export const PREDICATE_TYPE_ARTIFACT = {json.dumps(pt['artifact'])} as const;")
    lines.append(f"export const PREDICATE_TYPE_GOLDENS = {json.dumps(pt['goldens'])} as const;")
    lines.append(f"export const PREDICATE_TYPE_FIXTURES = {json.dumps(pt['fixtures'])} as const;")
    lines.append(f"export const PREDICATE_TYPE_CHANNEL = {json.dumps(pt['channel'])} as const;")
    lines.append("")
    lines.append(f"export const KEYID_ALGORITHM = {json.dumps(trust['keyid_algorithm'])} as const;")
    lines.append("export const RELEASE_KEYS = [")
    for k in trust["release_keys"]:
        lines.append(f"  {{ keyid: {json.dumps(k['keyid'])}, ed25519Hex: {json.dumps(k['ed25519_hex'])} }},")
    lines.append("] as const;")
    lines.append("")
    lines.append("// Fixture-only keys. A conformance case trusts one only by explicitly naming")
    lines.append("// it; trustedByDefault is always false and never consulted for real trust.")
    lines.append("export const TEST_KEYS = [")
    for k in data["test_keys"]:
        lines.append(
            f"  {{ keyid: {json.dumps(k['keyid'])}, ed25519Hex: {json.dumps(k['ed25519_hex'])}, "
            f"trustedByDefault: {json.dumps(k['trusted_by_default'])} }},"
        )
    lines.append("] as const;")
    lines.append("")
    lines.append(f"export const RETRY_ATTEMPTS = {retry['attempts']} as const;")
    lines.append(f"export const RETRY_FIRST_WAIT_S = {float(retry['first_wait_s'])} as const;")
    lines.append(f"export const RETRY_MULTIPLIER = {float(retry['multiplier'])} as const;")
    lines.append(f"export const RETRY_STATUSES = {arr([str(v) for v in retry['retry_statuses']])};" .replace('"', ""))
    lines.append(
        f"export const RETRY_AFTER_STATUSES = {arr([str(v) for v in retry['retry_after_statuses']])};".replace(
            '"', ""
        )
    )
    lines.append(f"export const RETRY_AFTER_OVER_BUDGET = {json.dumps(retry['retry_after_over_budget'])} as const;")
    lines.append(f"export const DIGEST_404_POLICY = {json.dumps(retry['digest_404'])} as const;")
    lines.append(f"export const TAG_404_POLICY = {json.dumps(retry['tag_404'])} as const;")
    lines.append(
        f"export const RETIRED_STATUSES = {arr([str(v) for v in retry['retired_statuses']])};".replace('"', "")
    )
    lines.append("")
    lines.append(f"export const CONNECT_TIMEOUT_S = {float(timeouts['connect_s'])} as const;")
    lines.append(f"export const IDLE_READ_TIMEOUT_S = {float(timeouts['idle_read_s'])} as const;")
    lines.append("")
    lines.append(f"export const MANIFEST_MAX_BYTES = {limits['manifest_bytes']} as const;")
    lines.append(f"export const BUNDLE_MAX_BYTES = {limits['bundle_bytes']} as const;")
    lines.append(f"export const TAGS_LIST_MAX_BYTES = {limits['tags_list_bytes']} as const;")
    lines.append(f"export const LAYOUT_INDEX_MAX_BYTES = {limits['layout_index_bytes']} as const;")
    lines.append(f"export const MAX_REFERRERS = {limits['max_referrers']} as const;")
    lines.append(f"export const MAX_REDIRECTS = {limits['max_redirects']} as const;")
    lines.append(f"export const MAX_UNPACKED_BYTES = {limits['max_unpacked_bytes']} as const;")
    lines.append(f"export const ZSTD_WINDOW_LOG_MAX = {limits['zstd_window_log_max']} as const;")
    lines.append(f"export const RETIRED_BODY_MAX_BYTES = {limits['retired_body_bytes']} as const;")
    lines.append(f"export const RETIRED_MESSAGE_MAX_CODE_POINTS = {limits['retired_message_code_points']} as const;")
    lines.append("")
    # A plain double-quoted string, deliberately NOT a template literal: the
    # value contains literal `${...}` shell-expansion syntax (a shell path
    # template, never interpolated by this language), and a backtick string
    # would try to evaluate it as a JS expression. biome's
    # noTemplateCurlyInString is a false positive here, suppressed inline.
    lines.append("// biome-ignore lint/suspicious/noTemplateCurlyInString: a shell path template, not JS")
    lines.append(f"export const CACHE_ROOT_TEMPLATE = {json.dumps(cache['root_template'])} as const;")
    lines.append(f"export const SYSTEM_CACHE_DIRS = {arr(cache['system_dirs'])} as const;")
    pv2 = data["prod_v2"]
    lines.append(f"export const PROD_V2_NAME = {json.dumps(pv2['name'])} as const;")
    lines.append(f"export const PROD_V2_ABI_GENERATION = {pv2['abi_generation']} as const;")
    lines.append(f"export const PROD_V2_RECORD_SCHEMA = {pv2['record_schema']} as const;")
    lines.append(f"export const PROD_V2_CACHE_DIR = {json.dumps(pv2['cache_leaf'])} as const;")
    lines.append(f"export const PROD_V2_BASES = {arr(pv2['default_bases'])} as const;")
    lines.append(f"export const PROD_V2_SYSTEM_DIRS = {arr(pv2['system_dirs'])} as const;")
    lines.append(f"export const CACHE_UNPACKED_DIR = {json.dumps(cache['unpacked_dir'])} as const;")
    lines.append(f"export const CACHE_VERIFIED_RECORD = {json.dumps(cache['verified_record'])} as const;")
    lines.append(f"export const CACHE_ANNOTATION_PREFIX = {json.dumps(cache['annotation_prefix'])} as const;")
    lines.append("")
    lines.append(f"export const LOCK_SCHEMA = {lock['schema']} as const;")
    lines.append(f"export const LOCK_DEFAULT_FILE = {json.dumps(lock['default_file'])} as const;")
    lines.append("")
    lines.append(f"export const AUTOFETCH_UNPUBLISHED_MEMO_S = {float(autofetch['unpublished_memo_s'])} as const;")
    lines.append("")
    lines.append("// docs/guides/fetch-v1.md §8: the six v0 codes carry v0's own real, measured")
    lines.append("// mapping (ts/src/cli.ts EXIT / errorToExitCode; Go, Python and Rust agree);")
    lines.append("// the four new v1-only codes are assigned here, not inherited.")
    lines.append("export const ERROR_EXIT_CODES: Readonly<Record<string, number>> = {")
    for code, exit_code in data["errors"].items():
        lines.append(f"  {json.dumps(code)}: {exit_code},")
    lines.append("};")
    lines.append("")
    return "\n".join(lines)


def render_rust(data: dict[str, Any], sha: str) -> str:
    reg = data["registry"]
    env = data["env"]
    spelling = data["spelling"]
    mt = data["media_types"]
    dsse = data["dsse"]
    pt = data["predicate_types"]
    trust = data["trust"]
    retry = data["retry"]
    timeouts = data["timeouts"]
    limits = data["limits"]
    cache = data["cache"]
    lock = data["lock"]
    autofetch = data["autofetch"]

    def rs_str(s: str) -> str:
        return json.dumps(s)

    def rs_arr(xs: list[str]) -> str:
        return "&[" + ", ".join(rs_str(x) for x in xs) + "]"

    lines: list[str] = []
    lines.append("// Code generated by scripts/fetch-v1/gen-constants.py from")
    lines.append(f"// {GENERATED_NOTICE} (sha256:{sha}). DO NOT EDIT.")
    lines.append("")
    lines.append(f"pub const SCHEMA_VERSION: u32 = {data['schema']};")
    lines.append(f"pub const ABI_GENERATION: u32 = {data['abi_generation']};")
    lines.append("")
    lines.append(f"pub const ENV_BASES_NAME: &str = {rs_str(reg['env_bases'])};")
    lines.append(f"pub const BASE_SEPARATOR: &str = {rs_str(reg['base_separator'])};")
    lines.append(f"pub const FIXTURES_REPO_SUFFIX: &str = {rs_str(reg['fixtures_repository_suffix'])};")
    lines.append(f"pub const DEFAULT_BASES: &[&str] = {rs_arr(reg['default_bases'])};")
    lines.append(f"pub const ALLOWED_SCHEMES: &[&str] = {rs_arr(reg['allowed_schemes'])};")
    lines.append("")
    lines.append(f"pub const ENV_CACHE_NAME: &str = {rs_str(env['cache'])};")
    lines.append(f"pub const ENV_TOKEN_NAME: &str = {rs_str(env['token'])};")
    lines.append(f"pub const ENV_TRUSTED_KEYS_NAME: &str = {rs_str(env['trusted_keys'])};")
    lines.append(f"pub const ENV_ALLOW_UNSIGNED_NAME: &str = {rs_str(env['allow_unsigned'])};")
    lines.append(f"pub const ENV_AUTOFETCH_NAME: &str = {rs_str(env['autofetch'])};")
    lines.append(f"pub const ENV_TARGET_NAME: &str = {rs_str(env['target'])};")
    lines.append(f"pub const ENV_CACHE_STRICT_NAME: &str = {rs_str(env['strict_cache'])};")
    lines.append(f"pub const ENV_RETIRED: &[&str] = {rs_arr(env['retired'])};")
    lines.append("")
    lines.append(f"pub const SPELLING_REGEX: &str =\n    {rs_str(spelling['regex'])};")
    lines.append(f"pub const SPELLING_REFUSE_HINT_REGEX: &str = {rs_str(spelling['refuse_hint_regex'])};")
    lines.append("")
    lines.append("/// One entry of PLATFORMS.")
    lines.append("pub struct Platform {")
    lines.append("    pub key: &'static str,")
    lines.append("    pub os: &'static str,")
    lines.append("    pub architecture: &'static str,")
    lines.append("}")
    lines.append("")
    lines.append("/// The v1 platform set (layout-v2 spec §0, A18): darwin-amd64 is not built.")
    lines.append("pub const PLATFORMS: &[Platform] = &[")
    for p in data["platforms"]:
        lines.append(
            f"    Platform {{ key: {rs_str(p['key'])}, os: {rs_str(p['os'])}, "
            f"architecture: {rs_str(p['architecture'])} }},"
        )
    lines.append("];")
    lines.append("")
    lines.append(f"pub const MEDIA_TYPE_INDEX: &str = {rs_str(mt['index'])};")
    lines.append(f"pub const MEDIA_TYPE_MANIFEST: &str = {rs_str(mt['manifest'])};")
    lines.append(f"pub const MEDIA_TYPE_EMPTY_CONFIG: &str = {rs_str(mt['empty_config'])};")
    lines.append(f"pub const MEDIA_TYPE_CONFIG: &str = {rs_str(mt['config'])};")
    lines.append(f"pub const MEDIA_TYPE_LAYER: &str = {rs_str(mt['layer'])};")
    lines.append(f"pub const MEDIA_TYPE_BUNDLE: &str = {rs_str(mt['bundle'])};")
    lines.append(f"pub const ARTIFACT_TYPE: &str = {rs_str(mt['artifact_type'])};")
    lines.append(f"pub const GOLDENS_ARTIFACT_TYPE: &str = {rs_str(mt['goldens_artifact_type'])};")
    lines.append(f"pub const FIXTURES_ARTIFACT_TYPE: &str = {rs_str(mt['fixtures_artifact_type'])};")
    lines.append(f"pub const CHANNEL_ARTIFACT_TYPE: &str = {rs_str(mt['channel_artifact_type'])};")
    lines.append("")
    lines.append(f"pub const DSSE_PAYLOAD_TYPE: &str = {rs_str(dsse['payload_type'])};")
    lines.append(f"pub const DSSE_MAX_SIGNATURES: u32 = {dsse['max_signatures']};")
    lines.append("")
    lines.append(f"pub const STATEMENT_TYPE: &str = {rs_str(data['statement_type'])};")
    lines.append("")
    lines.append(f"pub const PREDICATE_TYPE_ARTIFACT: &str = {rs_str(pt['artifact'])};")
    lines.append(f"pub const PREDICATE_TYPE_GOLDENS: &str = {rs_str(pt['goldens'])};")
    lines.append(f"pub const PREDICATE_TYPE_FIXTURES: &str =\n    {rs_str(pt['fixtures'])};")
    lines.append(f"pub const PREDICATE_TYPE_CHANNEL: &str = {rs_str(pt['channel'])};")
    lines.append("")
    lines.append(f"pub const KEYID_ALGORITHM: &str = {rs_str(trust['keyid_algorithm'])};")
    lines.append("")
    lines.append("/// One trusted ed25519 public key, raw 32 bytes as lowercase hex.")
    lines.append("pub struct ReleaseKey {")
    lines.append("    pub keyid: &'static str,")
    lines.append("    pub ed25519_hex: &'static str,")
    lines.append("}")
    lines.append("")
    lines.append("/// The default trust list: today's release key, unrotated for v1")
    lines.append("/// (layout-v2 spec §0/§4.2, D8).")
    lines.append("pub const RELEASE_KEYS: &[ReleaseKey] = &[")
    for k in trust["release_keys"]:
        lines.append(f"    ReleaseKey {{ keyid: {rs_str(k['keyid'])}, ed25519_hex: {rs_str(k['ed25519_hex'])} }},")
    lines.append("];")
    lines.append("")
    lines.append("/// A fixture-only ed25519 public key, never in the default trust list.")
    lines.append("pub struct TestKey {")
    lines.append("    pub keyid: &'static str,")
    lines.append("    pub ed25519_hex: &'static str,")
    lines.append("    pub trusted_by_default: bool,")
    lines.append("}")
    lines.append("")
    lines.append("/// Fixture-generator signing keys (tests/fixtures/fetch-v1/test-key/). A")
    lines.append("/// conformance case trusts one only by explicitly naming it.")
    lines.append("pub const TEST_KEYS: &[TestKey] = &[")
    for k in data["test_keys"]:
        lines.append(
            f"    TestKey {{ keyid: {rs_str(k['keyid'])}, ed25519_hex: {rs_str(k['ed25519_hex'])}, "
            f"trusted_by_default: {str(k['trusted_by_default']).lower()} }},"
        )
    lines.append("];")
    lines.append("")
    lines.append(f"pub const RETRY_ATTEMPTS: u32 = {retry['attempts']};")
    lines.append(f"pub const RETRY_FIRST_WAIT_S: f64 = {float(retry['first_wait_s'])};")
    lines.append(f"pub const RETRY_MULTIPLIER: f64 = {float(retry['multiplier'])};")
    go_list = ", ".join(str(v) for v in retry["retry_statuses"])
    lines.append(f"pub const RETRY_STATUSES: &[u16] = &[{go_list}];")
    go_list = ", ".join(str(v) for v in retry["retry_after_statuses"])
    lines.append(f"pub const RETRY_AFTER_STATUSES: &[u16] = &[{go_list}];")
    lines.append(f"pub const RETRY_AFTER_OVER_BUDGET: &str = {rs_str(retry['retry_after_over_budget'])};")
    lines.append(f"pub const DIGEST_404_POLICY: &str = {rs_str(retry['digest_404'])};")
    lines.append(f"pub const TAG_404_POLICY: &str = {rs_str(retry['tag_404'])};")
    go_list = ", ".join(str(v) for v in retry["retired_statuses"])
    lines.append(f"pub const RETIRED_STATUSES: &[u16] = &[{go_list}];")
    lines.append("")
    lines.append(f"pub const CONNECT_TIMEOUT_S: f64 = {float(timeouts['connect_s'])};")
    lines.append(f"pub const IDLE_READ_TIMEOUT_S: f64 = {float(timeouts['idle_read_s'])};")
    lines.append("")
    lines.append(f"pub const MANIFEST_MAX_BYTES: u64 = {limits['manifest_bytes']};")
    lines.append(f"pub const BUNDLE_MAX_BYTES: u64 = {limits['bundle_bytes']};")
    lines.append(f"pub const TAGS_LIST_MAX_BYTES: u64 = {limits['tags_list_bytes']};")
    lines.append(f"pub const LAYOUT_INDEX_MAX_BYTES: u64 = {limits['layout_index_bytes']};")
    lines.append(f"pub const MAX_REFERRERS: u32 = {limits['max_referrers']};")
    lines.append(f"pub const MAX_REDIRECTS: u32 = {limits['max_redirects']};")
    lines.append(f"pub const MAX_UNPACKED_BYTES: u64 = {limits['max_unpacked_bytes']};")
    lines.append(f"pub const ZSTD_WINDOW_LOG_MAX: u32 = {limits['zstd_window_log_max']};")
    lines.append(f"pub const RETIRED_BODY_MAX_BYTES: u64 = {limits['retired_body_bytes']};")
    lines.append(f"pub const RETIRED_MESSAGE_MAX_CODE_POINTS: u32 = {limits['retired_message_code_points']};")
    lines.append("")
    lines.append(f"pub const CACHE_ROOT_TEMPLATE: &str = {rs_str(cache['root_template'])};")
    lines.append(f"pub const SYSTEM_CACHE_DIRS: &[&str] = {rs_arr(cache['system_dirs'])};")
    pv2 = data["prod_v2"]
    lines.append(f"pub const PROD_V2_NAME: &str = {rs_str(pv2['name'])};")
    lines.append(f"pub const PROD_V2_ABI_GENERATION: u32 = {pv2['abi_generation']};")
    lines.append(f"pub const PROD_V2_RECORD_SCHEMA: u32 = {pv2['record_schema']};")
    lines.append(f"pub const PROD_V2_CACHE_DIR: &str = {rs_str(pv2['cache_leaf'])};")
    lines.append(f"pub const PROD_V2_BASES: &[&str] = {rs_arr(pv2['default_bases'])};")
    lines.append(f"pub const PROD_V2_SYSTEM_DIRS: &[&str] = {rs_arr(pv2['system_dirs'])};")
    lines.append(f"pub const CACHE_UNPACKED_DIR: &str = {rs_str(cache['unpacked_dir'])};")
    lines.append(f"pub const CACHE_VERIFIED_RECORD: &str = {rs_str(cache['verified_record'])};")
    lines.append(f"pub const CACHE_ANNOTATION_PREFIX: &str = {rs_str(cache['annotation_prefix'])};")
    lines.append("")
    lines.append(f"pub const LOCK_SCHEMA: u32 = {lock['schema']};")
    lines.append(f"pub const LOCK_DEFAULT_FILE: &str = {rs_str(lock['default_file'])};")
    lines.append("")
    lines.append(f"pub const AUTOFETCH_UNPUBLISHED_MEMO_S: f64 = {float(autofetch['unpublished_memo_s'])};")
    lines.append("")
    lines.append("/// One (error code, exit status) pair (docs/guides/fetch-v1.md §8).")
    lines.append("pub struct ErrorExitCode {")
    lines.append("    pub code: &'static str,")
    lines.append("    pub exit_code: u8,")
    lines.append("}")
    lines.append("")
    lines.append("/// The six v0 codes carry v0's own real, measured mapping (rust/src/main.rs")
    lines.append("/// exit_code_for; Go, Python and TypeScript agree); the four new v1-only")
    lines.append("/// codes are assigned here, not inherited.")
    lines.append("pub const ERROR_EXIT_CODES: &[ErrorExitCode] = &[")
    for code, exit_code in data["errors"].items():
        lines.append(f"    ErrorExitCode {{ code: {rs_str(code)}, exit_code: {exit_code} }},")
    lines.append("];")
    lines.append("")
    return "\n".join(lines)


def _md_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """A GitHub-flavored markdown table, column-padded exactly the way
    `dprint fmt` (markdown plugin) reformats one — so the generated block
    this feeds and the `prose` CI job's dprint check can never fight each
    other, the same discipline scripts/policy-merge-check.py's
    protected_globs_guide_block() follows for CONTRIBUTING.md. Each column's
    width is the longest cell in it, including the header."""
    widths = [max(len(c) for c in col) for col in zip(headers, *rows, strict=True)]

    def fmt_row(cells: list[str]) -> str:
        return "| " + " | ".join(cell.ljust(w) for cell, w in zip(cells, widths, strict=True)) + " |"

    out = [fmt_row(headers), "| " + " | ".join("-" * w for w in widths) + " |"]
    out.extend(fmt_row(row) for row in rows)
    return out


def render_guide_block(data: dict[str, Any], sha: str) -> str:
    """The exact text between GUIDE_BEGIN/GUIDE_END in docs/guides/fetch-v1.md:
    a human-readable table, never hand-typed (the same discipline
    scripts/policy-merge-check.py's CONTRIBUTING.md block follows)."""
    lines = [GUIDE_BEGIN, ""]
    lines.append(
        f"Generated from `{SOURCE_RELPATH}` (sha256:`{sha}`) by `scripts/fetch-v1/gen-constants.py`; "
        "do not hand-edit between the markers. Covers this guide's §7 and §8, plus a quick reference "
        "for the media types, predicate types and default trust key introduced in earlier sections."
    )
    lines.append("")
    lines.append("## Reference")
    lines.append("")
    lines.append(f"- schema `{data['schema']}` · ABI generation `{data['abi_generation']}`")
    lines.append(f"- default base(s): {', '.join(f'`{b}`' for b in data['registry']['default_bases'])}")
    pv2 = data["prod_v2"]
    lines.append(
        f"- production generation-2 channel `{pv2['name']}`: ABI `{pv2['abi_generation']}`, record schema "
        f"`{pv2['record_schema']}`, cache leaf `{pv2['cache_leaf']}`, base(s) "
        f"{', '.join(f'`{b}`' for b in pv2['default_bases'])}, system dirs "
        f"{', '.join(f'`{d}`' for d in pv2['system_dirs'])}, trusting the release key above"
    )
    platform_keys = ", ".join(f"`{p['key']}`" for p in data["platforms"])
    lines.append(f"- platforms: {platform_keys}")
    lines.append("")
    lines.extend(
        _md_table(
            ["media type / artifactType", "value"],
            [[f"`{key}`", f"`{value}`"] for key, value in data["media_types"].items()],
        )
    )
    lines.append("")
    lines.extend(
        _md_table(
            ["predicateType", "value"],
            [[f"`{key}`", f"`{value}`"] for key, value in data["predicate_types"].items()],
        )
    )
    lines.append("")
    lines.extend(
        _md_table(
            ["default trust (release key)", "key id"],
            [[f"`{k['ed25519_hex']}`", f"`{k['keyid']}`"] for k in data["trust"]["release_keys"]],
        )
    )
    lines.append("")
    lines.append(
        f"Key id algorithm: `{data['trust']['keyid_algorithm']}` — the first 16 hex characters of "
        "sha256 over the raw 32-byte public key (`go/internal/ocifetch/dsse.go` `keyIDFor`, unchanged from v0)."
    )
    lines.append("")
    lines.append("## 7. The retry table")
    lines.append("")
    lines.append(
        f"{data['retry']['attempts']} attempts, first wait {data['retry']['first_wait_s']}s, "
        f"×{data['retry']['multiplier']} each time; retry on "
        f"{', '.join(str(s) for s in data['retry']['retry_statuses'])}; honor `Retry-After` on "
        f"{', '.join(str(s) for s in data['retry']['retry_after_statuses'])}; a `Retry-After` past the "
        "remaining budget is refused rather than shortened, and the refusal names the value."
    )
    lines.append("")
    lines.append(f"A digest 404: `{data['retry']['digest_404']}`. A tag 404: `{data['retry']['tag_404']}`.")
    lines.append("")
    retired = ", ".join(str(s) for s in data["retry"]["retired_statuses"])
    lines.append(
        f"A {retired}, on any request: permanent, a retired repository. It is never retried and never a reason "
        "to try the next base, and it is `CHTYPES_SOURCE_RETIRED`, carrying the registry's own message from at "
        f"most {data['limits']['retired_body_bytes']} bytes of the body, cut to "
        f"{data['limits']['retired_message_code_points']} code points (§2, \"A retired repository\")."
    )
    lines.append("")
    lines.append("## 8. Errors")
    lines.append("")
    lines.extend(
        _md_table(
            ["error code", "exit status"],
            [[f"`{code}`", str(exit_code)] for code, exit_code in data["errors"].items()],
        )
    )
    lines.append("")
    lines.append(
        "The six codes above the line in `spec/fetch-v1/constants.json` carry v0's own real, measured exit "
        "status (every binding's CLI agreed on 2026-10-01: the four \"verification failed\" codes all exit "
        "1, `CHTYPES_ARTIFACT_UNPUBLISHED` exits 4, `CHTYPES_SOURCE_UNREACHABLE` exits 3). The new v1-only "
        "codes have no v0 precedent and are assigned here: the first four by lane 0A, "
        "`CHTYPES_CACHE_UNUSABLE` by public issue #486 and `CHTYPES_SOURCE_RETIRED` by public issue #571:"
    )
    lines.append("")
    lines.append(
        "- `CHTYPES_SOURCE_UNAUTHORIZED` and `CHTYPES_SOURCE_FORBIDDEN` — the fetch layer, a 401 or 403 "
        "from a source (§2, §5.1 of this guide)."
    )
    lines.append(
        "- `CHTYPES_SOURCE_INCOMPATIBLE` — the fetch layer, an unrecognized manifest or layer media type "
        "during resolve (§3)."
    )
    lines.append(
        "- `CHTYPES_ARTIFACT_INCOMPATIBLE` — the **FFI/loader layer**, never the fetch layer: a wrong "
        "`chs_abi_version`, an `abi_fingerprint` mismatch against the loaded library, a described symbol "
        "the library does not export, or a glibc floor the host does not meet (§9, the seam). It is "
        "reserved here, ahead of that lane landing, so the shared error vocabulary and exit-code table stay "
        "in one place while the ABI v1 design is still in flux. No v1 fetch-layer conformance case raises "
        "it — the fetch layer \"never dlopens, checks glibc, or reads `chs_*` symbols\" (§1.3 of the v1 "
        "fetch-layer plan)."
    )
    lines.append(
        "- `CHTYPES_CACHE_UNUSABLE` — the fetch layer, a cache directory or entry it could not read or "
        "write, or that strict mode refuses: it names the path and a reason (§1, the cache faults). It is "
        "the one code for the local filesystem, never the network's `CHTYPES_SOURCE_UNREACHABLE`, so a "
        "retry loop never retries a permission error."
    )
    lines.append(
        "- `CHTYPES_SOURCE_RETIRED` — the fetch layer, a `410 Gone` from a source: a retired repository, "
        "which is permanent, so the request is never retried and never sent to the next base (§2, \"A "
        "retired repository\"; §7). Its message names the URL that answered and carries the registry's own "
        "message, made safe to print. Assigned by public issue #571."
    )
    lines.append("")
    lines.append(GUIDE_END)
    return "\n".join(lines)


# ------------------------------------------------------------------ check / write


def render_all(data: dict[str, Any], sha: str) -> dict[str, str]:
    return {
        OUTPUT_RELPATHS["go"]: render_go(data, sha),
        OUTPUT_RELPATHS["python"]: render_python(data, sha),
        OUTPUT_RELPATHS["ts"]: render_ts(data, sha),
        OUTPUT_RELPATHS["rust"]: render_rust(data, sha),
    }


def guide_with_block(root: Path, block: str) -> str | None:
    """root's docs/guides/fetch-v1.md with its generated block replaced by
    `block`, or None if the guide does not exist or carries no markers."""
    path = root / GUIDE_RELPATH
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    start = text.find(GUIDE_BEGIN)
    end = text.find(GUIDE_END)
    if start == -1 or end == -1 or end < start:
        return None
    end += len(GUIDE_END)
    return text[:start] + block + text[end:]


def check_tree(root: Path) -> list[str]:
    """Every way root's generated outputs disagree with regenerating from
    root's own spec/fetch-v1/constants.json. [] means everything agrees."""
    data, sha = load_constants(root)
    problems = validate(data)
    if problems:
        return problems
    rendered = render_all(data, sha)
    for relpath, text in rendered.items():
        path = root / relpath
        if not path.exists():
            problems.append(f"{relpath} does not exist; run --write")
        elif path.read_text(encoding="utf-8") != text:
            problems.append(f"{relpath} disagrees with a fresh regeneration; run --write")
    guide_block = render_guide_block(data, sha)
    new_guide = guide_with_block(root, guide_block)
    if new_guide is None:
        problems.append(f"{GUIDE_RELPATH} is missing or carries no {GUIDE_BEGIN!r}/{GUIDE_END!r} markers")
    else:
        current = (root / GUIDE_RELPATH).read_text(encoding="utf-8")
        if current != new_guide:
            problems.append(f"{GUIDE_RELPATH}'s generated block disagrees with a fresh regeneration; run --write")
    # Also require that no `test_keys` entry ever reaches the generated
    # outputs' DEFAULT trust table under a different name — guards the
    # renderers themselves, not just the source.
    for relpath in (OUTPUT_RELPATHS["go"], OUTPUT_RELPATHS["python"], OUTPUT_RELPATHS["ts"], OUTPUT_RELPATHS["rust"]):
        path = root / relpath
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for k in data["test_keys"]:
            release_block_markers = ("ReleaseKeys", "RELEASE_KEYS")
            # A cheap, language-agnostic check: the test key's hex must not
            # appear anywhere near a "release" identifier's definition line.
            for line in text.splitlines():
                if k["ed25519_hex"] in line and any(m in line for m in release_block_markers):
                    problems.append(f"{relpath}: a test key hex literal appears on a release-keys line")
    return problems


def write_tree(root: Path) -> None:
    data, sha = load_constants(root)
    problems = validate(data)
    if problems:
        raise SystemExit("\n".join(f"gen-constants: {p}" for p in problems))
    rendered = render_all(data, sha)
    for relpath, text in rendered.items():
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    init_path = root / "python" / "src" / "chtypes" / "_ocifetch" / "__init__.py"
    if not init_path.exists():
        init_path.write_text(render_python_init(), encoding="utf-8")
    guide_block = render_guide_block(data, sha)
    new_guide = guide_with_block(root, guide_block)
    if new_guide is not None:
        (root / GUIDE_RELPATH).write_text(new_guide, encoding="utf-8")


# ------------------------------------------------------------------ selftest


def _copy_tree_for_selftest(tmp: Path) -> None:
    """Populate tmp with the real spec/fetch-v1/constants.json, the real
    docs/guides/fetch-v1.md (for its markers and surrounding prose), and then
    a FRESH, consistent regeneration of every binding output and the guide's
    block — exactly what `--write` would produce from the real source today.
    Never touches the real tree; everything happens under tmp."""
    (tmp / "spec" / "fetch-v1").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / SOURCE_RELPATH, tmp / SOURCE_RELPATH)
    (tmp / GUIDE_RELPATH).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / GUIDE_RELPATH, tmp / GUIDE_RELPATH)
    write_tree(tmp)


def cmd_selftest() -> int:
    with tempfile.TemporaryDirectory(prefix="gen-constants-selftest-") as tmpdir:
        tmp = Path(tmpdir)
        _copy_tree_for_selftest(tmp)

        baseline = check_tree(tmp)
        if baseline:
            print("gen-constants --selftest: FAIL — a freshly written, consistent tree should pass --check:",
                  file=sys.stderr)
            for p in baseline:
                print(f"  {p}", file=sys.stderr)
            return 1

        # 1. Flip a scalar value in the source without regenerating outputs:
        # --check must now fail, because the outputs no longer match a fresh
        # regeneration of the (now-different) source.
        data = json.loads((tmp / SOURCE_RELPATH).read_text(encoding="utf-8"))
        data["retry"]["attempts"] = data["retry"]["attempts"] + 1
        (tmp / SOURCE_RELPATH).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        drifted = check_tree(tmp)
        if not drifted:
            print("gen-constants --selftest: FAIL — flipping retry.attempts in the source was not caught",
                  file=sys.stderr)
            return 1

        # Restore and re-confirm the baseline, so the next case starts clean.
        _copy_tree_for_selftest(tmp)
        if check_tree(tmp):
            print("gen-constants --selftest: FAIL — could not restore a clean baseline", file=sys.stderr)
            return 1

        # 2. A test key id smuggled into trust.release_keys must be refused
        # by validate(), independent of the schema.
        data = json.loads((tmp / SOURCE_RELPATH).read_text(encoding="utf-8"))
        data["trust"]["release_keys"].append(
            {"keyid": data["test_keys"][0]["keyid"], "ed25519_hex": data["test_keys"][0]["ed25519_hex"]}
        )
        bad_problems = validate(data)
        if not bad_problems:
            print(
                "gen-constants --selftest: FAIL — a test key id in trust.release_keys was not caught",
                file=sys.stderr,
            )
            return 1

        # 3. A test key whose trusted_by_default is not false must be refused.
        data2 = json.loads((tmp / SOURCE_RELPATH).read_text(encoding="utf-8"))
        data2["test_keys"][0]["trusted_by_default"] = True
        bad_problems2 = validate(data2)
        if not bad_problems2:
            print(
                "gen-constants --selftest: FAIL — trusted_by_default: true on a test key was not caught",
                file=sys.stderr,
            )
            return 1

    print("gen-constants --selftest: ok — a clean tree passes, drift in the source is caught, and a "
          "test key cannot reach the default trust list")
    return 0


# ------------------------------------------------------------------ CLI


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="regenerate every output in place")
    group.add_argument("--check", action="store_true", help="regenerate into memory and diff against disk")
    group.add_argument("--selftest", action="store_true", help="prove --check catches drift, on a throwaway copy")
    args = ap.parse_args(argv)

    if args.selftest:
        return cmd_selftest()

    if args.write:
        write_tree(ROOT)
        print("gen-constants: wrote go/python/ts/rust constants and the docs/guides/fetch-v1.md block")
        return 0

    # --check
    problems = check_tree(ROOT)
    if problems:
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print(f"gen-constants: the tree disagrees with a fresh regeneration ({len(problems)}); run --write",
              file=sys.stderr)
        return 1
    print("gen-constants: ok, every generated output and the guide's block match spec/fetch-v1/constants.json")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
