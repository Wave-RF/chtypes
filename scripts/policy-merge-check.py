#!/usr/bin/env python3
"""policy-merge-check.py — may this pull request enqueue itself to merge?

    scripts/policy-merge-check.py --selftest          every refusal below fires, and an all-good input passes
    scripts/policy-merge-check.py --check-ci-names     REQUIRED_CHECKS agrees with ci.yml's blocking jobs (no network)
    scripts/policy-merge-check.py --check-guide        CONTRIBUTING.md's protected-glob block agrees with PROTECTED_GLOBS (no network)
    scripts/policy-merge-check.py --check-carve-out    the security carve-out agrees with the tree's verification code (no network)
    scripts/policy-merge-check.py --print-protected    PROTECTED_GLOBS, one per line
    scripts/policy-merge-check.py gate    --repo OWNER/NAME --run-head-sha SHA --run-head-repo OWNER/NAME
    scripts/policy-merge-check.py gate    --repo OWNER/NAME --pr N
    scripts/policy-merge-check.py enqueue --repo OWNER/NAME --pr N --head-sha SHA
                                          --regenerated docs/support.md=FILE
                                          [--run-head-repo OWNER/NAME] [--dry-run]

Run by .github/workflows/policy-merge.yml, whose header says why the workflow
exists and what it may touch; read that first.

This file, and the workflow that runs it, used to be named regen-automerge —
a pull request that only regenerated docs/support.md merged itself once `ci`
passed. Issue #280 generalized it: ANY pull request merges itself once every
condition below holds, not just a docs/support.md regeneration. The
docs/support.md guard survives as condition 6, the one case this script still
compares a file's actual bytes rather than just judging its path.

A pull request no longer merges directly. `enqueue` adds it to main's GitHub
merge queue (the `enqueuePullRequest` GraphQL mutation) once every condition
holds; the queue itself tests the actual combination against main's current
tip and performs the merge later, asynchronously. See ENQUEUE, NOT MERGE,
below for why this is what makes "the tree that lands is the tree CI tested"
true, which a direct merge against a branch whose protection does not
require being up to date with main could not guarantee on its own.

WHY THIS EXISTS. Merges are decided by policy; an agent is needed only when
something cannot be decided deterministically — a check fails, an issue needs
filing, or a pull request touches a file whose correctness this tree cannot
verify by rule alone (chtypes#280). Everything else is a mechanical decision a
machine can make from data GitHub already has: the required checks' own
verdicts, the diff's file list, and — for docs/support.md alone — a
byte-for-byte regeneration.

================================================================================
THE SEVEN CONDITIONS
================================================================================

A pull request enqueues itself when ALL of these hold. Evaluated in this
order; the first that fails is the one reported. A refusal is not an error:
the pull request is left for a human and the exit status is 0.

  fork       (1) The head is a branch of THIS repository: the head repository
                 the ci run reports AND the pull request's own head repository.
                 A fork's head never auto-enqueues — kept unconditionally from
                 regen-automerge.
  pull-request   Exactly one open pull request against main carries the
                 commit, and it is not a draft. Draft is the hold switch:
                 mark a pull request draft and this stops it from being
                 enqueued.
  stale          The pull request's head is still the commit ci judged. A push
                 after that run means the run judged something that is no
                 longer what would be enqueued.
  protected  (5) No file the pull request touches — its current path, or for
                 a rename its OLD path too; a deletion counts by its one path
                 — matches an unconditional glob in PROTECTED_GLOBS (below);
                 and for every binding whose SOURCE TREE it touches (the
                 four conditional entries, chtypes#285 §1), that binding's
                 api-surface verdict on the judged head is `changed=false`
                 — read from the `api-surface` job's own log
                 (gather_api_verdicts; a missing, unparseable or errored
                 verdict, or a job that did not complete with success,
                 counts as changed). This is also
                 what makes `checks` (next) mean anything: a pull_request run
                 of `ci` uses the pull request's own copy of `.github/` and
                 `scripts/`, which a pull request that edited either could
                 make pass trivially — "a pull request must not be able to
                 weaken its own judge" (chtypes#280, the lead's scope
                 addition). Evaluating this before `checks` means a pull
                 request that edited its own judge is refused before its
                 (possibly fabricated) green checks are ever trusted.
  checks     (2) Every context in REQUIRED_CHECKS has a GitHub Actions check
                 run on the head, and every such run is `completed` with
                 conclusion `success`. Checks outside that list are ignored:
                 ci.yml's `divergences` job can be red by design.
  test-counts    ONLY when the pull request touches a test or fixture path
                 (chtypes#285 §1b, is_test_or_fixture_path has the exact
                 rule): no suite's executed-test count, and no golden-case
                 count, fell below main's last green `ci` push run — read
                 from each job's own log (see gather_test_counts and its own
                 comment for why the log, not the check run's output, is
                 what a read-only token can actually read). A missing or
                 unparseable count on either side is a drop, never
                 "unchanged". Evaluated after `checks` so an already-red
                 suite is reported as `checks`, never masked as a
                 test-count drop; a pull request touching no test or
                 fixture path skips this at zero API cost.
  mergeable      GitHub has not already reported a merge conflict
                 (`mergeable: false`). An enqueue known to fail is not tried.
  review     (4) No reviewer's latest review requests changes, and no review
                 conversation exists. Branch protection requires every
                 conversation resolved, and the REST API cannot tell a
                 resolved thread from an open one, so any review comment
                 leaves the pull request for a human.
  bytes      (6) ONLY when the pull request touches docs/support.md: it was
                 MODIFIED (not added, deleted or renamed), and main's own
                 scripts/support-matrix.sh, run against the live index,
                 reproduces the head's copy byte for byte. A pull request
                 that does not touch docs/support.md skips this condition.
                 This is necessarily a PRE-ENQUEUE check only — see ENQUEUE,
                 NOT MERGE for why the old post-merge re-verification (the
                 merge commit's docs/support.md still equals the regeneration)
                 cannot run anymore.

`gate` checks everything except the byte HALF of `bytes`, which needs the
regeneration; the workflow regenerates only when the gate passes, so an
ordinary pull request costs a handful of API reads. `test-counts` has no such
split — gather_test_counts reads every job log it needs (never a head file)
up front, so `gate` checks it fully, at the API cost of up to five job-log
reads per side, and only for a pull request that touches a test or fixture
path. The api-surface verdicts are the same: one job-log read, only for a
pull request that touches binding source and nothing unconditionally
protected. `enqueue` reads every fact again, checks all of them including the
byte comparison, and enqueues with `expectedHeadOid=<head>` so GitHub itself
refuses if the head moved in between (reported as `stale`).

================================================================================
ENQUEUE, NOT MERGE — WHY THE QUEUE IS WHAT CLOSES THE RACE
================================================================================

`main`'s branch protection has `required_status_checks.strict: false`
(measured directly: `gh api repos/Wave-RF/chtypes/branches/main/protection/
required_status_checks`), so a pull request's required checks are judged
against ITS OWN base at whatever commit it branched from — GitHub does not
require the head to be up to date with main before it may merge. A pull
request merged directly (the old `PUT .../merge` this file used before Eric
approved the merge queue) could therefore land a combination `ci` never ran:
the PR's changes atop an older main, not atop the main that will actually
receive them. A direct merge made with the built-in token also starts no
`push` run of `ci` on main afterward (documented GitHub behavior), so nothing
would have caught it either.

The merge queue closes this structurally, not by this script re-reading
main's tip and hoping nothing moves in between: `ci.yml` also triggers on
`merge_group` (checks_requested) now, so the queue's own candidate combination
— the PR's changes merged with main's CURRENT tip — is what every required
check actually runs against, and the queue merges only once those checks
pass on THAT combination. Two pull requests enqueued close together are
serialized by the queue itself, each tested against the tip the one ahead of
it just produced. This is a stronger guarantee than this file could construct
on its own with a compare-API re-check: it is enforced by GitHub for every
entry in the queue, not by this workflow's timing.

This script's own part is the `expectedHeadOid` on the `enqueuePullRequest`
mutation (a pull request's HEAD still moving between the gate's read and the
enqueue call is this file's problem, same as it always was — the queue does
not protect against that, only against the BASE moving). `expectedHeadOid` is
the same role `sha=` played on the old REST merge call; a mismatch there is
GitHub's to refuse, not this script's to detect in the mutation's error text
— see cmd_enqueue's own comment for why this file does not attempt to decode
that error into a friendly `stale` refusal (unverified: the merge queue does
not exist on this repository yet to observe its exact error shape against).
The primary, verified defense against a moved head remains `decide()`'s
`stale` condition, checked immediately before the mutation is ever reached.

A PRE-ENQUEUE-ONLY byte check (condition 6, above) is the other consequence:
the old code re-read the merge commit after a successful `PUT .../merge` and
failed the run if it did not carry the regeneration, because a three-way
merge's result is not guaranteed to equal the head's raw diff even when the
head matched byte for byte. That re-read needed a `merge_sha` returned
synchronously by the merge call; `enqueuePullRequest` returns a queue entry,
not a merge result, and the actual merge happens later, asynchronously, on
whatever commit the queue eventually lands. This file therefore trusts the
pre-enqueue byte comparison against the head's own diff and does not attempt
a post-hoc re-verification — a real gap versus the old guarantee, accepted
because there is no synchronous moment left at which to make it.

BUILT-IN TOKEN EVENTS START NO WORKFLOW. An entry enqueued with the
workflow's built-in token gets no merge_group run of `ci`: GitHub starts no
workflow for an event that token causes, so the entry waits at
AWAITING_CHECKS until the queue's timeout (measured on #289; the same entry
re-enqueued with a user token got its run 13 seconds later; chtypes#291).
So `enqueue` here only JUDGES and hands the judged node id and head sha on as
step outputs (handoff_outputs), with read-only permissions. The workflow's
`enqueue` job, the only holder of the merge-bot App's token, then runs
`enqueue-as-bot`, which validates those two values (bot_args_problem) and
makes the one enqueuePullRequest call. An entry the App enqueues does get its
merge_group run.

MANUAL MERGES GO THROUGH THE QUEUE TOO. A protected-class pull request (left
for a human by condition 5) is enqueued by hand with the same
`enqueuePullRequest` mutation this file uses, pinned to the reviewed head
(CONTRIBUTING.md has the command). `gh pr merge` does not work here: with a
queue required it falls back to enabling auto-merge, which this repository
does not allow (measured). Draft status is still the hold switch either way.

================================================================================
PROTECTED_GLOBS
================================================================================

The paths a pull request may not touch and still merge itself — printed by
`--print-protected` and mirrored in CONTRIBUTING.md's generated block, which
`--check-guide` keeps from drifting out of sync with the constant below (see
guide_problems()). Decided by the chtypes lead (chtypes#280), conservative by
design: over-protect rather than under-protect.

  .github/**            the workflows define and run the required checks
  scripts/**             every gate a required check runs, this checker
                          itself, and scripts/fetch.sh, which verifies
                          release signatures
  include/**              the frozen C ABI header
  go/**/*.go (except *_test.go), python/src/**, ts/src/**, rust/src/**
                          each binding's source tree — CONDITIONAL since
                          chtypes#285 §1. No path rule can read an export
                          list (Go has none; Python and Rust export by
                          visibility), so the export list is COMPUTED instead:
                          ci.yml's non-blocking `api-surface` job runs one
                          pinned tool per binding (scripts/api-surface.py's
                          header names them, and why each is trusted not to
                          run anything of the head), merge base against
                          head, and a touch of a binding's source is
                          protected only while that binding's verdict is not
                          `changed=false`. An exported ADDITION is a change.
  the SECURITY CARVE-OUT  inside those trees but UNCONDITIONAL, whatever the
                          verdict: go/chtypes/{fetch_sign,fetch,registry_path,
                          multiversion}.go, python/src/chtypes/{_ed25519,
                          fetch,_manifest,registry}.py, ts/src/{fetch,
                          registry}.ts, rust/src/fetch/**, rust/src/digest.rs,
                          rust/src/registry.rs — the fetch chain (the ed25519
                          signature, each tarball's sha256, the lock pin, the
                          trusted keys and the allow-unsigned switch) and the
                          load-time checks (library_bytes on every load, the
                          opt-in sha256 re-hash). Derived by VERIFICATION_NEEDLES
                          (the primitives and trust anchors, never names a
                          re-export also carries), and `--check-carve-out`,
                          run by the required `abi` job, derives it again
                          from the tree on every pull request.
  rust/build.rs           not source by path, but code cargo RUNS on every
                          build — a consumer's, and the api-surface job's,
                          whose log the conditional entries above trust.
                          None exists today.
  go/go.mod, go/go.sum, python/pyproject.toml, python/uv.lock,
  ts/package.json, ts/pnpm-lock.yaml, ts/pnpm-workspace.yaml,
  rust/Cargo.toml, rust/Cargo.lock, RELEASING.md
                          release inputs and dependencies. ts/pnpm-workspace.yaml
                          is not in issue #280's own list — found via
                          `git ls-files` per this checker's own brief and
                          added, same conservative reasoning; go/go.sum does
                          not exist in the tree yet (the Go module has no
                          external dependency today) and is protected in
                          advance of needing one.
  .markdownlint.json, .markdownlint-cli2.jsonc, dprint.json
                          configure the required `prose` job's tools
                          (markdownlint-cli2, dprint); a PR could otherwise
                          disable the rule that would have caught it
  go/.golangci.yml        configures the required `lint-go` job's rules
  ts/biome.json           configures the required `lint-ts` job's rules
  ts/tsconfig.json, ts/tsconfig.test.json
                          configure the required `ts` job's build and
                          typecheck steps (`tsc -p tsconfig.json` /
                          `tsc -p tsconfig.test.json` in ts/package.json)
  tests/parity/manifest.json
                          the cross-binding parity contract: each of the
                          required `go`/`python`/`ts`/`rust` jobs' own parity
                          test (parity_test.go, test_parity.py,
                          parity.test.ts, parity.rs) reads and enforces it —
                          declares what each binding must support, the same
                          threat model as a config file that configures a
                          check's rules
  docs/divergences.json  the machine-checkable register of known divergences
                          the (non-required) `divergences` job reads — an
                          ALLOWLIST that excuses a result, the same threat
                          model as a lint exemption, protected even though
                          its own job is non-blocking

Found by a sweep of every tool a required job runs (`ci.yml`'s `run:` lines
for `--config`/`-c` flags and each tool's own name, each tool's default
config-file names checked against `git ls-files`): no default-name config
file exists for cargo clippy/fmt (no clippy.toml or rustfmt.toml — rust's
lint config lives entirely in the already-protected rust/Cargo.toml
`[lints]`), for shellcheck (no .shellcheckrc), or for actionlint (no config
file at all — both used inside the already-protected .github/**-covered
`lint-actions` job with no separate config); vitest (the `ts` job's test
runner, `pnpm test` → `vitest run`) has no config file in this tree either.
`.nvmrc` was considered and DROPPED: every `actions/setup-node` step in this
repository's workflows hardcodes `node-version: 22`; none reads
`node-version-file`, so nothing here is sensitive to its contents.

scripts/fetch.sh, the shell fetch that also verifies release signatures, is
inside scripts/**. scripts/lint-public.sh's one exemption (the literal `runner` segment in its
LOCAL_PATH_PLACEHOLDERS allowlist) lives inside the script itself, so
scripts/** already covers it; there is no exemption file outside that glob.

Tests, fixtures, examples, docs (docs/support.md alone excepted, as condition
6 above) and CHANGELOGs are deliberately NOT protected: the protected gates
(`checks`) judge them, so nothing about loosening what they judge weakens the
judge itself.

================================================================================
THE PWN-REQUEST RULE — THE ONE THING THIS FILE MUST NEVER BREAK
================================================================================

Unchanged from regen-automerge, and now backstopped twice over: once by this
script never reading a head file from disk, and once by `protected` refusing
before `checks` is ever trusted. `workflow_run` runs this file from the
DEFAULT BRANCH with a token that can write to this repository, after a `ci`
run that anyone can start by opening a pull request from a fork. So nothing
from a pull request's head may run here. This script never reads a head file
from disk and never executes, sources, imports or parses anything of the
head: a head file's bytes (docs/support.md alone, and only when it is part of
the diff) arrive from the contents API and are only compared with other
bytes. The workflow checks out main, and main's code is all that runs.

`test-counts` (chtypes#285 §1b) reads more than one file's bytes — up to five
job LOGS per side — but the posture is identical: `gather_test_counts` reads
each job's console output through the Actions API (`GET
repos/{repo}/actions/jobs/{id}/logs`), a passive read of text GitHub's own
runner already produced, and every line this file cares about is matched by
regex (`parse_suite_counts`/`parse_golden_count`) against fixed integers,
never executed, sourced, imported or otherwise interpreted as code. The job
whose log is read is itself one `.github/**`/`scripts/**` cannot touch
without tripping `protected` first (evaluated before `checks`, and
`test-counts` sits after `checks`), so by the time a log is ever read, the
scripts that produced it are provably main's own.

The binding-source half of `protected` (chtypes#285 §1) reads one more log
the same way — the `api-surface` job's — and only after the unconditional
half has refused any pull request that touched `.github/**` or `scripts/**`,
so the job that wrote it ran main's ci.yml and main's scripts/api-surface.py.
Each verdict line is matched by a fixed regex at the start of a log line and
must name the judged head's own sha, which no commit can contain in
advance; scripts/api-surface.py's header says why nothing of the head runs in
that job at all.

================================================================================
REQUIRED_CHECKS, AND WHY IT IS PINNED HERE
================================================================================

Unchanged from regen-automerge. Branch protection's list of required contexts
cannot be read with the workflow's built-in token (that endpoint needs admin
rights), so it is pinned below, once. `--check-ci-names` derives, from
.github/workflows/ci.yml itself, the check-run name of every job that does NOT
carry job-level `continue-on-error: true`, and fails if that set and
REQUIRED_CHECKS differ in either direction. ci.yml marks its deliberately
non-blocking jobs exactly that way, and every other job in it is meant to be
required. Branch protection still has the final word at merge time; this list
only stops the workflow from attempting an enqueue it already knows would fail.

Exit status: 0 for an enqueue, a dry run or a refusal; 1 for a failed
selftest, or a REQUIRED_CHECKS or guide disagreement; 2 for an API failure or
anything else unexpected, so it can never be mistaken for a quiet refusal.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CI_YML = os.path.join(ROOT, ".github", "workflows", "ci.yml")
POLICY_YML = os.path.join(ROOT, ".github", "workflows", "policy-merge.yml")
# The only permissions the merge-bot token may be minted with. The App itself
# carries more (the artifact producer's branch updates need Workflows); the
# SDK's mint must never ask for them.
MINT_PERMISSIONS = {"permission-contents": "write", "permission-pull-requests": "write"}
GUIDE_PATH = os.path.join(ROOT, "CONTRIBUTING.md")

BASE_BRANCH = "main"

# The generated files this script still checks byte for byte when a pull
# request touches them (condition 6). docs/support.md is the only one today;
# adding a second is a deliberate act with three parts: the workflow must
# regenerate it too and pass it as --regenerated PATH=FILE, `merge` refuses
# (exit 2) until every entry here has a regenerated copy, and decide()'s
# `bytes` condition needs a matching lookup — it is not derived from this
# tuple automatically, on purpose, because each generated file's status rule
# could one day differ.
GUARDED_FILES = ("docs/support.md",)


@dataclass(frozen=True)
class ProtectedGlob:
    """One entry of PROTECTED_GLOBS. `pattern` is matched by _match_protected
    (see its docstring for the restricted glob shapes supported); `reason` is
    the one-line justification shown in refusals, --print-protected and the
    CONTRIBUTING.md block --check-guide verifies. `except_suffix`, when set,
    exempts a path the pattern would otherwise match if its filename ends
    with it (go/**/*.go except *_test.go: a test is not part of the public
    API surface the glob exists to protect).

    `binding`, when set, makes the entry CONDITIONAL (chtypes#285 §1): it is
    that binding's source tree, and a touch of it is protected only while the
    `api-surface` verdict for that binding on the judged head is not
    `changed=false` (api_surface_problems). Every entry without a `binding`
    is protected unconditionally. `verification` marks an unconditional
    entry as part of the SECURITY CARVE-OUT — signature and checksum
    verification code and the embedded release key, inside a binding's
    source tree but protected whatever its API verdict says;
    `--check-carve-out` keeps those entries in step with the tree."""

    pattern: str
    reason: str
    except_suffix: str | None = None
    binding: str | None = None
    verification: bool = False

    def label(self) -> str:
        base = f"{self.pattern} (except *{self.except_suffix})" if self.except_suffix else self.pattern
        if self.binding:
            return f"{base} [unless the api-surface verdict for {self.binding} is changed=false]"
        return base

    def guide_bullet(self) -> str:
        exc = f" (except `*{self.except_suffix}`)" if self.except_suffix else ""
        if self.binding:
            return (f"- `{self.pattern}`{exc} — {self.reason}; protected only while the `api-surface` verdict for "
                    f"`{self.binding}` on the head is not `changed=false`")
        if self.verification:
            return f"- `{self.pattern}`{exc} — {self.reason}; security carve-out, protected whatever the API verdict"
        return f"- `{self.pattern}`{exc} — {self.reason}"


# Decided by the chtypes lead (chtypes#280): conservative, over-protect rather
# than under-protect. ONE constant — --print-protected, CONTRIBUTING.md's
# generated block (--check-guide) and every match in `decide` all read this;
# nothing is hand-copied anywhere else.
PROTECTED_GLOBS: tuple[ProtectedGlob, ...] = (
    ProtectedGlob(".github/**",
                  "the workflows define and run the required checks; a pull_request run of ci uses the pull "
                  "request's own copy"),
    ProtectedGlob("scripts/**",
                  "every gate a required check runs, this checker itself, and scripts/fetch.sh, which verifies "
                  "release signatures"),
    ProtectedGlob("include/**", "the frozen C ABI header"),
    # Each binding's source tree, CONDITIONAL since chtypes#285 §1: a touch
    # is refused only while the `api-surface` job's verdict for that binding
    # on the judged head is not `changed=false` (an exported addition, a
    # removal or a signature change, or no readable verdict at all).
    ProtectedGlob("go/**/*.go", "go binding source (the exported API is computed by apidiff)",
                  except_suffix="_test.go", binding="go"),
    ProtectedGlob("python/src/**", "python binding source (the public API is computed from griffe's model)",
                  binding="python"),
    ProtectedGlob("ts/src/**", "ts binding source (the exported API is computed by api-extractor)", binding="ts"),
    ProtectedGlob("rust/src/**", "rust binding source (the public API is computed by cargo-public-api)",
                  binding="rust"),
    # THE SECURITY CARVE-OUT (chtypes#285 §1): signature and checksum
    # verification code, the trust policy and the embedded release key sit
    # inside the binding-source globs above but stay protected
    # UNCONDITIONALLY — an unchanged API says nothing about whether a
    # verification still verifies. Derived by grepping each binding for its
    # verification primitives and trust anchors (VERIFICATION_NEEDLES, below),
    # and `--check-carve-out` fails when a file the needles hit is not covered
    # here, or an entry here no longer exists — so this list cannot silently
    # go stale when code moves.
    ProtectedGlob("go/chtypes/fetch_sign.go",
                  "the embedded release public key and the ed25519 signature check", verification=True),
    ProtectedGlob("go/chtypes/fetch.go",
                  "the fetch chain: the signature-check call, every tarball's sha256 against SHA256SUMS, the lock "
                  "pin, the trusted-keys and allow-unsigned options", verification=True),
    ProtectedGlob("go/chtypes/registry_path.go",
                  "names the CHTYPES_TRUSTED_KEYS and CHTYPES_ALLOW_UNSIGNED variables the trust policy reads",
                  verification=True),
    ProtectedGlob("go/chtypes/multiversion.go",
                  "load-time verification: the library_bytes size check on every load and the "
                  "WithVerifyChecksums sha256 re-hash", verification=True),
    ProtectedGlob("python/src/chtypes/_ed25519.py", "the ed25519 signature check itself", verification=True),
    ProtectedGlob("python/src/chtypes/fetch.py",
                  "the embedded release public key, the trust policy, and the fetch chain's signature and sha256 "
                  "checks", verification=True),
    ProtectedGlob("python/src/chtypes/_manifest.py",
                  "load-time verification: check_library_bytes and verify_library's sha256 re-hash",
                  verification=True),
    ProtectedGlob("python/src/chtypes/registry.py",
                  "calls the load-time verification on every load (verify_hashes)", verification=True),
    ProtectedGlob("ts/src/fetch.ts",
                  "the embedded release public keys, the trust policy, and the fetch chain's signature and sha256 "
                  "checks", verification=True),
    ProtectedGlob("ts/src/registry.ts",
                  "load-time verification: checkLibraryBytes on every load and the verifyChecksums sha256 re-hash",
                  verification=True),
    ProtectedGlob("rust/src/fetch/**",
                  "the embedded release public key, the trust policy, the signature check, the sha256 checks and "
                  "the lock pin", verification=True),
    ProtectedGlob("rust/src/digest.rs", "the sha256 helper every checksum check hashes with", verification=True),
    ProtectedGlob("rust/src/registry.rs",
                  "load-time verification: the library_bytes size check and the verify_checksums sha256 re-hash",
                  verification=True),
    # Not binding source by path, but code: cargo finds and RUNS a build
    # script at the crate root on every build — on every consumer's machine,
    # and inside the api-surface job, whose log the binding-source class
    # above trusts. None exists today; adding one waits for a human.
    ProtectedGlob("rust/build.rs",
                  "a build script cargo runs on every build, consumers' and the api-surface job's alike (none "
                  "exists today)"),
    ProtectedGlob("go/go.mod", "a release input: the Go module's own manifest"),
    ProtectedGlob("go/go.sum",
                  "a release input: the Go module's dependency lockfile (not yet present in this tree; "
                  "protected in advance of needing one)"),
    ProtectedGlob("python/pyproject.toml", "a release input: the Python package manifest"),
    ProtectedGlob("python/uv.lock", "a release input: the Python dependency lockfile"),
    ProtectedGlob("ts/package.json", "a release input: the npm package manifest"),
    ProtectedGlob("ts/pnpm-lock.yaml", "a release input: the npm dependency lockfile"),
    ProtectedGlob("ts/pnpm-workspace.yaml",
                  "a release input: the pnpm workspace manifest — not in issue #280's own list, found via "
                  "`git ls-files` per this checker's own brief"),
    ProtectedGlob("rust/Cargo.toml", "a release input: the crate manifest"),
    ProtectedGlob("rust/Cargo.lock", "a release input: the crate dependency lockfile"),
    ProtectedGlob("RELEASING.md", "the release procedure itself"),
    # Configures a required check's own tool, found by a sweep of every
    # ci.yml run: line for --config/-c flags and each tool's name, cross-
    # checked against that tool's default config-file names in git ls-files
    # (see the module docstring's PROTECTED_GLOBS section for what the sweep
    # ruled OUT — no clippy.toml, rustfmt.toml, .shellcheckrc, actionlint
    # config or vitest config exist in this tree, and .nvmrc is read by no
    # workflow here).
    ProtectedGlob(".markdownlint.json", "configures the required prose job's markdownlint rules"),
    ProtectedGlob(".markdownlint-cli2.jsonc", "configures the required prose job's markdownlint-cli2 file selection"),
    ProtectedGlob("dprint.json", "configures the required prose job's dprint formatting check"),
    ProtectedGlob("go/.golangci.yml", "configures the required lint-go job's golangci-lint rules"),
    ProtectedGlob("ts/biome.json", "configures the required lint-ts job's biome rules"),
    ProtectedGlob("ts/tsconfig.json", "configures the required ts job's build step (tsc -p tsconfig.json)"),
    ProtectedGlob("ts/tsconfig.test.json",
                  "configures the required ts job's typecheck step (tsc -p tsconfig.test.json)"),
    ProtectedGlob("tests/parity/manifest.json",
                  "the cross-binding parity contract each of the required go/python/ts/rust jobs' own parity "
                  "test reads and enforces — declares what every binding must support"),
    # Excuses a result rather than configuring a tool — same threat model as
    # a lint exemption, protected even though its own job (divergences) is
    # non-blocking.
    ProtectedGlob("docs/divergences.json",
                  "the machine-checkable register of known divergences the divergences job reads; an allowlist "
                  "that excuses a result"),
)

# --check-ci-names on this file's own PROTECTED_GLOBS: the two wildcard
# shapes PROTECTED_GLOBS actually uses, each anchored to the whole pattern.
_TRAILING_DOUBLE_STAR = re.compile(r"^(?P<dir>[\w./-]+)/\*\*$")
_MIDDLE_DOUBLE_STAR = re.compile(r"^(?P<dir>[\w./-]+)/\*\*/\*(?P<ext>\.[\w.]+)$")


def _match_protected(pattern: str, path: str) -> bool:
    """Whether `path` (a repository-relative POSIX path, no leading '/') is
    covered by one PROTECTED_GLOBS pattern. Deliberately supports only the
    shapes PROTECTED_GLOBS actually uses, and raises rather than guess at
    anything else — the same discipline _yaml_scalar below applies to
    ci.yml's text:
      - no '*' at all: exact match.
      - 'DIR/**': DIR/ is a strict prefix of `path` (matches everything
        under DIR, at any depth; never DIR itself, which cannot appear as a
        file path anyway).
      - 'DIR/**/*.EXT': DIR/ is a prefix and .EXT a suffix of `path`, with
        any number of path segments — including zero — in between. This is
        what makes go/**/*.go match go/x.go as well as go/a/b/x.go, and
        match neither gofoo/x.go (no '/' after the 'go' segment) nor
        go/x.txt (wrong suffix)."""
    if "*" not in pattern:
        return path == pattern
    m = _TRAILING_DOUBLE_STAR.match(pattern)
    if m:
        prefix = m["dir"] + "/"
        return path.startswith(prefix) and len(path) > len(prefix)
    m = _MIDDLE_DOUBLE_STAR.match(pattern)
    if m:
        prefix, suffix = m["dir"] + "/", m["ext"]
        return path.startswith(prefix) and path.endswith(suffix)
    raise ValueError(f"unsupported glob shape in PROTECTED_GLOBS: {pattern!r}; extend _match_protected first")


def _covers(g: ProtectedGlob, path: str) -> bool:
    return _match_protected(g.pattern, path) and not (g.except_suffix and path.endswith(g.except_suffix))


def is_protected(path: str) -> ProtectedGlob | None:
    """The first UNCONDITIONAL PROTECTED_GLOBS entry that covers `path`, or
    None. A binding-source entry is never returned here: whether a touch of
    one is protected depends on that binding's api-surface verdict, which
    binding_of() and api_surface_problems() decide."""
    for g in PROTECTED_GLOBS:
        if g.binding is None and _covers(g, path):
            return g
    return None


def binding_of(path: str) -> ProtectedGlob | None:
    """The binding-source entry (a PROTECTED_GLOBS entry with `binding` set)
    that covers `path`, or None. The ONE mapping from a path to a binding:
    scripts/api-surface.py imports this function to decide which bindings a
    diff touches, so the job that writes the verdicts and the checker that
    reads them cannot disagree about which binding a file belongs to."""
    for g in PROTECTED_GLOBS:
        if g.binding is not None and _covers(g, path):
            return g
    return None


# Every binding with a source-tree entry, in PROTECTED_GLOBS order.
BINDINGS: tuple[str, ...] = tuple(g.binding for g in PROTECTED_GLOBS if g.binding is not None)


def _touched_paths(files: list[dict]):
    """Every path a pull request's file list touches: each file's current
    path (`filename`) and, for a rename, its OLD path (`previous_filename`)
    too — moving a file out from under a glob, or into one, is a change to
    that glob's class either way. A deletion counts by its one path, the one
    that no longer exists."""
    for f in files:
        for path in (f.get("filename"), f.get("previous_filename")):
            if path:
                yield path


def first_protected_touch(files: list[dict]) -> tuple[str, ProtectedGlob] | None:
    """The first (path, glob) a pull request's file list touches that an
    UNCONDITIONAL PROTECTED_GLOBS entry covers (rename and deletion handling:
    _touched_paths)."""
    for path in _touched_paths(files):
        hit = is_protected(path)
        if hit is not None:
            return path, hit
    return None


def touched_bindings(files: list[dict]) -> dict[str, str]:
    """binding -> the first touched path of that binding's source tree, for
    every binding whose source the pull request touches (rename and deletion
    handling: _touched_paths)."""
    touched: dict[str, str] = {}
    for path in _touched_paths(files):
        g = binding_of(path)
        if g is not None and g.binding not in touched:
            touched[g.binding] = path
    return touched


# ------------------------------------------------------- test/fixture paths
#
# chtypes#285 §1b: which pull requests the `test-counts` condition even looks
# at. Derived from a sweep of this tree's own layout (`git ls-files` for
# every path segment named tests?/fixtures?, plus *_test.go/.test.ts), not
# guessed — the same discipline PROTECTED_GLOBS' own sweep used.

_TEST_PATH_PREFIXES = ("python/tests/", "ts/test/", "rust/tests/", "tests/fixtures/")


def is_test_or_fixture_path(path: str) -> bool:
    """Whether `path` is a test-or-fixture input the `test-counts` condition
    (chtypes#285 §1b) cares about:
      - `go/**/*_test.go` — the exact *_test.go exception PROTECTED_GLOBS'
        own `go/**/*.go` entry already carves out of the protected API
        surface (go/cmd/chtypes/main_test.go included: it is a plain suffix
        check, not anchored to go/chtypes/);
      - `python/tests/**`, `ts/test/**`, `rust/tests/**` — each binding's own
        suite;
      - `tests/fixtures/**` — the fetch fixtures every binding's fetch suite
        reads (docs/guides/fetch.md §9). The fixture-reading TEST files
        themselves (go/chtypes/fetch_fixtures_test.go,
        ts/test/fixture-revision.ts, and their python/rust counterparts)
        already match one of the rules above; this entry is for the fixture
        DATA under tests/fixtures/ itself.
    `tests/parity/manifest.json` is deliberately NOT one of these: it is
    already a PROTECTED_GLOBS entry (the cross-binding parity contract, a
    threat this condition does not need to duplicate) — a pull request may
    not touch it and merge itself at all, so `test-counts` never gets a
    chance to look at it either way."""
    if path.startswith("go/") and path.endswith("_test.go"):
        return True
    return path.startswith(_TEST_PATH_PREFIXES)


def touches_test_or_fixture_path(files: list[dict]) -> bool:
    """Whether ANY file the pull request touches — its current path, or for a
    rename its old path too — is a test or fixture path (same rename/deletion
    handling as first_protected_touch: a file moved OUT of a test directory,
    or a deleted test file, can drop a suite's count same as a weakened one,
    so both paths of a rename count, and a deletion counts by its one path)."""
    for f in files:
        for path in (f.get("filename"), f.get("previous_filename")):
            if path and is_test_or_fixture_path(path):
                return True
    return False


# ---------------------------------------------------- api-surface (chtypes#285 §1)
#
# ci.yml's non-blocking `api-surface` job (scripts/api-surface.py) prints one
# verdict line per touched binding into its own job LOG, and this reads that
# log the way `test-counts` reads its counts: a check run's output fields are
# null for an Actions job (measured; see gather_test_counts), so the console
# log is the one channel a read-only token can read. The job stays green for
# any genuine verdict, so a deliberate API change is not shown red; this
# file, not the job's colour, is what turns `changed=true` into "wait for a
# human".

API_SURFACE_CHECK = "api-surface — every touched binding's exported API, merge base against head (report only)"
API_SURFACE_PREFIX = "chtypes-api-surface"


def api_surface_line(binding: str, head_sha: str, changed: str) -> str:
    """The one verdict line scripts/api-surface.py prints per touched binding
    — defined here, ONCE, and imported by that script, so the writer and
    parse_api_surface (the reader) cannot drift apart. `changed` is `true`,
    `false` or `error`; only `false` ever unprotects anything."""
    return f"{API_SURFACE_PREFIX} binding={binding} head={head_sha} changed={changed}"


# A verdict starts its log line: GitHub prefixes every line with its own ISO
# timestamp (and the first line of a log with a byte-order mark), so the
# optional prefix is exactly that and nothing else. A tool's output that
# merely CONTAINS the text — a compiler warning quoting it, say — is not a
# verdict.
_API_VERDICT_RE = re.compile(r"^\ufeff?(?:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z )?"
                             + re.escape(API_SURFACE_PREFIX)
                             + r" binding=(\S+) head=(\S+) changed=(\S*)[ \t]*\r?$", re.M)


def parse_api_surface(log_text: str, head_sha: str) -> dict[str, list[str]]:
    """binding -> every `changed=` value the job log carries for `head_sha`,
    in log order. A line naming any other head is not a verdict about this
    one (a re-run on a moved head, a merge-group run), and a commit cannot
    name its own sha in advance, so nothing in the judged tree can print a
    line that passes for this head's verdict."""
    out: dict[str, list[str]] = {}
    for m in _API_VERDICT_RE.finditer(log_text):
        if m.group(2) == head_sha:
            out.setdefault(m.group(1), []).append(m.group(3))
    return out


def api_surface_problems(touched: dict[str, str], verdicts: dict[str, list[str]]) -> list[str]:
    """Why each touched binding's source is still protected. Unchanged needs
    at least one verdict line for that binding on the judged head and EVERY
    such line exactly `false`: a missing verdict, `true`, `error` and
    anything unparseable all count as changed, and a second line can only
    add doubt, never remove it. An untouched binding needs no verdict."""
    problems = []
    for binding, path in touched.items():
        values = verdicts.get(binding) or []
        glob = binding_of(path)
        where = f"{path} is {binding} binding source (`{glob.pattern if glob else '?'}`)"
        if not values:
            problems.append(f"{where} and the api-surface job printed no verdict for {binding} on this head — "
                            "a missing verdict counts as changed")
        elif any(v != "false" for v in values):
            seen = "/".join(sorted({v or "<empty>" for v in values if v != "false"}))
            problems.append(f"{where} and the api-surface verdict for {binding} on this head is changed={seen} — "
                            "an exported API change, or a tool that could not tell, waits for a human")
    return problems


def api_surface_job_problems(ci_text: str) -> list[str]:
    """Why ci.yml's api-surface job is not what this file reads: no job
    carries API_SURFACE_CHECK as its check-run name, or that job blocks
    (it must carry job-level `continue-on-error: true`, so a deliberate API
    change is never a red required check)."""
    jobs = [j for j in ci_jobs(ci_text) if j.name == API_SURFACE_CHECK]
    if not jobs:
        return [f"no ci.yml job carries the check-run name {API_SURFACE_CHECK!r} this checker reads verdicts from"]
    if any(j.blocking for j in jobs):
        return [f"ci.yml's job {jobs[0].job_id} ({API_SURFACE_CHECK!r}) blocks; it must carry job-level "
                "continue-on-error: true"]
    return []


# ------------------------------------------- the security carve-out's derivation
#
# What verification code and trust anchors look like in each binding — never
# a list of names (a re-export of RELEASE_PUBLIC_KEY is not verification
# code), always the primitive or the literal that does the work: a hash or
# signature library, the release key's own 64-hex literal, the trust
# policy's two environment variable names as whole string literals, and
# each binding's load-time verification entry points. Measured against the
# tree when this was written: the needles hit exactly the carve-out entries
# above and nothing else.
VERIFICATION_NEEDLES: dict[str, tuple[str, ...]] = {
    "*": (r"[\"'][0-9a-fA-F]{64}[\"']", r"[\"']CHTYPES_TRUSTED_KEYS[\"']", r"[\"']CHTYPES_ALLOW_UNSIGNED[\"']"),
    "go": (r'"crypto/ed25519"', r'"crypto/sha256"', r"\bfileSHA256\(", r"\bverifyArtifactLibrary\(",
           r"\bcheckLibraryBytes\("),
    "python": (r"\bimport hashlib\b", r"\bfrom hashlib import\b", r"\b_ed25519\b", r"\bverify_library\(",
               r"\bcheck_library_bytes\("),
    "ts": (r"['\"]node:crypto['\"]", r"\bverifyChecksum\(", r"\bcheckLibraryBytes\("),
    "rust": (r"\bsha2::", r"\bed25519_dalek\b", r"\bsha256_file\(", r"\bsha256_hex\(", r"\bverify_signature\("),
}
_COMMENT_PREFIXES = {"python": ("#",), "go": ("//", "/*", "*"), "ts": ("//", "/*", "*"), "rust": ("//", "/*", "*")}


def verification_hits(binding: str, text: str) -> list[str]:
    """Every VERIFICATION_NEEDLES pattern (the binding's own and the shared
    ones) that matches a non-comment line of `text`. A full-line comment is
    dropped first, so a comment that only DESCRIBES verification is not
    verification code."""
    code = "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith(_COMMENT_PREFIXES[binding]))
    return [p for p in VERIFICATION_NEEDLES["*"] + VERIFICATION_NEEDLES[binding] if re.search(p, code)]


def carve_out_problems(sources: dict[str, str]) -> list[str]:
    """Every way the carve-out entries in PROTECTED_GLOBS disagree with
    `sources` (path -> text, every tracked file of every binding's source
    tree): a file the needles hit that no carve-out entry covers — the
    verification code moved, or new code grew — and a carve-out entry that
    covers no file at all — it moved away. A pure function of the text, so
    the selftest drives it from fabricated trees."""
    problems = []
    carve_outs = [g for g in PROTECTED_GLOBS if g.verification]
    for path, text in sorted(sources.items()):
        g = binding_of(path)
        if g is None:
            continue
        hits = verification_hits(g.binding, text)
        if hits and not any(_covers(c, path) for c in carve_outs):
            problems.append(f"{path} matches the verification needle {hits[0]!r} but no carve-out entry covers it; "
                            "add it to PROTECTED_GLOBS with verification=True")
    for c in carve_outs:
        if not any(_covers(c, p) for p in sources):
            problems.append(f"the carve-out entry `{c.pattern}` covers no file in the tree; the code moved — find "
                            "where and update the entry")
    return problems


# ------------------------------------------------------- the CONTRIBUTING.md guide


GUIDE_BLOCK_START = ("<!-- BEGIN policy-merge protected globs (generated by "
                      "`scripts/policy-merge-check.py --print-protected`; do not hand-edit between the markers) -->")
GUIDE_BLOCK_END = "<!-- END policy-merge protected globs -->"


def protected_globs_guide_block(globs: tuple[ProtectedGlob, ...] = PROTECTED_GLOBS) -> str:
    """The exact text CONTRIBUTING.md must carry between GUIDE_BLOCK_START and
    GUIDE_BLOCK_END, derived from `globs` — never hand-typed. --check-guide
    compares this against what CONTRIBUTING.md actually has. The blank line
    right after the opening comment and right before the closing one is not
    decoration: an HTML comment is a markdown block, and `dprint fmt`
    (CI's `prose` job) inserts exactly this blank-line separation between a
    block comment and the list beside it — this has to match what a
    formatted file holds, or --check-guide and `prose` would fight forever."""
    lines = [GUIDE_BLOCK_START, ""] + [g.guide_bullet() for g in globs] + ["", GUIDE_BLOCK_END]
    return "\n".join(lines)


def guide_problems(guide_text: str, expected_block: str) -> list[str]:
    """Every way `guide_text` (CONTRIBUTING.md's contents) disagrees with
    `expected_block` (protected_globs_guide_block()'s output). A pure
    function of two strings, so the selftest can prove a divergence is
    caught without touching the real file."""
    start = guide_text.find(GUIDE_BLOCK_START)
    end = guide_text.find(GUIDE_BLOCK_END)
    if start == -1 or end == -1:
        return ["the guide does not contain both the BEGIN and END markers for the generated block"]
    end += len(GUIDE_BLOCK_END)
    actual = guide_text[start:end]
    if actual == expected_block:
        return []
    actual_lines, expected_lines = actual.splitlines(), expected_block.splitlines()
    extra = [line for line in actual_lines if line not in expected_lines]
    missing = [line for line in expected_lines if line not in actual_lines]
    problems = []
    if extra:
        problems.append("the guide has line(s) PROTECTED_GLOBS does not: " + "; ".join(extra[:5]))
    if missing:
        problems.append("PROTECTED_GLOBS has line(s) the guide does not: " + "; ".join(missing[:5]))
    return problems or ["the guide's block differs from PROTECTED_GLOBS (order or whitespace)"]


# The check-run names branch protection requires on `main`, copied from it.
# --check-ci-names fails when this and ci.yml's blocking jobs disagree; see
# the header for why the list lives here rather than being read at run time.
REQUIRED_CHECKS = (
    "abi — the header and every binding agree",
    "go — build, vet, standalone check (no artifacts)",
    "python — ruff, import, suite (no artifacts)",
    "ts — build, typecheck, suite (no artifacts)",
    "rust — build, clippy, fmt, suite (no artifacts)",
    "abi-fixtures — the ABI-revision refusal, all four bindings, no published artifact",
    "artifacts — published lines, linux-amd64, every suite runs the golden set",
    "lint-go — golangci-lint (go/.golangci.yml)",
    "lint-ts — biome (ts/biome.json)",
    "lint-actions — actionlint + shellcheck",
    "misspell — American spelling, whole tree (scripts/lint-spelling.sh)",
    "public — no pointers into the private repository",
    "prose — markdownlint + no-hard-wrap (dprint textWrap never)",
)

# Branch protection pins every required context to the GitHub Actions app, so
# a check run of the same name from any other app satisfies nothing.
REQUIRED_CHECK_APP = "github-actions"

# ---------------------------------------------------- test-counts (chtypes#285 §1b)
#
# Which check-run name to read each `chtypes-count` line from. Four suites,
# each printed from TWO jobs (its own no-artifact job, and the `artifacts`
# job's own step for that suite — see scripts/check-suite.sh and
# scripts/lib/standalone_census.py, which print the line), plus the
# golden-case count, sourced from wherever the goldens are already counted
# TODAY (the `artifacts` job's go step — see standalone_census.py's own
# docstring). The four `-artifacts` labels and `GOLDEN_LABEL` share ONE check
# name because all five lines are printed by steps inside that SAME job — one
# job log read serves all five, not five separate ones.
_ARTIFACTS_CHECK = "artifacts — published lines, linux-amd64, every suite runs the golden set"
SUITE_CHECK_NAME: dict[str, str] = {
    "go-no-artifacts": "go — build, vet, standalone check (no artifacts)",
    "python-no-artifacts": "python — ruff, import, suite (no artifacts)",
    "ts-no-artifacts": "ts — build, typecheck, suite (no artifacts)",
    "rust-no-artifacts": "rust — build, clippy, fmt, suite (no artifacts)",
    "go-artifacts": _ARTIFACTS_CHECK,
    "python-artifacts": _ARTIFACTS_CHECK,
    "ts-artifacts": _ARTIFACTS_CHECK,
    "rust-artifacts": _ARTIFACTS_CHECK,
}
SUITE_LABELS: tuple[str, ...] = tuple(SUITE_CHECK_NAME)
GOLDEN_LABEL = "golden-cases"
GOLDEN_CHECK_NAME = _ARTIFACTS_CHECK

CONDITIONS = {
    "fork": "condition 1, the head is a branch of this repository",
    "pull-request": "one open, non-draft pull request against main carries the commit",
    "stale": "the head is still the commit ci judged",
    "protected": "condition 5, no touched file (current or, for a rename, old path) matches a protected glob, "
                 "and every touched binding's source has an api-surface verdict of changed=false on the head",
    "checks": "condition 2, every required check passed",
    "test-counts": "condition 7 (chtypes#285 §1b), only when the diff touches a test or fixture path: no "
                   "suite's executed-test count, and no golden-case count, fell below main's last green ci "
                   "push run",
    "mergeable": "GitHub reports no merge conflict",
    "review": "condition 4, no requested changes and no review conversation",
    "bytes": "condition 6, docs/support.md — when touched — is main's own regeneration byte for byte",
}


@dataclass(frozen=True)
class Refusal:
    condition: str
    detail: str

    def __post_init__(self) -> None:
        if self.condition not in CONDITIONS:
            raise ValueError(f"unknown condition {self.condition!r}")

    def text(self) -> str:
        return f"{self.condition} ({CONDITIONS[self.condition]}): {self.detail}"


# ------------------------------------------------------------ pure decisions


def select_pr(repo: str, run_head_repo: str, associated: list[dict]) -> tuple[int | None, Refusal | None]:
    """Which pull request a ci run's head commit belongs to, from the list
    GET /repos/{repo}/commits/{sha}/pulls returns."""
    if run_head_repo != repo:
        return None, Refusal("fork", f"the ci run's head is in {run_head_repo or '<no repository>'}, not {repo}")
    candidates = [p for p in associated
                  if p.get("state") == "open" and (p.get("base") or {}).get("ref") == BASE_BRANCH]
    if not candidates:
        return None, Refusal("pull-request", f"no open pull request against {BASE_BRANCH} carries this commit")
    if len(candidates) > 1:
        numbers = ", ".join(f"#{p.get('number')}" for p in candidates)
        return None, Refusal("pull-request", f"more than one open pull request against {BASE_BRANCH} carries "
                                             f"this commit ({numbers})")
    return candidates[0]["number"], None


def check_run_problems(check_runs: list[dict], required: tuple[str, ...]) -> list[str]:
    """Every required context that is not a completed, successful GitHub Actions
    check run. Several runs of one name (a re-run) must ALL be successful."""
    problems = []
    for name in required:
        runs = [r for r in check_runs
                if r.get("name") == name and (r.get("app") or {}).get("slug") == REQUIRED_CHECK_APP]
        if not runs:
            problems.append(f"{name!r} has no check run")
            continue
        for r in runs:
            if r.get("status") != "completed":
                problems.append(f"{name!r} is {r.get('status')}")
            elif r.get("conclusion") != "success":
                problems.append(f"{name!r} concluded {r.get('conclusion')}")
    return problems


def first_difference(a: bytes, b: bytes) -> tuple[int, int]:
    """(byte offset, 1-based line) of the first difference between two byte strings."""
    n = min(len(a), len(b))
    i = next((k for k in range(n) if a[k] != b[k]), n)
    return i, a[:i].count(b"\n") + 1


@dataclass(frozen=True)
class TestCountFacts:
    """Everything `test_count_problems` needs, already fetched. `head`/`main`
    map a SUITE_LABELS entry to (ran, skipped); a label absent from a dict is
    exactly a missing count (see gather_test_counts). Empty dicts and None
    golden counts are the correct, zero-API-call value for a pull request
    that does not touch a test or fixture path — `decide()` never even looks
    at this unless `touches_test_or_fixture_path(files)` is true."""
    head: dict[str, tuple[int, int]]
    main: dict[str, tuple[int, int]]
    head_golden: int | None
    main_golden: int | None


def test_count_problems(head: dict[str, tuple[int, int]], main: dict[str, tuple[int, int]],
                        head_golden: int | None, main_golden: int | None) -> list[str]:
    """Every way chtypes#285 §1b's rule fails: a suite's (or the golden set's)
    executed-test count on the head is lower than main's last green `ci` push
    run, checked PER SUITE, never summed — one suite dropping is a refusal
    even while another rises. Only the `ran` half of each pair is compared:
    skips are printed for a human but never compared directly, because a test
    that moved from ran to skipped has ALREADY dropped `ran` by exactly one —
    comparing skip counts too would not catch anything `ran` does not already
    catch, and would give a rising skip count (new tests deliberately added
    as skip-when-no-registry, say) a second, spurious way to refuse. A count
    missing from either dict — gather_test_counts never put a line there
    because the job produced none, or a parse found nothing — is a drop,
    never 'unchanged'."""
    problems = []
    for label in SUITE_LABELS:
        h, m = head.get(label), main.get(label)
        if h is None or m is None:
            problems.append(f"{label}: {'no' if h is None else 'a'} count on the head, "
                            f"{'no' if m is None else 'a'} count on main's last green ci push — a missing "
                            "count is a drop")
            continue
        if h[0] < m[0]:
            problems.append(f"{label}: ran {h[0]}, main's last green ci push ran {m[0]}")
    if head_golden is None or main_golden is None:
        problems.append(f"{GOLDEN_LABEL}: {'no' if head_golden is None else 'a'} count on the head, "
                        f"{'no' if main_golden is None else 'a'} count on main's last green ci push — a "
                        "missing count is a drop")
    elif head_golden < main_golden:
        problems.append(f"{GOLDEN_LABEL}: checked {head_golden}, main's last green ci push checked {main_golden}")
    return problems


def decide(*, repo: str, expected_head_sha: str, run_head_repo: str | None, pr: dict,
           files: list[dict], check_runs: list[dict], reviews: list[dict],
           review_comments: list[dict], head_bytes: dict[str, bytes] | None = None,
           regenerated: dict[str, bytes] | None = None, test_counts: TestCountFacts | None = None,
           api_verdicts: dict[str, list[str]] | None = None,
           required: tuple[str, ...] = REQUIRED_CHECKS) -> Refusal | None:
    """The first condition that fails, or None when every condition holds.
    With `regenerated` None (the gate), the byte HALF of condition 6 is not
    checked — but a docs/support.md that was added, deleted or renamed rather
    than modified is still refused at gate time; that much is pure API data.
    `test_counts` has no such split: gather_test_counts reads everything
    condition 7 needs (job logs, never a head file) up front, so both `gate`
    and `enqueue` check it fully. `api_verdicts` (gather_api_verdicts, the
    api-surface job's log for this head) is the same: None or {} is no
    verdict at all, which keeps every touched binding's source protected."""
    # fork (condition 1)
    if run_head_repo is not None and run_head_repo != repo:
        return Refusal("fork", f"the ci run's head is in {run_head_repo or '<no repository>'}, not {repo}")
    head = pr.get("head") or {}
    head_repo = (head.get("repo") or {}).get("full_name")
    if head_repo != repo:
        return Refusal("fork", f"the pull request's head is in {head_repo or '<a deleted repository>'}, not {repo}")

    # pull-request
    if pr.get("state") != "open":
        return Refusal("pull-request", f"the pull request is {pr.get('state')}")
    base_ref = (pr.get("base") or {}).get("ref")
    if base_ref != BASE_BRANCH:
        return Refusal("pull-request", f"the pull request targets {base_ref}, not {BASE_BRANCH}")
    if pr.get("draft"):
        return Refusal("pull-request", "the pull request is a draft")

    # stale
    if head.get("sha") != expected_head_sha:
        return Refusal("stale", f"the head is now {head.get('sha')}, not the judged {expected_head_sha}")

    # protected (condition 5) — evaluated before `checks` so a pull request
    # that edited its own judge is refused before its checks are trusted.
    names = [f.get("filename") for f in files]
    if pr.get("changed_files") != len(names):
        return Refusal("protected", f"the pull request reports {pr.get('changed_files')} changed file(s) but the "
                                    f"file list has {len(names)}; a protected-glob scan of an incomplete list "
                                    "cannot be trusted")
    touch = first_protected_touch(files)
    if touch is not None:
        path, glob = touch
        return Refusal("protected", f"{path} matches the protected glob `{glob.pattern}` ({glob.reason})")
    # The binding-source class (chtypes#285 §1), still condition 5 and still
    # before `checks`: the api-surface verdicts it reads were written by the
    # pull request's own copy of ci.yml and scripts/, which the unconditional
    # check above has just shown to be main's.
    problems = api_surface_problems(touched_bindings(files), api_verdicts or {})
    if problems:
        more = f"; and {len(problems) - 2} more" if len(problems) > 2 else ""
        return Refusal("protected", "; ".join(problems[:2]) + more)

    # checks (condition 2)
    problems = check_run_problems(check_runs, required)
    if problems:
        more = f"; and {len(problems) - 3} more" if len(problems) > 3 else ""
        return Refusal("checks", "; ".join(problems[:3]) + more)

    # test-counts (condition 7, chtypes#285 §1b) — only when the diff touches
    # a test or fixture path (is_test_or_fixture_path has the exact rule).
    # Evaluated AFTER `checks` so a suite that is already red is reported as
    # `checks`, never masked as a test-count drop; a pull request that
    # touches no test or fixture path skips this at zero cost, both here and
    # in gather_test_counts, which never reads a job log in that case.
    if touches_test_or_fixture_path(files):
        tc = test_counts or TestCountFacts(head={}, main={}, head_golden=None, main_golden=None)
        problems = test_count_problems(tc.head, tc.main, tc.head_golden, tc.main_golden)
        if problems:
            more = f"; and {len(problems) - 3} more" if len(problems) > 3 else ""
            return Refusal("test-counts", "; ".join(problems[:3]) + more)

    # mergeable
    if pr.get("mergeable") is False:
        return Refusal("mergeable", "GitHub reports the pull request cannot merge cleanly")

    # review (condition 4)
    if review_comments:
        return Refusal("review", f"{len(review_comments)} review comment(s) exist; a human is in the conversation")
    latest: dict[str, str] = {}
    for r in reviews:
        state = r.get("state")
        if state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[(r.get("user") or {}).get("login") or "<unknown>"] = state
    blockers = sorted(user for user, state in latest.items() if state == "CHANGES_REQUESTED")
    if blockers:
        return Refusal("review", "changes requested by " + ", ".join(blockers))

    # bytes (condition 6) — only when the diff touches docs/support.md
    support = next((f for f in files if f.get("filename") == "docs/support.md"), None)
    if support is not None:
        if support.get("status") != "modified":
            return Refusal("bytes", f"docs/support.md is {support.get('status')}, not modified; a policy merge "
                                    "only verifies a plain regeneration")
        if regenerated is not None:
            if head_bytes is None:
                raise ValueError("a byte comparison needs the head's bytes")
            want, got = regenerated["docs/support.md"], head_bytes["docs/support.md"]
            if want != got:
                offset, line = first_difference(want, got)
                return Refusal("bytes", f"docs/support.md at the head differs from main's regeneration at byte "
                                        f"{offset} (line {line}); the head has {len(got)} bytes, the regeneration "
                                        f"{len(want)}")
    return None


def decode_contents(payload: dict, path: str) -> bytes:
    """A file's bytes from a GET /repos/{repo}/contents/{path} response."""
    if payload.get("type") != "file":
        raise ValueError(f"{path} is a {payload.get('type')}, not a file")
    if payload.get("encoding") != "base64":
        raise ValueError(f"{path} came back with encoding {payload.get('encoding')!r}, not base64 "
                         "(the contents API stops inlining files over 1 MB)")
    data = base64.b64decode(payload.get("content") or "", validate=False)
    if len(data) != payload.get("size"):
        raise ValueError(f"{path} decoded to {len(data)} bytes but the API reports {payload.get('size')}")
    return data


def http_status(stderr: str) -> int | None:
    """The HTTP status `gh api` names in its error line: 'gh: <message> (HTTP 409)'."""
    m = re.search(r"\(HTTP (\d{3})\)", stderr)
    return int(m.group(1)) if m else None


# --------------------------------------------------- ci.yml's blocking jobs


class CiShapeError(Exception):
    pass


@dataclass
class CiJob:
    job_id: str
    name: str
    blocking: bool


def _yaml_scalar(value: str, lineno: int) -> str:
    """A single-line YAML scalar, in only the shapes ci.yml uses. Anything else
    is refused rather than guessed, so a changed shape fails loudly here."""
    if value.startswith('"'):
        m = re.match(r'^"((?:[^"\\]|\\.)*)"\s*(#.*)?$', value)
        if not m:
            raise CiShapeError(f"ci.yml line {lineno}: unterminated double-quoted value {value!r}")
        if "\\" in m.group(1):
            raise CiShapeError(f"ci.yml line {lineno}: an escape sequence in {value!r}; extend this parser")
        return m.group(1)
    if value.startswith("'"):
        m = re.match(r"^'((?:[^']|'')*)'\s*(#.*)?$", value)
        if not m:
            raise CiShapeError(f"ci.yml line {lineno}: unterminated single-quoted value {value!r}")
        return m.group(1).replace("''", "'")
    plain = re.split(r"\s+#", value, maxsplit=1)[0].strip()
    if not plain or plain[0] in "&*!|>{[%@`" or ": " in plain:
        raise CiShapeError(f"ci.yml line {lineno}: a value this parser does not read: {value!r}")
    return plain


def ci_jobs(text: str) -> list[CiJob]:
    """Every job under ci.yml's top-level `jobs:` with the name its check run
    carries (`name:`, else the job id) and whether it blocks (no job-level
    `continue-on-error: true`). Only the keys at the job's own indentation are
    read: a step's `name:` or `continue-on-error:` sits deeper and is not the
    job's."""
    jobs: list[dict] = []
    in_jobs = False
    current: dict | None = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw.startswith(" "):
            in_jobs = re.match(r"^jobs:\s*(#.*)?$", raw) is not None
            current = None
            continue
        if not in_jobs:
            continue
        m = re.match(r"^  ([A-Za-z_][A-Za-z0-9_-]*):\s*(#.*)?$", raw)
        if m:
            current = {"id": m.group(1), "name": None, "coe": None, "line": lineno}
            jobs.append(current)
            continue
        if re.match(r"^ {1,3}\S", raw):
            raise CiShapeError(f"ci.yml line {lineno}: an entry under jobs: this parser does not read: {raw!r}")
        m = re.match(r"^    ([A-Za-z_][A-Za-z0-9_-]*):\s*(.*?)\s*$", raw)
        if not m or current is None:
            continue
        key, value = m.group(1), m.group(2)
        if key == "name":
            name = _yaml_scalar(value, lineno)
            if "${{" in name:
                raise CiShapeError(f"ci.yml line {lineno}: job {current['id']}'s name is an expression; "
                                   "its check-run name cannot be read statically")
            current["name"] = name
        elif key == "continue-on-error":
            flag = _yaml_scalar(value, lineno)
            if flag not in ("true", "false"):
                raise CiShapeError(f"ci.yml line {lineno}: job {current['id']}'s continue-on-error is {flag!r}, "
                                   "not a literal true or false")
            current["coe"] = flag == "true"
        elif key == "strategy":
            raise CiShapeError(f"ci.yml line {lineno}: job {current['id']} has a strategy; a matrix changes "
                               "its check-run names, so extend this parser before relying on it")
    return [CiJob(j["id"], j["name"] if j["name"] is not None else j["id"], not j["coe"]) for j in jobs]


def ci_name_problems(ci_text: str, required: tuple[str, ...]) -> list[str]:
    """Every way REQUIRED_CHECKS and ci.yml's blocking jobs disagree."""
    jobs = ci_jobs(ci_text)
    if not jobs:
        return ["ci.yml has no jobs this parser could read"]
    problems = []
    seen: dict[str, str] = {}
    for j in jobs:
        if j.name in seen:
            problems.append(f"jobs {seen[j.name]} and {j.job_id} share the check-run name {j.name!r}")
        seen[j.name] = j.job_id
    by_name = {j.name: j for j in jobs}
    for name in required:
        if name not in by_name:
            problems.append(f"REQUIRED_CHECKS names {name!r}, which no ci.yml job carries")
        elif not by_name[name].blocking:
            problems.append(f"REQUIRED_CHECKS names {name!r}, but ci.yml's job {by_name[name].job_id} is "
                            "continue-on-error (non-blocking)")
    if len(set(required)) != len(required):
        problems.append("REQUIRED_CHECKS lists a name more than once")
    for j in jobs:
        if j.blocking and j.name not in required:
            problems.append(f"ci.yml's job {j.job_id} ({j.name!r}) is blocking but not in REQUIRED_CHECKS")
    return problems


# ------------------------------------------------------------------- the API


class ApiError(Exception):
    def __init__(self, what: str, status: int | None, stderr: str):
        super().__init__(f"{what} failed (HTTP {status if status is not None else '?'}): {stderr}")
        self.status = status


def gh(args: list[str], what: str) -> str:
    proc = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        # gh prints an API error body on stdout; none of it is data.
        raise ApiError(what, http_status(proc.stderr), proc.stderr.strip())
    return proc.stdout


def gh_object(endpoint: str) -> dict:
    return json.loads(gh([endpoint], f"GET {endpoint}"))


def gh_items(endpoint: str, jq: str = ".[]") -> list[dict]:
    """Every item of a paginated list, one compact JSON value per output line."""
    out = gh(["--paginate", endpoint, "-q", jq], f"GET {endpoint}")
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def head_file(repo: str, path: str, sha: str) -> bytes:
    return decode_contents(gh_object(f"repos/{repo}/contents/{path}?ref={sha}"), path)


# --------------------------------------------- test-counts (chtypes#285 §1b)
#
# A check run's own `output.summary` and `output.text` are NOT populated for
# a GitHub-Actions-authored job — measured directly against a real run on
# this repository (`gh api repos/.../check-runs/<id>` on a completed `go`
# job: both fields came back null, `annotations_count` 1, and the one
# annotation was GitHub's own runner-image deprecation notice, not anything a
# workflow step wrote). So this reads the job's own LOG instead: `GET
# repos/{repo}/actions/jobs/{job_id}/logs`, confirmed readable and returning
# the full console output (`gh api ... --allow-escape-sequences`, the flag
# `gh` requires for content that contains raw ANSI escapes — check-suite.sh's
# `say()`/`note()` helpers print some — regardless of whether stdout is a
# terminal; without it `gh` refuses the whole response rather than emitting
# unsafe bytes, even into a redirected file). A check run's own `id` (already
# in `check_runs`, from `commits/{sha}/check-runs`) IS the workflow job id —
# also confirmed directly (`gh api repos/.../actions/jobs/<the check run's
# id>` returned the same job, by name and run_id). Both endpoints need the
# `actions: read` permission, which the `automerge` job does not otherwise
# use; policy-merge.yml documents why it is safe to add: this reads the
# ALREADY-COMPLETED run's own log text as data, through the API, exactly the
# same pwn-request posture as the docs/support.md byte comparison — nothing
# here executes, sources or parses-as-code anything of the head.
_COUNT_RE = re.compile(r"chtypes-count suite=(\S+) ran=(\d+) skipped=(\d+)")
_GOLDEN_COUNT_RE = re.compile(r"chtypes-count golden-cases=(\d+)")


def parse_suite_counts(log_text: str) -> dict[str, tuple[int, int]]:
    """Every `chtypes-count suite=<label> ran=<n> skipped=<n>` line in a
    job's log text, keyed by label — the last line for a given label wins,
    the same "several runs, take what actually happened" posture
    check_run_problems applies to a re-run's check runs. A job log commonly
    carries several labels at once: the `artifacts` job prints four (one per
    suite step)."""
    return {m.group(1): (int(m.group(2)), int(m.group(3))) for m in _COUNT_RE.finditer(log_text)}


def parse_golden_count(log_text: str) -> int | None:
    """The last `chtypes-count golden-cases=<n>` line in a job's log text, or
    None if it never printed one (a fetch failure before the go suite step
    ran, an older log predating this feature, or — genuinely — the
    no-artifact job's log, which never prints this line at all)."""
    last = None
    for m in _GOLDEN_COUNT_RE.finditer(log_text):
        last = int(m.group(1))
    return last


def readable_job_ids(runs: list[dict]) -> dict[str, int]:
    """Check-run (or job) name -> id, for only the runs that COMPLETED with
    conclusion `success`. A skipped, failed or canceled job has no usable
    count, and a skipped one has no log at all: GET .../jobs/{id}/logs
    answers 404 for it (measured on #311's skipped `artifacts` job, which
    turned this condition's read into an exit-2 error). Leaving such a job
    out makes its suite a missing count, which test_count_problems refuses
    loudly, never an error and never a pass."""
    return {r["name"]: r["id"] for r in runs
            if r.get("id") is not None and r.get("status") == "completed" and r.get("conclusion") == "success"}


def job_log(repo: str, job_id: int) -> str:
    return gh(["--allow-escape-sequences", f"repos/{repo}/actions/jobs/{job_id}/logs"],
              f"GET actions/jobs/{job_id}/logs")


def latest_green_push_jobs(repo: str) -> dict[str, int]:
    """Check-run name -> job id, for `repo`'s most recent completed,
    successful `push`-triggered run of `ci` on `main` — never a
    `pull_request` run (judges one pull request) or a `merge_group` run
    (judges the queue's own candidate combination): a `push` run is main's
    own tip judging itself, which is what "main's last green ci push run"
    means. Empty if none is found — a fresh repository, or immediately after
    ci.yml itself first gained the `push` trigger — which test_count_problems
    reads as every suite (and the golden count) being a missing count on the
    main side, refusing rather than guessing."""
    runs = gh_items(f"repos/{repo}/actions/workflows/ci.yml/runs?branch={BASE_BRANCH}&event=push&status=success"
                    "&per_page=1", ".workflow_runs[]")
    if not runs:
        return {}
    jobs = gh_items(f"repos/{repo}/actions/runs/{runs[0]['id']}/jobs?per_page=100", ".jobs[]")
    return readable_job_ids(jobs)


def gather_test_counts(repo: str, files: list[dict], check_runs: list[dict]) -> TestCountFacts:
    """The head's and main's chtypes-count facts, or four empty/None values
    at ZERO API cost when the diff does not touch a test or fixture path —
    `decide()` would ignore them either way, but there is no reason to read
    five job logs twice over for a pull request this condition never looks
    at."""
    if not touches_test_or_fixture_path(files):
        return TestCountFacts(head={}, main={}, head_golden=None, main_golden=None)
    head_job_id = readable_job_ids(check_runs)
    main_job_id = latest_green_push_jobs(repo)

    def read(job_id_by_name: dict[str, int]) -> tuple[dict[str, tuple[int, int]], int | None]:
        counts: dict[str, tuple[int, int]] = {}
        logs: dict[int, str] = {}

        def log_of(job_id: int) -> str:
            if job_id not in logs:
                logs[job_id] = job_log(repo, job_id)
            return logs[job_id]

        for label, check_name in SUITE_CHECK_NAME.items():
            job_id = job_id_by_name.get(check_name)
            if job_id is None:
                continue
            found = parse_suite_counts(log_of(job_id)).get(label)
            if found is not None:
                counts[label] = found
        golden_job_id = job_id_by_name.get(GOLDEN_CHECK_NAME)
        golden = parse_golden_count(log_of(golden_job_id)) if golden_job_id is not None else None
        return counts, golden

    head_counts, head_golden = read(head_job_id)
    main_counts, main_golden = read(main_job_id)
    return TestCountFacts(head=head_counts, main=main_counts, head_golden=head_golden, main_golden=main_golden)


def api_surface_job_id(check_runs: list[dict]) -> int | None:
    """The api-surface job whose log may be read for verdicts: the GitHub
    Actions check run named API_SURFACE_CHECK, only if it COMPLETED with
    success (readable_job_ids). A skipped job has no log at all (404) and a
    failed or canceled one may have stopped before printing a verdict, so
    either is None here: a missing verdict, which keeps every touched
    binding protected — never an exit-2 error and never a pass. A tool that
    fails makes the whole job red, so one broken tool holds every binding's
    source for a human until it is fixed; that is the fail-closed side."""
    runs = [r for r in check_runs if (r.get("app") or {}).get("slug") == REQUIRED_CHECK_APP]
    return readable_job_ids(runs).get(API_SURFACE_CHECK)


def gather_api_verdicts(repo: str, files: list[dict], check_runs: list[dict], head_sha: str) -> dict[str, list[str]]:
    """The api-surface verdicts for `head_sha`, read from that job's own log —
    at ZERO API cost when the diff touches no binding source, or touches an
    unconditionally protected file (decide() refuses that first, and reads
    nothing it has not already shown to be main's)."""
    if first_protected_touch(files) is not None or not touched_bindings(files):
        return {}
    job_id = api_surface_job_id(check_runs)
    if job_id is None:
        return {}
    return parse_api_surface(job_log(repo, job_id), head_sha)


@dataclass(frozen=True)
class EnqueuePlan:
    """What `cmd_enqueue` should do once `decide()` has passed: either
    enqueue for real, carrying the judged node id and head sha, or — for
    `--dry-run` — do nothing. A pure function of facts already in hand
    (`build_enqueue_plan`, below), so `--selftest` can prove "enqueue is
    called with the judged head sha" and "dry run never calls it" without a
    network call: the mutation itself (`enqueue_pull_request`) is a thin,
    untested-by-selftest wrapper, same as every other `gh api` call in this
    file, but exactly what arguments it WOULD be called with, and whether it
    is reached at all, are plain data here."""
    node_id: str
    head_sha: str
    dry_run: bool


def build_enqueue_plan(pr: dict, head_sha: str, dry_run: bool) -> EnqueuePlan:
    node_id = pr.get("node_id")
    if not node_id:
        raise ValueError("the pull request object carries no node_id to enqueue")
    return EnqueuePlan(node_id=node_id, head_sha=head_sha, dry_run=dry_run)


def handoff_outputs(plan: EnqueuePlan, number: int) -> dict[str, str]:
    """The step outputs that hand a judged pull request to the `enqueue` job,
    which holds the App token and does nothing else (BUILT-IN TOKEN EVENTS
    START NO WORKFLOW, below). A pure function, so --selftest proves the
    judged node id and head sha are what is handed on."""
    return {"enqueue": "1", "pr": str(number), "node_id": plan.node_id, "head_sha": plan.head_sha}


_SHA40 = re.compile(r"[0-9a-f]{40}")
_NODE_ID = re.compile(r"[A-Za-z0-9_=-]{1,200}")


def bot_args_problem(node_id: str, head_sha: str, pr: str) -> str | None:
    """Why `enqueue-as-bot`'s arguments cannot be what the judging job
    handed on, or None. They arrive through job outputs; anything that is not
    a node id, a 40-hex head sha and a pull request number is refused before
    the App token is used."""
    if not _NODE_ID.fullmatch(node_id or ""):
        return f"--node-id {node_id!r} is not a GraphQL node id"
    if not _SHA40.fullmatch(head_sha or ""):
        return f"--head-sha {head_sha!r} is not a 40-hex commit sha"
    if not (pr or "").isdigit():
        return f"--pr {pr!r} is not a pull request number"
    return None


def enqueue_pull_request(node_id: str, head_sha: str) -> dict:
    """Adds the pull request to main's merge queue: the enqueuePullRequest
    GraphQL mutation, passing expectedHeadOid=head_sha so GitHub itself
    refuses the enqueue if the head moved since it was judged — the same
    role `sha=` played on the REST merge call this replaced. Raises ApiError
    on any failure.

    This deliberately does NOT try to decode a head-mismatch out of the
    mutation's error text into a friendly `stale` refusal, the way the old
    merge call's HTTP 409 was decoded: that 409 case was verified against a
    real occurrence (see http_status()'s selftest, which quotes gh's actual
    error line); no equivalent has been observed for this mutation, because
    the merge queue does not exist on this repository to produce one against
    until after this change merges. The PRIMARY, verified defense against a
    moved head is decide()'s `stale` condition, checked immediately before
    this is ever reached — see the module docstring's ENQUEUE, NOT MERGE
    section. An unrecognized failure here is a hard ERROR (exit 2), never
    guessed into a quiet refusal."""
    query = ("mutation($id:ID!,$oid:GitObjectID!){enqueuePullRequest(input:{pullRequestId:$id,"
             "expectedHeadOid:$oid}){mergeQueueEntry{id}}}")
    out = gh(["graphql", "-f", f"query={query}", "-f", f"id={node_id}", "-f", f"oid={head_sha}"],
             f"POST graphql enqueuePullRequest({node_id[:16]}…)")
    return json.loads(out)


@dataclass
class Facts:
    pr: dict
    files: list[dict]
    check_runs: list[dict]
    reviews: list[dict]
    review_comments: list[dict]


def gather(repo: str, number: int, sha: str) -> Facts:
    return Facts(
        pr=gh_object(f"repos/{repo}/pulls/{number}"),
        files=gh_items(f"repos/{repo}/pulls/{number}/files?per_page=100"),
        check_runs=gh_items(f"repos/{repo}/commits/{sha}/check-runs?filter=latest&per_page=100", ".check_runs[]"),
        reviews=gh_items(f"repos/{repo}/pulls/{number}/reviews?per_page=100"),
        review_comments=gh_items(f"repos/{repo}/pulls/{number}/comments?per_page=100"),
    )


# ------------------------------------------------------- reporting, outputs


def summary(line: str) -> None:
    """One line to the log and, in Actions, to the job summary."""
    print(line)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def output(**values: object) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    for k, v in values.items():
        print(f"output {k}={v}")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            for k, v in values.items():
                f.write(f"{k}={v}\n")


def refuse(refusal: Refusal, where: str) -> int:
    summary(f"policy-merge: left for a human. {where}: {refusal.text()}")
    return 0


# ----------------------------------------------------------- the subcommands


def cmd_gate(args: argparse.Namespace) -> int:
    if args.pr is not None:
        number = args.pr
        sha = gh_object(f"repos/{args.repo}/pulls/{number}").get("head", {}).get("sha") or ""
        run_head_repo = None
    else:
        sha = args.run_head_sha
        run_head_repo = args.run_head_repo
        associated = gh_items(f"repos/{args.repo}/commits/{sha}/pulls?per_page=100") \
            if run_head_repo == args.repo else []
        number, refusal = select_pr(args.repo, run_head_repo, associated)
        if refusal:
            output(candidate=0)
            return refuse(refusal, f"commit {sha[:12]}")
    facts = gather(args.repo, number, sha)
    test_counts = gather_test_counts(args.repo, facts.files, facts.check_runs)
    api_verdicts = gather_api_verdicts(args.repo, facts.files, facts.check_runs, sha)
    refusal = decide(repo=args.repo, expected_head_sha=sha, run_head_repo=run_head_repo, pr=facts.pr,
                     files=facts.files, check_runs=facts.check_runs, reviews=facts.reviews,
                     review_comments=facts.review_comments, test_counts=test_counts, api_verdicts=api_verdicts)
    if refusal:
        output(candidate=0)
        return refuse(refusal, f"PR #{number} at {sha[:12]}")
    support_touched = any(f.get("filename") == "docs/support.md" for f in facts.files)
    note = ("passes every condition but the docs/support.md byte comparison" if support_touched
            else "passes every condition (does not touch docs/support.md)")
    print(f"policy-merge: PR #{number} at {sha} {note}; proceeding to the enqueue check")
    output(candidate=1, pr=number, head_sha=sha)
    return 0


def cmd_enqueue(args: argparse.Namespace) -> int:
    regenerated: dict[str, bytes] = {}
    for spec in args.regenerated:
        path, sep, local = spec.partition("=")
        if not sep:
            print(f"policy-merge-check: --regenerated needs PATH=FILE, got {spec!r}", file=sys.stderr)
            return 2
        with open(local, "rb") as f:
            regenerated[path] = f.read()
    if sorted(regenerated) != sorted(GUARDED_FILES):
        print(f"policy-merge-check: the workflow regenerated {sorted(regenerated)} but GUARDED_FILES is "
              f"{sorted(GUARDED_FILES)}; every guarded file needs a regenerated copy, whether or not this pull "
              "request happens to touch it", file=sys.stderr)
        return 2
    number, sha = args.pr, args.head_sha
    facts = gather(args.repo, number, sha)
    test_counts = gather_test_counts(args.repo, facts.files, facts.check_runs)
    api_verdicts = gather_api_verdicts(args.repo, facts.files, facts.check_runs, sha)
    judged = dict(repo=args.repo, expected_head_sha=sha, run_head_repo=args.run_head_repo or None, pr=facts.pr,
                  files=facts.files, check_runs=facts.check_runs, reviews=facts.reviews,
                  review_comments=facts.review_comments, test_counts=test_counts, api_verdicts=api_verdicts)
    # Every other condition first, fresh: a head that moved or vanished since
    # the gate is `stale`, never a failed read of its bytes. This IS the
    # primary defense against a moved head — see enqueue_pull_request()'s own
    # docstring for why the mutation's expectedHeadOid is a backstop on this,
    # not a replacement for it.
    refusal = decide(**judged)
    if refusal is None:
        head_bytes = {path: head_file(args.repo, path, sha) for path in GUARDED_FILES}
        refusal = decide(**judged, head_bytes=head_bytes, regenerated=regenerated)
    where = f"PR #{number} at {sha[:12]}"
    if refusal:
        return refuse(refusal, where)
    main_sha = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    touched_guarded = tuple(p for p in GUARDED_FILES if any(f.get("filename") == p for f in facts.files))
    # What to do next is a PURE decision (build_enqueue_plan) from facts
    # already in hand, so --selftest can prove the plan carries the judged
    # head sha and that a dry run's plan is never executed, without a
    # network call — see EnqueuePlan's own docstring.
    plan = build_enqueue_plan(facts.pr, sha, args.dry_run)
    if plan.dry_run:
        extra = f" (regenerated from main at {main_sha[:12]})" if touched_guarded else ""
        summary(f"policy-merge: DRY RUN. {where}: every condition holds{extra}; enqueuePullRequest was not called")
        return 0
    # The judging job never enqueues: an entry the built-in token enqueues
    # gets no merge_group run of `ci` and stalls (BUILT-IN TOKEN EVENTS START
    # NO WORKFLOW, above). It hands the judged node id and head sha to the
    # `enqueue` job, which mints the App token and calls the mutation.
    output(**handoff_outputs(plan, number))
    if touched_guarded:
        summary(f"policy-merge: every condition holds for PR #{number} (head {sha}); {', '.join(touched_guarded)} at "
                f"the head matched main's regeneration (main at {main_sha[:12]}) byte for byte; handed to the "
                "enqueue job")
    else:
        summary(f"policy-merge: every condition holds for PR #{number} (head {sha}); no guarded generated file was "
                "touched; handed to the enqueue job")
    return 0


def mint_scope_problems(text: str) -> list[str]:
    """Why policy-merge.yml's App-token mint is not scoped exactly as
    MINT_PERMISSIONS says, or []. Reads the workflow's text: every
    create-github-app-token step, its `permission-*` inputs, and that it sits
    in a job whose environment is merge-bot."""
    problems: list[str] = []
    lines = text.splitlines()
    steps = [i for i, l in enumerate(lines) if "actions/create-github-app-token@" in l]
    if len(steps) != 1:
        return [f"expected exactly one create-github-app-token step, found {len(steps)}"]
    i = steps[0]
    indent = len(lines[i]) - len(lines[i].lstrip(" -"))
    found: dict[str, str] = {}
    for l in lines[i + 1:]:
        stripped = l.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if len(l) - len(l.lstrip(" ")) <= indent - 2 or stripped.startswith("- "):
            break
        m = re.fullmatch(r"(permission-[a-z-]+):\s*(\S+)", stripped)
        if m:
            found[m.group(1)] = m.group(2)
    if found != MINT_PERMISSIONS:
        problems.append(f"the mint asks for {found}, not exactly {MINT_PERMISSIONS}")
    job_start = max((j for j in range(i) if re.fullmatch(r"  [a-z][a-z0-9_-]*:", lines[j])), default=None)
    if job_start is None or not any(lines[k].strip() == "environment: merge-bot" for k in range(job_start, i)):
        problems.append("the mint is not in a job whose environment is merge-bot")
    return problems


def cmd_check_mint_scope() -> int:
    with open(POLICY_YML, encoding="utf-8") as f:
        problems = mint_scope_problems(f.read())
    if problems:
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print("policy-merge-check: the merge-bot token mint is not scoped as MINT_PERMISSIONS says", file=sys.stderr)
        return 1
    print(f"policy-merge-check: ok, the merge-bot mint asks for exactly {MINT_PERMISSIONS} in the merge-bot job")
    return 0


def cmd_check_ci_names() -> int:
    with open(CI_YML, encoding="utf-8") as f:
        text = f.read()
    try:
        problems = ci_name_problems(text, REQUIRED_CHECKS) + api_surface_job_problems(text)
    except CiShapeError as e:
        print(f"policy-merge-check: {e}", file=sys.stderr)
        return 1
    if problems:
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print(f"policy-merge-check: REQUIRED_CHECKS and ci.yml's blocking jobs disagree, or the api-surface job "
              f"is not the non-blocking job this file reads ({len(problems)}).\n"
              "  Update REQUIRED_CHECKS (or API_SURFACE_CHECK) in scripts/policy-merge-check.py in the same\n"
              "  change, and ask an admin to update branch protection's required checks to match.", file=sys.stderr)
        return 1
    blocking = sum(1 for j in ci_jobs(text) if j.blocking)
    print(f"policy-merge-check: ok, REQUIRED_CHECKS names exactly ci.yml's {blocking} blocking job(s), and the "
          "api-surface job is there and non-blocking")
    return 0


def tracked_binding_sources() -> dict[str, str]:
    """path -> text for every tracked file of every binding's source tree,
    read from THIS checkout (`git ls-files`). Run by ci.yml on the pull
    request's own tree and by policy-merge.yml on main's; never on a head
    that policy-merge is judging."""
    out = subprocess.run(["git", "-C", ROOT, "ls-files", "-z"], capture_output=True, check=True).stdout
    sources: dict[str, str] = {}
    for path in out.decode("utf-8").split("\0"):
        if path and binding_of(path) is not None:
            with open(os.path.join(ROOT, path), encoding="utf-8", errors="replace") as f:
                sources[path] = f.read()
    return sources


def cmd_check_carve_out() -> int:
    sources = tracked_binding_sources()
    problems = carve_out_problems(sources)
    if problems:
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print(f"policy-merge-check: the security carve-out in PROTECTED_GLOBS disagrees with the tree "
              f"({len(problems)}). Verification code must stay protected whatever its API verdict.", file=sys.stderr)
        return 1
    covered = sorted(p for p in sources if any(g.verification and _covers(g, p) for g in PROTECTED_GLOBS))
    print(f"policy-merge-check: ok, every binding file the verification needles hit is in the carve-out "
          f"({len(covered)} file(s): {', '.join(covered)})")
    return 0


def cmd_print_protected() -> int:
    for g in PROTECTED_GLOBS:
        print(g.label())
    return 0


def cmd_check_guide() -> int:
    try:
        with open(GUIDE_PATH, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        print(f"policy-merge-check: could not read {GUIDE_PATH}: {e}", file=sys.stderr)
        return 2
    problems = guide_problems(text, protected_globs_guide_block())
    if problems:
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print(f"policy-merge-check: CONTRIBUTING.md's protected-glob block disagrees with PROTECTED_GLOBS "
              f"({len(problems)}). Regenerate the block between the markers from PROTECTED_GLOBS — see "
              "protected_globs_guide_block() — never by hand.", file=sys.stderr)
        return 1
    print("policy-merge-check: ok, CONTRIBUTING.md's protected-glob block matches PROTECTED_GLOBS exactly")
    return 0


# ------------------------------------------------------------------ selftest

REPO = "example/chtypes"
SHA = "a" * 40
OTHER_SHA = "b" * 40
GOOD = b"# Support\n\n| Line | Platforms |\n|---|---|\n| `25.8` | all |\n"


def _good_pr() -> dict:
    return {"number": 7, "state": "open", "draft": False, "mergeable": None, "changed_files": 1,
            "node_id": "PR_kwTEST00000007",
            "base": {"ref": "main", "repo": {"full_name": REPO}},
            "head": {"sha": SHA, "ref": "some-branch", "repo": {"full_name": REPO}}}


def _pr(**changes: object) -> dict:
    p = _good_pr()
    for k, v in changes.items():
        if k == "head_sha":
            p["head"]["sha"] = v
        elif k == "head_repo":
            p["head"]["repo"] = v
        elif k == "base_ref":
            p["base"]["ref"] = v
        else:
            p[k] = v
    return p


def _run(name: str, status: str = "completed", conclusion: str | None = "success",
         app: str = REQUIRED_CHECK_APP) -> dict:
    return {"name": name, "status": status, "conclusion": conclusion, "app": {"slug": app}}


def _good() -> dict:
    """A plain, unprotected change (README.md) that merges on its own: every
    required check green, nothing touches a protected glob, docs/support.md
    is not part of the diff at all."""
    return dict(
        repo=REPO, expected_head_sha=SHA, run_head_repo=REPO, pr=_good_pr(),
        files=[{"filename": "README.md", "status": "modified"}],
        check_runs=[_run(n) for n in REQUIRED_CHECKS]
        + [_run("divergences — docs/limitations.md's Known-divergences claims", conclusion="failure")],
        reviews=[], review_comments=[],
    )


def _with(**changes: object) -> dict:
    d = _good()
    d.update(changes)
    return d


def _checks(drop: str | None = None, replace: dict | None = None, extra: list | None = None) -> list[dict]:
    runs = [_run(n) for n in REQUIRED_CHECKS if n != drop]
    if replace:
        runs = [replace if r["name"] == replace["name"] else r for r in runs]
    return runs + (extra or [])


def _support_good() -> dict:
    """docs/support.md, alone, modified, with matching head/regenerated
    bytes — the classic regen-automerge case, preserved as condition 6."""
    d = _good()
    d["files"] = [{"filename": "docs/support.md", "status": "modified"}]
    d["head_bytes"] = {"docs/support.md": GOOD}
    d["regenerated"] = {"docs/support.md": GOOD}
    return d


ALL_SUITE_GOOD: dict[str, tuple[int, int]] = {label: (10, 0) for label in SUITE_LABELS}


def _tc(head: dict[str, tuple[int, int]], main: dict[str, tuple[int, int]],
        head_golden: int | None = 1, main_golden: int | None = 1) -> TestCountFacts:
    """A TestCountFacts for the selftest, defaulting the golden count to a
    matching, non-zero (1, 1) so a case about the SUITE side never
    incidentally also fails on the golden side, and vice versa."""
    return TestCountFacts(head=head, main=main, head_golden=head_golden, main_golden=main_golden)


def _sample_protected_path(g: ProtectedGlob) -> str:
    """A path _match_protected(g.pattern, ...) matches, derived from the
    pattern itself — never a hand-picked real file — so a new PROTECTED_GLOBS
    entry is exercised by the selftest without a matching selftest edit. The
    trailing-'**' sample carries no extension on purpose: scripts/lint-
    cited-paths.sh flags any tracked citation of a path, under one of a few
    watched top-level directories (scripts and include among them), that
    carries a real file extension and does not resolve to a file in this
    repository — and a fabricated sample under one of those names would be
    exactly that. Extension-free, it is outside what that rule matches, and
    _match_protected's trailing-'**' rule does not care about an extension
    anyway."""
    if "*" not in g.pattern:
        return g.pattern
    m = _TRAILING_DOUBLE_STAR.match(g.pattern)
    if m:
        return f"{m['dir']}/__selftest_sample__"
    m = _MIDDLE_DOUBLE_STAR.match(g.pattern)
    if m:
        return f"{m['dir']}/__selftest_sample__{m['ext']}"
    raise ValueError(f"selftest cannot derive a sample path for {g.pattern!r}")


CI_GOOD = """\
name: ci
on:
  pull_request:
jobs:
  alpha:
    name: "alpha — the first"
    runs-on: ubuntu-latest
    steps:
      - name: a step's name is not the job's
        continue-on-error: true
        run: echo
  beta:
    # name: a commented-out name is not the job's
    name: 'beta — it''s quoted'
    continue-on-error: false
    steps:
      - run: echo
  gamma:
    runs-on: ubuntu-latest
  delta:
    name: "delta — report only"
    continue-on-error: true
    steps:
      - run: echo
"""
CI_GOOD_REQUIRED = ("alpha — the first", "beta — it's quoted", "gamma")


def selftest() -> int:
    failures: list[str] = []

    def expect(label: str, got: Refusal | None, condition: str | None) -> None:
        if condition is None:
            if got is not None:
                failures.append(f"{label}: expected a pass, got {got.text()}")
        elif got is None:
            failures.append(f"{label}: expected a {condition} refusal, got a pass")
        elif got.condition != condition:
            failures.append(f"{label}: expected a {condition} refusal, got {got.text()}")

    # An unprotected, docs/support.md-free change merges on its own, at the
    # gate and at the merge.
    expect("an unprotected change passes, gate", decide(**_good()), None)
    expect("an unprotected change passes, merge (no guarded file touched)",
           decide(**_good(), head_bytes={}, regenerated={}), None)
    expect("mergeable unknown (null) is not a refusal", decide(**_with(pr=_pr(mergeable=None))), None)
    expect("mergeable true", decide(**_with(pr=_pr(mergeable=True))), None)
    expect("a manual re-check has no ci-run head repository", decide(**_with(run_head_repo=None)), None)
    expect("an approval after a change request clears it",
           decide(**_with(reviews=[{"state": "CHANGES_REQUESTED", "user": {"login": "r"}},
                                   {"state": "APPROVED", "user": {"login": "r"}}])), None)
    expect("a plain COMMENTED review with no review comment",
           decide(**_with(reviews=[{"state": "COMMENTED", "user": {"login": "r"}}])), None)
    expect("several unprotected files merge together",
           decide(**_with(pr=_pr(changed_files=2),
                          files=[{"filename": "README.md", "status": "modified"},
                                 {"filename": "docs/reference/bindings.md", "status": "added"}])), None)

    # condition 5 — protected, driven from PROTECTED_GLOBS itself, never a
    # hand-typed duplicate of it: every glob family refuses with no
    # api-surface verdict at all; a binding-source sample passes with that
    # binding's `changed=false`; every other entry — the security carve-out
    # among them — still refuses with EVERY binding's `changed=false`.
    all_unchanged = {b: ["false"] for b in BINDINGS}
    for g in PROTECTED_GLOBS:
        sample = _sample_protected_path(g)
        one = [{"filename": sample, "status": "modified"}]
        expect(f"protected: {g.pattern}", decide(**_with(pr=_pr(changed_files=1), files=one)), "protected")
        if g.binding is not None:
            expect(f"binding source {g.pattern} with its own changed=false verdict passes",
                   decide(**_with(files=one, api_verdicts={g.binding: ["false"]})), None)
        else:
            expect(f"{g.pattern} refuses whatever every binding's api-surface verdict says",
                   decide(**_with(files=one, api_verdicts=dict(all_unchanged))), "protected")
    expect("a go test file is the *_test.go exception, not protected (test-counts holds too)",
           decide(**_with(files=[{"filename": "go/chtypes/client_test.go", "status": "modified"}],
                          test_counts=_tc(dict(ALL_SUITE_GOOD), dict(ALL_SUITE_GOOD)))), None)
    expect("ts/biome.json alone (lint-ts's own config) is protected",
           decide(**_with(pr=_pr(changed_files=1), files=[{"filename": "ts/biome.json", "status": "modified"}])),
           "protected")
    expect("a rename whose OLD path was protected",
           decide(**_with(pr=_pr(changed_files=1),
                          files=[{"filename": "README.md", "status": "renamed",
                                  "previous_filename": ".github/workflows/ci.yml"}])), "protected")
    expect("a deletion of a protected file",
           decide(**_with(pr=_pr(changed_files=1),
                          files=[{"filename": "scripts/lint-public.sh", "status": "removed"}])), "protected")
    expect("a second, protected file changed alongside an unprotected one",
           decide(**_with(pr=_pr(changed_files=2),
                          files=[{"filename": "README.md", "status": "modified"},
                                 {"filename": "scripts/lint-public.sh", "status": "modified"}])), "protected")
    expect("a file list shorter than the pull request's own reported count",
           decide(**_with(pr=_pr(changed_files=2))), "protected")
    expect("no file at all is not a protected-glob hit (nothing to scan, nothing touched)",
           decide(**_with(pr=_pr(changed_files=0), files=[])), None)
    expect("protected outranks checks — a pull request that broke its own judge is refused before its "
           "(fabricated) checks are trusted",
           decide(**_with(check_runs=[], files=[{"filename": ".github/workflows/ci.yml", "status": "modified"}])),
           "protected")

    # The binding-source class (chtypes#285 §1), driven through decide()'s
    # own `api_verdicts` — and, where the question is what a log line means,
    # through parse_api_surface on a fabricated job log, never a hand-set
    # dict standing in for the parse.
    go_src = [{"filename": "go/chtypes/transform.go", "status": "modified"}]

    def log_of(*lines: str) -> str:
        return "\ufeff" + "".join(f"2026-10-01T00:49:13.2543384Z {line}\n" for line in lines)

    def verdicts(*lines: str, head: str = SHA) -> dict[str, list[str]]:
        return parse_api_surface(log_of(*lines), head)

    expect("api-surface: binding source with changed=false (parsed from a job log) passes",
           decide(**_with(files=go_src, api_verdicts=verdicts(api_surface_line("go", SHA, "false")))), None)
    expect("api-surface: changed=true refuses",
           decide(**_with(files=go_src, api_verdicts=verdicts(api_surface_line("go", SHA, "true")))), "protected")
    expect("api-surface: a missing verdict refuses (no api_verdicts at all)", decide(**_with(files=go_src)),
           "protected")
    expect("api-surface: a missing verdict refuses (a log with every binding but this one)",
           decide(**_with(files=go_src, api_verdicts=verdicts(*(api_surface_line(b, SHA, "false")
                                                                for b in BINDINGS if b != "go")))), "protected")
    expect("api-surface: a tool-error verdict refuses",
           decide(**_with(files=go_src, api_verdicts=verdicts(api_surface_line("go", SHA, "error")))), "protected")
    for label, line in (("an empty value", api_surface_line("go", SHA, "")),
                        ("a capitalized False", api_surface_line("go", SHA, "False")),
                        ("a short head sha", api_surface_line("go", SHA[:12], "false")),
                        ("a verdict quoted mid-line by a tool", "warning: x: " + api_surface_line("go", SHA, "false")),
                        ("a verdict for another head", api_surface_line("go", OTHER_SHA, "false")),
                        ("an untouched line", f"{API_SURFACE_PREFIX} binding=go head={SHA} untouched")):
        expect(f"api-surface: an unparseable or foreign verdict refuses ({label})",
               decide(**_with(files=go_src, api_verdicts=verdicts(line))), "protected")
    expect("api-surface: a later changed=false never outweighs an earlier changed=true",
           decide(**_with(files=go_src, api_verdicts=verdicts(api_surface_line("go", SHA, "true"),
                                                             api_surface_line("go", SHA, "false")))), "protected")
    two = [{"filename": "go/chtypes/transform.go", "status": "modified"},
           {"filename": "rust/src/schema.rs", "status": "modified"}]
    expect("api-surface: two bindings touched, only one verdict — refuses",
           decide(**_with(pr=_pr(changed_files=2), files=two, api_verdicts={"go": ["false"]})), "protected")
    expect("api-surface: two bindings touched, one changed — refuses",
           decide(**_with(pr=_pr(changed_files=2), files=two, api_verdicts={"go": ["false"], "rust": ["true"]})),
           "protected")
    expect("api-surface: two bindings touched, both unchanged — passes",
           decide(**_with(pr=_pr(changed_files=2), files=two, api_verdicts={"go": ["false"], "rust": ["false"]})),
           None)
    expect("api-surface: an untouched binding needs no verdict",
           decide(**_with(files=[{"filename": "python/src/chtypes/results.py", "status": "modified"}],
                          api_verdicts={"python": ["false"]})), None)
    expect("api-surface: a rename out of a binding's source counts by its old path",
           decide(**_with(files=[{"filename": "docs/x.md", "status": "renamed",
                                  "previous_filename": "ts/src/format.ts"}], api_verdicts={"go": ["false"]})),
           "protected")
    for g in PROTECTED_GLOBS:
        if g.verification:
            expect(f"api-surface: the carve-out {g.pattern} refuses even with its binding's changed=false",
                   decide(**_with(files=[{"filename": _sample_protected_path(g), "status": "modified"}],
                                  api_verdicts=dict(all_unchanged))), "protected")
    expect("api-surface: rust/build.rs (a build script cargo runs) refuses whatever the verdict",
           decide(**_with(files=[{"filename": "rust/build.rs", "status": "added"}], api_verdicts=dict(all_unchanged))),
           "protected")
    expect("api-surface: an unchanged API does not exempt the test-counts condition (a drop still refuses)",
           decide(**_with(pr=_pr(changed_files=2),
                          files=go_src + [{"filename": "go/chtypes/transform_test.go", "status": "modified"}],
                          api_verdicts={"go": ["false"]},
                          test_counts=_tc({**ALL_SUITE_GOOD, "go-no-artifacts": (9, 0)}, dict(ALL_SUITE_GOOD)))),
           "test-counts")
    expect("api-surface: an unchanged API and equal test counts pass together",
           decide(**_with(pr=_pr(changed_files=2),
                          files=go_src + [{"filename": "go/chtypes/transform_test.go", "status": "modified"}],
                          api_verdicts={"go": ["false"]},
                          test_counts=_tc(dict(ALL_SUITE_GOOD), dict(ALL_SUITE_GOOD)))), None)
    if touched_bindings(two) != {"go": "go/chtypes/transform.go", "rust": "rust/src/schema.rs"}:
        failures.append(f"touched_bindings: read {touched_bindings(two)!r}")
    if binding_of("go/chtypes/transform_test.go") is not None or binding_of("python/tests/x.py") is not None:
        failures.append("binding_of: a test file was read as binding source")

    # ci.yml's api-surface job: present under the exact name this file reads,
    # and non-blocking — derived from text, never a hand-set answer.
    job = f'  surface:\n    name: "{API_SURFACE_CHECK}"\n    continue-on-error: true\n    runs-on: x\n'
    if api_surface_job_problems(CI_GOOD + job):
        failures.append(f"api_surface_job_problems: refused a non-blocking job: {api_surface_job_problems(CI_GOOD + job)}")
    if not api_surface_job_problems(CI_GOOD + job.replace("continue-on-error: true", "continue-on-error: false")):
        failures.append("api_surface_job_problems: accepted a BLOCKING api-surface job")
    if not api_surface_job_problems(CI_GOOD):
        failures.append("api_surface_job_problems: accepted a ci.yml with no api-surface job")

    # The security carve-out's derivation, on fabricated trees: every entry
    # present with a needle in it is clean; a needle in an uncovered file, a
    # carve-out entry that covers nothing, and a needle only in a comment are
    # each read the right way.
    tree = {"go/chtypes/fetch_sign.go": 'import "crypto/ed25519"',
            "go/chtypes/fetch.go": 'import "crypto/sha256"',
            "go/chtypes/registry_path.go": 'envAllowUnsign = "CHTYPES_ALLOW_UNSIGNED"',
            "go/chtypes/multiversion.go": "if err := checkLibraryBytes(path); err != nil {",
            "go/chtypes/transform.go": "package chtypes",
            "python/src/chtypes/_ed25519.py": "import hashlib",
            "python/src/chtypes/fetch.py": "from ._ed25519 import verify",
            "python/src/chtypes/_manifest.py": "def verify_library(d):",
            "python/src/chtypes/registry.py": "check_library_bytes(entry, manifest)",
            "ts/src/fetch.ts": "import { createHash } from 'node:crypto';",
            "ts/src/registry.ts": "verifyChecksum(libPath, manifest);",
            "rust/src/fetch/trust.rs": "use ed25519_dalek::VerifyingKey;",
            "rust/src/digest.rs": "use sha2::{Digest, Sha256};",
            "rust/src/registry.rs": "let actual = crate::digest::sha256_file(&path);",
            "rust/src/lib.rs": "pub mod fetch;"}
    if carve_out_problems(tree):
        failures.append(f"carve_out_problems: refused a tree that matches the carve-out: {carve_out_problems(tree)}")
    if not carve_out_problems({**tree, "go/chtypes/sneaky.go": 'import "crypto/sha256"'}):
        failures.append("carve_out_problems: a sha256 import in an uncovered Go file was not caught")
    if not carve_out_problems({**tree, "ts/src/keys.ts": f"const K = '{'ab' * 32}';"}):
        failures.append("carve_out_problems: a 64-hex key literal in an uncovered TS file was not caught")
    if carve_out_problems({**tree, "go/chtypes/doc.go": '// we import "crypto/sha256" elsewhere'}):
        failures.append("carve_out_problems: a needle inside a comment only was read as verification code")
    if not carve_out_problems({k: v for k, v in tree.items() if k != "go/chtypes/fetch_sign.go"}):
        failures.append("carve_out_problems: a carve-out entry covering no file was not caught")
    if not carve_out_problems({k: v for k, v in tree.items() if not k.startswith("rust/src/fetch/")}):
        failures.append("carve_out_problems: rust/src/fetch/** covering no file was not caught")

    # build_enqueue_plan — a pure function of facts already in hand, so
    # "enqueue is called with the judged head sha" and "dry run never calls
    # it" are provable without a network call (enqueue_pull_request itself,
    # the actual GraphQL mutation, is untested here — same as every other
    # `gh api` wrapper in this file).
    plan = build_enqueue_plan(_good_pr(), SHA, dry_run=False)
    if plan.dry_run or plan.head_sha != SHA or plan.node_id != _good_pr()["node_id"]:
        failures.append(f"build_enqueue_plan: did not carry the judged node_id/head_sha for a real run: {plan!r}")
    dry_plan = build_enqueue_plan(_good_pr(), SHA, dry_run=True)
    if not dry_plan.dry_run:
        failures.append("build_enqueue_plan: dry_run was not carried through")
    try:
        build_enqueue_plan({"number": 7}, SHA, dry_run=False)
        failures.append("build_enqueue_plan: a pull request object with no node_id was accepted")
    except ValueError:
        pass
    # The hand-off to the App-token job carries exactly the judged node id
    # and head sha, and enqueue-as-bot refuses anything else before the App
    # token is used.
    handed = handoff_outputs(plan, 7)
    if handed != {"enqueue": "1", "pr": "7", "node_id": _good_pr()["node_id"], "head_sha": SHA}:
        failures.append(f"handoff_outputs: did not hand on the judged node_id/head_sha: {handed!r}")
    if bot_args_problem(_good_pr()["node_id"], SHA, "7") is not None:
        failures.append("bot_args_problem: refused a well-formed node id, head sha and number")
    good_yml = ("jobs:\n  enqueue:\n    environment: merge-bot\n    steps:\n"
                "      - uses: actions/create-github-app-token@abc # v3\n        id: app\n        with:\n"
                "          app-id: x\n          permission-contents: write\n          permission-pull-requests: write\n"
                "      - name: next\n        run: true\n")
    if mint_scope_problems(good_yml):
        failures.append(f"mint_scope_problems: refused the exact scope: {mint_scope_problems(good_yml)}")
    wider = good_yml.replace("          permission-pull-requests: write\n",
                             "          permission-pull-requests: write\n          permission-workflows: write\n")
    if not mint_scope_problems(wider):
        failures.append("mint_scope_problems: accepted a mint that also asks for permission-workflows")
    if not mint_scope_problems(good_yml.replace("          permission-contents: write\n", "")):
        failures.append("mint_scope_problems: accepted a mint with no permission list (the App's full set)")
    if not mint_scope_problems(good_yml.replace("    environment: merge-bot\n", "")):
        failures.append("mint_scope_problems: accepted a mint outside the merge-bot environment")
    # A skipped / failed / unfinished job is never read for a
    # count: a skipped job has no log (404), so reading it was an exit-2
    # error on #311 instead of a refusal.
    runs_mix = [_run("a"), _run("b", conclusion="skipped"), _run("c", conclusion="failure"),
                _run("d", status="in_progress", conclusion=None)]
    for i, r in enumerate(runs_mix):
        r["id"] = 100 + i
    if readable_job_ids(runs_mix) != {"a": 100}:
        failures.append(f"readable_job_ids: kept a job that has no usable log: {readable_job_ids(runs_mix)!r}")
    for bad in ((_good_pr()["node_id"], "abc", "7"), (_good_pr()["node_id"], SHA.upper(), "7"),
                ("", SHA, "7"), ("PR_x y", SHA, "7"), (_good_pr()["node_id"], SHA, "7; rm")):
        if bot_args_problem(*bad) is None:
            failures.append(f"bot_args_problem: accepted {bad!r}")
    # cmd_enqueue's own control flow is what makes "dry run never calls the
    # mutation" and "a moved head refuses before the mutation" true: the
    # dry-run branch returns before enqueue_pull_request() is ever
    # referenced, and decide()'s freshly re-checked `stale` condition (a
    # moved head, proven above) already returns before build_enqueue_plan is
    # reached at all — neither is a claim a pure-function selftest can make
    # about a live network call, so it is a property of the code path,
    # confirmed by reading cmd_enqueue: the `if plan.dry_run: ...; return 0`
    # line precedes `enqueue_pull_request(...)`, and both live entirely after
    # `if refusal: return refuse(refusal, where)`.

    # condition 6 — bytes, only when docs/support.md is touched
    expect("docs/support.md untouched: no byte guard at all",
           decide(**_with(files=[{"filename": "README.md", "status": "modified"}],
                          head_bytes={}, regenerated={})), None)
    sg_gate = _support_good()
    sg_gate.pop("head_bytes")
    sg_gate.pop("regenerated")
    expect("docs/support.md modified, matching, gate (no regenerated bytes yet)", decide(**sg_gate), None)
    expect("docs/support.md modified, matching, merge", decide(**_support_good()), None)
    sg = _support_good()
    one_byte = bytearray(GOOD)
    one_byte[-3] ^= 0x01
    expect("docs/support.md modified, a one-byte difference",
           decide(**{**sg, "head_bytes": {"docs/support.md": bytes(one_byte)}}), "bytes")
    expect("docs/support.md modified, a missing trailing newline",
           decide(**{**sg, "head_bytes": {"docs/support.md": GOOD[:-1]}}), "bytes")
    expect("docs/support.md modified, one extra byte at the end",
           decide(**{**sg, "head_bytes": {"docs/support.md": GOOD + b"\n"}}), "bytes")
    expect("docs/support.md added, not modified",
           decide(**{**sg, "files": [{"filename": "docs/support.md", "status": "added"}]}), "bytes")
    sg_gate_added = {k: v for k, v in sg.items() if k not in ("head_bytes", "regenerated")}
    sg_gate_added["files"] = [{"filename": "docs/support.md", "status": "added"}]
    expect("docs/support.md added, not modified, caught at GATE time with no regenerated bytes at all",
           decide(**sg_gate_added), "bytes")
    expect("docs/support.md deleted, not modified",
           decide(**{**sg, "files": [{"filename": "docs/support.md", "status": "removed"}]}), "bytes")
    expect("docs/support.md renamed into place, not modified",
           decide(**{**sg, "files": [{"filename": "docs/support.md", "status": "renamed",
                                      "previous_filename": "README.md"}]}), "bytes")
    if first_difference(GOOD, bytes(one_byte)) != (len(GOOD) - 3, GOOD.count(b"\n")):
        failures.append(f"first_difference located the planted byte wrongly: {first_difference(GOOD, bytes(one_byte))}")

    # condition 2 — required checks
    required_one = REQUIRED_CHECKS[3]
    expect("a missing required check", decide(**_with(check_runs=_checks(drop=required_one))), "checks")
    expect("a failing required check",
           decide(**_with(check_runs=_checks(replace=_run(required_one, conclusion="failure")))), "checks")
    expect("a pending required check",
           decide(**_with(check_runs=_checks(replace=_run(required_one, status="in_progress", conclusion=None)))),
           "checks")
    expect("a queued required check",
           decide(**_with(check_runs=_checks(replace=_run(required_one, status="queued", conclusion=None)))),
           "checks")
    expect("a run not completed is not a pass, whatever its conclusion field says",
           decide(**_with(check_runs=_checks(replace=_run(required_one, status="in_progress")))), "checks")
    expect("a skipped required check is not a pass",
           decide(**_with(check_runs=_checks(replace=_run(required_one, conclusion="skipped")))), "checks")
    expect("a timed-out required check",
           decide(**_with(check_runs=_checks(replace=_run(required_one, conclusion="timed_out")))), "checks")
    expect("a required name from another app satisfies nothing",
           decide(**_with(check_runs=_checks(drop=required_one, extra=[_run(required_one, app="someone-else")]))),
           "checks")
    expect("two runs of one required check, one failed",
           decide(**_with(check_runs=_checks(extra=[_run(required_one, conclusion="failure")]))), "checks")
    expect("no check runs at all", decide(**_with(check_runs=[])), "checks")

    # test-counts (condition 7, chtypes#285 §1b) — driven directly through
    # decide()'s own `test_counts` parameter, the same way condition 6 is
    # driven through `head_bytes`/`regenerated` rather than a real job-log
    # fetch: gather_test_counts (the network half) is not exercised here, on
    # the same "a pure decision is what --selftest proves" principle as the
    # rest of this file.
    a_test_path = "python/tests/test_foo.py"
    expect("test-counts: every suite (and the golden count) equal, a test path touched",
           decide(**_with(files=[{"filename": a_test_path, "status": "modified"}],
                          test_counts=_tc(dict(ALL_SUITE_GOOD), dict(ALL_SUITE_GOOD)))), None)
    expect("test-counts: one suite rises, the rest equal, still passes",
           decide(**_with(files=[{"filename": a_test_path, "status": "modified"}],
                          test_counts=_tc({**ALL_SUITE_GOOD, "go-no-artifacts": (11, 0)},
                                          dict(ALL_SUITE_GOOD)))), None)
    expect("test-counts: a drop in one suite refuses even while another rises",
           decide(**_with(files=[{"filename": "rust/tests/foo.rs", "status": "modified"}],
                          test_counts=_tc({**ALL_SUITE_GOOD, "rust-artifacts": (5, 0),
                                           "go-no-artifacts": (99, 0)},
                                          dict(ALL_SUITE_GOOD)))), "test-counts")
    expect("test-counts: a count missing on the head refuses",
           decide(**_with(files=[{"filename": "ts/test/foo.test.ts", "status": "modified"}],
                          test_counts=_tc({k: v for k, v in ALL_SUITE_GOOD.items() if k != "ts-artifacts"},
                                          dict(ALL_SUITE_GOOD)))), "test-counts")
    expect("test-counts: a count missing on main (main's push run predates this feature) refuses",
           decide(**_with(files=[{"filename": a_test_path, "status": "modified"}],
                          test_counts=_tc(dict(ALL_SUITE_GOOD), {}))), "test-counts")
    expect("test-counts: a case moved from ran to skipped is a drop in ran, and refuses",
           decide(**_with(files=[{"filename": "go/chtypes/foo_test.go", "status": "modified"}],
                          test_counts=_tc({**ALL_SUITE_GOOD, "go-artifacts": (9, 1)},
                                          {**ALL_SUITE_GOOD, "go-artifacts": (10, 0)}))), "test-counts")
    expect("test-counts: the golden-case count drops",
           decide(**_with(files=[{"filename": "tests/fixtures/fetch/x", "status": "modified"}],
                          test_counts=_tc(dict(ALL_SUITE_GOOD), dict(ALL_SUITE_GOOD),
                                          head_golden=3, main_golden=5))), "test-counts")
    expect("test-counts: the golden-case count missing on the head refuses",
           decide(**_with(files=[{"filename": a_test_path, "status": "modified"}],
                          test_counts=_tc(dict(ALL_SUITE_GOOD), dict(ALL_SUITE_GOOD),
                                          head_golden=None))), "test-counts")
    expect("test-counts: a pull request touching no test or fixture path skips the condition "
           "entirely, even with a real drop sitting in test_counts",
           decide(**_with(files=[{"filename": "README.md", "status": "modified"}],
                          test_counts=_tc({**ALL_SUITE_GOOD, "rust-artifacts": (0, 0)},
                                          dict(ALL_SUITE_GOOD)))), None)
    expect("test-counts: no test_counts argument at all, on a PR that touches no test path, still passes "
           "(the default TestCountFacts is never consulted)",
           decide(**_with(files=[{"filename": "README.md", "status": "modified"}])), None)

    # is_test_or_fixture_path / touches_test_or_fixture_path — concrete sanity
    # checks, independent of the decide()-level cases above.
    if not is_test_or_fixture_path("go/chtypes/client_test.go"):
        failures.append("is_test_or_fixture_path: a go _test.go file was not recognized")
    if not is_test_or_fixture_path("go/cmd/chtypes/main_test.go"):
        failures.append("is_test_or_fixture_path: a nested go _test.go file was not recognized")
    if is_test_or_fixture_path("go/chtypes/client.go"):
        failures.append("is_test_or_fixture_path: a non-test go file was recognized as one")
    for p in ("python/tests/test_golden.py", "ts/test/golden.test.ts", "rust/tests/golden.rs",
             "tests/fixtures/fetch/signed/index.json"):
        if not is_test_or_fixture_path(p):
            failures.append(f"is_test_or_fixture_path: {p!r} was not recognized")
    for p in ("python/src/chtypes/fetch.py", "tests/parity/manifest.json", "README.md"):
        if is_test_or_fixture_path(p):
            failures.append(f"is_test_or_fixture_path: {p!r} was wrongly recognized as a test/fixture path")
    if not touches_test_or_fixture_path([{"filename": "README.md", "status": "renamed",
                                          "previous_filename": "rust/tests/old.rs"}]):
        failures.append("touches_test_or_fixture_path: a rename's OLD path under rust/tests/ was not caught")
    if touches_test_or_fixture_path([{"filename": "README.md", "status": "modified"}]):
        failures.append("touches_test_or_fixture_path: an unrelated file was read as a test/fixture touch")

    # test_count_problems — the pure comparison, independent of decide()'s
    # own plumbing above.
    if test_count_problems(dict(ALL_SUITE_GOOD), dict(ALL_SUITE_GOOD), 1, 1):
        failures.append("test_count_problems: an all-equal input was refused")
    if not test_count_problems({**ALL_SUITE_GOOD, "python-no-artifacts": (1, 0)}, dict(ALL_SUITE_GOOD), 1, 1):
        failures.append("test_count_problems: a single-suite drop was not caught")
    if test_count_problems(dict(ALL_SUITE_GOOD), dict(ALL_SUITE_GOOD), 5, 5):
        failures.append("test_count_problems: equal golden counts were refused")

    # condition 1 — fork
    expect("the ci run's head is a fork", decide(**_with(run_head_repo="someone/chtypes")), "fork")
    expect("the pull request's head is a fork",
           decide(**_with(pr=_pr(head_repo={"full_name": "someone/chtypes"}))), "fork")
    expect("the pull request's head repository was deleted", decide(**_with(pr=_pr(head_repo=None))), "fork")

    # stale head
    expect("a stale head", decide(**_with(pr=_pr(head_sha=OTHER_SHA))), "stale")

    # pull-request state
    expect("a closed pull request", decide(**_with(pr=_pr(state="closed"))), "pull-request")
    expect("a pull request against another branch", decide(**_with(pr=_pr(base_ref="release"))), "pull-request")
    expect("a draft", decide(**_with(pr=_pr(draft=True))), "pull-request")

    # mergeable, review
    expect("a known merge conflict", decide(**_with(pr=_pr(mergeable=False))), "mergeable")
    expect("a review comment", decide(**_with(review_comments=[{"id": 1}])), "review")
    expect("changes requested",
           decide(**_with(reviews=[{"state": "APPROVED", "user": {"login": "r"}},
                                   {"state": "CHANGES_REQUESTED", "user": {"login": "r"}}])), "review")

    # The ORDER is part of the contract: a fork is reported as a fork even
    # when every other condition also fails.
    everything_wrong = _with(run_head_repo="someone/chtypes", pr=_pr(state="closed", head_sha=OTHER_SHA),
                             check_runs=[], files=[{"filename": "scripts/fetch.sh", "status": "added"}])
    expect("a fork outranks everything else", decide(**everything_wrong), "fork")

    # select_pr — which pull request a ci run belongs to
    def sel(run_head_repo: str, prs: list[dict]) -> Refusal | None:
        return select_pr(REPO, run_head_repo, prs)[1]
    open_main = {"number": 7, "state": "open", "base": {"ref": "main"}}
    expect("select: one open pull request against main", sel(REPO, [open_main]), None)
    if select_pr(REPO, REPO, [open_main])[0] != 7:
        failures.append("select: did not return the pull request's number")
    expect("select: a fork", sel("someone/chtypes", [open_main]), "fork")
    expect("select: none", sel(REPO, []), "pull-request")
    expect("select: only a closed one", sel(REPO, [dict(open_main, state="closed")]), "pull-request")
    expect("select: only one against another branch",
           sel(REPO, [dict(open_main, base={"ref": "release"})]), "pull-request")
    expect("select: two open ones", sel(REPO, [open_main, dict(open_main, number=8)]), "pull-request")
    expect("select: a closed one beside the open one",
           sel(REPO, [dict(open_main, number=6, state="closed"), open_main]), None)

    # _match_protected / is_protected — concrete sanity checks, independent
    # of the derived per-glob loop above.
    if not _match_protected(".github/**", ".github/workflows/ci.yml"):
        failures.append("_match_protected: .github/** did not match a nested workflow file")
    if _match_protected(".github/**", "github/workflows/ci.yml"):
        failures.append("_match_protected: .github/** matched a path missing the leading dot (no path boundary)")
    if not _match_protected("go/**/*.go", "go/chtypes/client.go"):
        failures.append("_match_protected: go/**/*.go did not match a nested .go file")
    if _match_protected("go/**/*.go", "go/chtypes/client.txt"):
        failures.append("_match_protected: go/**/*.go matched a non-.go file")
    if _match_protected("go/**/*.go", "gochtypes/client.go"):
        failures.append("_match_protected: go/**/*.go matched a path without the go/ segment boundary")
    if is_protected("go/chtypes/client_test.go") is not None:
        failures.append("is_protected: a _test.go file under go/ was protected despite the except_suffix rule")
    if is_protected("README.md") is not None:
        failures.append("is_protected: README.md was protected")
    for g in PROTECTED_GLOBS:
        try:
            _match_protected(g.pattern, "irrelevant")
        except ValueError as e:
            failures.append(f"_match_protected: PROTECTED_GLOBS entry {g.pattern!r} is an unsupported shape: {e}")

    # the CONTRIBUTING.md guide/constant divergence case (chtypes#280: pin one
    # selftest case that fails if the guide and the constant diverge).
    expected_block = protected_globs_guide_block()
    matching_guide = f"# Contributing\n\nSome prose.\n\n{expected_block}\n\nMore prose.\n"
    if guide_problems(matching_guide, expected_block):
        failures.append("guide_problems: an exactly matching block was flagged as diverging")
    if not guide_problems("# Contributing\n\nNo generated block here.\n", expected_block):
        failures.append("guide_problems: a guide with no markers at all was not caught")
    stale_guide = matching_guide.replace("`.github/**`", "`.github/*`", 1)
    if not guide_problems(stale_guide, expected_block):
        failures.append("guide_problems: a hand-edited glob inside the block was not caught")
    lines = matching_guide.splitlines()
    drop_at = next(i for i, line in enumerate(lines) if line.startswith("- `"))
    truncated_guide = "\n".join(lines[:drop_at] + lines[drop_at + 1:])
    if not guide_problems(truncated_guide, expected_block):
        failures.append("guide_problems: a line dropped from the guide's block was not caught")

    # the contents API and gh's error line
    if decode_contents({"type": "file", "encoding": "base64", "size": len(GOOD),
                        "content": base64.encodebytes(GOOD).decode()}, "p") != GOOD:
        failures.append("decode_contents: a line-wrapped base64 payload did not round-trip")
    for bad, label in (({"type": "file", "encoding": "none", "size": 5, "content": ""}, "a file over 1 MB"),
                       ({"type": "dir"}, "a directory"),
                       ({"type": "file", "encoding": "base64", "size": 99,
                         "content": base64.b64encode(GOOD).decode()}, "a size mismatch")):
        try:
            decode_contents(bad, "p")
            failures.append(f"decode_contents: {label} was accepted")
        except ValueError:
            pass
    if http_status("gh: Head branch was modified. Review and try the merge again. (HTTP 409)") != 409:
        failures.append("http_status: did not read 409 from gh's error line")
    if http_status("error connecting to api.github.com") is not None:
        failures.append("http_status: invented a status for a network error")

    # ci.yml's blocking jobs: the derivation itself, driven from text, never a
    # hand-set answer about the real file.
    jobs = {j.job_id: j for j in ci_jobs(CI_GOOD)}
    if sorted(jobs) != ["alpha", "beta", "delta", "gamma"]:
        failures.append(f"ci_jobs: read jobs {sorted(jobs)}")
    else:
        if jobs["alpha"].name != "alpha — the first" or not jobs["alpha"].blocking:
            failures.append("ci_jobs: a STEP's name or continue-on-error was read as the job's")
        if jobs["beta"].name != "beta — it's quoted":
            failures.append(f"ci_jobs: a commented name or a single-quoted one was misread: {jobs['beta'].name!r}")
        if jobs["gamma"].name != "gamma":
            failures.append("ci_jobs: a job without a name must carry its id as its check-run name")
        if jobs["delta"].blocking:
            failures.append("ci_jobs: a job-level continue-on-error: true was not read as non-blocking")
    agreeing = ci_name_problems(CI_GOOD, CI_GOOD_REQUIRED)
    if agreeing:
        failures.append(f"ci_name_problems: an agreeing list was refused: {agreeing}")
    for label, required in (
        ("a renamed job", ("alpha — renamed", "beta — it's quoted", "gamma")),
        ("a blocking job missing from the list", ("alpha — the first", "beta — it's quoted")),
        ("a non-blocking job in the list", CI_GOOD_REQUIRED + ("delta — report only",)),
        ("a name listed twice", CI_GOOD_REQUIRED + ("gamma",)),
    ):
        if not ci_name_problems(CI_GOOD, required):
            failures.append(f"ci_name_problems: {label} was not caught")
    renamed_in_ci = CI_GOOD.replace('"alpha — the first"', '"alpha — the first, renamed"')
    if not ci_name_problems(renamed_in_ci, CI_GOOD_REQUIRED):
        failures.append("ci_name_problems: a job renamed in ci.yml was not caught")
    new_job = CI_GOOD + "  epsilon:\n    name: \"epsilon — new and blocking\"\n    runs-on: ubuntu-latest\n"
    if not ci_name_problems(new_job, CI_GOOD_REQUIRED):
        failures.append("ci_name_problems: a new blocking job in ci.yml was not caught")
    for label, text in (
        ("an expression as a job name", CI_GOOD.replace('"alpha — the first"', '"alpha ${{ matrix.os }}"')),
        ("a matrix", CI_GOOD.replace("  gamma:\n", "  gamma:\n    strategy:\n      matrix: {os: [a, b]}\n")),
        ("an expression as continue-on-error",
         CI_GOOD.replace("continue-on-error: false", "continue-on-error: ${{ github.event_name == 'push' }}")),
    ):
        try:
            ci_jobs(text)
            failures.append(f"ci_jobs: {label} was read instead of refused")
        except CiShapeError:
            pass

    if failures:
        for f in failures:
            print(f"SELFTEST FAILED: {f}", file=sys.stderr)
        return 1
    print("policy-merge-check: selftest ok — every PROTECTED_GLOBS family refuses (incl. a rename's old path and "
          "a deletion, and ts/biome.json by name), docs/support.md's byte guard refuses a mismatch and a "
          "non-modification, a missing, failing or pending required check, a fork, a stale head, a draft, a "
          "conflict and a review each refuse; an all-good input passes; build_enqueue_plan carries the judged "
          "head sha and node_id and never reaches the mutation on a dry run; the hand-off to the enqueue job carries "
          "exactly that node id and head sha, and enqueue-as-bot refuses malformed ones; only a job that completed "
          "with success is read for a count; the merge-bot mint must ask for "
          "exactly contents and pull-requests write inside the merge-bot job; ci.yml's blocking jobs and the "
          "CONTRIBUTING.md guide block are both derived from their source, never hand-set; test-counts "
          "(chtypes#285 §1b) refuses a single-suite drop even while another suite rises, a missing count on "
          "either side, a ran-to-skipped shift, and a golden-case-count drop, passes an equal or rising count, "
          "and is skipped entirely — at zero API cost — for a pull request that touches no test or fixture path; "
          "binding source (chtypes#285 §1) passes on its own binding's changed=false, read from a job log, and "
          "refuses on changed=true, a missing, tool-error or unparseable verdict, a verdict for another head or "
          "quoted mid-line, a later false after a true, and a second touched binding without its own false; the "
          "security carve-out and rust/build.rs refuse whatever every verdict says; test-counts still applies; the "
          "api-surface job must exist under the name read and be non-blocking; and the carve-out's derivation "
          "catches verification code outside it and an entry that covers nothing")
    return 0


# ----------------------------------------------------------------------- main


def cmd_enqueue_as_bot(args: argparse.Namespace) -> int:
    """The `enqueue` job's one call: enqueuePullRequest with the App
    installation token in GH_TOKEN, pinned to the judged head. It reads no
    repository file and judges nothing; the judging job already did."""
    problem = bot_args_problem(args.node_id, args.head_sha, args.pr)
    if problem:
        print(f"policy-merge-check: {problem}", file=sys.stderr)
        return 2
    result = enqueue_pull_request(args.node_id, args.head_sha)
    entry = ((result.get("data") or {}).get("enqueuePullRequest") or {}).get("mergeQueueEntry")
    if not entry:
        print(f"policy-merge-check: the enqueue mutation returned {result!r}", file=sys.stderr)
        return 2
    summary(f"policy-merge: enqueued PR #{args.pr} (head {args.head_sha}) as the merge-bot App; the queue's "
            "merge_group run of ci is what happens next")
    return 0


def main(argv: list[str]) -> int:
    if argv == ["--selftest"]:
        return selftest()
    if argv == ["--check-ci-names"]:
        return cmd_check_ci_names()
    if argv == ["--print-protected"]:
        return cmd_print_protected()
    if argv == ["--check-guide"]:
        return cmd_check_guide()
    if argv == ["--check-mint-scope"]:
        return cmd_check_mint_scope()
    if argv == ["--check-carve-out"]:
        return cmd_check_carve_out()
    parser = argparse.ArgumentParser(prog="policy-merge-check.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gate", help="every condition but the byte comparison")
    g.add_argument("--repo", required=True)
    g.add_argument("--pr", type=int)
    g.add_argument("--run-head-sha")
    g.add_argument("--run-head-repo")
    m = sub.add_parser("enqueue", help="every condition, then hand the judged head to the enqueue job")
    m.add_argument("--repo", required=True)
    m.add_argument("--pr", type=int, required=True)
    m.add_argument("--head-sha", required=True)
    m.add_argument("--run-head-repo", default="")
    m.add_argument("--regenerated", action="append", default=[], metavar="PATH=FILE")
    m.add_argument("--dry-run", action="store_true")
    q = sub.add_parser("enqueue-as-bot", help="enqueuePullRequest with the App token in GH_TOKEN")
    q.add_argument("--pr", required=True)
    q.add_argument("--node-id", required=True)
    q.add_argument("--head-sha", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "gate":
        if (args.pr is None) == (args.run_head_sha is None):
            parser.error("gate needs exactly one of --pr and --run-head-sha")
        if args.run_head_sha is not None and args.run_head_repo is None:
            parser.error("--run-head-sha needs --run-head-repo")
    try:
        if args.cmd == "gate":
            return cmd_gate(args)
        if args.cmd == "enqueue-as-bot":
            return cmd_enqueue_as_bot(args)
        return cmd_enqueue(args)
    except Exception as e:  # noqa: BLE001 — every unexpected failure must read as one, never as a refusal
        print(f"policy-merge-check: {type(e).__name__}: {e}", file=sys.stderr)
        summary(f"policy-merge: ERROR ({type(e).__name__}), nothing was merged: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
