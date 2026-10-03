package ocifetch

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func writeTestFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("writing %s: %v", path, err)
	}
}

func TestReadLockRefusesSchema1And2(t *testing.T) {
	dir := t.TempDir()
	for _, schema := range []string{"1", "2"} {
		path := filepath.Join(dir, "chtypes.lock")
		writeTestFile(t, path, `{"schema":`+schema+`,"abi":1,"platforms":[],"requests":{}}`)
		_, err := readLock(path)
		if err == nil {
			t.Fatalf("readLock accepted a schema %s (v0) lock", schema)
		}
		var fe *FetchError
		if !errors.As(err, &fe) || fe.Code != CodeArtifactPinned {
			t.Fatalf("schema %s lock error = %v, want CHTYPES_ARTIFACT_PINNED", schema, err)
		}
	}
}

func TestReadLockRefusesWrongABI(t *testing.T) {
	path := filepath.Join(t.TempDir(), "chtypes.lock")
	writeTestFile(t, path, `{"schema":3,"abi":2,"platforms":["linux-arm64"],"requests":{}}`)
	_, err := readLock(path)
	if err == nil {
		t.Fatalf("readLock accepted a lock naming a different ABI generation")
	}
}

func TestReadLockRoundTrip(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "chtypes.lock")
	lock := &Lock{
		Schema:    LockSchema,
		ABI:       ABIGeneration,
		Platforms: []string{"linux-arm64"},
		Requests: map[string]map[string]LockPin{
			"26.8": {
				"linux-arm64": {
					Version:  "26.8.15.10",
					Build:    "20261001.183455",
					Manifest: Digest("sha256:" + hex64('a')),
					Layer:    Digest("sha256:" + hex64('b')),
					Bundle:   Digest("sha256:" + hex64('c')),
				},
			},
		},
	}
	if err := writeLock(path, lock); err != nil {
		t.Fatalf("writeLock: %v", err)
	}
	read, err := readLock(path)
	if err != nil {
		t.Fatalf("readLock: %v", err)
	}
	pin, ok := read.Pin("26.8", "linux-arm64")
	if !ok {
		t.Fatalf("Pin(26.8, linux-arm64) not found after round trip")
	}
	if pin.Version != "26.8.15.10" {
		t.Fatalf("pin.Version = %q", pin.Version)
	}
}

func TestLockPinMissingIsNotOK(t *testing.T) {
	lock := &Lock{Requests: map[string]map[string]LockPin{}}
	if _, ok := lock.Pin("26.8", "linux-arm64"); ok {
		t.Fatalf("Pin on an empty lock reported ok=true")
	}
}

func TestMergeLockRequestPreservesOtherSpellings(t *testing.T) {
	existing := &Lock{
		Schema: LockSchema, ABI: ABIGeneration,
		Platforms: []string{"linux-arm64"},
		Requests: map[string]map[string]LockPin{
			"25.3": {"linux-arm64": {Version: "25.3.1.1"}},
		},
	}
	merged := mergeLockRequest(existing, "26.8", map[string]LockPin{"linux-arm64": {Version: "26.8.15.10"}})
	if _, ok := merged.Pin("25.3", "linux-arm64"); !ok {
		t.Fatalf("mergeLockRequest dropped the pre-existing 25.3 entry")
	}
	if pin, ok := merged.Pin("26.8", "linux-arm64"); !ok || pin.Version != "26.8.15.10" {
		t.Fatalf("mergeLockRequest did not add the new 26.8 entry correctly: %+v, ok=%v", pin, ok)
	}
}

func TestRewriteLockForUpdateDropsStaleEntries(t *testing.T) {
	// `update` rewrites from the fresh set alone; a spelling not present in
	// freshResults must not survive.
	fresh := map[string]map[string]LockPin{
		"26.8": {"linux-arm64": {Version: "26.8.16.1"}},
	}
	lock := rewriteLockForUpdate(fresh)
	if _, ok := lock.Pin("25.3", "linux-arm64"); ok {
		t.Fatalf("rewriteLockForUpdate must never carry a spelling it was not given")
	}
	if pin, ok := lock.Pin("26.8", "linux-arm64"); !ok || pin.Version != "26.8.16.1" {
		t.Fatalf("rewriteLockForUpdate did not carry the fresh pin: %+v, ok=%v", pin, ok)
	}
}
