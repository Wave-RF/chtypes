package ocifetch

// prodv2_smoke_test.go — a live smoke of the production generation-2 channel
// against a real registry, opt-in: CHTYPES_PRODV2_SMOKE_BASE names the
// repository (the staging dev repository, whose builds are abi 2 and signed with
// the staging key) and CHTYPES_PRODV2_SMOKE_KEY the key it is signed with. It
// skips loudly when either is unset, and the sandboxed CI runs never set them.
// It proves the channel honors a base and a trust override, writes a schema-2
// record signed by that key, and that --lock then --frozen pins one manifest.

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestProdV2LiveSmoke(t *testing.T) {
	base, key := os.Getenv("CHTYPES_PRODV2_SMOKE_BASE"), os.Getenv("CHTYPES_PRODV2_SMOKE_KEY")
	if base == "" || key == "" {
		t.Skip("CHTYPES_PRODV2_SMOKE_BASE and CHTYPES_PRODV2_SMOKE_KEY are not set; skipping the live production-v2 smoke")
	}
	defer UseProdV2ForTests()()
	spelling := os.Getenv("CHTYPES_PRODV2_SMOKE_SPELLING")
	if spelling == "" {
		spelling = "26.9"
	}
	work := t.TempDir()
	lock := filepath.Join(work, "chtypes.lock")
	opts := func(cache string) *Options {
		return &Options{Bases: []string{base}, TrustedKeys: []string{key}, CacheDir: cache, SystemDirs: []string{}, LockPath: lock}
	}

	first := opts(filepath.Join(work, "cache1"))
	first.LockWrite = true
	res, err := Ensure(context.Background(), Request{Spelling: spelling}, first)
	if err != nil {
		t.Fatalf("Ensure(%s) under the production-v2 channel with the overrides: %v", spelling, err)
	}
	raw, err := os.ReadFile(filepath.Join(res.Dir, "verified.json"))
	if err != nil {
		t.Fatal(err)
	}
	var rec struct {
		Schema   int    `json:"schema"`
		SignedBy string `json:"signed_by"`
		Digests  struct {
			Manifest string `json:"manifest"`
		} `json:"digests"`
		Predicate map[string]any `json:"predicate"`
	}
	if err := json.Unmarshal(raw, &rec); err != nil {
		t.Fatal(err)
	}
	t.Logf("fetched %s: version %s build %s manifest %s", spelling, res.Version, res.Build, res.Digests.Manifest)
	t.Logf("record: schema %d, signed_by %s, predicate abi %v, dir under %s", rec.Schema, rec.SignedBy, rec.Predicate["abi"], filepath.Base(filepath.Dir(filepath.Dir(filepath.Dir(res.Dir)))))
	if rec.Schema != 2 || rec.Predicate["abi"] != float64(2) || rec.SignedBy == "" {
		t.Fatalf("record = schema %d, abi %v, signed_by %q; want schema 2, abi 2, a signer", rec.Schema, rec.Predicate["abi"], rec.SignedBy)
	}
	if filepath.Base(filepath.Dir(filepath.Dir(filepath.Dir(res.Dir)))) != "v2" {
		t.Errorf("install dir %s is not under the v2 subroot", res.Dir)
	}

	frozen := opts(filepath.Join(work, "cache2"))
	frozen.Frozen = true
	again, err := Ensure(context.Background(), Request{Spelling: spelling}, frozen)
	if err != nil {
		t.Fatalf("frozen Ensure: %v", err)
	}
	t.Logf("frozen: manifest %s", again.Digests.Manifest)
	if again.Digests.Manifest != res.Digests.Manifest {
		t.Errorf("frozen manifest %s != locked %s", again.Digests.Manifest, res.Digests.Manifest)
	}
}
