//! apifix: `greet` takes one more parameter than in the base, so this must
//! read changed.

/// Returns a greeting for `name`.
pub fn greet(name: &str, excited: bool) -> String {
    let bang = if excited { "!" } else { "" };
    format!("{}{name}{bang}", prefix())
}

/// Exported and identical in every variant.
pub fn keep() -> u32 {
    1
}

fn prefix() -> &'static str {
    "hello, "
}
