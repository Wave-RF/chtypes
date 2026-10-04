package ocifetch

import "regexp"

// bindingVersion is this module's own version. Go has no manifest to read it
// from (the release tag is the version), so a release bumps this constant
// with the other three bindings' manifests (RELEASING.md, step 1). It is
// never empty: an unknown version would be spelled userAgentDevVersion.
const bindingVersion = "1.0.1"

// userAgentDevVersion stands in when no version is available. The header is
// never omitted (docs/guides/fetch-v1.md §2).
const userAgentDevVersion = "0.0.0-dev"

// userAgentPattern is the only shape the header may take.
var userAgentPattern = regexp.MustCompile(`^chtypes-(go|python|ts|rust)/[0-9A-Za-z.+-]+$`)

// userAgent is the User-Agent every request this package makes carries:
// `chtypes-go/<version>`. Delivery hosts may refuse a generic library agent
// (net/http's `Go-http-client/…` among them), so no request is sent without
// this one (docs/guides/fetch-v1.md §2).
func userAgent() string {
	v := bindingVersion
	if v == "" {
		v = userAgentDevVersion
	}
	return "chtypes-go/" + v
}
