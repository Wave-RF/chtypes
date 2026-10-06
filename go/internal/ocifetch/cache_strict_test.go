package ocifetch

// cache_strict_test.go — public issue #486: every cache fault, in the default
// mode (absent, plus a warning naming the path) and in strict mode
// (CHTYPES_CACHE_UNUSABLE naming the path and the reason, and no fall-through
// to a system dir), and the class of a failed write. Every fault is made by a
// real chmod, a real write or a real install, and each chmod is proved to have
// taken effect before the case runs.

import (
	"context"
	"errors"
	"io/fs"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// denied proves a chmod took effect for this process: path must refuse to be
// read (a directory listed, a file opened). Root ignores modes, so a test that
// needs this skips loudly when it runs as root.
func denied(t *testing.T, path string) {
	t.Helper()
	if os.Geteuid() == 0 {
		t.Skip("SKIPPED: running as root, which a mode cannot deny")
	}
	var err error
	if fi, serr := os.Lstat(path); serr == nil && fi.IsDir() {
		_, err = os.ReadDir(path)
	} else {
		_, err = os.ReadFile(path)
	}
	if !errors.Is(err, fs.ErrPermission) {
		t.Fatalf("positive control: reading %s gave %v, want permission denied", path, err)
	}
}

// chmod sets mode and restores 0755/0644 when the test ends, so the test's own
// directory can be removed.
func chmod(t *testing.T, path string, mode fs.FileMode) {
	t.Helper()
	fi, err := os.Lstat(path)
	if err != nil {
		t.Fatal(err)
	}
	restore := fs.FileMode(0o644)
	if fi.IsDir() {
		restore = 0o755
	}
	if err := os.Chmod(path, mode); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(path, restore) })
}

type faultCase struct {
	name   string
	reason string
	warns  bool // the default mode warns about it (K1, K2)
	// apply makes the fault on a cache holding one good 26.8 record at
	// entry, and returns the path the error and the warning must name.
	apply func(t *testing.T, cache, entry string) string
}

func faultCases() []faultCase {
	return []faultCase{
		{"root-000", ReasonUnreadableRoot, true, func(t *testing.T, cache, _ string) string {
			chmod(t, cache, 0)
			denied(t, cache)
			return cache
		}},
		{"unpacked-000", ReasonUnreadableRoot, true, func(t *testing.T, cache, _ string) string {
			p := filepath.Join(cache, UnpackedDirName())
			chmod(t, p, 0)
			denied(t, p)
			return p
		}},
		{"entry-000", ReasonUnreadableEntry, true, func(t *testing.T, _, entry string) string {
			chmod(t, entry, 0)
			denied(t, entry)
			return entry
		}},
		{"record-000", ReasonUnreadableEntry, true, func(t *testing.T, _, entry string) string {
			p := filepath.Join(entry, CacheVerifiedRecord)
			chmod(t, p, 0)
			denied(t, p)
			return p
		}},
		{"record-garbage-noblobs", ReasonUnacceptableRecord, false, func(t *testing.T, cache, entry string) string {
			p := filepath.Join(entry, CacheVerifiedRecord)
			if err := os.WriteFile(p, []byte("not json {"), 0o644); err != nil {
				t.Fatal(err)
			}
			if _, err := os.Lstat(filepath.Join(cache, "blobs")); !errors.Is(err, fs.ErrNotExist) {
				t.Fatalf("positive control: %s has blobs to re-verify from", cache)
			}
			return p
		}},
		{"root-is-a-file", ReasonNotADirectory, true, func(t *testing.T, cache, _ string) string {
			if err := os.RemoveAll(cache); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(cache, []byte("not a cache"), 0o644); err != nil {
				t.Fatal(err)
			}
			return cache
		}},
		{"layout-0x", ReasonLayout0x, false, func(t *testing.T, cache, _ string) string {
			if err := os.RemoveAll(cache); err != nil {
				t.Fatal(err)
			}
			writeZeroXRegistry(t, cache)
			return cache
		}},
	}
}

func boolp(b bool) *bool { return &b }

// TestEveryCacheFaultInBothModes: the K1 to K4 table of docs/guides/fetch-v1.md
// §1, through resolve_installed, list_installed, verify_installed and an
// offline ensure, without and with a system dir that holds a readable build.
func TestEveryCacheFaultInBothModes(t *testing.T) {
	for _, fc := range faultCases() {
		t.Run(fc.name, func(t *testing.T) {
			tmp := t.TempDir()
			cache, sys := filepath.Join(tmp, "cache"), filepath.Join(tmp, "system")
			entry := writeRecordRoot(t, cache, "26.8.1.1", "20260801.000001")
			sysEntry := writeRecordRoot(t, sys, "26.8.1.1", "20260801.000001")
			path := fc.apply(t, cache, entry)
			warning := path + " could not be read ("

			for _, withSys := range []bool{false, true} {
				sysDirs := []string{}
				if withSys {
					sysDirs = []string{sys}
				}
				// Default mode: the fault reads as absent, with a warning for K1/K2.
				opts := &Options{CacheDir: cache, SystemDirs: sysDirs, StrictCache: boolp(false)}
				res, err := ResolveInstalled(Request{Spelling: "26.8"}, "linux-arm64", opts)
				switch {
				case err != nil:
					t.Fatalf("default, system %v: ResolveInstalled = %v", withSys, err)
				case withSys && (res == nil || res.Dir != sysEntry):
					t.Fatalf("default: answered %+v, want the system dir's %s", res, sysEntry)
				case !withSys && res != nil:
					t.Fatalf("default: answered %s from a faulted cache", res.Dir)
				}
				notes := strings.Join(MissingNotes(opts), "\n")
				if fc.warns != strings.Contains(notes, warning) {
					t.Errorf("default, system %v: notes %q, want a warning naming %s: %v", withSys, notes, path, fc.warns)
				}
				if withSys && fc.warns && !strings.Contains(strings.Join(res.Warnings, "\n"), warning) {
					t.Errorf("default: the answer's warnings %q do not name %s", res.Warnings, path)
				}
				if _, err := ListInstalled(opts); err != nil {
					t.Errorf("default: ListInstalled = %v", err)
				}
				if _, err := VerifyInstalled(opts); err != nil {
					t.Errorf("default: VerifyInstalled = %v", err)
				}

				// Strict mode: CHTYPES_CACHE_UNUSABLE, the same with or without a system dir.
				opts.StrictCache = boolp(true)
				off := *opts
				off.Offline = true
				for name, call := range map[string]func() error{
					"ResolveInstalled": func() error { _, err := ResolveInstalled(Request{Spelling: "26.8"}, "linux-arm64", opts); return err },
					"ListInstalled":    func() error { _, err := ListInstalled(opts); return err },
					"VerifyInstalled":  func() error { _, err := VerifyInstalled(opts); return err },
					"Ensure(offline)": func() error {
						_, err := Ensure(context.Background(), Request{Spelling: "26.8", Platform: "linux-arm64"}, &off)
						return err
					},
					"ProbeCache": func() error { _, err := ProbeCache(opts); return err },
				} {
					err := call()
					var fe *FetchError
					if !errors.As(err, &fe) || !errors.Is(err, ErrCacheUnusable) || fe.Reason != fc.reason || fe.Path != path {
						t.Errorf("strict, system %v: %s = %v, want CHTYPES_CACHE_UNUSABLE %s at %s", withSys, name, err, fc.reason, path)
						continue
					}
					if !strings.Contains(fe.Msg, path+" is unusable as a cache: "+fc.reason) || ExitCode(err) != 9 {
						t.Errorf("strict: %s message %q, exit %d", name, fe.Msg, ExitCode(err))
					}
					if fc.warns && fe.OSError == "" {
						t.Errorf("strict: %s names no errno", name)
					}
				}
			}
		})
	}
}

// TestStrictComesFromTheOptionThenTheEnvironment: CHTYPES_CACHE_STRICT=1 turns
// strict mode on, and an explicit StrictCache wins over it either way.
func TestStrictComesFromTheOptionThenTheEnvironment(t *testing.T) {
	tmp := t.TempDir()
	cache := filepath.Join(tmp, "cache")
	writeZeroXRegistry(t, cache)
	for _, c := range []struct {
		env    string
		option *bool
		strict bool
	}{
		{"", nil, false}, {"1", nil, true}, {"0", nil, false}, {"1", boolp(false), false}, {"", boolp(true), true},
	} {
		t.Setenv(EnvCacheStrictName, c.env)
		_, err := ProbeCache(&Options{CacheDir: cache, SystemDirs: []string{}, StrictCache: c.option})
		if got := errors.Is(err, ErrCacheUnusable); got != c.strict {
			t.Errorf("env %q, option %v: strict = %v, want %v (%v)", c.env, c.option, got, c.strict, err)
		}
	}
}

// TestAStrictSystemDirFaultIsAnError: a system dir that exists but cannot be
// read is an error in strict mode, and one that does not exist is skipped.
func TestAStrictSystemDirFaultIsAnError(t *testing.T) {
	tmp := t.TempDir()
	cache, sys := filepath.Join(tmp, "cache"), filepath.Join(tmp, "system")
	writeRecordRoot(t, cache, "26.8.1.1", "20260801.000001")
	writeRecordRoot(t, sys, "26.8.1.1", "20260801.000001")
	opts := &Options{CacheDir: cache, SystemDirs: []string{filepath.Join(tmp, "absent"), sys}, StrictCache: boolp(true)}
	if _, err := ResolveInstalled(Request{Spelling: "26.8"}, "linux-arm64", opts); err != nil {
		t.Fatalf("a missing system dir is skipped: %v", err)
	}
	chmod(t, sys, 0)
	denied(t, sys)
	_, err := ResolveInstalled(Request{Spelling: "26.8"}, "linux-arm64", opts)
	var fe *FetchError
	if !errors.As(err, &fe) || fe.Code != CodeCacheUnusable || fe.Path != sys || fe.Reason != ReasonUnreadableRoot {
		t.Fatalf("an unreadable system dir in strict mode = %v", err)
	}
}

// TestAFailedWriteIsCacheUnusable: a write the fetch layer needed that failed
// is CHTYPES_CACHE_UNUSABLE with reason unwritable in the default mode too,
// never a bare filesystem error (which the public layer read as misuse).
func TestAFailedWriteIsCacheUnusable(t *testing.T) {
	reg, _ := buildFakeRegistry(t)
	srv := httptest.NewServer(reg.handler())
	defer srv.Close()
	opts := testOptions(t, srv.URL)
	opts.SystemDirs = []string{}
	chmod(t, opts.CacheDir, 0o555)
	if os.Geteuid() == 0 {
		t.Skip("SKIPPED: running as root, which a mode cannot deny")
	}
	if err := os.Mkdir(filepath.Join(opts.CacheDir, "probe"), 0o755); !errors.Is(err, fs.ErrPermission) {
		t.Fatalf("positive control: a mkdir in a 0555 cache gave %v", err)
	}
	_, err := Ensure(context.Background(), Request{Spelling: "26.8", Platform: "linux-arm64"}, opts)
	var fe *FetchError
	if !errors.As(err, &fe) || fe.Code != CodeCacheUnusable || fe.Reason != ReasonUnwritable || fe.OSError != "EACCES" ||
		!strings.HasPrefix(fe.Path, opts.CacheDir) || ExitCode(err) != 9 {
		t.Fatalf("Ensure into a read-only cache = %#v", err)
	}
}
