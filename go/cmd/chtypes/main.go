// Command chtypes is the Go SDK's artifact tool: the one CLI surface every
// chtypes SDK spells identically (docs/guides/fetch-v1.md, sections 6 and 8):
//
//	chtypes fetch <spelling>... | --all [--platform <os-arch>] [--cache <dir>]
//	                                    [--lock <file>] [--frozen] [--offline] [--update] [--strict]
//	chtypes verify [--cache <dir>] [--strict]
//	                                    re-verify the installed cache
//	chtypes list   [--cache <dir>] [--offline] [--strict]
//	                                    what is installed, and what is published
//	chtypes where  [--cache <dir>] [--strict] [--all]
//	                                    the cache root; --all: every search directory
//
// Run it without installing anything:
//
//	go run github.com/wave-rf/chtypes/go/v2/cmd/chtypes@latest fetch 26.8
//
// Progress and warnings go to stderr; `fetch` prints the installed directory
// of each request alone on stdout, so `dir="$(chtypes fetch 26.8)"` composes.
// Exit statuses: 0 ok, 2 usage, and for a failure the status of its error code
// in spec/fetch-v1/constants.json (docs/guides/fetch-v1.md section 8), read
// from the generated table, never hard-coded here.
//
// Environment: CHTYPES_CACHE (the cache: this 2.0.0-dev SDK uses its subroot
// <CHTYPES_CACHE>/v2-dev), CHTYPES_DOWNLOAD_TOKEN, CHTYPES_TARGET (the default
// --platform) and CHTYPES_CACHE_STRICT=1 (--strict).
//
// 2.0.0-dev: UNSTABLE, staging only, not for production. This CLI speaks the
// ABI v2 dev channel (spec/abi-v2/docs.md, rules r5 and r6): it fetches only
// from https://registry-staging.wavehouse.dev/chtypes/v2-dev and trusts only
// the staging key; CHTYPES_ARTIFACTS_URL, CHTYPES_TRUSTED_KEYS and
// CHTYPES_ALLOW_UNSIGNED are ignored, each with one warning; --lock, --frozen
// and --update are refused before any network call; its default cache is
// ${XDG_CACHE_HOME:-~/.cache}/chtypes/v2-dev.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"runtime/debug"
	"sort"
	"strings"

	"github.com/wave-rf/chtypes/go/v2/internal/ocifetch"
)

const usageText = `usage:
  chtypes fetch <spelling>... | --all [--platform <os-arch>] [--cache <dir>]
                                      [--lock <file>] [--frozen] [--offline] [--update] [--strict]
  chtypes verify [--cache <dir>] [--strict]
                                      re-verify the installed cache
  chtypes list   [--cache <dir>] [--offline] [--strict]
                                      what is installed, and what is published
  chtypes where  [--cache <dir>] [--strict] [--all]
                                      the cache root; --all: every directory searched, in order
  chtypes --version

--offline (or CHTYPES_OFFLINE=1): read the cache only, make no request
--strict (or CHTYPES_CACHE_STRICT=1): a cache that cannot be read is CHTYPES_CACHE_UNUSABLE, never "not installed"
exit statuses: 0 ok, 2 usage, otherwise the failure's own status (docs/guides/fetch-v1.md section 8)

2.0.0-dev: UNSTABLE, staging only, not for production. Fetches only from the staging dev channel and
trusts only its key; --lock, --frozen and --update are refused; CHTYPES_ARTIFACTS_URL,
CHTYPES_TRUSTED_KEYS and CHTYPES_ALLOW_UNSIGNED are ignored (spec/abi-v2/docs.md, rule r6).
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
	// -h and --help print the usage to stdout and exit 0 wherever they appear.
	for _, a := range args {
		if a == "--" {
			break
		}
		if a == "-h" || a == "--help" {
			fmt.Fprint(stdout, usageText)
			return 0
		}
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
	case "--version":
		fmt.Fprintf(stdout, "chtypes %s\n", version())
		return 0
	default:
		err = &usageError{fmt.Sprintf("unknown command %q", args[0])}
	}
	if err == nil {
		return 0
	}
	if errors.Is(err, flag.ErrHelp) {
		return 0
	}
	var ue *usageError
	if errors.As(err, &ue) {
		fmt.Fprintf(stderr, "chtypes: %s\n", ue.msg)
		fmt.Fprint(stderr, usageText)
		return 2
	}
	fmt.Fprintf(stderr, "%s\n", err)
	return exitStatus(err)
}

// exitStatus is the process status for a failure: the status of its shared
// error code, from the generated table; any other failure is a refused
// request or option, which is a usage error.
func exitStatus(err error) int {
	var fe *ocifetch.FetchError
	if errors.As(err, &fe) {
		return fe.Code.ExitCode()
	}
	return 2
}

// version is the binding's own version as the build reports it, without the
// module path's "v"; an untagged or dev build is 0.0.0-dev.
func version() string {
	if bi, ok := debug.ReadBuildInfo(); ok && bi.Main.Version != "" && bi.Main.Version != "(devel)" {
		return strings.TrimPrefix(bi.Main.Version, "v")
	}
	return "0.0.0-dev"
}

func newFlagSet(name string, stderr io.Writer) *flag.FlagSet {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	fs.SetOutput(stderr)
	fs.Usage = func() { fmt.Fprint(stderr, usageText) }
	return fs
}

// parseInterleaved parses flags that may come before, between or after the
// positional arguments, which the standard flag package alone does not.
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

type commonFlags struct {
	platform, cache string
	offline, strict bool
}

func (c *commonFlags) bind(fs *flag.FlagSet, withPlatform, withOffline bool) {
	fs.StringVar(&c.cache, "cache", "", "the cache root (default: $CHTYPES_CACHE, else the per-user cache; `chtypes where`)")
	fs.BoolVar(&c.strict, "strict", false, "a cache that cannot be read is CHTYPES_CACHE_UNUSABLE, never not-installed (default: $CHTYPES_CACHE_STRICT=1)")
	if withPlatform {
		fs.StringVar(&c.platform, "platform", "", "platform key <os>-<arch> (default: this host, or $CHTYPES_TARGET)")
	}
	if withOffline {
		fs.BoolVar(&c.offline, "offline", false, "read the cache only; never the network (default: $CHTYPES_OFFLINE=1)")
	}
}

// options are the fetch options every command shares: the cache, and strict
// mode when --strict asks for it (else CHTYPES_CACHE_STRICT decides).
func (c *commonFlags) options() *ocifetch.Options {
	opts := &ocifetch.Options{CacheDir: c.cache, Offline: c.offline}
	if c.strict {
		strict := true
		opts.StrictCache = &strict
	}
	return opts
}

// strictMode is whether strict mode is on: --strict, else CHTYPES_CACHE_STRICT=1.
func (c *commonFlags) strictMode() bool {
	return c.strict || os.Getenv(ocifetch.EnvCacheStrictName) == "1"
}

// offlineMode is whether the fetch is offline: --offline, else CHTYPES_OFFLINE=1.
func (c *commonFlags) offlineMode() bool {
	return c.offline || os.Getenv(ocifetch.EnvOfflineName) == "1"
}

// printNotes prints what the cache says about itself in the default mode: a
// warning per root or entry it could not read, and the 0.x hint.
func printNotes(stderr io.Writer, opts *ocifetch.Options) {
	for _, note := range ocifetch.MissingNotes(opts) {
		fmt.Fprintf(stderr, "chtypes: %s\n", note)
	}
}

// platformKey is the requested platform, validated, or "" for the host's own.
func (c *commonFlags) platformKey() (string, error) {
	p := c.platform
	if p == "" {
		p = os.Getenv("CHTYPES_TARGET")
	}
	if p != "" && !knownPlatform(p) {
		keys := make([]string, 0, len(ocifetch.Platforms))
		for _, k := range ocifetch.Platforms {
			keys = append(keys, k.Key)
		}
		return "", &usageError{fmt.Sprintf("not a known platform key: %s (one of %s)", p, strings.Join(keys, ", "))}
	}
	return p, nil
}

func knownPlatform(key string) bool {
	for _, p := range ocifetch.Platforms {
		if p.Key == key {
			return true
		}
	}
	return false
}

func cmdFetch(ctx context.Context, args []string, stdout, stderr io.Writer) error {
	fs := newFlagSet("fetch", stderr)
	var cf commonFlags
	cf.bind(fs, true, true)
	var all, frozen, update bool
	var lock string
	fs.BoolVar(&all, "all", false, "every line the registry publishes (or, with --frozen, every request the lock pins)")
	fs.StringVar(&lock, "lock", "", "write the lock to this file after resolving (with --frozen: the lock to enforce; default chtypes.lock)")
	fs.BoolVar(&update, "update", false, "re-resolve every locked request and rewrite the lock (requires --lock)")
	fs.BoolVar(&frozen, "frozen", false, "fetch exactly what the lock pins, by digest, without resolving")
	spellings, err := parseInterleaved(fs, args)
	if err != nil {
		return err
	}
	// A dev SDK pins nothing: the refusal comes before anything else, the
	// network above all (rule r6).
	if lock != "" || frozen || update {
		if probe := (&ocifetch.Options{Frozen: frozen, LockPath: lock, Update: update}); ocifetch.PinningRefusedFor(probe) {
			return &usageError{ocifetch.PinningRefused}
		}
	}
	platform, err := cf.platformKey()
	if err != nil {
		return err
	}
	switch {
	case update && frozen:
		return &usageError{"--update re-resolves and --frozen forbids resolving; pass one"}
	case update && lock == "":
		return &usageError{"--update requires --lock <file>"}
	case update && cf.offline:
		return &usageError{"--update resolves against the registry and --offline forbids the network; pass one"}
	case all && len(spellings) > 0:
		return &usageError{fmt.Sprintf("--all fetches every line; drop the version arguments (%s)", strings.Join(spellings, " "))}
	case !all && !update && len(spellings) == 0:
		return &usageError{"a ClickHouse version spelling is required (or --all)"}
	}
	opts := cf.options()
	opts.Frozen, opts.LockPath = frozen, lock
	if lock != "" && !frozen {
		opts.LockWrite = true
	}
	opts.Update = update
	if update {
		// An update's requests are the lock's own, whatever was named.
		spellings, err = allSpellings(ctx, opts, true, lock)
		if err != nil {
			return err
		}
	} else if all {
		spellings, err = allSpellings(ctx, opts, frozen, lock)
		if err != nil {
			return err
		}
	}
	for _, s := range spellings {
		res, err := ocifetch.Ensure(ctx, ocifetch.Request{Spelling: s, Platform: platform}, opts)
		if err != nil {
			return err
		}
		for _, w := range res.Warnings {
			fmt.Fprintf(stderr, "chtypes: warning: %s\n", w)
		}
		fmt.Fprintln(stdout, res.Dir)
	}
	return nil
}

// allSpellings is what --all means: the published lines, or under --frozen
// the requests the lock pins, which is every request the lock names.
func allSpellings(ctx context.Context, opts *ocifetch.Options, frozen bool, lock string) ([]string, error) {
	if frozen {
		path := lock
		if path == "" {
			path = ocifetch.LockDefaultFile
		}
		l, err := ocifetch.ReadLock(path)
		if err != nil {
			return nil, err
		}
		out := make([]string, 0, len(l.Requests))
		for s := range l.Requests {
			out = append(out, s)
		}
		sort.Strings(out)
		return out, nil
	}
	if opts.Offline {
		inst, err := ocifetch.ListInstalled(opts)
		if err != nil {
			return nil, err
		}
		seen := map[string]bool{}
		var out []string
		for _, r := range inst {
			if s := lineOf(r.Version); s != "" && !seen[s] {
				seen[s] = true
				out = append(out, s)
			}
		}
		sort.Strings(out)
		return out, nil
	}
	return ocifetch.ListTags(ctx, opts)
}

// lineOf is the two-part spelling of a four-part version.
func lineOf(version string) string {
	parts := strings.Split(version, ".")
	if len(parts) < 2 {
		return ""
	}
	return parts[0] + "." + parts[1]
}

func cmdVerify(args []string, stdout, stderr io.Writer) error {
	fs := newFlagSet("verify", stderr)
	var cf commonFlags
	cf.bind(fs, false, false)
	if rest, err := parseInterleaved(fs, args); err != nil {
		return err
	} else if len(rest) > 0 {
		return &usageError{fmt.Sprintf("verify takes no positional arguments (%s)", strings.Join(rest, " "))}
	}
	opts := cf.options()
	results, err := ocifetch.VerifyInstalled(opts)
	if err != nil {
		return err
	}
	root, _ := ocifetch.CacheRoot(opts)
	if len(results) == 0 {
		// An empty pass must never look like a good one (public issue #486):
		// say that nothing was verified, and why when the cache says why. In
		// strict mode it is a failure, so a mounted cache can be health-checked.
		fmt.Fprintf(stderr, "chtypes: verified 0 builds under %s\n", root)
		printNotes(stderr, opts)
		if cf.strictMode() {
			return &ocifetch.FetchError{Code: ocifetch.CodeArtifactMissing,
				Msg: fmt.Sprintf("chtypes: no build is installed under %s, and strict mode needs one [%s]", root, ocifetch.CodeArtifactMissing)}
		}
		return nil
	}
	printNotes(stderr, opts)
	bad := 0
	for _, r := range results {
		if !r.OK {
			bad++
			fmt.Fprintf(stderr, "MISMATCH %-16s %-14s %s: %s\n", r.Version, r.Platform, r.Dir, r.Detail)
		}
	}
	if bad > 0 {
		return &ocifetch.FetchError{Code: ocifetch.CodeArtifactCorrupt,
			Msg: fmt.Sprintf("chtypes: %d of %d installed build(s) under %s do not match what was verified when they were installed [%s]",
				bad, len(results), root, ocifetch.CodeArtifactCorrupt)}
	}
	return nil
}

func cmdList(ctx context.Context, args []string, stdout, stderr io.Writer) error {
	fs := newFlagSet("list", stderr)
	var cf commonFlags
	cf.bind(fs, false, true)
	if rest, err := parseInterleaved(fs, args); err != nil {
		return err
	} else if len(rest) > 0 {
		return &usageError{fmt.Sprintf("list takes no positional arguments (%s)", strings.Join(rest, " "))}
	}
	opts := cf.options()
	installed, err := ocifetch.ListInstalled(opts)
	if err != nil {
		return err
	}
	for _, r := range installed {
		fmt.Fprintf(stdout, "installed %s %s %s\n", r.Version, r.Platform, r.Dir)
	}
	printNotes(stderr, opts)
	if cf.offlineMode() {
		return nil
	}
	lines, err := ocifetch.ListTags(ctx, opts)
	if err != nil {
		return err
	}
	// The registry lists lines, not platforms or support: whether a line is
	// supported is unknown here (the channel statement is out of v1).
	for _, l := range lines {
		fmt.Fprintf(stdout, "published %s support unknown\n", l)
	}
	return nil
}

func cmdWhere(args []string, stdout, stderr io.Writer) error {
	fs := newFlagSet("where", stderr)
	var cf commonFlags
	cf.bind(fs, false, false)
	all := fs.Bool("all", false, "print every directory searched (the cache root first, then the system directories), one per line")
	if rest, err := parseInterleaved(fs, args); err != nil {
		return err
	} else if len(rest) > 0 {
		return &usageError{fmt.Sprintf("where takes no positional arguments (%s)", strings.Join(rest, " "))}
	}
	opts := cf.options()
	root, err := ocifetch.CacheRoot(opts)
	if err != nil {
		return err
	}
	if cf.strictMode() {
		// Strict mode checks the root before naming it.
		if _, err := ocifetch.ProbeCache(opts); err != nil {
			return err
		}
	}
	if *all {
		dirs, err := ocifetch.SearchDirs(opts)
		if err != nil {
			return err
		}
		for _, d := range dirs {
			fmt.Fprintln(stdout, d)
		}
		return nil
	}
	fmt.Fprintln(stdout, root)
	return nil
}
