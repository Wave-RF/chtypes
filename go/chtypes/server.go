package chtypes

// server.go — the server profile (spec/abi-v2/docs.md: chs_server,
// input:server_profile, chs_server_create, chs_server_free): one ClickHouse
// server, as a profile describes it, and the handle a table is compiled on
// (OnServer). The binding serializes the profile and validates nothing in it:
// the zone, every setting and every macro are the library's to judge.

import (
	"runtime"

	"github.com/wave-rf/chtypes/go/v2/internal/abi2"
)

// ServerProfile describes one ClickHouse server (input:server_profile). Every
// field is optional, and a field left at its zero value is omitted from the
// document, so the server does not describe it. Every value is passed through
// as given: the library validates the zone with DateLUT, each setting with the
// server's own SET check, and the macros with ClickHouse's own reader.
type ServerProfile struct {
	// Timezone is the server's zone, the one its timezone() returns. A schema
	// compiled on the server binds it: its zone-less DateTime and DateTime64
	// columns, and every call that names no session_timezone. "" leaves it
	// undescribed (omitted), and the image zone applies.
	Timezone string
	// Settings is what the server's profile applies to every query, layered
	// under a schema's and a call's own settings. nil omits it: no server
	// settings layer. A non-nil empty map is sent as {}.
	Settings map[string]string
	// Macros is the server's <macros>, which a Replicated engine's ZooKeeper
	// path and replica name expand. nil omits it, and the server's macros are
	// UNKNOWN: a schema whose engine reads one is declined. A non-nil map, even
	// an empty one, is sent ({} when empty) and is the server's COMPLETE set
	// (its whole system.macros): a macro the set lacks is then the server's own
	// refusal.
	Macros map[string]string
}

// serverProfileDoc is the profile's document. omitzero omits a nil map and
// keeps a non-nil empty one, which is the distinction input:server_profile
// makes normative for macros ("absent = unknown, present even {} = complete").
type serverProfileDoc struct {
	Timezone string            `json:"timezone,omitzero"`
	Settings map[string]string `json:"settings,omitzero"`
	Macros   map[string]string `json:"macros,omitzero"`
}

// serverProfileJSON is the profile document, with the stock encoder settings
// use (keys in sorted order, no HTML escaping, no value rewritten).
func serverProfileJSON(p ServerProfile) ([]byte, error) {
	return marshalNoEscape(serverProfileDoc(p))
}

// Server is one ClickHouse server (chs_server), made by Library.NewServer and
// passed to CompileTable with OnServer. It is immutable once made, so it is
// safe for concurrent use: any number of goroutines may compile tables on one
// server at once. Close releases the caller's reference, after the compiles
// already using the server have returned; a schema compiled on the server
// holds its own counted reference inside the library, so a server and its
// schemas close in any order. A finalizer frees what the caller abandons.
type Server struct {
	g guard
	h *abi2.Server
}

// NewServer describes one ClickHouse server from a profile
// (chs_server_create). The library validates the whole profile here, once,
// and the server never changes after it. A zone DateLUT cannot load, or a
// setting the server's SET check refuses, is the library's own refusal, a
// *SchemaError carrying ClickHouse's code; a malformed macro set is a
// *UsageError; a build that will not describe the profile declines it, an
// *UnsupportedError. The binding checks none of it first. No server option is
// defined yet, so the options document is empty.
func (l *Library) NewServer(p ServerProfile) (*Server, error) {
	profile, err := serverProfileJSON(p)
	if err != nil {
		return nil, err
	}
	h, cerr := l.tbl.ServerCreate(profile, nil)
	if cerr != nil {
		return nil, callError(cerr)
	}
	s := &Server{h: h}
	runtime.SetFinalizer(s, func(s *Server) { _ = s.Close() })
	return s, nil
}

// Close releases the caller's reference to the server, after the compiles
// already using it have returned. It is idempotent, and any close order is
// safe: every schema compiled on the server keeps it alive inside the
// library. A later OnServer compile is a *UsageError.
func (s *Server) Close() error {
	if s == nil {
		return nil
	}
	s.g.shut(func() {
		runtime.SetFinalizer(s, nil)
		_ = s.h.Close()
	})
	return nil
}

func (s *Server) enter() error {
	return s.g.enter("the server")
}
