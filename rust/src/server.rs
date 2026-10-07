//! The server profile (`spec/abi-v2/docs.md`: `chs_server`,
//! `input:server_profile`, `chs_server_create`, `chs_server_free`): one
//! ClickHouse server, as a profile describes it, and the handle a table is
//! compiled on ([`CompileOptions::server`](crate::CompileOptions::server)).
//!
//! The binding serializes the profile and validates nothing in it: the zone,
//! every setting and every macro are the library's to judge, and a refusal is
//! the library's own error, mapped by the generated table.

use std::collections::BTreeMap;
use std::sync::Arc;

use serde::Serialize;

use crate::abi2::calls_gen::ServerHandle;
use crate::error::{Error, Result};

/// One ClickHouse server, as `input:server_profile` describes it. Every field
/// is optional, and an absent field is omitted from the document, so the
/// server does not describe it. Every value is passed through as given: the
/// library validates the zone with `DateLUT`, each setting with the server's
/// own `SET` check, and the macros with ClickHouse's own reader.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ServerProfile {
    /// The server's zone, the one its `timezone()` returns. A schema compiled
    /// on the server binds it: its zone-less `DateTime` and `DateTime64`
    /// columns, and every call that names no `session_timezone`. `None` and
    /// the empty string both omit it, and the image zone applies.
    pub timezone: Option<String>,
    /// What the server's profile applies to every query, layered under a
    /// schema's and a call's own settings. `None` omits it: no server settings
    /// layer. `Some` of an empty map is sent as `{}`.
    pub settings: Option<BTreeMap<String, String>>,
    /// The server's `<macros>`, which a Replicated engine's ZooKeeper path and
    /// replica name expand. `None` omits it, and the server's macros are
    /// UNKNOWN: a schema whose engine reads one is declined. `Some`, even of an
    /// empty map, is sent (`{}` when empty) and is the server's COMPLETE set
    /// (its whole `system.macros`): a macro the set lacks is then the server's
    /// own refusal. **`None` is not `Some` of an empty map.**
    pub macros: Option<BTreeMap<String, String>>,
}

/// The profile's document, in the description's member order. A `BTreeMap`
/// serializes its keys sorted, and `serde_json` rewrites no value.
#[derive(Serialize)]
struct ProfileDoc<'a> {
    #[serde(skip_serializing_if = "Option::is_none")]
    timezone: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    settings: Option<&'a BTreeMap<String, String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    macros: Option<&'a BTreeMap<String, String>>,
}

/// The profile document: an empty or absent zone and an absent map are
/// omitted, and a present map is sent, as `{}` when empty.
pub(crate) fn server_profile_json(profile: &ServerProfile) -> Result<Vec<u8>> {
    serde_json::to_vec(&ProfileDoc {
        timezone: profile.timezone.as_deref().filter(|z| !z.is_empty()),
        settings: profile.settings.as_ref(),
        macros: profile.macros.as_ref(),
    })
    .map_err(|e| Error::internal(format!("the server profile does not encode: {e}")))
}

/// One ClickHouse server (`chs_server`), made by
/// [`Library::new_server`](crate::Library::new_server) and passed to
/// [`Library::compile_table`](crate::Library::compile_table) in
/// [`CompileOptions::server`](crate::CompileOptions::server).
///
/// It is immutable, `Clone + Send + Sync + 'static`, and a clone shares one
/// handle, freed when the last clone drops. A schema compiled on the server
/// holds its own counted reference inside the library, so a server and its
/// schemas drop in any order. There is no closed server in Rust: a call
/// borrows the handle, so `Drop` cannot precede it. A server from another
/// library is that library's own `CHS_INVALID_ARGUMENT` ([`Error::Usage`]).
#[derive(Clone)]
pub struct Server {
    handle: Arc<ServerHandle>,
}

impl Server {
    pub(crate) fn new(handle: ServerHandle) -> Server {
        Server {
            handle: Arc::new(handle),
        }
    }

    pub(crate) fn handle(&self) -> &ServerHandle {
        &self.handle
    }
}

impl std::fmt::Debug for Server {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Server").finish_non_exhaustive()
    }
}

/// Two servers are equal when they are one handle (a clone of the other).
impl PartialEq for Server {
    fn eq(&self, other: &Server) -> bool {
        Arc::ptr_eq(&self.handle, &other.handle)
    }
}

impl Eq for Server {}

#[cfg(test)]
mod tests {
    use super::*;

    fn map(p: &[(&str, &str)]) -> BTreeMap<String, String> {
        p.iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    fn json(p: &ServerProfile) -> String {
        String::from_utf8(server_profile_json(p).unwrap()).unwrap()
    }

    /// The profile document, byte for byte: a zero field is omitted, an absent
    /// map is omitted, and a present map is sent, as `{}` when empty. Nothing is
    /// validated or rewritten.
    #[test]
    fn the_profile_document_for_every_omit_and_empty_case() {
        let zone = |z: &str| ServerProfile {
            timezone: Some(z.to_string()),
            ..Default::default()
        };
        let cases: Vec<(&str, ServerProfile, &str)> = vec![
            ("nothing described", ServerProfile::default(), "{}"),
            (
                "timezone",
                zone("Asia/Tokyo"),
                r#"{"timezone":"Asia/Tokyo"}"#,
            ),
            ("an empty zone is omitted", zone(""), "{}"),
            (
                "absent settings omitted",
                ServerProfile {
                    timezone: Some("UTC".into()),
                    settings: None,
                    macros: None,
                },
                r#"{"timezone":"UTC"}"#,
            ),
            (
                "empty settings sent",
                ServerProfile {
                    settings: Some(BTreeMap::new()),
                    ..Default::default()
                },
                r#"{"settings":{}}"#,
            ),
            (
                "absent macros omitted (unknown)",
                ServerProfile {
                    macros: None,
                    ..Default::default()
                },
                "{}",
            ),
            (
                "empty macros sent (the complete set is empty)",
                ServerProfile {
                    macros: Some(BTreeMap::new()),
                    ..Default::default()
                },
                r#"{"macros":{}}"#,
            ),
            (
                "every member, keys sorted",
                ServerProfile {
                    timezone: Some("Europe/Berlin".into()),
                    settings: Some(map(&[
                        ("max_threads", "8"),
                        ("date_time_input_format", "best_effort"),
                    ])),
                    macros: Some(map(&[("shard", "01"), ("replica", "r1")])),
                },
                r#"{"timezone":"Europe/Berlin","settings":{"date_time_input_format":"best_effort","max_threads":"8"},"macros":{"replica":"r1","shard":"01"}}"#,
            ),
            // Passed through as given: a zone DateLUT will not load, a setting
            // no server knows, a value no server parses and a macro name no
            // reader keeps are the library's to refuse, never the binding's.
            (
                "never validated",
                ServerProfile {
                    timezone: Some("Not/AZone".into()),
                    settings: Some(map(&[("no_such_setting", "eight")])),
                    macros: Some(map(&[("", "x"), ("a.b", "<&>")])),
                },
                r#"{"timezone":"Not/AZone","settings":{"no_such_setting":"eight"},"macros":{"":"x","a.b":"<&>"}}"#,
            ),
        ];
        for (name, profile, want) in cases {
            assert_eq!(json(&profile), want, "{name}");
        }
    }

    /// Fails if the normative distinction collapses: absent macros mean
    /// UNKNOWN, an empty set means the server has none (`input:server_profile`).
    #[test]
    fn absent_macros_are_not_empty_macros() {
        let absent = json(&ServerProfile {
            timezone: Some("UTC".into()),
            macros: None,
            ..Default::default()
        });
        let empty = json(&ServerProfile {
            timezone: Some("UTC".into()),
            macros: Some(BTreeMap::new()),
            ..Default::default()
        });
        assert_ne!(
            absent, empty,
            "absent (unknown) and {{}} (complete) must differ"
        );
        assert!(!absent.contains("macros"), "{absent}");
        assert!(empty.contains(r#""macros":{}"#), "{empty}");
    }

    #[test]
    fn a_server_is_clone_send_sync_and_static() {
        fn shareable<T: Clone + Send + Sync + 'static>() {}
        shareable::<Server>();
    }
}
