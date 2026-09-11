// Command chtypes is the Go SDK's artifact tool — the one CLI surface every
// chtypes SDK spells identically (docs/guides/fetch.md §6):
//
//	chtypes fetch <line>... [--all] [--platform <os-arch>] [--dest <dir>]
//	                        [--tag <t> | --url <base>] [--lock <file>] [--frozen]
//	                        [--force] [--offline]
//	chtypes verify [--dest <dir>]        re-hash every installed line against its manifest
//	chtypes list   [--dest <dir>]        what is installed, and what the release offers
//	chtypes where                        the registry directory fetch would write to
//
// Run it without installing anything:
//
//	go run github.com/wave-rf/chtypes/go/cmd/chtypes@latest fetch 25.8
//
// Progress goes to stderr; `fetch` prints the installed directory alone on
// stdout, so `dir="$(chtypes fetch 25.8)"` composes. Exit codes: 0 ok ·
// 1 verification failed · 2 usage · 3 source unreachable · 4 not published
// for this platform/line.
//
// Environment: CHTYPES_ARTIFACTS_URL (the host), CHTYPES_REGISTRY (where
// to install when --dest is not given), CHTYPES_TRUSTED_KEYS (replaces the
// embedded release key), CHTYPES_ALLOW_UNSIGNED=1 (skip the signature,
// loudly), CHTYPES_TARGET (default --platform), CHTYPES_DOWNLOAD_TOKEN.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"strings"

	"github.com/wave-rf/chtypes/go/chtypes"
)

const usageText = `usage:
  chtypes fetch <line>... [--all] [--platform <os-arch>] [--dest <dir>]
                          [--tag <t> | --url <base>] [--lock <file>] [--frozen]
                          [--force] [--offline]
  chtypes verify [--dest <dir>]        re-hash every installed line against its manifest
  chtypes list   [--dest <dir>] [--platform <os-arch>] [--tag <t> | --url <base>] [--offline]
                                       what is installed, and what the release offers
  chtypes where                        the registry directory fetch would write to

exit codes: 0 ok · 1 verification failed · 2 usage · 3 source unreachable · 4 not published
`

// usageError is exit 2.
type usageError struct{ msg string }

func (e *usageError) Error() string { return e.msg }

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	os.Exit(run(ctx, os.Args[1:], os.Stdout, os.Stderr))
}

func run(ctx context.Context, args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		fmt.Fprint(stderr, usageText)
		return 2
	}
	var err error
	switch args[0] {
	case "fetch":
		err = cmdFetch(ctx, args[1:], stdout, stderr)
	case "verify":
		err = cmdVerify(args[1:], stdout, stderr)
	case "list":
		err = cmdList(ctx, args[1:], stdout, stderr)
	case "where":
		err = cmdWhere(args[1:], stdout, stderr)
	case "help", "-h", "--help", "-help":
		fmt.Fprint(stdout, usageText)
		return 0
	default:
		err = &usageError{fmt.Sprintf("unknown command %q", args[0])}
	}
	if err == nil {
		return 0
	}
	var ue *usageError
	if errors.As(err, &ue) {
		fmt.Fprintf(stderr, "chtypes: %s\n", ue.msg)
		fmt.Fprint(stderr, usageText)
		return 2
	}
	if errors.Is(err, flag.ErrHelp) {
		return 0
	}
	fmt.Fprintf(stderr, "%s\n", err)
	return chtypes.ExitCode(err)
}

// newFlagSet is a stdlib FlagSet whose own error output is the usage text
// on stderr.
func newFlagSet(name string, stderr io.Writer) *flag.FlagSet {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	fs.SetOutput(stderr)
	fs.Usage = func() { fmt.Fprint(stderr, usageText) }
	return fs
}

// parseInterleaved parses flags that may come before, between or after
// positional arguments (`chtypes fetch 25.8 --dest x`), which stdlib flag
// alone stops at. Returns the positionals in order.
func parseInterleaved(fs *flag.FlagSet, args []string) ([]string, error) {
	var positional []string
	for {
		if err := fs.Parse(args); err != nil {
			if errors.Is(err, flag.ErrHelp) {
				return nil, err
			}
			return nil, &usageError{err.Error()}
		}
		rest := fs.Args()
		if len(rest) == 0 {
			return positional, nil
		}
		if rest[0] == "--" {
			return append(positional, rest[1:]...), nil
		}
		positional = append(positional, rest[0])
		args = rest[1:]
	}
}

type sourceFlags struct {
	platform, dest, tag, url string
	offline                  bool
}

func (s *sourceFlags) bind(fs *flag.FlagSet) {
	fs.StringVar(&s.platform, "platform", "", "platform key <os>-<arch> (default: this host, or $CHTYPES_TARGET)")
	fs.StringVar(&s.dest, "dest", "", "registry directory to install into (default: `chtypes where`)")
	fs.StringVar(&s.tag, "tag", "", "release tag on the artifacts host (default: the rolling `artifacts` release)")
	fs.StringVar(&s.url, "url", "", "any base: an http(s) mirror, a file:// path, a local directory")
	fs.BoolVar(&s.offline, "offline", false, "never read the source")
}

func (s *sourceFlags) options(stderr io.Writer) (chtypes.FetchOptions, error) {
	if s.tag != "" && s.url != "" {
		return chtypes.FetchOptions{}, &usageError{"--url names a full base; --tag selects a release on the artifacts host — pass one"}
	}
	if s.platform != "" && !chtypes.ValidPlatform(s.platform) {
		return chtypes.FetchOptions{}, &usageError{fmt.Sprintf("not a known platform key: %s ((linux|darwin)-(arm64|amd64))", s.platform)}
	}
	return chtypes.FetchOptions{
		Dest: s.dest, Platform: s.platform, Tag: s.tag, URL: s.url, Offline: s.offline,
		Progress: stderr,
	}, nil
}

func cmdFetch(ctx context.Context, args []string, stdout, stderr io.Writer) error {
	fs := newFlagSet("fetch", stderr)
	var sf sourceFlags
	sf.bind(fs)
	var all, frozen, force bool
	var lock string
	fs.BoolVar(&all, "all", false, "every line the release publishes for the platform")
	fs.StringVar(&lock, "lock", "", "write the installed asset and sha256 to this lock file (with --frozen: the lock to enforce)")
	fs.BoolVar(&frozen, "frozen", false, "refuse anything the lock file does not pin (default lock: chtypes.lock)")
	fs.BoolVar(&force, "force", false, "re-download an installed line")
	lines, err := parseInterleaved(fs, args)
	if err != nil {
		return err
	}
	opts, err := sf.options(stderr)
	if err != nil {
		return err
	}
	opts.LockFile, opts.Frozen, opts.Force = lock, frozen, force
	switch {
	case all && len(lines) > 0:
		return &usageError{fmt.Sprintf("--all installs every published line; drop the version argument (%s)", strings.Join(lines, " "))}
	case !all && len(lines) == 0:
		return &usageError{"a ClickHouse version spelling is required (or --all)"}
	}
	if all {
		installed, err := chtypes.FetchAll(ctx, opts)
		for _, inst := range installed {
			fmt.Fprintln(stdout, inst.Dir)
		}
		return err
	}
	for _, line := range lines {
		inst, err := chtypes.Ensure(ctx, line, opts)
		if err != nil {
			return err
		}
		fmt.Fprintln(stdout, inst.Dir)
	}
	return nil
}

func cmdVerify(args []string, stdout, stderr io.Writer) error {
	fs := newFlagSet("verify", stderr)
	var dest, platform string
	fs.StringVar(&dest, "dest", "", "registry directory (default: `chtypes where`)")
	fs.StringVar(&platform, "platform", "", "platform key, for the default directory")
	if rest, err := parseInterleaved(fs, args); err != nil {
		return err
	} else if len(rest) > 0 {
		return &usageError{fmt.Sprintf("verify takes no positional arguments (%s)", strings.Join(rest, " "))}
	}
	dir, err := registryDir(dest, platform)
	if err != nil {
		return err
	}
	results, err := chtypes.VerifyInstalled(dir)
	if err != nil {
		return err
	}
	if len(results) == 0 {
		fmt.Fprintf(stdout, "nothing installed under %s\n", dir)
		return nil
	}
	bad := 0
	for _, r := range results {
		switch {
		case r.Err != nil:
			bad++
			fmt.Fprintf(stdout, "MISSING  %-6s %-20s %s: %v\n", r.Line, r.Version, r.Dir, r.Err)
		case r.OK:
			fmt.Fprintf(stdout, "ok       %-6s %-20s %s/%s sha256 %s\n", r.Line, r.Version, r.Dir, r.Library, r.Got)
		default:
			bad++
			fmt.Fprintf(stdout, "MISMATCH %-6s %-20s %s/%s hashes %s, manifest says %s\n", r.Line, r.Version, r.Dir, r.Library, r.Got, r.LibrarySHA256)
		}
	}
	if g := filepath.Join(dir, "sdk-goldens.json"); fileExists(g) {
		fmt.Fprintf(stdout, "ok       %-6s %s\n", "golden", g)
	} else {
		fmt.Fprintf(stdout, "absent   %-6s %s (fetch installs it; the golden tests skip without it)\n", "golden", g)
	}
	fmt.Fprintf(stdout, "%d installed, %d verified, %d bad (%s)\n", len(results), len(results)-bad, bad, dir)
	if bad > 0 {
		return &chtypes.ArtifactError{Code: chtypes.CodeArtifactCorrupt, Platform: platform,
			Msg: fmt.Sprintf("chtypes: %d of %d installed line(s) under %s do not hash what their manifest says [%s]", bad, len(results), dir, chtypes.CodeArtifactCorrupt)}
	}
	return nil
}

func cmdList(ctx context.Context, args []string, stdout, stderr io.Writer) error {
	fs := newFlagSet("list", stderr)
	var sf sourceFlags
	sf.bind(fs)
	if rest, err := parseInterleaved(fs, args); err != nil {
		return err
	} else if len(rest) > 0 {
		return &usageError{fmt.Sprintf("list takes no positional arguments (%s)", strings.Join(rest, " "))}
	}
	opts, err := sf.options(stderr)
	if err != nil {
		return err
	}
	dir, err := registryDir(sf.dest, sf.platform)
	if err != nil {
		return err
	}
	installed, err := chtypes.ListInstalled(dir)
	if err != nil {
		return err
	}
	have := map[string]chtypes.Installed{}
	fmt.Fprintf(stdout, "installed (%s):\n", dir)
	if len(installed) == 0 {
		fmt.Fprintln(stdout, "  (nothing)")
	}
	for _, inst := range installed {
		have[inst.Line] = inst
		fmt.Fprintf(stdout, "  %-6s %-20s %s\n", inst.Line, inst.Version, inst.Library)
	}
	if sf.offline {
		fmt.Fprintln(stdout, "release: not read (--offline)")
		return nil
	}
	index, err := chtypes.ListRelease(ctx, opts)
	if err != nil {
		return err
	}
	platform := sf.platform
	if platform == "" {
		platform = os.Getenv("CHTYPES_TARGET")
	}
	if platform == "" {
		platform = chtypes.HostPlatform()
	}
	signed := "unsigned"
	if index.SignedBy != "" {
		signed = "signed by key " + index.SignedBy
	}
	fmt.Fprintf(stdout, "release (%s, %s, %s):\n", index.Source, platform, signed)
	n := 0
	for _, a := range index.Artifacts {
		if a.Platform() != platform {
			continue
		}
		n++
		state := ""
		if inst, ok := have[a.ClickHouseMinor]; ok {
			if inst.Version == a.ClickHouseVersion {
				state = "  (installed)"
			} else {
				state = "  (installed: " + inst.Version + ")"
			}
		}
		fmt.Fprintf(stdout, "  %-6s %-20s b%-3d %s  %d bytes%s\n", a.ClickHouseMinor, a.ClickHouseVersion, a.BuildNumber(), a.File, a.Bytes, state)
	}
	if n == 0 {
		fmt.Fprintf(stdout, "  (nothing for %s)\n", platform)
	}
	return nil
}

func cmdWhere(args []string, stdout, stderr io.Writer) error {
	fs := newFlagSet("where", stderr)
	var platform string
	fs.StringVar(&platform, "platform", "", "platform key (default: this host)")
	if rest, err := parseInterleaved(fs, args); err != nil {
		return err
	} else if len(rest) > 0 {
		return &usageError{fmt.Sprintf("where takes no positional arguments (%s)", strings.Join(rest, " "))}
	}
	dir, err := registryDir("", platform)
	if err != nil {
		return err
	}
	fmt.Fprintln(stdout, dir)
	// The served golden set lives beside the artifacts, and "where is my
	// registry" is exactly when someone wants to know whether it is there.
	g := filepath.Join(dir, "sdk-goldens.json")
	if st, err := os.Stat(g); err == nil {
		fmt.Fprintf(stdout, "%s  (golden set, %d bytes)\n", g, st.Size())
	} else {
		fmt.Fprintf(stdout, "%s  (golden set: not fetched — the golden tests will skip)\n", g)
	}
	return nil
}

// registryDir resolves --dest the way a fetch does (docs/guides/fetch.md §1):
// explicit, else $CHTYPES_REGISTRY, else the per-user cache — the cache
// alone for a platform other than this host's.
func registryDir(dest, platform string) (string, error) {
	if dest != "" {
		return dest, nil
	}
	if platform == "" {
		platform = os.Getenv("CHTYPES_TARGET")
	}
	if platform == "" {
		platform = chtypes.HostPlatform()
	}
	if !chtypes.ValidPlatform(platform) {
		return "", &usageError{fmt.Sprintf("not a known platform key: %s ((linux|darwin)-(arm64|amd64))", platform)}
	}
	var dir string
	if platform == chtypes.HostPlatform() {
		dir = chtypes.FetchRegistryDir("")
	} else {
		dir = chtypes.DefaultRegistryDirFor(platform)
	}
	if dir == "" {
		return "", errors.New("chtypes: cannot determine a registry directory (no home directory); pass --dest or set CHTYPES_REGISTRY")
	}
	return dir, nil
}

// fileExists is the one question `verify` and `where` ask about the served
// golden set: is it beside the artifacts, or will the golden tests skip?
func fileExists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}
