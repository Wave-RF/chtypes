package chtypes

// decode_batch.go — the batch document decoder (bindings-v1.md section 5).
//
// decodeBatch reads the document in one streaming pass with stock
// encoding/json: a json.Decoder walks the top-level object key by key, and
// every nested value decodes straight into a typed struct, so no generic tree
// is built and no path string is formatted. The strict generic reader in
// decode.go (decodeBatchGeneric) stays as the fallback: the fast path answers
// only for a document it can prove the generic reader would read the same way,
// and hands every other document, including every malformed one, to the
// generic reader, so a refusal and its message come from one place.
//
// What the fast path declines (and the generic reader then judges):
//   - a key it does not know, at any depth (DisallowUnknownFields), so a field
//     the library adds still decodes, only more slowly;
//   - a duplicate key, found by the field types below, which refuse a second
//     value, and by the key set of the top-level object;
//   - a value of the wrong JSON type, a name carried both ways or neither, a
//     base64 value outside the standard alphabet, an integer that does not
//     parse, and trailing data. A vocabulary value outside the description is
//     not declined: both readers keep it as its unknown(n) (ABI v2 rule r3).
//
// One difference stays, and is the stock decoder's: encoding/json matches a
// struct field's key case-insensitively, so below the top level a key that
// differs from a known key only in letter case reads as that key here, where
// the generic reader would ignore it as unknown. The library never emits one.
// docs/reference/bindings-v1.md section 5 rule 3 records this tolerance, and the
// TestDecodeBatchEquivalence tests keep it from diverging silently: they compare
// this decoder with the generic reader on every document fixture.

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"maps"
	"math"
	"strconv"
	"sync"
	"sync/atomic"
	"unicode/utf8"
)

// errDeclined means the fast path will not answer; the caller falls back.
var errDeclined = errors.New("chtypes: batch fast path declined")

func decodeBatch(raw []byte, payload []byte) (BatchResult, error) {
	if res, ok := decodeBatchFast(raw, payload); ok {
		return res, nil
	}
	return decodeBatchGeneric(raw, payload)
}

// ---- field types. Each refuses a second value for its key (a duplicate) and
// mirrors one accessor of the generic reader: JSON null is read as absent.

func isJSONNull(data []byte) bool { return len(data) == 4 && data[0] == 'n' }

// jsonString decodes a JSON string value the way encoding/json does. A string
// with no escape and valid UTF-8 is copied as it is; anything else (an escape,
// invalid UTF-8) goes through the stock decoder, which also does the
// replacement of invalid UTF-8 the generic reader's strings get.
func jsonString(data []byte, word bool) (string, error) {
	if len(data) < 2 || data[0] != '"' {
		return "", errDeclined
	}
	in := data[1 : len(data)-1]
	if bytes.IndexByte(in, '\\') < 0 {
		if utf8.Valid(in) {
			if word {
				return internWord(in), nil
			}
			return string(in), nil
		}
	}
	// The stock decoder unquotes it, through a pooled decoder so no decoder is
	// built for each string.
	sd, _ := stringDecoders.Get().(*subDecoder)
	if sd == nil {
		sd = newSubDecoder()
	}
	var s string
	err := sd.decode(data, &s)
	if err == nil {
		stringDecoders.Put(sd)
	}
	return s, err
}

// stringDecoders holds decoders for the strings that carry an escape. A
// decoder that failed is dropped, never returned.
var stringDecoders sync.Pool

// words interns the short strings a document repeats on every row (column
// names, a value's source, a transform's reason): a lookup allocates nothing,
// and a new word is added by copying the map, so reads take no lock. The table
// stops growing at wordMax entries, after which a word is just copied, so a
// stream of distinct names cannot grow it.
var words atomic.Pointer[map[string]string]

const (
	wordMax = 512
	wordLen = 32
)

func internWord(in []byte) string {
	if len(in) > wordLen {
		return string(in)
	}
	if m := words.Load(); m != nil {
		if s, ok := (*m)[string(in)]; ok {
			return s
		}
	}
	s := string(in)
	for {
		old := words.Load()
		n := map[string]string{}
		if old != nil {
			if len(*old) >= wordMax {
				return s
			}
			maps.Copy(n, *old)
		}
		n[s] = s
		if words.CompareAndSwap(old, &n) {
			return s
		}
	}
}

// fString is a string field; null reads as absent.
type fString struct {
	seen, null bool
	s          string
}

// fWord is a string field of a small repeated vocabulary, interned.
type fWord struct{ fString }

func (f *fWord) UnmarshalJSON(data []byte) error {
	if f.seen {
		return errDeclined
	}
	f.seen = true
	if isJSONNull(data) {
		f.null = true
		return nil
	}
	s, err := jsonString(data, true)
	f.s = s
	return err
}

func (f *fString) UnmarshalJSON(data []byte) error {
	if f.seen {
		return errDeclined
	}
	f.seen = true
	if isJSONNull(data) {
		f.null = true
		return nil
	}
	s, err := jsonString(data, false)
	f.s = s
	return err
}

// fB64 is a "_b64" field: the standard base64 of raw bytes, decoded as it is
// read and kept as the byte string. Null is kept apart from absent because
// the generic reader refuses a null "<key>_b64" next to nothing.
type fB64 struct {
	seen, null bool
	s          string
}

func (f *fB64) UnmarshalJSON(data []byte) error {
	if f.seen {
		return errDeclined
	}
	f.seen = true
	if isJSONNull(data) {
		f.null = true
		return nil
	}
	if len(data) < 2 || data[0] != '"' {
		return errDeclined
	}
	in := data[1 : len(data)-1]
	if bytes.IndexByte(in, '\\') >= 0 {
		s, err := jsonString(data, false)
		if err != nil {
			return err
		}
		in = []byte(s)
	}
	var scratch [128]byte
	dst, err := base64.StdEncoding.AppendDecode(scratch[:0], in)
	if err != nil {
		return errDeclined
	}
	f.s = string(dst)
	return nil
}

// fBool is a boolean field; null reads as absent (false), and optBool's
// "unknown" is seen && !null.
type fBool struct {
	seen, null, v bool
}

func (f *fBool) UnmarshalJSON(data []byte) error {
	if f.seen {
		return errDeclined
	}
	f.seen = true
	switch {
	case isJSONNull(data):
		f.null = true
	case len(data) == 4 && data[0] == 't':
		f.v = true
	case len(data) == 5 && data[0] == 'f':
	default:
		return errDeclined
	}
	return nil
}

// intText is the digits of an integer value: a JSON number as written, or the
// content of an escape-free JSON string (an integer beyond 2^53 travels as a
// string).
func intText(data []byte) ([]byte, bool) {
	switch c := data[0]; {
	case c == '"':
		in := data[1 : len(data)-1]
		return in, bytes.IndexByte(in, '\\') < 0
	case c == '-' || (c >= '0' && c <= '9'):
		return data, true
	}
	return nil, false
}

type fU64 struct {
	seen, null bool
	v          uint64
}

func (f *fU64) UnmarshalJSON(data []byte) error {
	if f.seen {
		return errDeclined
	}
	f.seen = true
	if isJSONNull(data) {
		f.null = true
		return nil
	}
	txt, ok := intText(data)
	if !ok {
		return errDeclined
	}
	v, err := strconv.ParseUint(string(txt), 10, 64)
	if err != nil {
		return errDeclined
	}
	f.v = v
	return nil
}

type fI32 struct {
	seen bool
	v    int32
}

func (f *fI32) UnmarshalJSON(data []byte) error {
	if f.seen {
		return errDeclined
	}
	f.seen = true
	if isJSONNull(data) {
		return nil
	}
	txt, ok := intText(data)
	if !ok {
		return errDeclined
	}
	v, err := strconv.ParseInt(string(txt), 10, 32)
	if err != nil || v < math.MinInt32 || v > math.MaxInt32 {
		return errDeclined
	}
	f.v = int32(v)
	return nil
}

// fInt is the generic reader's non-negative int (at most MaxInt32).
type fInt struct {
	seen bool
	v    int
}

func (f *fInt) UnmarshalJSON(data []byte) error {
	if f.seen {
		return errDeclined
	}
	f.seen = true
	if isJSONNull(data) {
		return nil
	}
	txt, ok := intText(data)
	if !ok {
		return errDeclined
	}
	v, err := strconv.ParseInt(string(txt), 10, 64)
	if err != nil || v < 0 || v > math.MaxInt32 {
		return errDeclined
	}
	f.v = int(v)
	return nil
}

// fIgnore is a key the decoder knows is in the document and does not read.
type fIgnore struct{ seen bool }

func (f *fIgnore) UnmarshalJSON([]byte) error {
	if f.seen {
		return errDeclined
	}
	f.seen = true
	return nil
}

// ---- nested values.

// feeder hands one value's bytes to a long-lived decoder, so a list inside a
// row decodes without a new json.Decoder and buffer each time.
type feeder struct{ b []byte }

func (f *feeder) Read(p []byte) (int, error) {
	if len(f.b) == 0 {
		return 0, io.EOF
	}
	n := copy(p, f.b)
	f.b = f.b[n:]
	return n, nil
}

// subDecoder decodes one self-contained value at a time into a typed target,
// refusing unknown keys. It is not re-entrant: a value decoded through it must
// not itself reach for the same subDecoder.
type subDecoder struct {
	feed feeder
	dec  *json.Decoder
}

func newSubDecoder() *subDecoder {
	s := &subDecoder{}
	s.dec = json.NewDecoder(&s.feed)
	s.dec.DisallowUnknownFields()
	return s
}

func (s *subDecoder) decode(data []byte, v any) error {
	s.feed.b = data
	err := s.dec.Decode(v)
	s.feed.b = nil
	if err != nil {
		return errDeclined
	}
	return nil
}

// fList is an array field decoded as a list of T. "[]" and null are told
// apart from absent by seen and null; the backing array is reused across rows.
type fList[T any] struct {
	sub        *subDecoder
	seen, null bool
	v          []T
}

func (l *fList[T]) UnmarshalJSON(data []byte) error {
	if l.seen {
		return errDeclined
	}
	l.seen = true
	switch {
	case isJSONNull(data):
		l.null = true
		return nil
	case len(data) == 2 && data[0] == '[':
		return nil
	case data[0] != '[':
		return errDeclined
	}
	if l.sub == nil {
		l.sub = newSubDecoder()
	}
	return l.sub.decode(data, &l.v)
}

func (l *fList[T]) present() bool { return l.seen && !l.null }

// reset readies the list for the next row, zeroing what it decoded.
func (l *fList[T]) reset() {
	clear(l.v)
	l.v = l.v[:0]
	l.seen, l.null = false, false
}

// fObj is an object field decoded as a T.
type fObj[T any] struct {
	sub        *subDecoder
	seen, null bool
	v          T
}

func (o *fObj[T]) UnmarshalJSON(data []byte) error {
	if o.seen {
		return errDeclined
	}
	o.seen = true
	if isJSONNull(data) {
		o.null = true
		return nil
	}
	if data[0] != '{' {
		return errDeclined
	}
	if o.sub == nil {
		o.sub = newSubDecoder()
	}
	return o.sub.decode(data, &o.v)
}

func (o *fObj[T]) present() bool { return o.seen && !o.null }

// ---- document shapes.

type nameDoc struct {
	Name    fWord `json:"name"`
	NameB64 fB64  `json:"name_b64"`
}

type spanDoc struct {
	Off fU64 `json:"off"`
	Len fU64 `json:"len"`
}

// spanSlot is one element of a list of spans (row_spans, unconsumed). A
// pointer element would cost the stock decoder an allocation per span; a value
// element does not, and a null element is told apart by null.
type spanSlot struct {
	null bool
	v    spanDoc
}

func (s *spanSlot) UnmarshalJSON(data []byte) error {
	if isJSONNull(data) {
		s.null = true
		return nil
	}
	if data[0] != '{' {
		return errDeclined
	}
	sd, _ := stringDecoders.Get().(*subDecoder)
	if sd == nil {
		sd = newSubDecoder()
	}
	err := sd.decode(data, &s.v)
	if err == nil {
		stringDecoders.Put(sd)
	}
	return err
}

type transformDoc struct {
	Row       fInt    `json:"row"`
	Reason    fWord   `json:"reason"`
	Lossy     fIgnore `json:"lossy"`
	Column    fWord   `json:"column"`
	ColumnB64 fB64    `json:"column_b64"`
	Input     fString `json:"input"`
	InputB64  fB64    `json:"input_b64"`
	Stored    fString `json:"stored"`
	StoredB64 fB64    `json:"stored_b64"`
}

type colDoc struct {
	Name       fWord   `json:"name"`
	NameB64    fB64    `json:"name_b64"`
	Src        fWord   `json:"src"`
	Stored     fString `json:"stored"`
	StoredB64  fB64    `json:"stored_b64"`
	ValueB64   fB64    `json:"value_b64"`
	Null       fBool   `json:"null"`
	Type       fIgnore `json:"type"`
	TypeB64    fIgnore `json:"type_b64"`
	Base       fIgnore `json:"base"`
	BaseB64    fIgnore `json:"base_b64"`
	Input      fIgnore `json:"input"`
	InputB64   fIgnore `json:"input_b64"`
	Ref        fIgnore `json:"ref"`
	RefB64     fIgnore `json:"ref_b64"`
	RefType    fIgnore `json:"ref_type"`
	RefTypeB64 fIgnore `json:"ref_type_b64"`
	Wire       fIgnore `json:"wire"`
	WireB64    fIgnore `json:"wire_b64"`
	Nullable   fIgnore `json:"nullable"`
	Poison     fIgnore `json:"poison"`
	NullInput  fIgnore `json:"null_input"`
	DupDropped fIgnore `json:"dup_dropped"`
	IsStored   fIgnore `json:"is_stored"`
}

type computedDoc struct {
	Name      fWord   `json:"name"`
	NameB64   fB64    `json:"name_b64"`
	Kind      fWord   `json:"kind"`
	Stored    fString `json:"stored"`
	StoredB64 fB64    `json:"stored_b64"`
	ValueB64  fB64    `json:"value_b64"`
}

type cellDoc struct {
	Name      fWord   `json:"name"`
	NameB64   fB64    `json:"name_b64"`
	Stored    fString `json:"stored"`
	StoredB64 fB64    `json:"stored_b64"`
	ValueB64  fB64    `json:"value_b64"`
	Null      fBool   `json:"null"`
}

type headerDoc struct {
	Consumed fBool          `json:"consumed"`
	Lines    fInt           `json:"lines"`
	Names    fList[nameDoc] `json:"names"`
}

type framingDoc struct {
	BomSkipped fBool           `json:"bom_skipped"`
	Container  fString         `json:"container"`
	Header     fObj[headerDoc] `json:"header"`
}

// batchRowDoc is one entry of rows. The lists keep their backing arrays between
// rows (reset), the scalars are cleared.
type batchRowDoc struct {
	Outcome            fWord               `json:"outcome"`
	Code               fI32                `json:"code"`
	Err                fString             `json:"err"`
	ErrB64             fB64                `json:"err_b64"`
	Verdict            fString             `json:"verdict"`
	VerdictCode        fI32                `json:"verdict_code"`
	VerdictErr         fString             `json:"verdict_err"`
	VerdictErrB64      fB64                `json:"verdict_err_b64"`
	PartitionID        fString             `json:"partition_id"`
	PartitionIDB64     fB64                `json:"partition_id_b64"`
	InputSpan          fObj[spanDoc]       `json:"input_span"`
	UnknownFields      fList[nameDoc]      `json:"unknown_fields"`
	UnsupportedSetting fList[nameDoc]      `json:"unsupported_settings"`
	Computed           fList[computedDoc]  `json:"computed"`
	Cols               fList[colDoc]       `json:"cols"`
	Transformed        fList[transformDoc] `json:"transformed"`
}

func newBatchRowDoc() *batchRowDoc {
	sub := newSubDecoder()
	r := &batchRowDoc{}
	r.InputSpan.sub = sub
	r.UnknownFields.sub = sub
	r.UnsupportedSetting.sub = sub
	r.Computed.sub = sub
	r.Cols.sub = sub
	r.Transformed.sub = sub
	return r
}

// reset readies the row for the next one: every scalar cleared, every list
// emptied with its backing array kept.
func (d *batchRowDoc) reset() {
	d.Outcome, d.Code, d.Err, d.ErrB64 = fWord{}, fI32{}, fString{}, fB64{}
	d.Verdict, d.VerdictCode = fString{}, fI32{}
	d.VerdictErr, d.VerdictErrB64 = fString{}, fB64{}
	d.PartitionID, d.PartitionIDB64 = fString{}, fB64{}
	d.InputSpan.seen, d.InputSpan.null, d.InputSpan.v = false, false, spanDoc{}
	d.UnknownFields.reset()
	d.UnsupportedSetting.reset()
	d.Computed.reset()
	d.Cols.reset()
	d.Transformed.reset()
}

// ---- conversions. Each mirrors one accessor of the generic reader and reports
// false where that accessor would have failed.

// byteOf is byteField for a "<key>" / "<key>_b64" pair.
func byteOf(plain *fString, b64 *fB64) (value string, present, ok bool) {
	switch {
	case plain.seen && b64.seen:
		return "", false, false
	case plain.seen:
		if plain.null {
			return "", false, true
		}
		return plain.s, true, true
	case b64.seen:
		if b64.null {
			return "", false, false
		}
		return b64.s, true, true
	}
	return "", false, true
}

// bytesOf is reader.bytes: absent is "".
func bytesOf(plain *fString, b64 *fB64) (string, bool) {
	s, _, ok := byteOf(plain, b64)
	return s, ok
}

// nameOf is reader.name: exactly one of the pair.
func nameOf(plain *fString, b64 *fB64) (string, bool) {
	s, present, ok := byteOf(plain, b64)
	return s, ok && present
}

// strSlab hands out *string from one allocation: a row's value_b64 entries
// share a backing array instead of allocating a pointer each.
type strSlab []string

func (p *strSlab) take(s string) *string {
	(*p)[0] = s
	out := &(*p)[0]
	*p = (*p)[1:]
	return out
}

// rawValueOf is reader.rawValue.
func rawValueOf(b64 *fB64, slab *strSlab) *string {
	if !b64.seen || b64.null {
		return nil
	}
	return slab.take(b64.s)
}

func hasRawValue(b64 *fB64) int {
	if b64.seen && !b64.null {
		return 1
	}
	return 0
}

func namesOf(l *fList[nameDoc]) ([]string, bool) {
	if !l.present() {
		return nil, true
	}
	out := make([]string, 0, len(l.v))
	for i := range l.v {
		s, ok := nameOf(&l.v[i].Name.fString, &l.v[i].NameB64)
		if !ok {
			return nil, false
		}
		out = append(out, s)
	}
	return out, true
}

func transformsOf(list []transformDoc, present bool) ([]Transform, bool) {
	if !present {
		return nil, true
	}
	out := make([]Transform, 0, len(list))
	for i := range list {
		t := &list[i]
		column, ok := nameOf(&t.Column.fString, &t.ColumnB64)
		if !ok {
			return nil, false
		}
		input, ok := bytesOf(&t.Input, &t.InputB64)
		if !ok {
			return nil, false
		}
		stored, ok := bytesOf(&t.Stored, &t.StoredB64)
		if !ok {
			return nil, false
		}
		reason := Reason(t.Reason.s)
		out = append(out, Transform{
			Column: column, Input: input, Stored: stored,
			Reason: reason, Lossy: reason.Lossy(), Row: t.Row.v,
		})
	}
	return out, true
}

// decodeSpanList reads a list of spans one element at a time, so the decoder
// never holds the whole list in its buffer (a list of one span per row is
// most of a document that has no row detail), and straight into the []Span the
// result carries. The result is nil for JSON null and non-nil, however short,
// for a list; hint sizes it. A null element is an error, as it is for the
// generic reader.
func decodeSpanList(dec *json.Decoder, hint int) ([]Span, error) {
	t, err := dec.Token()
	if err != nil {
		return nil, errDeclined
	}
	if t == nil {
		return nil, nil
	}
	if t != json.Delim('[') {
		return nil, errDeclined
	}
	list := make([]Span, 0, hint)
	// One slot for every element: a slot declared per element would be a heap
	// allocation each, since the decoder keeps the pointer.
	slot := new(spanSlot)
	for dec.More() {
		*slot = spanSlot{}
		if err := dec.Decode(slot); err != nil || slot.null {
			return nil, errDeclined
		}
		list = append(list, Span{Off: slot.v.Off.v, Len: slot.v.Len.v})
	}
	if t, err := dec.Token(); err != nil || t != json.Delim(']') {
		return nil, errDeclined
	}
	return list, nil
}

// spanHint is the count a document has already announced (one span per row),
// or 0 when none was or it is not credible for a document of docLen bytes.
func spanHint(n fU64, docLen int) int {
	if !n.seen || n.v > uint64(docLen/16) { // a span takes at least 16 bytes
		return 0
	}
	return int(n.v)
}

func (d *batchRowDoc) result(slabs *rowSlabs) (RowResult, bool) {
	var res RowResult
	var ok bool
	res.Outcome = parseRowOutcome(d.Outcome.s)
	res.ErrCode = d.Code.v
	if res.ErrMsg, ok = bytesOf(&d.Err, &d.ErrB64); !ok {
		return res, false
	}
	if res.Transformed, ok = transformsOf(d.Transformed.v, d.Transformed.present()); !ok {
		return res, false
	}
	if res.UnsupportedSettings, ok = namesOf(&d.UnsupportedSetting); !ok {
		return res, false
	}
	if res.UnknownFields, ok = namesOf(&d.UnknownFields); !ok {
		return res, false
	}
	res.VerdictCode = d.VerdictCode.v
	if res.VerdictErr, ok = bytesOf(&d.VerdictErr, &d.VerdictErrB64); !ok {
		return res, false
	}
	nValues := 0
	for i := range d.Cols.v {
		nValues += hasRawValue(&d.Cols.v[i].ValueB64)
	}
	for i := range d.Computed.v {
		nValues += hasRawValue(&d.Computed.v[i].ValueB64)
	}
	var slab strSlab
	if nValues > 0 {
		slab = make(strSlab, nValues)
	}
	if d.Cols.present() && len(d.Cols.v) > 0 {
		cols := d.Cols.v
		stored := 0
		for i := range cols {
			// An unlisted src is its unknown(n), as in the generic reader (r3);
			// an absent one is the generic reader's to refuse.
			if !cols[i].Src.seen || cols[i].Src.null {
				return res, false
			}
			src := Source(cols[i].Src.s)
			if src.IsStored() {
				stored++
			}
		}
		res.Columns = make([]Value, 0, len(cols))
		if stored > 0 {
			res.Values = make([]Value, 0, stored)
		}
		for i := range cols {
			c := &cols[i]
			src := Source(c.Src.s)
			v := Value{Source: src, IsStored: src.IsStored(), Null: c.Null.v}
			if v.Column, ok = nameOf(&c.Name.fString, &c.NameB64); !ok {
				return res, false
			}
			if v.Text, ok = bytesOf(&c.Stored, &c.StoredB64); !ok {
				return res, false
			}
			v.Value = rawValueOf(&c.ValueB64, &slab)
			res.Columns = append(res.Columns, v)
			if v.IsStored {
				res.Values = append(res.Values, v)
			}
		}
	}
	if d.Computed.present() && len(d.Computed.v) > 0 {
		res.Computed = make([]Computed, 0, len(d.Computed.v))
		for i := range d.Computed.v {
			c := &d.Computed.v[i]
			var cp Computed
			if cp.Column, ok = nameOf(&c.Name.fString, &c.NameB64); !ok {
				return res, false
			}
			cp.Kind = c.Kind.s
			if cp.Text, ok = bytesOf(&c.Stored, &c.StoredB64); !ok {
				return res, false
			}
			cp.Value = rawValueOf(&c.ValueB64, &slab)
			res.Computed = append(res.Computed, cp)
		}
	}
	if d.Verdict.seen && !d.Verdict.null {
		res.Verdict = slabs.verdict(parseVerdict(d.Verdict.s))
	}
	if s, present, good := byteOf(&d.PartitionID, &d.PartitionIDB64); !good {
		return res, false
	} else if present {
		res.PartitionID = slabs.partition(s)
	}
	if d.InputSpan.present() {
		res.InputSpan = slabs.span(Span{Off: d.InputSpan.v.Off.v, Len: d.InputSpan.v.Len.v})
	}
	return res, true
}

// rowSlabs hands out the per-row pointers a RowResult carries (Verdict,
// PartitionID, InputSpan) from shared chunks, one allocation for many rows
// where one allocation per row was. Every pointer still addresses its own
// element, so a caller that writes through one changes only that row.
type rowSlabs struct {
	spans    []Span
	verdicts []Verdict
	parts    []string
}

const slabChunk = 32

func (s *rowSlabs) span(v Span) *Span {
	if len(s.spans) == 0 {
		s.spans = make([]Span, slabChunk)
	}
	p := &s.spans[0]
	s.spans = s.spans[1:]
	*p = v
	return p
}

func (s *rowSlabs) verdict(v Verdict) *Verdict {
	if len(s.verdicts) == 0 {
		s.verdicts = make([]Verdict, slabChunk)
	}
	p := &s.verdicts[0]
	s.verdicts = s.verdicts[1:]
	*p = v
	return p
}

func (s *rowSlabs) partition(v string) *string {
	if len(s.parts) == 0 {
		s.parts = make([]string, slabChunk)
	}
	p := &s.parts[0]
	s.parts = s.parts[1:]
	*p = v
	return p
}

// batch top-level keys, as a bit set for the duplicate check.
const (
	kOutcome = 1 << iota
	kCode
	kErr
	kErrB64
	kRowsRead
	kRowsSkipped
	kRowsPassed
	kRowsCut
	kExportDeclined
	kExportDeclinedB64
	kPartitionCount
	kTransformed
	kRowSpans
	kUnconsumed
	kEngineRows
	kFraming
	kRows
	kStorageTransforms
)

// decodeBatchFast reads a batch document; ok is false when the generic reader
// must judge it.
func decodeBatchFast(raw, payload []byte) (res BatchResult, ok bool) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if t, err := dec.Token(); err != nil || t != json.Delim('{') {
		return res, false
	}

	// Every field below is decoded through a pointer the decoder keeps, so
	// each would be its own heap allocation as a separate variable; one
	// struct is one.
	var top struct {
		outcome               fString
		code                  fI32
		errText               fString
		errB64                fB64
		rowsRead, rowsSkipped fU64
		rowsPassed, rowsCut   fU64
		exportDeclined        fString
		exportDeclinedB64     fB64
		partitionCount        fU64
		transformed           []transformDoc
		rowSpans, unconsumed  []Span
		engineRows            [][]cellDoc
		framing               *framingDoc
		storageTransforms     fIgnore
	}
	var (
		seen        uint32
		rows        []RowResult
		rowsPresent bool
	)
	for dec.More() {
		kt, err := dec.Token()
		if err != nil {
			return res, false
		}
		key, isStr := kt.(string)
		if !isStr {
			return res, false
		}
		var bit uint32
		switch key {
		case "outcome":
			bit, err = kOutcome, dec.Decode(&top.outcome)
		case "code":
			bit, err = kCode, dec.Decode(&top.code)
		case "err":
			bit, err = kErr, dec.Decode(&top.errText)
		case "err_b64":
			bit, err = kErrB64, dec.Decode(&top.errB64)
		case "rows_read":
			bit, err = kRowsRead, dec.Decode(&top.rowsRead)
		case "rows_skipped":
			bit, err = kRowsSkipped, dec.Decode(&top.rowsSkipped)
		case "rows_passed":
			bit, err = kRowsPassed, dec.Decode(&top.rowsPassed)
		case "rows_cut":
			bit, err = kRowsCut, dec.Decode(&top.rowsCut)
		case "export_declined":
			bit, err = kExportDeclined, dec.Decode(&top.exportDeclined)
		case "export_declined_b64":
			bit, err = kExportDeclinedB64, dec.Decode(&top.exportDeclinedB64)
		case "partition_count":
			bit, err = kPartitionCount, dec.Decode(&top.partitionCount)
		case "transformed":
			bit, err = kTransformed, dec.Decode(&top.transformed)
		case "row_spans":
			// One span per row, so rows_read, when it came first, sizes the list.
			bit = kRowSpans
			top.rowSpans, err = decodeSpanList(dec, spanHint(top.rowsRead, len(raw)))
		case "unconsumed":
			bit = kUnconsumed
			top.unconsumed, err = decodeSpanList(dec, 0)
		case "engine_rows":
			bit, err = kEngineRows, dec.Decode(&top.engineRows)
		case "framing":
			bit, err = kFraming, dec.Decode(&top.framing)
		case "storage_transforms":
			bit, err = kStorageTransforms, dec.Decode(&top.storageTransforms)
		case "rows":
			bit = kRows
			hint := 0
			if top.rowsRead.seen && top.rowsRead.v <= uint64(len(raw)) {
				hint = int(top.rowsRead.v)
			}
			rows, rowsPresent, err = decodeBatchRows(dec, hint)
		default:
			return res, false
		}
		if err != nil || seen&bit != 0 {
			return res, false
		}
		seen |= bit
	}
	if t, err := dec.Token(); err != nil || t != json.Delim('}') {
		return res, false
	}
	if _, err := dec.Token(); err != io.EOF {
		return res, false
	}

	var good bool
	res.Outcome = parseBatchOutcome(top.outcome.s)
	res.ErrCode = top.code.v
	if res.ErrMsg, good = bytesOf(&top.errText, &top.errB64); !good {
		return res, false
	}
	res.RowsRead, res.RowsSkipped = top.rowsRead.v, top.rowsSkipped.v
	if res.Transformed, good = transformsOf(top.transformed, top.transformed != nil); !good {
		return res, false
	}
	res.Payload = payload
	res.Spans = top.rowSpans
	if res.ExportDeclined, good = bytesOf(&top.exportDeclined, &top.exportDeclinedB64); !good {
		return res, false
	}
	res.RowsPassed, res.RowsCut = top.rowsPassed.v, top.rowsCut.v
	res.Unconsumed = top.unconsumed
	if rowsPresent {
		res.Rows = rows
	}
	if top.engineRows != nil {
		cellSlab := make(strSlab, 0)
		for _, cells := range top.engineRows {
			for i := range cells {
				if hasRawValue(&cells[i].ValueB64) == 1 {
					cellSlab = append(cellSlab, "")
				}
			}
		}
		res.EngineRows = make([][]EngineCell, 0, len(top.engineRows))
		for _, cells := range top.engineRows {
			out := make([]EngineCell, 0, len(cells))
			for i := range cells {
				c := &cells[i]
				var cell EngineCell
				if cell.Column, good = nameOf(&c.Name.fString, &c.NameB64); !good {
					return res, false
				}
				if cell.Text, good = bytesOf(&c.Stored, &c.StoredB64); !good {
					return res, false
				}
				cell.Null = c.Null.v
				cell.Value = rawValueOf(&c.ValueB64, &cellSlab)
				out = append(out, cell)
			}
			res.EngineRows = append(res.EngineRows, out)
		}
	}
	if top.partitionCount.seen && !top.partitionCount.null {
		n := top.partitionCount.v
		res.PartitionCount = &n
	}
	if top.framing != nil {
		f := &Framing{}
		if top.framing.BomSkipped.seen && !top.framing.BomSkipped.null {
			b := top.framing.BomSkipped.v
			f.BomSkipped = &b
		}
		f.Container = top.framing.Container.s
		if top.framing.Header.present() {
			h := &top.framing.Header.v
			hd := &Header{Consumed: h.Consumed.v, Lines: h.Lines.v}
			if h.Names.present() {
				hd.Names = make([]string, 0, len(h.Names.v))
				for i := range h.Names.v {
					s, good := nameOf(&h.Names.v[i].Name.fString, &h.Names.v[i].NameB64)
					if !good {
						return res, false
					}
					hd.Names = append(hd.Names, s)
				}
			}
			f.Header = hd
		}
		res.Framing = f
	}
	return res, true
}

// decodeBatchRows streams the rows array of the document, one row at a time
// through a reused batchRowDoc. present is false for JSON null.
func decodeBatchRows(dec *json.Decoder, hint int) (rows []RowResult, present bool, err error) {
	t, err := dec.Token()
	if err != nil {
		return nil, false, errDeclined
	}
	if t == nil {
		return nil, false, nil
	}
	if t != json.Delim('[') {
		return nil, false, errDeclined
	}
	rows = make([]RowResult, 0, hint)
	row := getRowDoc()
	p := row
	var slabs rowSlabs
	for dec.More() {
		row.reset()
		p = row
		if err := dec.Decode(&p); err != nil || p == nil {
			return nil, false, errDeclined
		}
		res, ok := row.result(&slabs)
		if !ok {
			return nil, false, errDeclined
		}
		rows = append(rows, res)
	}
	if t, err := dec.Token(); err != nil || t != json.Delim(']') {
		return nil, false, errDeclined
	}
	putRowDoc(row)
	return rows, true, nil
}

// rowDocs keeps row documents, each with its sub-decoder and the backing
// arrays of its lists, between calls, so a call builds none. A document is
// put back only after a clean pass, emptied; one that met an error is dropped,
// and nothing a RowResult holds points into one (every value is copied out in
// result).
var rowDocs sync.Pool

func getRowDoc() *batchRowDoc {
	if d, _ := rowDocs.Get().(*batchRowDoc); d != nil {
		return d
	}
	return newBatchRowDoc()
}

func putRowDoc(d *batchRowDoc) {
	d.reset()
	rowDocs.Put(d)
}
