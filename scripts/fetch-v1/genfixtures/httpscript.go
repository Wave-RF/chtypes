package main

// httpscript.go — the Go-side shape of
// spec/fetch-v1/schema/http-script.schema.json. Field tags with
// `omitempty` on every variant-specific field are deliberate: the schema's
// `response` is a `oneOf` of four shapes, each `additionalProperties:
// false`, so a response object must carry EXACTLY the keys its one active
// variant uses — `omitempty` is what makes a flat Go struct emit that.

type HTTPScript struct {
	Schema             int         `json:"schema"`
	ID                 string      `json:"id"`
	Tree               string      `json:"tree"`
	Routes             []httpRoute `json:"routes"`
	SecondOriginRoutes []httpRoute `json:"second_origin_routes"`
}

type httpRoute struct {
	Method    string         `json:"method"`
	Path      string         `json:"path"`
	Responses []httpResponse `json:"responses"`
}

// httpResponse is exactly one oneOf variant, selected by which field is
// set: FromTree, Stall, Close, or Status (with optional Headers /
// BodyFromTree). server.py documents the two header-value placeholders it
// substitutes at serve time: `{origin}`/`{second-origin}` in a Location
// header (the actual ephemeral port is not known until the server binds
// it), and the literal string "@date+N" in a Retry-After header (N
// seconds after that response's own Date header, for the HTTP-date form).
type httpResponse struct {
	FromTree     bool              `json:"from_tree,omitempty"`
	Stall        bool              `json:"stall,omitempty"`
	Close        bool              `json:"close,omitempty"`
	Status       *int              `json:"status,omitempty"`
	Headers      map[string]string `json:"headers,omitempty"`
	BodyFromTree bool              `json:"body_from_tree,omitempty"`
	// Body, when set, is the response body as text (a registry's error
	// document: retiredcases.go's 410 Gone). Never together with BodyFromTree.
	Body *string `json:"body,omitempty"`
}
