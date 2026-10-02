package main

import (
	"encoding/json"
	"os"
	"path/filepath"
)

// constants.go — genfixtures reads spec/fetch-v1/constants.json directly
// (the one frozen source, lane 0A's contract) instead of hand-copying its
// media types, predicate types, platform set and limits into Go literals.
// This is the one dependency this generator has on anything outside its
// own directory, and it is read-only: genfixtures never writes to spec/.

type constantsFile struct {
	Platforms []struct {
		Key          string `json:"key"`
		OS           string `json:"os"`
		Architecture string `json:"architecture"`
	} `json:"platforms"`
	MediaTypes struct {
		Index                string `json:"index"`
		Manifest             string `json:"manifest"`
		EmptyConfig          string `json:"empty_config"`
		Config               string `json:"config"`
		Layer                string `json:"layer"`
		Bundle               string `json:"bundle"`
		ArtifactType         string `json:"artifact_type"`
		GoldensArtifactType  string `json:"goldens_artifact_type"`
		FixturesArtifactType string `json:"fixtures_artifact_type"`
		ChannelArtifactType  string `json:"channel_artifact_type"`
	} `json:"media_types"`
	PredicateTypes struct {
		Artifact string `json:"artifact"`
		Goldens  string `json:"goldens"`
		Fixtures string `json:"fixtures"`
		Channel  string `json:"channel"`
	} `json:"predicate_types"`
	Trust struct {
		ReleaseKeys []struct {
			KeyID      string `json:"keyid"`
			Ed25519Hex string `json:"ed25519_hex"`
		} `json:"release_keys"`
	} `json:"trust"`
	TestKeys []struct {
		KeyID            string `json:"keyid"`
		Ed25519Hex       string `json:"ed25519_hex"`
		TrustedByDefault bool   `json:"trusted_by_default"`
	} `json:"test_keys"`
	Limits struct {
		ManifestBytes    int64 `json:"manifest_bytes"`
		BundleBytes      int64 `json:"bundle_bytes"`
		MaxUnpackedBytes int64 `json:"max_unpacked_bytes"`
		ZstdWindowLogMax int   `json:"zstd_window_log_max"`
		MaxRedirects     int   `json:"max_redirects"`
	} `json:"limits"`
	Registry struct {
		FixturesRepositorySuffix string `json:"fixtures_repository_suffix"`
	} `json:"registry"`
}

func loadConstants(repoRoot string) constantsFile {
	path := filepath.Join(repoRoot, "spec", "fetch-v1", "constants.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		panic("genfixtures: reading " + path + ": " + err.Error())
	}
	var c constantsFile
	if err := json.Unmarshal(raw, &c); err != nil {
		panic("genfixtures: parsing " + path + ": " + err.Error())
	}
	return c
}

// platformKeys returns the platform keys in constants.json's own order
// (linux-amd64, linux-arm64, darwin-arm64) — the order every per-platform
// descriptor list in this generator's output follows.
func (c constantsFile) platformKeys() []string {
	out := make([]string, 0, len(c.Platforms))
	for _, p := range c.Platforms {
		out = append(out, p.Key)
	}
	return out
}

func (c constantsFile) platform(key string) Platform {
	for _, p := range c.Platforms {
		if p.Key == key {
			return Platform{OS: p.OS, Architecture: p.Architecture}
		}
	}
	panic("genfixtures: unknown platform " + key)
}
