package main

import (
	"os"
	"path/filepath"
)

// selftest.go — proves --check actually catches drift, the same discipline
// every gate in this repository follows (scripts/lint-public.sh,
// scripts/check-abi-decls.py, ...): a checker nobody has seen fail is not a
// checker. Runs entirely in a throwaway temp directory; never touches the
// real tests/fixtures/fetch-v1/ tree.
func runSelftest(root string) {
	fs := buildAll()
	if len(fs.Paths()) == 0 {
		fatalf("selftest: buildAll produced zero files")
	}

	tmp, err := os.MkdirTemp("", "genfixtures-selftest-*")
	if err != nil {
		fatalf("selftest: %v", err)
	}
	defer os.RemoveAll(tmp)

	if err := fs.WriteTo(tmp); err != nil {
		fatalf("selftest: writing to temp dir: %v", err)
	}

	// 1. A fresh write must diff clean against itself.
	clean := fs.Diff(tmp, managedPrefixes)
	if len(clean) != 0 {
		fatalf("selftest: a fresh --write is not clean against its own --check:\n  %v", clean)
	}

	// 2. Flipping one byte of one file must be caught.
	victim := fs.Paths()[0]
	victimPath := filepath.Join(tmp, victim)
	content, err := os.ReadFile(victimPath)
	if err != nil {
		fatalf("selftest: %v", err)
	}
	flipped := append([]byte(nil), content...)
	flipped[0] ^= 0xFF
	if err := os.WriteFile(victimPath, flipped, 0o644); err != nil {
		fatalf("selftest: %v", err)
	}
	problems := fs.Diff(tmp, managedPrefixes)
	if len(problems) == 0 {
		fatalf("selftest: flipping a byte of %s was not caught by --check", victim)
	}

	// 3. Restore it, delete a different file entirely (an "extra file on
	// disk" the generator no longer produces must also be caught — this is
	// what protects a deleted case from leaving its old fixture behind).
	if err := os.WriteFile(victimPath, content, 0o644); err != nil {
		fatalf("selftest: %v", err)
	}
	// Always under trees/ (never beside victim, which may itself be
	// cases.json at the tree root — a prefix Diff walks as a single named
	// file, not a directory, so a sibling placed next to it would never be
	// scanned by any managed-prefix walk).
	extraPath := filepath.Join(tmp, "trees", "unexpected-extra-file.json")
	if err := os.WriteFile(extraPath, []byte("{}"), 0o644); err != nil {
		fatalf("selftest: %v", err)
	}
	problems = fs.Diff(tmp, managedPrefixes)
	if len(problems) == 0 {
		fatalf("selftest: an extra file under a managed prefix was not caught by --check")
	}
	foundExtra := false
	for _, p := range problems {
		if len(p) >= len("extra on disk") && p[:len("extra on disk")] == "extra on disk" {
			foundExtra = true
		}
	}
	if !foundExtra {
		fatalf("selftest: the extra-file problem was not reported as such:\n  %v", problems)
	}

	// 4. Deleting a file the generator DOES produce must be caught as missing.
	if err := os.Remove(extraPath); err != nil {
		fatalf("selftest: %v", err)
	}
	if err := os.Remove(victimPath); err != nil {
		fatalf("selftest: %v", err)
	}
	problems = fs.Diff(tmp, managedPrefixes)
	if len(problems) == 0 {
		fatalf("selftest: a missing generated file was not caught by --check")
	}
}
