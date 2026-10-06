package ocifetch

// devchannel_test.go — the ABI v2 dev channel (channel.go; spec/abi-v2/docs.md,
// rules r5 and r6), tested as a non-test binary speaks it: each test switches
// to it with UseDevChannelForTests for its own duration, undoing the package's
// v1 seam (main_test.go). Nothing here reaches the network: every refusal is
// proven to come before the first request, and every cache read is offline.

import (
	"bytes"
	"context"
	"encoding/hex"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func devChannelForTest(t *testing.T) {
	t.Helper()
	t.Cleanup(UseDevChannelForTests())
	for _, k := range []string{EnvBasesName, EnvTrustedKeysName, EnvAllowUnsignedName, EnvCacheName, EnvCacheStrictName} {
		t.Setenv(k, "")
	}
}

// captureIgnored redirects the ignored-setting warnings and forgets which were
// already given, so a test sees exactly its own.
func captureIgnored(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	ignoredMu.Lock()
	prevOut, prevWarned := ignoredOut, ignoredWarned
	ignoredOut, ignoredWarned = &buf, map[string]bool{}
	ignoredMu.Unlock()
	t.Cleanup(func() {
		ignoredMu.Lock()
		ignoredOut, ignoredWarned = prevOut, prevWarned
		ignoredMu.Unlock()
	})
	return &buf
}

// r6: the base, the key and its id are the ones the rule pins, and the release
// key is not in the dev trust list.
func TestDevChannelPinsTheStagingBaseAndKey(t *testing.T) {
	devChannelForTest(t)
	if DevChannelBase != "https://registry-staging.wavehouse.dev/chtypes/v2-dev" {
		t.Errorf("DevChannelBase = %q", DevChannelBase)
	}
	pub, err := hexToPublicKey(DevKeyHex)
	if err != nil {
		t.Fatal(err)
	}
	if got := keyIDFor(pub); got != DevKeyID || DevKeyID != "824345f9bcf8e5bf" {
		t.Errorf("the staging key's id is %q (KeyIDAlgorithm), the rule names %q (DevKeyID %q)", got, "824345f9bcf8e5bf", DevKeyID)
	}
	ro, err := resolveOptions(nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(ro.bases) != 1 || ro.bases[0] != DevChannelBase {
		t.Errorf("default bases = %v, want only %s", ro.bases, DevChannelBase)
	}
	if len(ro.trustedKeys) != 1 || hex.EncodeToString(ro.trustedKeys[0]) != DevKeyHex {
		t.Errorf("default trust = %d key(s), want only the staging key", len(ro.trustedKeys))
	}
	for _, rk := range ReleaseKeys {
		for _, k := range ro.trustedKeys {
			if hex.EncodeToString(k) == rk.Ed25519Hex {
				t.Errorf("the release key %s is in the dev trust list", rk.KeyID)
			}
		}
	}
	if ChannelName() != "v2-dev" || ABI() != 2 {
		t.Errorf("the active contract is %s, abi %d", ChannelName(), ABI())
	}
}

// r6: no override. Every base, trust and unsigned setting, from the
// environment and from the options, is ignored, and each is named exactly
// once per process, however many calls see it.
func TestDevChannelIgnoresEveryOverrideLoudlyOnce(t *testing.T) {
	devChannelForTest(t)
	out := captureIgnored(t)
	testKey := TestKeys[0].Ed25519Hex
	t.Setenv(EnvBasesName, "http://127.0.0.1:9/chtypes/v1")
	t.Setenv(EnvTrustedKeysName, testKey)
	t.Setenv(EnvAllowUnsignedName, "1")
	opts := &Options{Bases: []string{"http://127.0.0.1:9/x"}, TrustedKeys: []string{testKey}, AllowUnsigned: true}
	for range 3 {
		ro, err := resolveOptions(opts)
		if err != nil {
			t.Fatal(err)
		}
		if len(ro.bases) != 1 || ro.bases[0] != DevChannelBase {
			t.Errorf("bases = %v: an override was honored", ro.bases)
		}
		if len(ro.trustedKeys) != 1 || hex.EncodeToString(ro.trustedKeys[0]) != DevKeyHex {
			t.Errorf("a trust override was honored")
		}
		if ro.allowUnsigned {
			t.Errorf("an unsigned override was honored")
		}
	}
	want := []string{EnvAllowUnsignedName, EnvBasesName, EnvTrustedKeysName, "the AllowUnsigned option", "the Bases option", "the TrustedKeys option"}
	got := IgnoredSettingsWarned()
	if strings.Join(got, "|") != strings.Join(want, "|") {
		t.Errorf("warned about %v, want %v", got, want)
	}
	text := out.String()
	for _, s := range want {
		if n := strings.Count(text, "WARNING: "+s+" is set and IGNORED"); n != 1 {
			t.Errorf("%s was warned about %d times, want exactly once:\n%s", s, n, text)
		}
	}
	if !strings.Contains(text, DevChannelBase) || !strings.Contains(text, DevKeyID) {
		t.Errorf("the warning does not name the base and the key it uses instead:\n%s", text)
	}
}

// r6: --lock, --frozen and update, as options, are refused before any network
// call, with the dev-channel reason.
func TestDevChannelRefusesPinningBeforeAnyRequest(t *testing.T) {
	devChannelForTest(t)
	cases := map[string]Options{
		"frozen":       {Frozen: true},
		"lock write":   {LockWrite: true, LockPath: "chtypes.lock"},
		"update":       {Update: true, LockPath: "chtypes.lock"},
		"a lock path":  {LockPath: "chtypes.lock"},
		"frozen+cache": {Frozen: true, CacheDir: t.TempDir()},
	}
	for name, o := range cases {
		requests := 0
		o.OnRequest = func(*http.Request) { requests++ }
		_, err := Ensure(context.Background(), Request{Spelling: "26.8", Platform: "linux-amd64"}, &o)
		var pe *PinningError
		if !errors.As(err, &pe) {
			t.Errorf("%s: Ensure = %v, want a *PinningError", name, err)
			continue
		}
		if requests != 0 {
			t.Errorf("%s: %d request(s) were made before the refusal", name, requests)
		}
		if !strings.Contains(err.Error(), "refused by a 2.0.0-dev SDK") || !strings.Contains(err.Error(), "14 days") {
			t.Errorf("%s: the refusal does not say why: %v", name, err)
		}
		if !PinningRefusedFor(&o) {
			t.Errorf("%s: PinningRefusedFor = false", name)
		}
	}
}

// r5: the default root, and an explicit cache from the environment or the
// option, each used through the v2-dev subroot, never as a whole layout.
func TestDevChannelCacheRoots(t *testing.T) {
	devChannelForTest(t)
	xdg := t.TempDir()
	t.Setenv("XDG_CACHE_HOME", xdg)
	if root, err := CacheRoot(nil); err != nil || root != filepath.Join(xdg, "chtypes", "v2-dev") {
		t.Errorf("default root = %q, %v; want ${XDG_CACHE_HOME}/chtypes/v2-dev", root, err)
	}
	env := t.TempDir()
	t.Setenv(EnvCacheName, env)
	if root, err := CacheRoot(nil); err != nil || root != filepath.Join(env, "v2-dev") {
		t.Errorf("CHTYPES_CACHE root = %q, %v; want <CHTYPES_CACHE>/v2-dev", root, err)
	}
	opt := t.TempDir()
	if root, err := CacheRoot(&Options{CacheDir: opt}); err != nil || root != filepath.Join(opt, "v2-dev") {
		t.Errorf("CacheDir root = %q, %v; want <CacheDir>/v2-dev", root, err)
	}
	ro, err := resolveOptions(nil)
	if err != nil {
		t.Fatal(err)
	}
	for _, d := range ro.systemDirs {
		if !strings.HasSuffix(d, "/v2-dev") {
			t.Errorf("system dir %s is not a v2-dev dir: a 1.x system dir is never read", d)
		}
	}
}

// r5: the dev channel writes schema-2 records and reads only those; a schema-1
// record (every released 1.x writer's) is absent to it.
func TestDevChannelRecordsAreSchema2(t *testing.T) {
	devChannelForTest(t)
	rec := verifiedRecord{
		Platform: "linux-amd64", Version: "26.8.15.10", Build: "20261001.000000", LibraryPath: "libchtypes.so",
		LibrarySHA256: strings.Repeat("a", 64), LibraryBytes: 1,
		Digests:   Digests{Manifest: Digest("sha256:" + strings.Repeat("b", 64)), Layer: Digest("sha256:" + strings.Repeat("c", 64))},
		Predicate: map[string]any{"abi": float64(2)}, SignedBy: DevKeyID,
	}
	b, err := encodeRecord(rec)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(b, []byte(`"schema":2`)) {
		t.Errorf("the dev channel wrote %s, want schema 2", b)
	}
	if _, err := decodeRecord(b); err != nil {
		t.Errorf("the dev channel refused its own record: %v", err)
	}
	one := bytes.Replace(b, []byte(`"schema":2`), []byte(`"schema":1`), 1)
	if _, err := decodeRecord(one); err == nil {
		t.Errorf("the dev channel accepted a schema-1 record: a 1.x record must read as absent")
	}
	if res := recordToResolved(&rec, "/d", "linux-amd64", "26.8", "", false, "", nil); res.ABIGeneration != 2 {
		t.Errorf("Resolved.ABIGeneration = %d, want 2", res.ABIGeneration)
	}
}

// r6: a signed predicate must say abi 2; an abi-1 one (a 1.x build) is
// refused even when every other field matches.
func TestDevChannelPredicateIsABI2(t *testing.T) {
	devChannelForTest(t)
	platform := Platform{Key: "linux-arm64", OS: "linux", Architecture: "arm64"}
	pred := sampleArtifactStatement(hex64('a'))["predicate"].(map[string]any)
	pred["abi"] = float64(1)
	if err := validateArtifactPredicate(pred, platform, "26.8"); err == nil || !strings.Contains(err.Error(), "expected 2") {
		t.Errorf("an abi-1 predicate = %v, want refused as not abi 2", err)
	}
	pred["abi"] = float64(2)
	if err := validateArtifactPredicate(pred, platform, "26.8"); err != nil {
		t.Errorf("an abi-2 predicate was refused: %v", err)
	}
}

// r5, end to end: a cache a 1.x binding wrote is never what the dev channel
// reads, whether CHTYPES_CACHE names it (the dev channel reads its subroot)
// or the 1.x layout sits in the subroot itself (its schema-1 records are
// absent). The control is the same cache read under the v1 contract.
func TestDevChannelNeverReadsAV1Cache(t *testing.T) {
	layout := filepath.Join("..", "..", "..", "tests", "fixtures", "fetch-v1", "layouts", "cache-record-canonical")
	if _, err := os.Stat(layout); err != nil {
		t.Skipf("SKIPPED: the fetch-v1 fixtures are not beside this checkout (%v)", err)
	}
	cache := t.TempDir()
	if err := copyDir(layout, cache); err != nil {
		t.Fatal(err)
	}
	if err := copyDir(layout, filepath.Join(cache, "v2-dev")); err != nil {
		t.Fatal(err)
	}
	opts := &Options{CacheDir: cache, SystemDirs: []string{}}

	restore := UseFetchV1ForTests()
	v1, err := ListInstalled(opts)
	restore()
	if err != nil || len(v1) == 0 {
		t.Fatalf("control: the v1 contract lists %d builds in the 1.x cache (%v); the fixture is not a 1.x cache", len(v1), err)
	}

	devChannelForTest(t)
	dev, err := ListInstalled(opts)
	if err != nil || len(dev) != 0 {
		t.Errorf("the dev channel listed %+v (%v) from a 1.x cache", dev, err)
	}
	for _, r := range v1 {
		got, err := ResolveInstalled(Request{Spelling: r.Version}, r.Platform, opts)
		if err != nil || got != nil {
			t.Errorf("the dev channel resolved %s from a 1.x cache: %+v, %v", r.Version, got, err)
		}
	}
}
