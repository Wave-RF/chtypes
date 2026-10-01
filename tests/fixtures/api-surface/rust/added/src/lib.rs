//! apifix: the base plus one public function, so this must read changed.

/// Returns a greeting for `name`.
pub fn greet(name: &str) -> String {
    format!("{}{name}", prefix())
}

/// Exported and identical in every variant.
pub fn keep() -> u32 {
    1
}

/// The addition.
pub fn farewell(name: &str) -> String {
    format!("bye, {name}")
}

fn prefix() -> &'static str {
    "hello, "
}
