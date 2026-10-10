package main

import "sort"

// prunecases.go — `chtypes prune` (docs/guides/fetch-v1.md §1, "Pruning";
// public issue #494). A case id starting `prune-` exercises the binding's
// prune instead of ensure(), with request.prune saying what it prunes. Its
// layout holds several builds, every one installed by the runner from
// installed.json before the call. expect.prune is the outcome: what the
// prune reports removed and in use, in report order (platform, then oldest
// first), the paths that must be gone afterwards (each removed build's
// unpacked entry, its manifest, config and layer blobs, and its signature
// referrer's manifest and bundle blobs), and what index.json lists after it.
// Everything else stays: records_intact names every build that must be byte
// for byte as it was. A held build is held by the runner through its
// binding's own hold before the call, as a process using it holds it.

func buildPruneCases(fs *FileSet) []Case {
	var cases []Case
	tree := NewTree("prune")

	art := func(platformKey, version, build string, opts ArtifactOptions) PlatformArtifact {
		if opts.LibraryContentSeed == "" {
			opts.LibraryContentSeed = "prune-" + platformKey + "-" + version + "-" + build
		}
		return buildPlatformArtifact(tree, testKey, platformKey, version, build, "lts", opts)
	}

	// Two lines on linux-arm64, three builds of 26.8 (two of one version)
	// and two of 26.9, and one build of 26.8 on linux-amd64, which nothing
	// supersedes: a prune groups by line AND platform.
	amd268 := art("linux-amd64", "26.8.14.1", "20260814.000001", ArtifactOptions{})
	arm268old := art("linux-arm64", "26.8.14.1", "20260814.000001", ArtifactOptions{})
	arm268b1 := art("linux-arm64", "26.8.15.2", "20260815.000001", ArtifactOptions{})
	arm268b2 := art("linux-arm64", "26.8.15.2", "20260815.000002", ArtifactOptions{})
	arm269old := art("linux-arm64", "26.9.1.1", "20260901.000001", ArtifactOptions{})
	arm269new := art("linux-arm64", "26.9.2.1", "20260902.000001", ArtifactOptions{})
	lines := []PlatformArtifact{amd268, arm268old, arm268b1, arm268b2, arm269old, arm269new}
	linesLayout := NewLayout("prune-lines")
	for _, a := range lines {
		copyArtifactIntoLayout(linesLayout, tree, a, a.Predicate.ClickHouseVersion)
	}
	linesLayout.SetInstalled(digestsOf(lines)...)
	linesLayout.Flush(fs)

	// One line on linux-arm64 shared with a dev SDK of another fingerprint:
	// two builds of this SDK's (fingerprint A), and a newer one of a newer
	// SDK's (N). Under A, only A's builds are seen: the foreign one neither
	// counts as newer nor is touched.
	ownOld := art("linux-arm64", "26.8.15.8", "20260908.000000", ArtifactOptions{ABIFingerprint: "sha256:" + fixtureFingerprintA})
	ownNew := art("linux-arm64", "26.8.15.9", "20260920.000000", ArtifactOptions{ABIFingerprint: "sha256:" + fixtureFingerprintA})
	foreign := art("linux-arm64", "26.8.15.10", "20261005.000000", ArtifactOptions{ABIFingerprint: "sha256:" + fixtureFingerprintN})
	fingerprints := []PlatformArtifact{ownOld, ownNew, foreign}
	fpLayout := NewLayout("prune-fingerprints")
	for _, a := range fingerprints {
		copyArtifactIntoLayout(fpLayout, tree, a, a.Predicate.ClickHouseVersion)
	}
	fpLayout.SetInstalled(digestsOf(fingerprints)...)
	fpLayout.Flush(fs)
	flushTrees(fs, tree)

	mk := func(id, layout string, keep int, line string, dryRun bool) Case {
		c := newCase(id, "prune", "file")
		c.Setup.Cache = layout
		pr := &PruneRequest{Keep: keep, DryRun: dryRun}
		if line != "" {
			pr.Line = strp(line)
		}
		c.Request.Prune = pr
		c.Expect.Requests.Max = intp(0)
		return c
	}
	// outcome fills expect.prune and records_intact: removed builds go,
	// in-use ones and everything else stay (a dry run removes nothing).
	outcome := func(c *Case, all []PlatformArtifact, removed, inUse []PlatformArtifact, dryRun bool) {
		gone := []string{}
		doomed := map[string]bool{}
		if !dryRun {
			for _, a := range removed {
				doomed[a.ManifestDesc.Digest] = true
				gone = append(gone, pruneGone(tree, a)...)
			}
		}
		sort.Strings(gone)
		index := []string{}
		intact := []string{}
		for _, a := range all {
			if !doomed[a.ManifestDesc.Digest] {
				index = append(index, a.ManifestDesc.Digest)
				intact = append(intact, a.ManifestDesc.Digest)
			}
		}
		c.Expect.Prune = &PruneExpect{Removed: digestsOf(removed), InUse: digestsOf(inUse), Gone: gone, IndexAfter: index}
		c.Expect.RecordsIntact = intact
	}

	// 1. Keep the newest of each line and platform: the two older 26.8
	// builds (one of them the same version as the kept one, an older build)
	// and the older 26.9 build go, oldest first; linux-amd64's one build stays.
	newest := mk("prune-keep-newest", "prune-lines", 1, "", false)
	outcome(&newest, lines, []PlatformArtifact{arm268old, arm268b1, arm269old}, nil, false)
	cases = append(cases, newest)

	// 2. Keep two: only 26.8's oldest goes.
	keepTwo := mk("prune-keep-two", "prune-lines", 2, "", false)
	outcome(&keepTwo, lines, []PlatformArtifact{arm268old}, nil, false)
	cases = append(cases, keepTwo)

	// 3. One line: 26.9's older build goes, and 26.8 is untouched.
	oneLine := mk("prune-line", "prune-lines", 1, "26.9", false)
	outcome(&oneLine, lines, []PlatformArtifact{arm269old}, nil, false)
	cases = append(cases, oneLine)

	// 4. A dry run reports exactly what the first case removes, and removes
	// nothing.
	dry := mk("prune-dry-run", "prune-lines", 1, "", true)
	outcome(&dry, lines, []PlatformArtifact{arm268old, arm268b1, arm269old}, nil, true)
	cases = append(cases, dry)

	// 5. A superseded build a process holds is reported in use and kept;
	// the rest go.
	held := mk("prune-in-use", "prune-lines", 1, "", false)
	held.Setup.Held = []string{arm268old.ManifestDesc.Digest}
	outcome(&held, lines, []PlatformArtifact{arm268b1, arm269old}, []PlatformArtifact{arm268old}, false)
	cases = append(cases, held)

	// 6. A system directory is never pruned: with the builds there and an
	// empty cache, nothing is superseded.
	system := mk("prune-system-dirs-never", "empty", 1, "", false)
	system.Setup.SystemDirs = []string{"prune-lines"}
	outcome(&system, nil, nil, nil, false)
	cases = append(cases, system)

	// 7. Under fingerprint A, the newer foreign build neither supersedes
	// A's newest nor is touched: only A's older build goes.
	ownOnly := mk("prune-foreign-fingerprint-untouched", "prune-fingerprints", 1, "", false)
	ownOnly.Request.OwnFingerprint = strp(fixtureFingerprintA)
	outcome(&ownOnly, fingerprints, []PlatformArtifact{ownOld}, nil, false)
	cases = append(cases, ownOnly)

	// The control: with no own fingerprint every build is seen, so both of
	// A's builds are superseded by the newest.
	seesAll := mk("prune-v1-channel-sees-all", "prune-fingerprints", 1, "", false)
	outcome(&seesAll, fingerprints, []PlatformArtifact{ownOld, ownNew}, nil, false)
	cases = append(cases, seesAll)

	return cases
}

func digestsOf(arts []PlatformArtifact) []string {
	out := []string{}
	for _, a := range arts {
		out = append(out, a.ManifestDesc.Digest)
	}
	return out
}

// pruneGone are the paths a prune of art removes from a layout this tree's
// artifacts were copied into: the unpacked entry, the manifest, config and
// layer blobs, and each referrer manifest of it with that referrer's layers.
// A referrer's config, the empty `{}` every referrer shares, stays.
func pruneGone(tree *Tree, art PlatformArtifact) []string {
	blob := func(digest string) string { return "blobs/sha256/" + digestHexPart(digest) }
	out := []string{
		"unpacked/sha256/" + digestHexPart(art.ManifestDesc.Digest),
		blob(art.ManifestDesc.Digest), blob(art.ConfigDesc.Digest), blob(art.LayerDesc.Digest),
	}
	for _, ref := range tree.referrersOf[art.ManifestDesc.Digest] {
		out = append(out, blob(ref.Digest))
		var m ImageManifest
		if err := unmarshalJSON(mustGetManifest(tree, ref.Digest), &m); err != nil {
			panic(err)
		}
		for _, l := range m.Layers {
			out = append(out, blob(l.Digest))
		}
	}
	return out
}
