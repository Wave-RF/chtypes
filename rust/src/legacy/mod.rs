//! The v0 fetch machinery's remaining dependencies, quarantined. Everything
//! here exists only for the v0 `fetch` module and the `chtypes` binary, and is
//! deleted with them by the switch lane (the v1 fetch plan's §2.4).

#![allow(dead_code)]

pub mod error;
pub mod registry;
