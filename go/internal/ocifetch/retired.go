package ocifetch

// retired.go — a retired repository (docs/guides/fetch-v1.md §2, "A retired
// repository"; public issue #571). The registry's operator answers every
// route of a retired repository with 410 Gone (RetiredStatuses) and a short
// error document. That is permanent: doGet never retries it, and every base
// loop stops at it rather than asking the next base, so the caller sees
// CHTYPES_SOURCE_RETIRED with the URL that answered and the registry's own
// message, made safe to print.

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"unicode/utf8"
)

// isRetiredStatus reports whether status means a retired repository.
func isRetiredStatus(status int) bool {
	for _, s := range RetiredStatuses {
		if s == status {
			return true
		}
	}
	return false
}

// isRetired reports whether err is a CHTYPES_SOURCE_RETIRED, which every
// loop over bases or candidates returns at once instead of moving on.
func isRetired(err error) bool {
	var fe *FetchError
	return errors.As(err, &fe) && fe.Code == CodeSourceRetired
}

// retiredError is the error for a retired repository's response: the status
// and URL that answered, and the registry's message when it sent one.
func retiredError(rawURL string, status int, body []byte) *FetchError {
	text := fmt.Sprintf("%s answered %d Gone: the repository is retired", rawURL, status)
	if msg := retiredMessage(body); msg != "" {
		text += "; the registry says: " + msg
	}
	return newError(CodeSourceRetired, "", "", rawURL, nil, "%s", text)
}

// retiredMessage is the registry's own message in a retired repository's
// response body, made safe to print, or "" when it sent none. The body is
// what was read of it (at most RetiredBodyMaxBytes). When it is UTF-8 JSON
// whose errors[0].message is a string, that string is the message: every
// code point in U+0000-U+001F, U+007F-U+009F, U+200E, U+200F, U+202A-U+202E
// and U+2066-U+2069 is removed, and the rest is cut to
// RetiredMessageMaxCodePoints code points, with U+2026 appended when it was
// cut. Nothing else is changed. tests/fixtures/retired-message/cases.json is
// the table all four bindings answer alike.
func retiredMessage(body []byte) string {
	// encoding/json replaces invalid UTF-8 and lone surrogate escapes with
	// U+FFFD rather than refusing them; the other three bindings' readers
	// refuse, so this one checks both itself.
	if !utf8.Valid(body) {
		return ""
	}
	var doc map[string]json.RawMessage // exact keys: a struct would also match "Message"
	if json.Unmarshal(body, &doc) != nil {
		return ""
	}
	var errs []json.RawMessage
	if raw, ok := doc["errors"]; !ok || json.Unmarshal(raw, &errs) != nil || len(errs) == 0 {
		return ""
	}
	var first map[string]json.RawMessage
	if json.Unmarshal(errs[0], &first) != nil {
		return ""
	}
	raw := bytes.TrimSpace(first["message"])
	if len(raw) == 0 || raw[0] != '"' || hasLoneSurrogateEscape(raw) {
		return ""
	}
	var message string
	if json.Unmarshal(raw, &message) != nil {
		return ""
	}
	return sanitizeRetiredMessage(message)
}

// sanitizeRetiredMessage removes the control and bidirectional-formatting
// code points and applies the cap (retiredMessage).
func sanitizeRetiredMessage(message string) string {
	var b strings.Builder
	kept := 0
	for _, r := range message {
		if removedFromRetiredMessage(r) {
			continue
		}
		if kept == RetiredMessageMaxCodePoints {
			b.WriteRune('…')
			return b.String()
		}
		b.WriteRune(r)
		kept++
	}
	return b.String()
}

func removedFromRetiredMessage(r rune) bool {
	return r <= 0x1f || (r >= 0x7f && r <= 0x9f) || r == 0x200e || r == 0x200f ||
		(r >= 0x202a && r <= 0x202e) || (r >= 0x2066 && r <= 0x2069)
}

// hasLoneSurrogateEscape reports whether a JSON string token holds a \u
// escape of a UTF-16 surrogate that is not half of a high-then-low pair.
func hasLoneSurrogateEscape(token []byte) bool {
	hex4 := func(i int) (rune, bool) {
		if i+6 > len(token) || token[i] != '\\' || token[i+1] != 'u' {
			return 0, false
		}
		var v rune
		for _, c := range token[i+2 : i+6] {
			switch {
			case c >= '0' && c <= '9':
				v = v<<4 | rune(c-'0')
			case c >= 'a' && c <= 'f':
				v = v<<4 | rune(c-'a'+10)
			case c >= 'A' && c <= 'F':
				v = v<<4 | rune(c-'A'+10)
			default:
				return 0, false
			}
		}
		return v, true
	}
	for i := 0; i < len(token); i++ {
		if token[i] != '\\' {
			continue
		}
		v, ok := hex4(i)
		if !ok {
			i++ // a two-character escape such as \\ or \"
			continue
		}
		switch {
		case v >= 0xd800 && v <= 0xdbff:
			low, ok := hex4(i + 6)
			if !ok || low < 0xdc00 || low > 0xdfff {
				return true
			}
			i += 11
		case v >= 0xdc00 && v <= 0xdfff:
			return true
		default:
			i += 5
		}
	}
	return false
}
