package ocifetch

// prodv2channel_test.go — the production generation-2 channel (channel.go,
// prodV2Channel; docs/guides/fetch-v1.md, "Generation 2 after the lock"), built
// and tested but not the default. The fetch conformance cases run under it in
// TestConformanceProdV2; these are the pins the cases do not reach.

import (
	"encoding/hex"
	"path/filepath"
	"testing"
)

func prodV2ForTest(t *testing.T) {
	t.Helper()
	t.Cleanup(UseProdV2ForTests())
	for _, k := range []string{EnvBasesName, EnvTrustedKeysName, EnvAllowUnsignedName, EnvCacheName, EnvCacheStrictName, EnvOfflineName} {
		t.Setenv(k, "")
	}
}

func TestProdV2ChannelValues(t *testing.T) {
	prodV2ForTest(t)
	if ChannelName() != "v2" || ABI() != 2 {
		t.Fatalf("channel %q abi %d, want v2 and 2", ChannelName(), ABI())
	}
	c := active()
	if c.recordSchema != 2 || c.rootLeaf != "v2" || c.subroot != "v2" || !c.overridable || !c.pinnable || c.ownFingerprint != "" {
		t.Errorf("prod v2 channel = %+v", *c)
	}
	if ProdV2Bases[0] != "https://registry.wavehouse.dev/chtypes/v2" || len(ProdV2Bases) != 1 {
		t.Errorf("ProdV2Bases = %v", ProdV2Bases)
	}
	ro, err := resolveOptions(nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(ro.bases) != 1 || ro.bases[0] != ProdV2Bases[0] {
		t.Errorf("default bases = %v", ro.bases)
	}
	if len(ro.trustedKeys) != len(ReleaseKeys) {
		t.Fatalf("default trust = %d key(s), want the release keys (%d)", len(ro.trustedKeys), len(ReleaseKeys))
	}
	for i, k := range ro.trustedKeys {
		if hex.EncodeToString(k) != ReleaseKeys[i].Ed25519Hex {
			t.Errorf("trusted key %d is not release key %s", i, ReleaseKeys[i].KeyID)
		}
		if hex.EncodeToString(k) == DevKeyHex {
			t.Error("the staging key is in the production-v2 trust list")
		}
	}
	if ReleaseKeys[0].KeyID != "deb275922dbff76e" {
		t.Errorf("release key id = %s", ReleaseKeys[0].KeyID)
	}
}

func TestProdV2ChannelHonorsOverridesAndPins(t *testing.T) {
	prodV2ForTest(t)
	t.Setenv(EnvBasesName, "https://mirror.example/chtypes/v2")
	ro, err := resolveOptions(&Options{Frozen: true})
	if err != nil {
		t.Fatal(err)
	}
	if len(ro.bases) != 1 || ro.bases[0] != "https://mirror.example/chtypes/v2" {
		t.Errorf("CHTYPES_ARTIFACTS_URL not honored: %v", ro.bases)
	}
	if PinningRefusedFor(&Options{Frozen: true, LockWrite: true, Update: true}) {
		t.Error("the production-v2 channel refuses pinning")
	}
}

func TestProdV2ChannelCacheRoots(t *testing.T) {
	prodV2ForTest(t)
	dir := t.TempDir()
	ro, err := resolveOptions(&Options{CacheDir: dir})
	if err != nil {
		t.Fatal(err)
	}
	if want := filepath.Join(dir, "v2"); ro.cacheDir != want {
		t.Errorf("explicit cache used as %s, want %s", ro.cacheDir, want)
	}
	t.Setenv("XDG_CACHE_HOME", dir)
	ro, err = resolveOptions(nil)
	if err != nil {
		t.Fatal(err)
	}
	if want := filepath.Join(dir, "chtypes", "v2"); ro.cacheDir != want {
		t.Errorf("default root %s, want %s", ro.cacheDir, want)
	}
	if len(ro.systemDirs) != 2 || ro.systemDirs[0] != "/usr/local/share/chtypes/v2" || ro.systemDirs[1] != "/opt/chtypes/v2" {
		t.Errorf("system dirs = %v", ro.systemDirs)
	}
}

// A non-test binary still speaks the dev channel: with no test seam selected,
// active() is devChannel, and prodV2Channel is reachable only through
// UseProdV2ForTests.
func TestProdV2IsNotTheDefault(t *testing.T) {
	prev := current.Swap(nil)
	t.Cleanup(func() { current.Store(prev) })
	if active() != &devChannel || ChannelName() != "v2-dev" {
		t.Fatalf("the default channel is %q, want v2-dev", ChannelName())
	}
}
