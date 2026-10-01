//! apifix: the base without `greet`, so this must read changed.

/// Exported and identical in every variant.
pub fn keep() -> u32 {
    1
}

#[allow(dead_code)]
fn prefix() -> &'static str {
    "hello, "
}
