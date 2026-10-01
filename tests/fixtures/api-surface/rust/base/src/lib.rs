//! apifix: a fixture for scripts/api-surface.py, the base API.

/// Returns a greeting for `name`.
pub fn greet(name: &str) -> String {
    format!("{}{name}", prefix())
}

/// Exported and identical in every variant.
pub fn keep() -> u32 {
    1
}

fn prefix() -> &'static str {
    "hello, "
}
