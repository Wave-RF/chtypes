// Command genfixtures regenerates every fixture under
// tests/fixtures/fetch-v1/ EXCEPT tests/fixtures/fetch-v1/{test-key,other-key}/
// (lane 0A's static keys, read here but never written) and
// tests/fixtures/fetch-v1/layouts/oras-preseed/ (written by a REAL `oras
// copy -r --to-oci-layout` in the v1-oras-preseed workflow job, never by
// this generator). See docs/guides/fetch-v1.md §10.
//
//	go run ./scripts/fetch-v1/genfixtures --write      regenerate every fixture in place
//	go run ./scripts/fetch-v1/genfixtures --check      regenerate into a temp dir and diff
//	                                                    against what is on disk; exit 1 on drift
//	go run ./scripts/fetch-v1/genfixtures --selftest   prove --check catches drift, on a
//	                                                    throwaway copy — never the real tree
//
// Deterministic by construction: no time.Now(), no math/rand, no map
// iteration feeding output bytes directly (every map that does — OCI
// annotations — is sorted by encoding/json already). The same source
// always regenerates the same bytes.
package main

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
)

func repoRoot() string {
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		panic("genfixtures: runtime.Caller failed")
	}
	// this file: scripts/fetch-v1/genfixtures/main.go -> repo root is three
	// directories up.
	return filepath.Dir(filepath.Dir(filepath.Dir(filepath.Dir(file))))
}

const fixturesRelPath = "tests/fixtures/fetch-v1"

// layoutPresentOnDisk reports whether tests/fixtures/fetch-v1/layouts/<name>
// is physically present in this checkout right now, by checking for that
// layout's own "oci-layout" marker file (the one file every OCI image
// layout has, real or synthetic). Used only for externally-provided
// layouts this generator never writes itself (excludedFromManagement,
// below) — a case that depends on one of those must be gated on this, so
// it is never emitted into cases.json while the directory is absent (see
// cachecases.go's preseed-oras, and checkCacheLayoutsExist in cases.go).
func layoutPresentOnDisk(name string) bool {
	info, err := os.Stat(filepath.Join(repoRoot(), fixturesRelPath, "layouts", name, "oci-layout"))
	return err == nil && !info.IsDir()
}

// managedPrefixes are the fixture subtrees this generator owns outright:
// cleared and rewritten on --write, diffed in full on --check. "layouts" is
// managed too, EXCEPT layouts/oras-preseed/ (excludedFromManagement),
// written only by a real `oras copy --to-oci-layout` in the
// workflow_dispatch-only v1-oras-preseed job.
var managedPrefixes = []string{"trees", "http", "locks", "cases.json", "layouts"}
var excludedFromManagement = []string{"layouts/oras-preseed"}

func main() {
	write := false
	check := false
	selftest := false
	for _, arg := range os.Args[1:] {
		if arg == "--write" {
			write = true
		} else if arg == "--check" {
			check = true
		} else if arg == "--selftest" {
			selftest = true
		} else {
			fmt.Fprintf(os.Stderr, "genfixtures: unknown flag %q\n", arg)
			os.Exit(2)
		}
	}
	if !write && !check && !selftest {
		fmt.Fprintln(os.Stderr, "genfixtures: one of --write, --check or --selftest is required")
		os.Exit(2)
	}

	root := repoRoot()
	C = loadConstants(root)
	loadKeys(root)

	if selftest {
		runSelftest(root)
		fmt.Println("genfixtures --selftest: OK")
		return
	}

	fs := buildAll()

	if write {
		fixturesDir := filepath.Join(root, fixturesRelPath)
		for _, p := range managedPrefixes {
			if p == "layouts" {
				clearLayoutsExceptOrasPreseed(fixturesDir)
				continue
			}
			full := filepath.Join(fixturesDir, p)
			if err := os.RemoveAll(full); err != nil {
				fatalf("removing %s: %v", full, err)
			}
		}
		if err := fs.WriteTo(fixturesDir); err != nil {
			fatalf("writing fixtures: %v", err)
		}
		fmt.Printf("genfixtures --write: wrote %d files under %s\n", len(fs.Paths()), fixturesRelPath)
		return
	}

	if check {
		fixturesDir := filepath.Join(root, fixturesRelPath)
		problems := fs.Diff(fixturesDir, managedPrefixes, excludedFromManagement...)
		if len(problems) > 0 {
			fmt.Fprintln(os.Stderr, "genfixtures --check: drift between the generator and tests/fixtures/fetch-v1/:")
			for _, p := range problems {
				fmt.Fprintln(os.Stderr, "  "+p)
			}
			os.Exit(1)
		}
		fmt.Printf("genfixtures --check: %d files match byte-for-byte\n", len(fs.Paths()))
		return
	}
}

func fatalf(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "genfixtures: "+format+"\n", args...)
	os.Exit(1)
}

// clearLayoutsExceptOrasPreseed removes every child of
// tests/fixtures/fetch-v1/layouts/ except oras-preseed/, which this
// generator never writes and never deletes.
func clearLayoutsExceptOrasPreseed(fixturesDir string) {
	dir := filepath.Join(fixturesDir, "layouts")
	entries, err := os.ReadDir(dir)
	if err != nil {
		if os.IsNotExist(err) {
			return
		}
		fatalf("reading %s: %v", dir, err)
	}
	for _, e := range entries {
		if e.Name() == "oras-preseed" {
			continue
		}
		full := filepath.Join(dir, e.Name())
		if err := os.RemoveAll(full); err != nil {
			fatalf("removing %s: %v", full, err)
		}
	}
}
