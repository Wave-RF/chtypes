package ocifetch

// revocation.go — build-level revocation (docs/guides/fetch-v1.md, "Build-level
// revocation"; public issue #625). A revocation is a signed referrer of a
// platform manifest, read from the listing its signature lookup reads; one that
// verifies refuses the build with CHTYPES_ARTIFACT_REVOKED and is stored in the
// build's cache entry, and one that does not is ignored with one warning. A 410
// whose body's errors[0].detail.revoked is an object is the same code. A status
// that cannot be read fails open with one warning, or in strict mode is the
// transport's error. Generation 2 only (channel.revocation): the v1 contract
// has none of it.
//
// Four paths check, each with its own budget:
//
//   - an install reads its revocation candidates from the trust listing it
//     already made (resolveAndInstall), under the retry table;
//   - --frozen reads one listing, warm or cold, under the retry table;
//   - a build already installed and used without a download (a registry open,
//     preload, an ensure that finds it installed) is checked at most once per
//     process (revocationMemo), one attempt per base, no backoff, and one
//     deadline (revocationCheckDeadline) for the whole check;
//   - --lock and update refuse a revoked build on every platform they pin.

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

// Advisory is one advisory a revocation names, every string made safe to
// print (the retired message's rule, §2). A signed revocation's carries its
// RevokedAt; a 410's carries the Build it names, newest first.
type Advisory struct {
	ID           string
	URL          string
	Class        string
	Summary      string
	Build        string
	SupersededBy string
	RevokedAt    string
}

// Revocation is what a CHTYPES_ARTIFACT_REVOKED FetchError carries for
// programs. Signed is true for a verified, signed revocation of Manifest,
// whose one advisory it is, and false for a 410's body, which is unsigned,
// names no manifest, and lists one advisory per revoked build.
type Revocation struct {
	Signed     bool
	Manifest   Digest
	Advisories []Advisory
}

// revocationCheckDeadline is the installed-build check's one deadline,
// RevocationCheckSeconds; a package variable so a test can shorten it.
var revocationCheckDeadline = time.Duration(RevocationCheckSeconds * float64(time.Second))

// isGone reports whether err is a 410's: CHTYPES_SOURCE_RETIRED or, under
// generation 2, CHTYPES_ARTIFACT_REVOKED. Either is permanent, so every loop
// over bases or candidates returns it at once instead of moving on (§2).
func isGone(err error) bool {
	var fe *FetchError
	return errors.As(err, &fe) && (fe.Code == CodeSourceRetired || fe.Code == CodeArtifactRevoked)
}

// goneError is the error for a 410: CHTYPES_ARTIFACT_REVOKED when revocation
// is on and the body's errors[0].detail.revoked is an object, else a retired
// repository's.
func goneError(rawURL string, status int, body []byte, revocation bool) *FetchError {
	if revocation {
		if advisories, ok := revoked410(body); ok {
			fe := newError(CodeArtifactRevoked, "", "", rawURL, nil, "%s answered %d Gone: %s", rawURL, status, revoked410Text(advisories))
			fe.Revocation = &Revocation{Advisories: advisories}
			return fe
		}
	}
	return retiredError(rawURL, status, body)
}

// revoked410 reads a 410 body: ok when it is UTF-8 JSON whose
// errors[0].detail.revoked is an object, with that object's advisories, each
// field made safe and one that is absent, not a string, empty once made safe
// or a lone surrogate escape left empty. An entry that is not an object, or
// leaves every field empty, is skipped. A repeated key reads as its last
// value. tests/fixtures/revoked-410/cases.json is the table all four bindings
// answer alike.
func revoked410(body []byte) ([]Advisory, bool) {
	if !utf8.Valid(body) {
		return nil, false
	}
	var doc map[string]json.RawMessage
	if json.Unmarshal(body, &doc) != nil {
		return nil, false
	}
	var errs []json.RawMessage
	if json.Unmarshal(doc["errors"], &errs) != nil || len(errs) == 0 {
		return nil, false
	}
	var first map[string]json.RawMessage
	if json.Unmarshal(errs[0], &first) != nil {
		return nil, false
	}
	var detail map[string]json.RawMessage
	if json.Unmarshal(first["detail"], &detail) != nil {
		return nil, false
	}
	raw := bytes.TrimSpace(detail["revoked"])
	var revoked map[string]json.RawMessage
	if len(raw) == 0 || raw[0] != '{' || json.Unmarshal(raw, &revoked) != nil {
		return nil, false
	}
	advisories := []Advisory{}
	var entries []json.RawMessage
	if json.Unmarshal(revoked["advisories"], &entries) != nil {
		return advisories, true
	}
	for _, e := range entries {
		e = bytes.TrimSpace(e)
		var obj map[string]json.RawMessage
		if len(e) == 0 || e[0] != '{' || json.Unmarshal(e, &obj) != nil {
			continue
		}
		a := Advisory{
			ID: safeField(obj, "id"), URL: safeField(obj, "url"), Class: safeField(obj, "class"),
			Summary: safeField(obj, "summary"), Build: safeField(obj, "build"), SupersededBy: safeField(obj, "superseded_by"),
		}
		if a == (Advisory{}) {
			continue
		}
		advisories = append(advisories, a)
	}
	return advisories, true
}

// safeField is obj[key] when it is a JSON string with no lone surrogate
// escape, made safe to print; "" otherwise.
func safeField(obj map[string]json.RawMessage, key string) string {
	raw := bytes.TrimSpace(obj[key])
	if len(raw) == 0 || raw[0] != '"' || hasLoneSurrogateEscape(raw) {
		return ""
	}
	var s string
	if json.Unmarshal(raw, &s) != nil {
		return ""
	}
	return sanitizeRetiredMessage(s)
}

// revoked410Text is what follows "<url> answered 410 Gone: ".
func revoked410Text(advisories []Advisory) string {
	var parts []string
	for _, a := range advisories {
		if t := advisoryText(a); t != "" {
			parts = append(parts, t)
		}
	}
	if len(parts) == 0 {
		return "revoked, and the registry names no advisory"
	}
	return "revoked: " + strings.Join(parts, "; ")
}

// advisoryText is one 410 advisory: "<id> <url> (<class>) build <build>,
// superseded by build <superseded_by>: <summary>", a part whose field is
// empty left out with the separator before it, and the first part present
// taking none.
func advisoryText(a Advisory) string {
	var s string
	join := func(sep, part string) {
		if s != "" {
			s += sep
		}
		s += part
	}
	for _, p := range []string{a.ID, a.URL} {
		if p != "" {
			join(" ", p)
		}
	}
	if a.Class != "" {
		join(" ", "("+a.Class+")")
	}
	if a.Build != "" {
		join(" ", "build "+a.Build)
	}
	if a.SupersededBy != "" {
		join(", ", "superseded by build "+a.SupersededBy)
	}
	if a.Summary != "" {
		join(": ", a.Summary)
	}
	return s
}

// referrerListing is one platform manifest's referrers listing: the first
// answer that names a signature referrer (the referrers API on the bases in
// order, then the fallback tag), its signature and revocation referrers, and,
// when no answer named a signature, the last failure that kept a base from
// answering (nil when every base answered 404 or a listing without one).
type referrerListing struct {
	found       bool
	signatures  []Descriptor
	revocations []Descriptor
	failure     error
}

// readListing reads subject's listing exactly as the signature lookup does
// (§4). single makes one attempt per request, with no backoff: the
// installed-build check's budget. Its error is only ever a 410's.
func (s *session) readListing(ctx context.Context, bases []string, subject Digest, single bool) (*referrerListing, error) {
	l := &referrerListing{}
	for _, step := range []struct{ suffix, accept string }{
		// The referrers API is not a manifests/<ref> GET, so it sends no Accept.
		{"referrers/" + string(subject), ""},
		{"manifests/" + fallbackTag(subject), manifestAccept},
	} {
		idx, err := s.listingIndex(ctx, bases, step.suffix, step.accept, single, l)
		if err != nil {
			return nil, err
		}
		if idx == nil {
			continue
		}
		sigs := appendMatchingArtifactType(nil, idx, MediaTypeBundle)
		if len(sigs) == 0 {
			continue // not a listing of this manifest's signature: try the fallback tag
		}
		l.found = true
		l.signatures = capReferrers(sigs)
		l.revocations = capReferrers(appendMatchingArtifactType(nil, idx, RevocationArtifactType))
		return l, nil
	}
	return l, nil
}

func capReferrers(d []Descriptor) []Descriptor {
	if len(d) > MaxReferrers {
		return d[:MaxReferrers]
	}
	return d
}

// listingIndex is fetchAuxIndex that also records, on l, why a base did not
// answer: the first base that serves a 200 with an index is the answer.
func (s *session) listingIndex(ctx context.Context, bases []string, suffix, accept string, single bool, l *referrerListing) (*ReferrersIndex, error) {
	for _, base := range bases {
		u, err := buildRequestURL(base, suffix)
		if err != nil {
			continue
		}
		result, err := s.client.doGet(ctx, u, requestOptions{maxBytes: ManifestMaxBytes, accept: accept, single: single})
		if isGone(err) {
			return nil, err
		}
		if err != nil {
			l.failure = err
			continue
		}
		switch result.status {
		case http.StatusOK:
		case http.StatusNotFound:
			continue
		case http.StatusUnauthorized:
			l.failure = newError(CodeSourceUnauthorized, "", "", u, nil, "%s returned 401", u)
			continue
		case http.StatusForbidden:
			l.failure = newError(CodeSourceForbidden, "", "", u, nil, "%s returned 403", u)
			continue
		default:
			l.failure = newError(CodeSourceUnreachable, "", "", u, nil, "%s returned unexpected status %d", u, result.status)
			continue
		}
		var idx ReferrersIndex
		if uerr := strictUnmarshal(result.body, &idx); uerr != nil {
			l.failure = newError(CodeSourceUnreachable, "", "", u, uerr, "%s is not a referrers listing: %v", u, uerr)
			continue
		}
		return &idx, nil
	}
	return nil, nil
}

// revocationSubject is the build a check concerns: its own digests and signed
// predicate, and its installed entry (dir) when it has one, writable when that
// entry is in the user cache.
type revocationSubject struct {
	manifest  Digest
	layer     Digest
	predicate map[string]any
	version   string
	platform  string
	build     string
	request   string
	dir       string
	writable  bool
}

// subjectOf is the subject an installed build's Resolved names.
func subjectOf(ro resolvedOptions, res *Resolved) revocationSubject {
	return revocationSubject{
		manifest: res.Digests.Manifest, layer: res.Digests.Layer, predicate: res.Predicate,
		version: res.Version, platform: res.Platform, build: res.Build, request: res.Request,
		dir: res.Dir, writable: res.Dir != "" && underDir(ro.cacheDir, res.Dir),
	}
}

// subjectOfStatement is the subject of a build not yet installed: the signed
// statement verified for it.
func subjectOfStatement(stmt *verifiedStatement, manifest, layer Digest, platform, request string) revocationSubject {
	pred := stmt.Statement.Predicate
	return revocationSubject{
		manifest: manifest, layer: layer, predicate: pred,
		version: stringPredicate(pred, "clickhouse_version"), platform: platform, build: stringPredicate(pred, "build"), request: request,
	}
}

func underDir(root, dir string) bool {
	rel, err := filepath.Rel(root, dir)
	return err == nil && rel != "." && !strings.HasPrefix(rel, "..")
}

// verifyRevocation checks one revocation bundle against sub (the guide's five
// rules): the signature under keys, the statement and predicate types, the
// subject and manifest, the build's identity, and the advisory's shape. It
// returns the Revocation, or nil and why it does not verify.
func verifyRevocation(body []byte, keys []ed25519.PublicKey, sub revocationSubject) (*Revocation, string) {
	vr, err := verifyBundle(body, keys)
	if err != nil {
		return nil, err.Error()
	}
	if !vr.verified {
		return nil, "no signature verifies under a trusted key"
	}
	st := vr.statement
	if st.Type != StatementType {
		return nil, fmt.Sprintf("statement _type %q, expected %q", st.Type, StatementType)
	}
	if st.PredicateType != PredicateTypeRevocation {
		return nil, fmt.Sprintf("predicateType %q, expected %q", st.PredicateType, PredicateTypeRevocation)
	}
	if !subjectMatchesLayer(st.Subject, sub.layer) {
		return nil, fmt.Sprintf("no subject digest is the build's layer %s", sub.layer)
	}
	pred := st.Predicate
	if m, _ := pred["manifest"].(string); Digest(m) != sub.manifest {
		return nil, fmt.Sprintf("predicate manifest %q is not the build's %s", m, sub.manifest)
	}
	got, okGot := asInt(pred["abi"])
	want, okWant := asInt(sub.predicate["abi"])
	if !okGot || !okWant || got != want {
		return nil, fmt.Sprintf("predicate abi %v is not the build's %v", pred["abi"], sub.predicate["abi"])
	}
	for _, f := range []string{"abi_fingerprint", "os", "arch", "clickhouse_version", "build"} {
		g, gok := pred[f].(string)
		w, wok := sub.predicate[f].(string)
		if !gok || !wok || g != w {
			return nil, fmt.Sprintf("predicate %s %v is not the build's %v", f, pred[f], sub.predicate[f])
		}
	}
	adv, _ := pred["advisory"].(map[string]any)
	id, _ := adv["id"].(string)
	u, _ := adv["url"].(string)
	if id == "" || u == "" {
		return nil, "predicate advisory does not name an id and a URL"
	}
	strs := map[string]string{}
	for _, f := range []string{"class", "summary", "superseded_by", "revoked_at"} {
		v, ok := pred[f].(string)
		if !ok {
			return nil, fmt.Sprintf("predicate %s is not a string", f)
		}
		strs[f] = v
	}
	return &Revocation{Signed: true, Manifest: sub.manifest, Advisories: []Advisory{{
		ID: sanitizeRetiredMessage(id), URL: sanitizeRetiredMessage(u), Class: sanitizeRetiredMessage(strs["class"]),
		Summary: sanitizeRetiredMessage(strs["summary"]), Build: sub.build,
		SupersededBy: sanitizeRetiredMessage(strs["superseded_by"]), RevokedAt: sanitizeRetiredMessage(strs["revoked_at"]),
	}}}, ""
}

// revokedText is the guide's message for a verified revocation: "<version>
// <platform> build <build> (manifest <digest>) is revoked: <id> <url>
// (<class>); superseded by build <superseded_by>: <summary>", a part whose
// field is empty left out.
func revokedText(sub revocationSubject, rev *Revocation) string {
	a := rev.Advisories[0]
	msg := fmt.Sprintf("%s %s build %s (manifest %s) is revoked: %s %s", sub.version, sub.platform, sub.build, sub.manifest, a.ID, a.URL)
	if a.Class != "" {
		msg += " (" + a.Class + ")"
	}
	if a.SupersededBy != "" {
		msg += "; superseded by build " + a.SupersededBy
	}
	if a.Summary != "" {
		msg += ": " + a.Summary
	}
	return msg
}

// found is one verified revocation: the bundle, for the stored record, and
// what it says.
type found struct {
	bundle []byte
	rev    *Revocation
}

// revocationOutcome is what one check learned: a verified revocation, the
// warnings for the candidates it ignored, and, when it could not read the
// status, why.
type revocationOutcome struct {
	verified *found
	warnings []string
	unread   error
}

// checkCandidates fetches and verifies each revocation candidate in turn; the
// first that verifies is the answer. Its error is only ever a 410's.
func (s *session) checkCandidates(ctx context.Context, bases []string, cands []Descriptor, sub revocationSubject, keys []ed25519.PublicKey, single bool) (revocationOutcome, error) {
	var out revocationOutcome
	for _, cand := range cands {
		body, _, _, err := s.fetchReferrerContentWith(ctx, bases, cand, BundleMaxBytes, single)
		if isGone(err) {
			return out, err
		}
		var fe *FetchError
		if err != nil && !(errors.As(err, &fe) && fe.Code == CodeArtifactCorrupt) {
			out.unread = fmt.Errorf("revocation %s could not be read: %w", cand.Digest, err)
			continue
		}
		var rev *Revocation
		var reason string
		if err != nil {
			reason = err.Error()
		} else {
			rev, reason = verifyRevocation(body, keys, sub)
		}
		if rev == nil {
			out.warnings = append(out.warnings, fmt.Sprintf("chtypes: revocation %s for manifest %s does not verify and is ignored: %s", cand.Digest, sub.manifest, reason))
			continue
		}
		out.verified = &found{bundle: body, rev: rev}
		return out, nil
	}
	return out, nil
}

// checkListing is one network check of sub: its listing, then its
// candidates. A listing that names no signature referrer is an unread status.
func (s *session) checkListing(ctx context.Context, ro resolvedOptions, sub revocationSubject, single bool) (revocationOutcome, error) {
	l, err := s.readListing(ctx, ro.bases, sub.manifest, single)
	if err != nil {
		return revocationOutcome{}, err
	}
	if !l.found {
		cause := l.failure
		if cause == nil {
			cause = errors.New("no listing names a signature for this manifest")
		}
		return revocationOutcome{unread: cause}, nil
	}
	return s.checkCandidates(ctx, ro.bases, l.revocations, sub, ro.trustedKeys, single)
}

// notCheckedText is the guide's fail-open warning, without the "chtypes: "
// every warning starts with: "revocation status not checked for manifest
// <digest> (bases: <base>, <base>): <cause>".
func notCheckedText(sub revocationSubject, bases []string, cause error) string {
	shown := make([]string, 0, len(bases))
	for _, b := range bases {
		if u, err := url.Parse(b); err == nil && u.User != nil {
			b = u.Redacted()
		}
		shown = append(shown, b)
	}
	return fmt.Sprintf("revocation status not checked for manifest %s (bases: %s): %v", sub.manifest, strings.Join(shown, ", "), cause)
}

// settle turns an outcome into the call's answer: a verified revocation is
// stored in an installed, writable entry (unless store is false) and refuses
// the build, or with AllowRevoked becomes one warning; an unread status is one
// warning, or in strict mode the transport's error.
func (ro resolvedOptions) settle(sub revocationSubject, out revocationOutcome, source string, store bool) ([]string, error) {
	warnings := append([]string(nil), out.warnings...)
	if out.verified != nil {
		if store && sub.writable {
			_ = writeRevocationRecord(sub.dir, out.verified.bundle) // a read-only cache still refuses, for this call and process
		}
		text := revokedText(sub, out.verified.rev)
		if ro.allowRevoked {
			return append(warnings, "chtypes: allowing a revoked build: "+text), nil
		}
		fe := newError(CodeArtifactRevoked, sub.request, sub.platform, source, nil, "%s", text)
		fe.Revocation = out.verified.rev
		return warnings, fe
	}
	if out.unread != nil {
		text := notCheckedText(sub, ro.bases, out.unread)
		if ro.requireRevocationCheck {
			code := CodeSourceUnreachable
			var fe *FetchError
			if errors.As(out.unread, &fe) && (fe.Code == CodeSourceUnauthorized || fe.Code == CodeSourceForbidden) {
				code = fe.Code
			}
			return warnings, newError(code, sub.request, sub.platform, "", out.unread, "%s", text)
		}
		return append(warnings, "chtypes: "+text), nil
	}
	return warnings, nil
}

// writeRevocationRecord stores a verified revocation's bundle, byte for byte,
// in an installed build's entry: a temporary file, then a rename.
func writeRevocationRecord(dir string, bundle []byte) error {
	dest := filepath.Join(dir, CacheRevocationRecord)
	if b, err := os.ReadFile(dest); err == nil && bytes.Equal(b, bundle) {
		return nil
	}
	return writeFileAtomic(dir, dest, bundle)
}

// checkStored reads sub's stored record, if its entry holds one: verified, it
// decides the call (decided true); one that does not verify is a warning, and
// the call goes on.
func (ro resolvedOptions) checkStored(sub revocationSubject) (warnings []string, decided bool, err error) {
	if sub.dir == "" {
		return nil, false, nil
	}
	path := filepath.Join(sub.dir, CacheRevocationRecord)
	body, rerr := os.ReadFile(path)
	if rerr != nil {
		return nil, false, nil
	}
	rev, reason := verifyRevocation(body, ro.trustedKeys, sub)
	if rev == nil {
		return []string{fmt.Sprintf("chtypes: revocation record %s for manifest %s does not verify and is ignored: %s", path, sub.manifest, reason)}, false, nil
	}
	w, err := ro.settle(sub, revocationOutcome{verified: &found{bundle: body, rev: rev}}, "", false)
	return w, true, err
}

// checkInstalledStored is the check every call that answers from the cache
// with no request makes (--offline, --frozen --offline): the stored record.
func checkInstalledStored(ro resolvedOptions, res *Resolved) error {
	if !ro.ch.revocation || res == nil {
		return nil
	}
	w, _, err := ro.checkStored(subjectOf(ro, res))
	res.Warnings = append(res.Warnings, w...)
	return err
}

// checkFrozenInstalled is --frozen's check of the build installed under its
// pin: the stored record, then one listing under the retry table.
func (s *session) checkFrozenInstalled(ctx context.Context, ro resolvedOptions, res *Resolved) error {
	if !ro.ch.revocation {
		return nil
	}
	sub := subjectOf(ro, res)
	w, decided, err := ro.checkStored(sub)
	res.Warnings = append(res.Warnings, w...)
	if decided || err != nil {
		return err
	}
	out, err := s.checkListing(ctx, ro, sub, false)
	if err != nil {
		return err
	}
	w, err = ro.settle(sub, out, "", true)
	res.Warnings = append(res.Warnings, w...)
	return err
}

// The installed-build check's in-process outcomes, by platform manifest
// digest: at most one check per build per process.
type memoState int

const (
	memoNotRevoked memoState = iota
	memoRevoked
	memoNotChecked
)

type memoEntry struct {
	done  chan struct{} // closed when the check has an outcome
	state memoState
	found *found // memoRevoked's revocation
}

var revocationMemo = struct {
	sync.Mutex
	m map[Digest]*memoEntry
}{m: map[Digest]*memoEntry{}}

// ResetRevocationMemoForTests forgets every kept outcome, as a new process
// would. A conformance runner calls it before every case, because the cases
// reuse manifests across cache directories, which one process never sees in
// use. It panics outside a test binary.
func ResetRevocationMemoForTests() {
	testOnly("ResetRevocationMemoForTests")
	revocationMemo.Lock()
	revocationMemo.m = map[Digest]*memoEntry{}
	revocationMemo.Unlock()
}

// checkInstalledOnce is the installed-build check (item 12): the stored record,
// then, unless offline, the kept outcome or one check of its own, with one
// attempt per base, no backoff and one deadline. Concurrent checks of one
// build share one; a kept "not checked" answers only a call without strict
// mode, and a check that ends in an error keeps nothing.
func (s *session) checkInstalledOnce(ctx context.Context, ro resolvedOptions, res *Resolved) ([]string, error) {
	if !ro.ch.revocation || res == nil {
		return nil, nil
	}
	sub := subjectOf(ro, res)
	warnings, decided, err := ro.checkStored(sub)
	if decided || err != nil || ro.offline {
		return warnings, err
	}
	for {
		revocationMemo.Lock()
		e := revocationMemo.m[sub.manifest]
		lead := e == nil
		if lead {
			e = &memoEntry{done: make(chan struct{})}
			revocationMemo.m[sub.manifest] = e
		}
		revocationMemo.Unlock()

		if !lead {
			select {
			case <-e.done:
			case <-ctx.Done():
				return warnings, ctx.Err()
			}
			revocationMemo.Lock()
			current := revocationMemo.m[sub.manifest] == e
			state, kept := e.state, e.found
			revocationMemo.Unlock()
			if !current {
				continue // that check kept nothing: check again
			}
			switch {
			case state == memoRevoked:
				w, err := ro.settle(sub, revocationOutcome{verified: kept}, "", true)
				return append(warnings, w...), err
			case state == memoNotChecked && ro.requireRevocationCheck:
				// A strict call never takes a kept "not checked": it checks for
				// itself, and keeps a better answer when it gets one.
				out, err := s.checkWithDeadline(ctx, ro, sub)
				if err != nil {
					return warnings, err
				}
				if out.unread == nil {
					s.keep(sub.manifest, out)
				}
				w, err := ro.settle(sub, out, "", true)
				return append(warnings, w...), err
			default:
				return warnings, nil // not revoked, or not checked and already warned
			}
		}

		out, cerr := s.checkWithDeadline(ctx, ro, sub)
		var w []string
		if cerr == nil {
			w, cerr = ro.settle(sub, out, "", true)
		}
		revocationMemo.Lock()
		switch {
		case cerr != nil && out.verified == nil:
			delete(revocationMemo.m, sub.manifest) // a 410 or a strict failure: keep nothing
		case out.verified != nil:
			e.state, e.found = memoRevoked, out.verified
		case out.unread != nil:
			e.state = memoNotChecked
		default:
			e.state = memoNotRevoked
		}
		revocationMemo.Unlock()
		close(e.done)
		return append(warnings, w...), cerr
	}
}

// keep records out as sub's kept outcome, replacing a "not checked".
func (s *session) keep(manifest Digest, out revocationOutcome) {
	revocationMemo.Lock()
	defer revocationMemo.Unlock()
	if e := revocationMemo.m[manifest]; e != nil {
		if out.verified != nil {
			e.state, e.found = memoRevoked, out.verified
		} else {
			e.state = memoNotRevoked
		}
	}
}

// checkWithDeadline is one network check under the installed-build check's
// budget. Passing the deadline is an unread status.
func (s *session) checkWithDeadline(ctx context.Context, ro resolvedOptions, sub revocationSubject) (revocationOutcome, error) {
	dctx, cancel := context.WithTimeout(ctx, revocationCheckDeadline)
	defer cancel()
	out, err := s.checkListing(dctx, ro, sub, true)
	if out.verified == nil && out.unread != nil && errors.Is(dctx.Err(), context.DeadlineExceeded) && ctx.Err() == nil {
		out.unread = fmt.Errorf("the check passed its %s deadline: %w", revocationCheckDeadline, out.unread)
	}
	return out, err
}

// CheckInstalled is the installed-build revocation check a registry makes
// after ResolveInstalled answers and before it loads the build
// (docs/guides/fetch-v1.md §9, "check_installed"): the stored record, then,
// unless offline, at most one check per build per process. It returns the
// warnings the open prints, or CHTYPES_ARTIFACT_REVOKED, or in strict mode the
// transport's error. Under the v1 contract it does nothing.
func CheckInstalled(ctx context.Context, res *Resolved, opts *Options) ([]string, error) {
	ro, err := resolveOptions(opts)
	if err != nil {
		return nil, err
	}
	w, err := newSession(ro).checkInstalledOnce(ctx, ro, res)
	return w, cacheIOError(err, ro.cacheDir)
}
