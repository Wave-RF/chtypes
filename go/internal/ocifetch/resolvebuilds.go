package ocifetch

// resolvebuilds.go — `chtypes resolve` (docs/guides/fetch-v1.md §3, "What a
// request resolves to"; public issue #493): what a line, a patch or an exact
// version resolves to on every platform the registry offers, each verified
// exactly as a fetch verifies it (§4), and never a layer downloaded. Under
// offline it is the cache's answer instead, the lookup an open makes.

import (
	"context"
	"errors"
	"fmt"
)

// Resolution is what a request resolves to on one platform.
type Resolution struct {
	Platform string
	Version  string // the signed clickhouse_version
	Build    string // the signed build id
	Manifest Digest // the platform manifest's digest
}

// Resolve answers what spelling resolves to, one Resolution per platform, in
// the constants' platform order. Online it resolves the index (through the
// dev channel's alias first, §3), and for each platform the index offers it
// fetches the manifest by digest and verifies its signed statement against
// the request (§4); no layer is requested and nothing is written. A platform
// the index does not offer is left out. Offline it answers from the cache:
// each platform's resolve_installed, and CHTYPES_ARTIFACT_MISSING when no
// platform has an installed build. The warnings are allow-unsigned's.
func Resolve(ctx context.Context, spelling string, opts *Options) ([]Resolution, []string, error) {
	ro, err := resolveOptions(opts)
	if err != nil {
		return nil, nil, err
	}
	out, warnings, err := resolveBuilds(ctx, ro, spelling)
	return out, warnings, cacheIOError(err, ro.cacheDir)
}

func resolveBuilds(ctx context.Context, ro resolvedOptions, spelling string) ([]Resolution, []string, error) {
	if err := validateSpelling(spelling); err != nil {
		return nil, nil, err
	}
	if ro.offline {
		var out []Resolution
		for _, p := range Platforms {
			res, err := resolveInstalledInternal(ro, Request{Spelling: spelling, Platform: p.Key}, p.Key)
			if err != nil {
				return nil, nil, err
			}
			if res != nil {
				out = append(out, Resolution{Platform: p.Key, Version: res.Version, Build: res.Build, Manifest: res.Digests.Manifest})
			}
		}
		if len(out) == 0 {
			return nil, nil, newError(CodeArtifactMissing, spelling, "", "", nil, "%s", WithNotes(
				fmt.Sprintf("no installed artifact for %s on any platform, and --offline forbids a network fetch", spelling), missingNotes(ro)))
		}
		return out, nil, nil
	}

	s := newSession(ro)
	idx, _, _, aliasAbsent, err := s.resolveIndex(ctx, ro.bases, spelling)
	if err != nil {
		return nil, nil, err
	}
	cache := newLayout(ro.cacheDir, true)
	var out []Resolution
	var warnings []string
	for _, p := range Platforms {
		desc, err := selectPlatformDescriptor(idx, p.Key)
		if err != nil {
			var fe *FetchError
			if errors.As(err, &fe) && fe.Code == CodeArtifactUnpublished {
				continue // the index does not offer this platform
			}
			return nil, nil, err
		}
		manifest, body, _, err := s.fetchManifestByDigest(ctx, ro.bases, *desc)
		if err != nil {
			return nil, nil, err
		}
		manifestDigest := digestOf(body)
		stmt, w, err := s.verifyManifestTrust(ctx, ro.bases, manifestDigest, manifest.Layers[0].Digest, p, spelling, ro.trustedKeys, ro.allowUnsigned)
		if err != nil {
			return nil, nil, err
		}
		warnings = append(warnings, w...)
		var predicate map[string]any
		if stmt != nil {
			if err := ro.ch.aheadOfRegistry(cache, aliasAbsent, stmt.Statement.Predicate, spelling, p.Key); err != nil {
				return nil, nil, err
			}
			predicate = stmt.Statement.Predicate
		} else {
			// Allow-unsigned: no statement verified, so the version and build
			// are the config blob's, as an unsigned install records them.
			_, cfg, err := s.libraryPathFor(ctx, ro.bases, manifest, nil)
			if err != nil {
				return nil, nil, err
			}
			predicate = cfg
		}
		out = append(out, Resolution{
			Platform: p.Key,
			Version:  stringPredicate(predicate, "clickhouse_version"),
			Build:    stringPredicate(predicate, "build"),
			Manifest: manifestDigest,
		})
	}
	if len(out) == 0 {
		return nil, nil, newError(CodeArtifactUnpublished, spelling, "", "", nil, "the index for %s offers no platform this SDK knows", spelling)
	}
	return out, warnings, nil
}
