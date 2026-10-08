package ocifetch

// channel.go — the fetch contract this build speaks. A 2.0.0-dev SDK speaks
// the ABI v2 dev channel (spec/abi-v2/docs.md, rules r5 and r6), which is the
// v1 fetch contract (docs/guides/fetch-v1.md) narrowed in five ways:
//
//   - it fetches ONLY from DevChannelBase and trusts ONLY the staging key
//     (DevKeyHex, id DevKeyID); the release key is not in its trust list;
//   - it has no override: CHTYPES_ARTIFACTS_URL, CHTYPES_TRUSTED_KEYS and
//     CHTYPES_ALLOW_UNSIGNED, and the options that set a base, a trust list or
//     an unsigned fetch, are ignored, each with one loud warning per process;
//   - it refuses --lock, --frozen and update, and their options, before any
//     network call: a dev build is replaceable and a superseded one expires,
//     so nothing may pin one;
//   - its cache is one no released 1.x reader ever reads: verified.json records
//     are schema 2, the default root is ${XDG_CACHE_HOME:-~/.cache}/chtypes/v2-dev,
//     and an explicit cache (CHTYPES_CACHE, --cache, the CacheDir option) is
//     used through its subroot <cache>/v2-dev, never as a whole layout;
//   - a signed predicate must say abi 2.
//
// It also resolves one tag the v1 contract never asks for: a version request
// fetches <tag>--fp-<its own fingerprint> first, the newest dev build of the
// ABI this module speaks, and falls back to <tag> only when no base has that
// alias (docs/guides/fetch-v1.md §3, "The dev channel's alias step"), so a dev
// build of a newer fingerprint never strands this one. Its cache lookups
// (ResolveInstalled, and Ensure's offline answer and its monotonic rule) see
// only the records whose signed abi_fingerprint is its own: a build of another
// fingerprint in a shared cache is never returned and never kept over this
// one's own, and it is never removed or rewritten either. It is automatic, with
// no override; the trust checks are unchanged, and a listing never shows an
// alias.
//
// The v1 contract itself stays in this package, unchanged, because the fetch-v1
// conformance cases (tests/fixtures/fetch-v1) are its specification and the
// dev channel shares every other rule with it. Only a test binary reaches it:
// UseFetchV1ForTests and AllowOverridesForTests panic anywhere else
// (testing.Testing), so neither is an override a user can reach.

import (
	"fmt"
	"io"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
)

// The dev channel's registry and key (spec/abi-v2/docs.md, rule r6).
const (
	// DevChannelBase is the only base a 2.0.0-dev SDK fetches from.
	DevChannelBase = `https://registry-staging.wavehouse.dev/chtypes/v2-dev`
	// DevKeyID is the staging key's id (KeyIDAlgorithm over DevKeyHex).
	DevKeyID = `824345f9bcf8e5bf`
	// DevKeyHex is the staging key, the only key a 2.0.0-dev SDK trusts: an
	// ed25519 public key, raw 32 bytes as lowercase hex.
	DevKeyHex = "5cd30c53c65a1ebc2d85836a41deb06661bb0ae7b658adb9eb116ec2db8e9b1c"
	// DevCacheDir is the dev channel's cache directory name: the default
	// root's last element and an explicit cache's subroot (rule r5).
	DevCacheDir = `v2-dev`
	// DevRecordSchema is the verified.json schema the dev channel writes, and
	// the only one it reads (rule r5).
	DevRecordSchema = 2
	// DevABIGeneration is the abi a dev predicate must carry.
	DevABIGeneration = 2
)

// EnvOfflineName is the environment twin of the Offline option: CHTYPES_OFFLINE=1
// reads the cache only and makes no request (public issue #528). It names no
// source, so rule r6 holds. A fetch layer constant, not a generated one: the
// generated set is the v1 fetch contract's.
const EnvOfflineName = `CHTYPES_OFFLINE`

// AliasSeparator joins a tag and a fingerprint into the dev channel's alias
// tag: <tag>--fp-<64 lowercase hex> (docs/guides/fetch-v1.md §3).
const AliasSeparator = "--fp-"

// PinningRefused is the message every lock, frozen or update request gets
// from a 2.0.0-dev SDK, before any network call (rule r6).
const PinningRefused = "--lock, --frozen and --update are refused by a 2.0.0-dev SDK: a dev build is replaceable, " +
	"and a superseded one expires after 14 days, so nothing may pin one (spec/abi-v2/docs.md, rule r6)"

// channel is one fetch contract.
type channel struct {
	name         string
	abi          int      // a predicate's abi; a lock's abi; Resolved.ABIGeneration
	recordSchema int      // the verified.json schema written, and the only one read
	rootLeaf     string   // the default root is ${XDG_CACHE_HOME:-~/.cache}/chtypes/<rootLeaf>
	subroot      string   // an explicit cache is used as <cache>/<subroot>; "" uses it whole
	systemDirs   []string // the read-only system cache dirs searched after the cache
	bases        []string // the default bases
	keys         []ReleaseKey
	overridable  bool // the base, trust and unsigned overrides are honored
	pinnable     bool // lock, frozen and update are honored
	// ownFingerprint is the fingerprint, 64 lowercase hex, this contract's
	// SDK speaks: a version request resolves its alias tag before the tag
	// itself, and the cache lookups see only records signed with it. ""
	// does neither (the v1 contract and every production channel).
	ownFingerprint string
}

// devChannel is what a 2.0.0-dev SDK speaks, and what every non-test binary of
// this module speaks.
var devChannel = channel{
	name:         "v2-dev",
	abi:          DevABIGeneration,
	recordSchema: DevRecordSchema,
	rootLeaf:     DevCacheDir,
	subroot:      DevCacheDir,
	systemDirs:   []string{"/usr/local/share/chtypes/" + DevCacheDir, "/opt/chtypes/" + DevCacheDir},
	bases:        []string{DevChannelBase},
	keys:         []ReleaseKey{{KeyID: DevKeyID, Ed25519Hex: DevKeyHex}},
	// The generated constant (abi_fingerprint_gen.go), never a hand-written one.
	ownFingerprint: strings.TrimPrefix(DevABIFingerprint, "sha256:"),
}

// fetchV1Channel is the v1 contract the conformance cases specify, from the
// generated constants. Only UseFetchV1ForTests selects it.
var fetchV1Channel = channel{
	name:         "v1",
	abi:          ABIGeneration,
	recordSchema: SchemaVersion,
	rootLeaf:     "v1",
	subroot:      "",
	systemDirs:   SystemCacheDirs,
	bases:        DefaultBases,
	keys:         ReleaseKeys,
	overridable:  true,
	pinnable:     true,
}

var current atomic.Pointer[channel]

// active is the contract this process fetches under: the dev channel, unless
// a test binary selected another.
func active() *channel {
	if c := current.Load(); c != nil {
		return c
	}
	return &devChannel
}

func testOnly(what string) {
	if !testing.Testing() {
		panic("ocifetch: " + what + " is test-only; a 2.0.0-dev SDK has no override (spec/abi-v2/docs.md, rule r6)")
	}
}

func use(c *channel) func() {
	prev := current.Swap(c)
	return func() { current.Store(prev) }
}

// UseFetchV1ForTests makes this TEST binary speak the v1 fetch contract
// (docs/guides/fetch-v1.md): overrides, locks, schema-1 records and abi-1
// predicates. The fetch-v1 conformance cases and the v1-protocol tests run
// under it; every case names its own fixture registry and the test key. It
// returns the function that restores the previous contract, and panics outside
// a test binary.
func UseFetchV1ForTests() (restore func()) {
	testOnly("UseFetchV1ForTests")
	c := fetchV1Channel
	return use(&c)
}

// AllowOverridesForTests makes this TEST binary's dev channel honor the base,
// trust and unsigned overrides, so a test can reach a fixture registry signed
// with the test key; everything else stays the dev channel's (abi 2, schema-2
// records, the v2-dev cache, no pinning). It returns the restore function, and
// panics outside a test binary.
func AllowOverridesForTests() (restore func()) {
	testOnly("AllowOverridesForTests")
	c := devChannel
	c.overridable = true
	return use(&c)
}

// UseOwnFingerprintForTests makes this TEST binary's active contract speak
// fingerprint (64 lowercase hex) as the dev channel speaks its own: a version
// request resolves that fingerprint's alias tag first, and the cache lookups
// see only records signed with it. The fetch-v1 conformance cases that carry
// request.own_fingerprint run under it, against fixtures that name a fixture
// fingerprint. Everything else stays the active contract's. It returns the
// restore function, and panics outside a test binary or on a fingerprint that
// is not 64 lowercase hex.
func UseOwnFingerprintForTests(fingerprint string) (restore func()) {
	testOnly("UseOwnFingerprintForTests")
	if !fingerprintHex.MatchString(fingerprint) {
		panic("ocifetch: UseOwnFingerprintForTests: " + strconv.Quote(fingerprint) + " is not 64 lowercase hex")
	}
	c := *active()
	c.ownFingerprint = fingerprint
	return use(&c)
}

var fingerprintHex = regexp.MustCompile(`^[0-9a-f]{64}$`)

// aliasTag is the alias a version request for tag resolves first under a
// contract with an own fingerprint, or "" when it resolves tag alone: under
// the v1 contract and every production channel, and for a request that is not
// a version spelling (an arbitrary tag has no alias).
func (c *channel) aliasTag(tag string) string {
	if c == nil || c.ownFingerprint == "" || !spellingRegex.MatchString(tag) {
		return ""
	}
	return tag + AliasSeparator + c.ownFingerprint
}

// visible reports whether a cache record (or a pre-seeded entry) whose SIGNED
// predicate is pred may answer a request under this contract: under one with
// an own fingerprint (the dev channel), only when the predicate's
// abi_fingerprint is that fingerprint; under every other, always. A record it
// cannot see is never returned and never kept by the monotonic rule, and
// nothing removes or rewrites it: another SDK of another fingerprint owns it.
func (c *channel) visible(pred map[string]any) bool {
	if c == nil || c.ownFingerprint == "" {
		return true
	}
	fp, _ := pred["abi_fingerprint"].(string)
	return fp == "sha256:"+c.ownFingerprint
}

// aheadOfRegistry is the dev channel's answer for an SDK whose fingerprint
// no published build carries yet (docs/guides/fetch-v1.md §3; public issue
// #578): when every base answered its own alias 404 (aliasAbsent) and the
// build the tag names is signed for another fingerprint (pred, the SIGNED
// predicate, the field visible keys on), it is CHTYPES_ARTIFACT_UNPUBLISHED,
// the code a request no build answers gets, naming both fingerprints. Nothing
// is installed and an install of that build is never reported, because the
// lookup this SDK opens through would refuse it. nil in every other case:
// the alias answered, the tag's build is this SDK's own, or the contract has
// no own fingerprint.
func (c *channel) aheadOfRegistry(aliasAbsent bool, pred map[string]any, request, platform string) error {
	if !aliasAbsent || c.visible(pred) {
		return nil
	}
	return newError(CodeArtifactUnpublished, request, platform, "", nil, "%s", aheadMessage(c.ownFingerprint, pred))
}

// aheadMessage is aheadOfRegistry's message: "no published build for this
// SDK's fingerprint <own>; newest published on this channel is <fp> (build
// <id>)", each fingerprint 64 lowercase hex without its "sha256:" prefix, the
// parenthesis dropped when the predicate names no build, and "unnamed" for a
// predicate that names no fingerprint.
func aheadMessage(own string, pred map[string]any) string {
	theirs, _ := pred["abi_fingerprint"].(string)
	theirs = strings.TrimPrefix(theirs, "sha256:")
	if theirs == "" {
		theirs = "unnamed"
	}
	msg := "no published build for this SDK's fingerprint " + own + "; newest published on this channel is " + theirs
	if build, _ := pred["build"].(string); build != "" {
		msg += " (build " + build + ")"
	}
	return msg
}

// UseDevChannelForTests makes this TEST binary speak the dev channel exactly
// as a non-test binary does (undoing either function above, for a test that
// checks the dev channel itself). It returns the restore function.
func UseDevChannelForTests() (restore func()) {
	testOnly("UseDevChannelForTests")
	c := devChannel
	return use(&c)
}

// ChannelName is the fetch contract this process speaks: "v2-dev" in every
// non-test binary.
func ChannelName() string { return active().name }

// ABI is the abi a predicate must carry under the active contract, and the
// generation a Resolved names.
func ABI() int { return active().abi }

var (
	ignoredMu     sync.Mutex
	ignoredWarned = map[string]bool{}
	// ignoredOut is where the warnings go; a test may redirect it.
	ignoredOut io.Writer = os.Stderr
)

// warnIgnored prints, once per process per setting, that a dev SDK ignores
// it (rule r6: "a dev SDK says so, once and loudly").
func warnIgnored(setting string) {
	ignoredMu.Lock()
	defer ignoredMu.Unlock()
	if ignoredWarned[setting] {
		return
	}
	ignoredWarned[setting] = true
	fmt.Fprintf(ignoredOut, "chtypes: WARNING: %s is set and IGNORED: this is a 2.0.0-dev SDK, which fetches only from %s "+
		"and trusts only the staging key %s (spec/abi-v2/docs.md, rule r6)\n", setting, DevChannelBase, DevKeyID)
}

// IgnoredSettingsWarned lists the settings warned about so far, sorted; for tests.
func IgnoredSettingsWarned() []string {
	ignoredMu.Lock()
	defer ignoredMu.Unlock()
	out := make([]string, 0, len(ignoredWarned))
	for k := range ignoredWarned {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// pinningRequested names the pinning options a call set, if any.
func pinningRequested(o *Options) []string {
	var set []string
	if o.Frozen {
		set = append(set, "frozen")
	}
	if o.LockWrite {
		set = append(set, "lock")
	}
	if o.Update {
		set = append(set, "update")
	}
	if o.LockPath != "" {
		set = append(set, "a lock path")
	}
	return set
}

// PinningError is the refusal of a lock, frozen or update request by a
// 2.0.0-dev SDK. It is the caller's misuse, never an artifact failure.
type PinningError struct{ Requested []string }

func (e *PinningError) Error() string {
	return "chtypes: " + PinningRefused + " (requested: " + strings.Join(e.Requested, ", ") + ")"
}

// PinningRefusedFor reports whether the active contract refuses the pinning
// options o sets (the dev channel refuses every one; rule r6).
func PinningRefusedFor(o *Options) bool {
	return !active().pinnable && len(pinningRequested(o)) > 0
}
