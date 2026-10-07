//! A retired repository (docs/guides/fetch-v1.md §2, "A retired
//! repository"; public issue #571). The registry's operator answers every
//! route of a retired repository with 410 Gone
//! (`constants::RETIRED_STATUSES`) and a short error document. That is
//! permanent: `http.rs` never retries it and returns
//! [`super::error::Error::SourceRetired`], which every loop over bases or
//! candidates returns at once, so the caller sees the URL that answered and
//! the registry's own message, made safe to print by [`retired_message`], the
//! one function every binding has (`tests/fixtures/retired-message/cases.json`
//! is the table all four answer alike).

use super::constants;

/// Whether `status` is a retired repository's.
pub fn is_retired_status(status: u16) -> bool {
    constants::RETIRED_STATUSES.contains(&status)
}

fn removed(c: char) -> bool {
    let cp = c as u32;
    cp <= 0x1f
        || (0x7f..=0x9f).contains(&cp)
        || cp == 0x200e
        || cp == 0x200f
        || (0x202a..=0x202e).contains(&cp)
        || (0x2066..=0x2069).contains(&cp)
}

/// The registry's own message in a retired repository's response body, made
/// safe to print, or `None` when it sent none. `body` is what was read of it
/// (at most `constants::RETIRED_BODY_MAX_BYTES`). When it is UTF-8 JSON whose
/// `errors[0].message` is a string, that string is the message: every code
/// point in U+0000-U+001F, U+007F-U+009F, U+200E, U+200F, U+202A-U+202E and
/// U+2066-U+2069 is removed, and the rest is cut to
/// `constants::RETIRED_MESSAGE_MAX_CODE_POINTS` code points, with U+2026
/// appended when it was cut. Nothing else is changed, and an empty result is
/// no message.
pub fn retired_message(body: &[u8]) -> Option<String> {
    let text = std::str::from_utf8(body).ok()?;
    // serde_json refuses a byte order mark, NaN and a lone surrogate escape,
    // as the other three bindings' readers do.
    let doc: serde_json::Value = serde_json::from_str(text).ok()?;
    let message = doc
        .get("errors")?
        .as_array()?
        .first()?
        .as_object()?
        .get("message")?
        .as_str()?;
    let max = constants::RETIRED_MESSAGE_MAX_CODE_POINTS as usize;
    let mut out = String::new();
    for (kept, c) in message.chars().filter(|c| !removed(*c)).enumerate() {
        if kept == max {
            out.push('\u{2026}');
            break;
        }
        out.push(c);
    }
    if out.is_empty() { None } else { Some(out) }
}

/// The error text for a retired repository's answer: the URL and status
/// that answered, and the registry's message when it sent one.
pub fn retired_text(url: &str, status: u16, body: &[u8]) -> String {
    let text = format!("{url} answered {status} Gone: the repository is retired");
    match retired_message(body) {
        Some(message) => format!("{text}; the registry says: {message}"),
        None => text,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn decode_hex(s: &str) -> Vec<u8> {
        (0..s.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&s[i..i + 2], 16).expect("hex"))
            .collect()
    }

    /// The table all four bindings read: one answer for every body in it.
    #[test]
    fn retired_message_answers_the_shared_table() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../tests/fixtures/retired-message/cases.json");
        let table: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&path).expect("read the shared table"))
                .expect("parse the shared table");
        let cases = table["cases"].as_array().expect("cases");
        let mut failures = Vec::new();
        for case in cases {
            let name = case["name"].as_str().expect("name");
            let body = match (case["body_text"].as_str(), case["body_hex"].as_str()) {
                (Some(text), _) => text.as_bytes().to_vec(),
                (None, Some(hex)) => decode_hex(hex),
                (None, None) => panic!("{name}: neither body_text nor body_hex"),
            };
            let want = case["message"].as_str().map(str::to_string);
            let got = retired_message(&body);
            if got != want {
                failures.push(format!("{name}: got {got:?}, want {want:?}"));
            }
        }
        assert!(failures.is_empty(), "{}", failures.join("\n"));
        for name in [
            "c0-escape-sequence",
            "bidi-override",
            "over-cap-300",
            "non-bmp-is-the-256th",
            "empty-string",
            "message-number",
        ] {
            assert!(
                cases.iter().any(|c| c["name"] == name),
                "the shared table has no {name:?} row"
            );
        }
    }

    #[test]
    fn retired_text_with_and_without_a_message() {
        let body = br#"{"errors":[{"code":"DENIED","message":"use chtypes/v2"}]}"#;
        assert_eq!(
            retired_text("https://r/v2/x/tags/list", 410, body),
            "https://r/v2/x/tags/list answered 410 Gone: the repository is retired; the registry says: use chtypes/v2"
        );
        assert_eq!(
            retired_text("https://r/v2/x/tags/list", 410, b""),
            "https://r/v2/x/tags/list answered 410 Gone: the repository is retired"
        );
    }
}
