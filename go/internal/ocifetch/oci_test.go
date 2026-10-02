package ocifetch

import "testing"

func TestValidateSpelling(t *testing.T) {
	ok := []string{"26.8", "26.8.15", "26.8.15.10", "0.1", "1.2.3.4"}
	for _, s := range ok {
		if err := validateSpelling(s); err != nil {
			t.Errorf("validateSpelling(%q) = %v, want nil", s, err)
		}
	}
	refused := []string{"v26.8", "26.8.15.10-lts", "26.8-stable", "26", "", "26.08", "a.b"}
	for _, s := range refused {
		if err := validateSpelling(s); err == nil {
			t.Errorf("validateSpelling(%q) = nil, want an error", s)
		}
	}
}

func TestVersionWithin(t *testing.T) {
	cases := []struct {
		request, resolved string
		want              bool
	}{
		{"26.8", "26.8.15.10", true},
		{"26.8.15", "26.8.15.10", true},
		{"26.8.15.10", "26.8.15.10", true},
		{"26.7", "26.8.15.10", false},
		{"26.8.16", "26.8.15.10", false},
		{"26.8.15.11", "26.8.15.10", false},
		{"26.8.15.10.1", "26.8.15.10", false}, // more components than resolved has
	}
	for _, c := range cases {
		if got := versionWithin(c.request, c.resolved); got != c.want {
			t.Errorf("versionWithin(%q, %q) = %v, want %v", c.request, c.resolved, got, c.want)
		}
	}
}

func TestBuildRequestURL(t *testing.T) {
	cases := []struct {
		base, suffix, want string
	}{
		{"https://registry.wavehouse.dev/chtypes/v1", "manifests/26.8", "https://registry.wavehouse.dev/v2/chtypes/v1/manifests/26.8"},
		{"http://127.0.0.1:5000/s-case1/chtypes/v1", "manifests/26.8", "http://127.0.0.1:5000/v2/s-case1/chtypes/v1/manifests/26.8"},
		{"file:///abs/trees/basic/v2/chtypes/v1", "manifests/26.8", "file:///abs/trees/basic/v2/chtypes/v1/manifests/26.8"},
		{"file:///abs/trees/basic/v2/chtypes/v1/", "manifests/26.8", "file:///abs/trees/basic/v2/chtypes/v1/manifests/26.8"},
	}
	for _, c := range cases {
		got, err := buildRequestURL(c.base, c.suffix)
		if err != nil {
			t.Errorf("buildRequestURL(%q, %q) error: %v", c.base, c.suffix, err)
			continue
		}
		if got != c.want {
			t.Errorf("buildRequestURL(%q, %q) = %q, want %q", c.base, c.suffix, got, c.want)
		}
	}
}

func TestBuildRequestURLRejectsUnsupportedScheme(t *testing.T) {
	if _, err := buildRequestURL("ftp://example.com/x", "manifests/26.8"); err == nil {
		t.Fatalf("buildRequestURL with ftp scheme = nil error, want an error")
	}
}

func TestSelectPlatformDescriptor(t *testing.T) {
	idx := &ImageIndex{Manifests: []Descriptor{
		{Digest: Digest("sha256:" + hex64('a')), Platform: &PlatformDescriptor{OS: "linux", Architecture: "amd64"}},
		{Digest: Digest("sha256:" + hex64('b')), Platform: &PlatformDescriptor{OS: "linux", Architecture: "arm64"}},
	}}
	d, err := selectPlatformDescriptor(idx, "linux-arm64")
	if err != nil {
		t.Fatalf("selectPlatformDescriptor: %v", err)
	}
	if d.Digest.Hex() != hex64('b') {
		t.Fatalf("selected wrong descriptor: %+v", d)
	}

	if _, err := selectPlatformDescriptor(idx, "darwin-arm64"); err == nil {
		t.Fatalf("selectPlatformDescriptor(darwin-arm64) = nil error, want CHTYPES_ARTIFACT_UNPUBLISHED")
	}
}

func TestSelectPlatformDescriptorDuplicate(t *testing.T) {
	idx := &ImageIndex{Manifests: []Descriptor{
		{Digest: Digest("sha256:" + hex64('a')), Platform: &PlatformDescriptor{OS: "linux", Architecture: "arm64"}},
		{Digest: Digest("sha256:" + hex64('c')), Platform: &PlatformDescriptor{OS: "linux", Architecture: "arm64"}},
	}}
	_, err := selectPlatformDescriptor(idx, "linux-arm64")
	if err == nil {
		t.Fatalf("selectPlatformDescriptor with a duplicate platform = nil error, want CHTYPES_ARTIFACT_CORRUPT")
	}
}

func TestVerifyDescriptor(t *testing.T) {
	body := []byte("hello world")
	good := Descriptor{Digest: digestOf(body), Size: int64(len(body))}
	if err := verifyDescriptor(body, good); err != nil {
		t.Fatalf("verifyDescriptor with matching digest/size: %v", err)
	}
	bad := Descriptor{Digest: digestOf(body), Size: int64(len(body)) + 1}
	if err := verifyDescriptor(body, bad); err == nil {
		t.Fatalf("verifyDescriptor with wrong size = nil error")
	}
	wrongDigest := Descriptor{Digest: Digest("sha256:" + hex64('0')), Size: int64(len(body))}
	if err := verifyDescriptor(body, wrongDigest); err == nil {
		t.Fatalf("verifyDescriptor with wrong digest = nil error")
	}
}

// hex64 repeats r to make a syntactically valid 64-hex-char digest for tests
// that do not care about its actual preimage.
func hex64(r byte) string {
	b := make([]byte, 64)
	for i := range b {
		b[i] = r
	}
	return string(b)
}
