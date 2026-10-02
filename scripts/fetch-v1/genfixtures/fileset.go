package main

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
)

// FileSet is the generator's whole output, held in memory until main.go
// decides whether to write it to disk (--write) or diff it against what is
// already there (--check). Keying by the file's path relative to
// tests/fixtures/fetch-v1/ keeps every builder (tree, layout, http-script,
// lock, cases.json) oblivious to whether it is ultimately written or merely
// compared.
type FileSet struct {
	files map[string][]byte
}

func NewFileSet() *FileSet {
	return &FileSet{files: map[string][]byte{}}
}

// Put adds a file. It panics on a path collision with different content:
// two cases silently sharing one path but disagreeing about its bytes is a
// generator bug, not a fixture to tolerate.
func (fs *FileSet) Put(relPath string, content []byte) {
	if existing, ok := fs.files[relPath]; ok {
		if string(existing) != string(content) {
			panic(fmt.Sprintf("genfixtures: %s was written twice with different content", relPath))
		}
		return
	}
	fs.files[relPath] = content
}

func (fs *FileSet) Paths() []string {
	out := make([]string, 0, len(fs.files))
	for p := range fs.files {
		out = append(out, p)
	}
	sort.Strings(out)
	return out
}

func (fs *FileSet) Get(relPath string) ([]byte, bool) {
	b, ok := fs.files[relPath]
	return b, ok
}

// WriteTo writes every file in the set under root, creating directories as
// needed. It does not remove anything first; the caller decides what to
// clear (main.go clears the managed subtrees before regenerating).
func (fs *FileSet) WriteTo(root string) error {
	for _, p := range fs.Paths() {
		full := filepath.Join(root, p)
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			return err
		}
		if err := os.WriteFile(full, fs.files[p], 0o644); err != nil {
			return err
		}
	}
	return nil
}

// Diff compares this set against what is physically present under root,
// restricted to paths under any of the given managed prefixes, and
// excluding anything under excludeDirs (so --check never complains about
// test-key/, other-key/, or layouts/oras-preseed/, which this generator
// does not own even though it owns the rest of layouts/). It reports
// missing files, extra files, and files whose bytes differ.
func (fs *FileSet) Diff(root string, managedPrefixes []string, excludeDirs ...string) []string {
	var problems []string

	onDisk := map[string][]byte{}
	for _, prefix := range managedPrefixes {
		base := filepath.Join(root, prefix)
		_ = filepath.Walk(base, func(path string, info os.FileInfo, err error) error {
			if err != nil {
				if os.IsNotExist(err) {
					return nil
				}
				return err
			}
			rel, rerr := filepath.Rel(root, path)
			if rerr != nil {
				return rerr
			}
			rel = filepath.ToSlash(rel)
			for _, ex := range excludeDirs {
				if rel == ex || len(rel) > len(ex) && rel[:len(ex)+1] == ex+"/" {
					if info.IsDir() {
						return filepath.SkipDir
					}
					return nil
				}
			}
			if info.IsDir() {
				return nil
			}
			b, rerr := os.ReadFile(path)
			if rerr != nil {
				return rerr
			}
			onDisk[rel] = b
			return nil
		})
	}

	for _, p := range fs.Paths() {
		diskBytes, ok := onDisk[p]
		if !ok {
			problems = append(problems, fmt.Sprintf("missing on disk: %s", p))
			continue
		}
		if string(diskBytes) != string(fs.files[p]) {
			problems = append(problems, fmt.Sprintf("content differs: %s", p))
		}
		delete(onDisk, p)
	}
	extra := make([]string, 0, len(onDisk))
	for p := range onDisk {
		extra = append(extra, p)
	}
	sort.Strings(extra)
	for _, p := range extra {
		problems = append(problems, fmt.Sprintf("extra on disk (generator no longer produces it): %s", p))
	}
	sort.Strings(problems)
	return problems
}
