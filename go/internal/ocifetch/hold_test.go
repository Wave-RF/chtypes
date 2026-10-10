package ocifetch

// hold_test.go — the in-use signal across processes (docs/guides/fetch-v1.md
// §1, "In use"; public issue #494): a build ANOTHER process holds is kept by
// prune, and becomes prunable when that process exits. The other process is
// this test binary run again as a helper, holding the build through Hold, the
// call a registry makes before it loads one.

import (
	"bufio"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

const holdHelperEnv = "CHTYPES_TEST_HOLD_DIR"

// TestHoldHelperProcess is the other process: it holds the entry named by
// CHTYPES_TEST_HOLD_DIR, says so, and keeps it until its stdin closes. It
// does nothing in an ordinary run.
func TestHoldHelperProcess(t *testing.T) {
	dir := os.Getenv(holdHelperEnv)
	if dir == "" {
		t.Skip("the helper process of TestPruneKeepsWhatAnotherProcessHolds; nothing to do on its own")
	}
	if got := Hold(dir); got != HoldHeld {
		os.Stdout.WriteString("not held\n")
		os.Exit(3)
	}
	os.Stdout.WriteString("held\n")
	_, _ = io.Copy(io.Discard, os.Stdin)
	os.Exit(0)
}

func TestPruneKeepsWhatAnotherProcessHolds(t *testing.T) {
	fixtures, err := filepath.Abs(filepath.Join("..", "..", "..", "tests", "fixtures", "fetch-v1"))
	if err != nil {
		t.Fatal(err)
	}
	cache := t.TempDir()
	if err := copyDir(filepath.Join(fixtures, "layouts", "prune-lines"), cache); err != nil {
		t.Skipf("SKIPPED: the prune-lines fixture is not beside this checkout (%v)", err)
	}
	keys, err := resolvePreInstallKeys([]string{testKeyHexForConformance(t, fixtures)})
	if err != nil {
		t.Fatal(err)
	}
	if err := preInstallFromDir(cache, keys); err != nil {
		t.Fatal(err)
	}
	opts := &Options{CacheDir: cache, SystemDirs: []string{}}
	dry, err := Prune(opts, PruneOptions{Keep: 1, DryRun: true})
	if err != nil || len(dry) == 0 {
		t.Fatalf("the dry run = %+v, %v, want superseded builds", dry, err)
	}
	victim := dry[0]

	helper := exec.Command(os.Args[0], "-test.run=^TestHoldHelperProcess$", "-test.count=1")
	helper.Env = append(os.Environ(), holdHelperEnv+"="+victim.Dir)
	stdin, err := helper.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	stdout, err := helper.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := helper.Start(); err != nil {
		t.Fatal(err)
	}
	line, _ := bufio.NewReader(stdout).ReadString('\n')
	if strings.TrimSpace(line) != "held" {
		_ = helper.Process.Kill()
		t.Fatalf("the helper process said %q, want held", line)
	}

	got, err := Prune(opts, PruneOptions{Keep: 1})
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range got {
		if s.Dir == victim.Dir && !s.InUse {
			t.Errorf("prune removed %s, which another process holds", s.Dir)
		}
		if s.Dir != victim.Dir && s.InUse {
			t.Errorf("prune kept %s as in use, which no process holds", s.Dir)
		}
	}
	if _, err := os.Stat(filepath.Join(victim.Dir, CacheVerifiedRecord)); err != nil {
		t.Fatalf("the held build is gone: %v", err)
	}

	_ = stdin.Close()
	if err := helper.Wait(); err != nil {
		t.Fatalf("the helper process: %v", err)
	}
	again, err := Prune(opts, PruneOptions{Keep: 1})
	if err != nil || len(again) != 1 || again[0].Dir != victim.Dir || again[0].InUse {
		t.Fatalf("prune once the holder exited = %+v, %v, want %s removed", again, err, victim.Dir)
	}
	if _, err := os.Stat(victim.Dir); !os.IsNotExist(err) {
		t.Errorf("%s is still there after the second prune (%v)", victim.Dir, err)
	}
}

// TestHoldSeesAnEntryReplacedOrGone: a hold of an entry that is gone, or
// whose record is no longer the file the lookup read, is HoldVanished.
func TestHoldSeesAnEntryReplacedOrGone(t *testing.T) {
	if got := Hold(filepath.Join(t.TempDir(), "absent")); got != HoldVanished {
		t.Errorf("Hold of a missing entry = %v, want HoldVanished", got)
	}
	dir := t.TempDir()
	record := filepath.Join(dir, CacheVerifiedRecord)
	if err := os.WriteFile(record, []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := Hold(dir); got != HoldHeld {
		t.Fatalf("Hold = %v, want HoldHeld", got)
	}
	// Removed and installed again: the old hold protects nothing, and a new
	// one is taken on the new record.
	if err := os.Remove(record); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(record, []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := Hold(dir); got != HoldHeld {
		t.Fatalf("Hold after the entry was replaced = %v, want HoldHeld on the new record", got)
	}
	if state, release := claim(dir); state != claimInUse || release != nil {
		t.Errorf("claim of an entry this process holds = %v, want claimInUse", state)
	}
}
