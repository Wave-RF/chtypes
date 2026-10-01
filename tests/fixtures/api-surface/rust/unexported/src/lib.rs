//! apifix: only private code and doc comments differ from the base, so this
//! must read unchanged.

/// Returns a friendly greeting for `name`, built by a renamed helper.
pub fn greet(name: &str) -> String {
    format!("{}{name}", salutation())
}

/// Exported and identical in every variant.
pub fn keep() -> u32 {
    1
}

fn salutation() -> &'static str {
    "hi, "
}

#[allow(dead_code)]
struct State {
    calls: u32,
}
