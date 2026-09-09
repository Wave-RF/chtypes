package chtypes

// registry_path.go — where artifacts are looked for and where a fetch
// writes (docs/fetch.md §1).
//
// A registry is a directory holding <minor>/manifest.json entries. Lookup
// tries, in order, and takes the FIRST directory that contains the
// requested line:
//
//  1. a path given explicitly to the registry constructor;
//  2. CHTYPES_REGISTRY;
//  3. ${XDG_CACHE_HOME:-~/.cache}/chtypes/artifacts/<os>-<arch> — the
//     per-user cache, where fetch installs;
//  4. /usr/local/share/chtypes/artifacts/<os>-<arch>, then
//     /opt/chtypes/artifacts/<os>-<arch> — system locations, reserved for
//     the deferred system packages and for images that bake artifacts in.
//
// Fetch WRITES to the first of (1), (2), (3) that is set; never to (4).

import (
	"os"
	"path/filepath"
	"regexp"
	"runtime"
)

const (
	envRegistry     = "CHTYPES_REGISTRY"
	envAutoFetch    = "CHTYPES_AUTOFETCH"
	envTrustedKeys  = "CHTYPES_TRUSTED_KEYS"
	envAllowUnsign  = "CHTYPES_ALLOW_UNSIGNED"
	envArtifactsURL = "CHTYPES_ARTIFACTS_URL"
	envDownloadTok  = "CHTYPES_DOWNLOAD_TOKEN"
	envTarget       = "CHTYPES_TARGET"
)

// HostPlatform is this process's own platform key, "<os>-<arch>" in the
// artifact spelling: linux|darwin, arm64|amd64 — which is exactly what
// runtime.GOOS and runtime.GOARCH spell on the four supported hosts.
func HostPlatform() string { return runtime.GOOS + "-" + runtime.GOARCH }

var platformKey = regexp.MustCompile(`^(linux|darwin)-(arm64|amd64)$`)

// ValidPlatform reports whether p is one of the four platform keys an
// artifact can be published for.
func ValidPlatform(p string) bool { return platformKey.MatchString(p) }

// DefaultRegistryDirFor is DefaultRegistryDir for an arbitrary platform key:
// ${XDG_CACHE_HOME:-~/.cache}/chtypes/artifacts/<platform>. A fetch for
// another platform (Linux artifacts on a Mac, for a container) lands there.
// Empty when no home directory can be determined.
func DefaultRegistryDirFor(platform string) string {
	base := os.Getenv("XDG_CACHE_HOME")
	if base == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return ""
		}
		base = filepath.Join(home, ".cache")
	}
	return filepath.Join(base, "chtypes", "artifacts", platform)
}

// SystemRegistryDirs are the §1 item-4 locations for a platform: read by the
// loader, never written by a fetch.
func SystemRegistryDirs(platform string) []string {
	return []string{
		filepath.Join("/usr/local/share/chtypes/artifacts", platform),
		filepath.Join("/opt/chtypes/artifacts", platform),
	}
}

// RegistrySearchPath is the ordered list of directories a lookup for this
// host consults (§1): explicit (when non-empty), $CHTYPES_REGISTRY (when
// set), the per-user cache, then the system locations. Duplicates are
// dropped, first occurrence kept, so the "Looked in" list reads cleanly.
func RegistrySearchPath(explicit string) []string {
	return registrySearchPathFor(explicit, HostPlatform())
}

func registrySearchPathFor(explicit, platform string) []string {
	cands := []string{explicit, os.Getenv(envRegistry), DefaultRegistryDirFor(platform)}
	cands = append(cands, SystemRegistryDirs(platform)...)
	seen := map[string]bool{}
	var out []string
	for _, c := range cands {
		if c == "" || seen[c] {
			continue
		}
		seen[c] = true
		out = append(out, c)
	}
	return out
}

// FetchRegistryDir is the directory a fetch for this host writes to (§1):
// the explicit path when given, else $CHTYPES_REGISTRY, else the per-user
// cache. Never a system location.
func FetchRegistryDir(explicit string) string {
	return fetchRegistryDirFor(explicit, HostPlatform())
}

func fetchRegistryDirFor(explicit, platform string) string {
	if explicit != "" {
		return explicit
	}
	if env := os.Getenv(envRegistry); env != "" {
		return env
	}
	return DefaultRegistryDirFor(platform)
}
