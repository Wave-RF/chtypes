//! The public golden set — SERVED beside the artifacts as `sdk-goldens.json`.
//!
//! A few dozen cases whose expectations were produced by the library itself
//! and agreed on by every ClickHouse version in the generating registry
//! (the golden-set generator). Every SDK runs the
//! same file, so the four bindings are held to one answer. It is not the
//! corpus; that lives with the rigs.

use std::collections::BTreeMap;
use std::path::PathBuf;

use chtypes::{Error, FilterOutcome, Format, NO_SETTINGS, Registry, Verdict};
use serde_json::Value as Json;

/// The SERVED set: core publishes `sdk-goldens.json` in the rolling release as a
/// row in the signed `SHA256SUMS`, so a fetch installs it beside the artifacts
/// and this test reads it offline. `CHTYPES_GOLDENS` overrides the path.
///
/// `None` when this machine has not fetched one — a loud skip, never a panic
/// that reads as a failure.
fn goldens(registry_dir: &std::path::Path) -> Option<Json> {
    let path = std::env::var_os("CHTYPES_GOLDENS")
        .map(PathBuf::from)
        .unwrap_or_else(|| registry_dir.join("sdk-goldens.json"));
    let blob = match std::fs::read(&path) {
        Ok(b) => b,
        Err(e) => {
            announce(&format!(
                "\nSKIP goldens_hold_on_every_artifact: no golden set at {} ({e}) — it is served \
                 beside the artifacts now; scripts/fetch.sh 25.8 installs it, and \
                 $CHTYPES_GOLDENS overrides the path\n",
                path.display()
            ));
            return None;
        }
    };
    let doc: Json = serde_json::from_slice(&blob).expect("golden set parses");
    assert_eq!(doc["schema"], 1, "this test reads schema 1");
    assert!(
        !doc["cases"].as_array().unwrap().is_empty(),
        "golden set holds no cases"
    );
    Some(doc)
}

fn format_of(name: &str) -> Format {
    match name {
        "JSONEachRow" => Format::JsonEachRow,
        "CSV" => Format::Csv,
        "TSV" => Format::Tsv,
        "Values" => Format::Values,
        "JSONCompactEachRow" => Format::JsonCompactEachRow,
        other => panic!("unknown golden format {other}"),
    }
}

fn verdict_name(v: Verdict) -> &'static str {
    match v {
        Verdict::True => "true",
        Verdict::False => "false",
        Verdict::Error => "error",
        Verdict::Decline => "decline",
    }
}

fn str_map(v: &Json) -> BTreeMap<String, String> {
    v.as_object()
        .map(|m| {
            m.iter()
                .map(|(k, v)| (k.clone(), v.as_str().unwrap().to_string()))
                .collect()
        })
        .unwrap_or_default()
}
fn str_list(v: &Json) -> Vec<String> {
    v.as_array()
        .map(|a| a.iter().map(|s| s.as_str().unwrap().to_string()).collect())
        .unwrap_or_default()
}

/// Announce a skip on the *real* stderr: `eprintln!` is captured by libtest
/// for a passing test, so a skip printed with it is invisible — the one way a
/// suite that tested nothing looks exactly like one that passed.
fn announce(message: &str) {
    use std::io::Write;
    let _ = std::io::stderr().write_all(message.as_bytes());
    let _ = std::io::stderr().flush();
}

#[test]
fn goldens_hold_on_every_artifact() {
    let registry = match Registry::from_env_or_default() {
        Ok(r) if !r.libraries().is_empty() => r,
        Ok(r) => {
            announce(&format!(
                "\nSKIP goldens_hold_on_every_artifact: registry {} holds no artifact — fetch one \
                 with scripts/fetch.sh 25.8 (docs/guides/fetch.md)\n",
                r.dir().display()
            ));
            return;
        }
        Err(e) => {
            announce(&format!(
                "\nSKIP goldens_hold_on_every_artifact: no artifact registry ({e}) — fetch one \
                 with scripts/fetch.sh 25.8 (docs/guides/fetch.md), or set $CHTYPES_REGISTRY\n"
            ));
            return;
        }
    };
    let Some(doc) = goldens(registry.dir()) else {
        return;
    };
    let cases = doc["cases"].as_array().unwrap();
    let exact = &doc["generated"]["exact"];
    let mut checked = 0usize;
    let mut ran_on = 0usize;
    for lib in registry.libraries() {
        // A case is only a golden for the EXACT build it was generated against.
        // The rolling index keeps older patch rows, so a machine can hold a
        // patch the generator never saw; that is a loud skip, never a failure.
        match exact[lib.minor()].as_str() {
            None => {
                announce(&format!(
                    "\nSKIP goldens {}: the set was not generated on line {}\n",
                    lib.version(),
                    lib.minor()
                ));
                continue;
            }
            Some(want) if want != lib.version() => {
                announce(&format!(
                    "\nSKIP goldens {}: generated on ClickHouse {want} for line {} — fetch {} to \
                     run these cases\n",
                    lib.version(),
                    lib.minor(),
                    lib.minor()
                ));
                continue;
            }
            Some(_) => {}
        }
        ran_on += 1;
        for c in cases {
            let id = c["id"].as_str().unwrap();
            let ctx = format!("{}/{id}", lib.version());
            let expect = &c["expect"];
            let format = format_of(c["format"].as_str().unwrap());
            let body = c["body"].as_str().unwrap().as_bytes();
            let settings: Vec<(String, String)> = str_map(&c["settings"]).into_iter().collect();
            if let Some(code) = expect["compile_error_code"].as_i64() {
                match lib.compile(c["ddl"].as_str().unwrap()).compile() {
                    Err(Error::Schema { code: got, .. }) => {
                        assert_eq!(got as i64, code, "{ctx}: compile code")
                    }
                    other => panic!("{ctx}: want SchemaError {code}, got {other:?}"),
                }
                checked += 1;
                continue;
            }
            let schema = lib
                .compile(c["ddl"].as_str().unwrap())
                .compile()
                .unwrap_or_else(|e| panic!("{ctx}: {e}"));
            if let Some(filter_expr) = c["filter"].as_str() {
                let filter = schema
                    .compile_filter(filter_expr, NO_SETTINGS)
                    .unwrap_or_else(|e| panic!("{ctx}: {e}"));
                let fr = filter
                    .rows(format, body, &settings)
                    .unwrap_or_else(|e| panic!("{ctx}: {e}"));
                assert_eq!(fr.outcome, FilterOutcome::Ok, "{ctx}: filter outcome");
                assert_eq!(expect["outcome"], "ok", "{ctx}");
                let got: Vec<&str> = fr.verdicts.iter().map(|v| verdict_name(*v)).collect();
                assert_eq!(got, str_list(&expect["verdicts"]), "{ctx}: verdicts");
                checked += 1;
                continue;
            }
            let br = schema
                .rows(format, body, &settings)
                .unwrap_or_else(|e| panic!("{ctx}: {e}"));
            assert_eq!(
                br.outcome.as_str(),
                expect["outcome"].as_str().unwrap(),
                "{ctx}: batch outcome"
            );
            assert_eq!(
                br.err_code as i64,
                expect["err_code"].as_i64().unwrap_or(0),
                "{ctx}: batch code"
            );
            let want_rows = expect["rows"].as_array().cloned().unwrap_or_default();
            assert_eq!(br.rows.len(), want_rows.len(), "{ctx}: row count");
            for (i, (row, want)) in br.rows.iter().zip(want_rows.iter()).enumerate() {
                let want_outcome = want["outcome"].as_str().unwrap();
                assert_eq!(
                    row.outcome.as_str(),
                    want_outcome,
                    "{ctx}: row {i} outcome ({row:?})"
                );
                if want_outcome == "rejected" || want_outcome == "skipped" {
                    assert_eq!(
                        row.err_code as i64,
                        want["err_code"].as_i64().unwrap_or(0),
                        "{ctx}: row {i} code"
                    );
                    continue;
                }
                let values: BTreeMap<String, String> = row
                    .values
                    .iter()
                    .map(|v| {
                        (
                            v.column.clone(),
                            v.text.as_str().expect("utf-8 text").to_string(),
                        )
                    })
                    .collect();
                assert_eq!(values, str_map(&want["values"]), "{ctx}: row {i} values");
                let mut nulls: Vec<String> = row
                    .values
                    .iter()
                    .filter(|v| v.null)
                    .map(|v| v.column.clone())
                    .collect();
                nulls.sort();
                assert_eq!(nulls, str_list(&want["nulls"]), "{ctx}: row {i} nulls");
                let mut tr: Vec<String> = row
                    .transformed
                    .iter()
                    .map(|t| format!("{}:{}", t.column, t.reason))
                    .collect();
                tr.sort();
                let mut wtr: Vec<String> = want["transformed"]
                    .as_array()
                    .map(|a| {
                        a.iter()
                            .map(|t| {
                                format!(
                                    "{}:{}",
                                    t["column"].as_str().unwrap(),
                                    t["reason"].as_str().unwrap()
                                )
                            })
                            .collect()
                    })
                    .unwrap_or_default();
                wtr.sort();
                assert_eq!(tr, wtr, "{ctx}: row {i} transformed");
                let mut sub: Vec<String> =
                    row.substituted.iter().map(|s| s.column.clone()).collect();
                sub.sort();
                assert_eq!(
                    sub,
                    str_list(&want["substituted"]),
                    "{ctx}: row {i} substituted"
                );
                let comp: BTreeMap<String, String> = row
                    .computed
                    .iter()
                    .map(|k| {
                        (
                            k.column.clone(),
                            k.text.as_str().expect("utf-8 text").to_string(),
                        )
                    })
                    .collect();
                assert_eq!(comp, str_map(&want["computed"]), "{ctx}: row {i} computed");
            }
            checked += 1;
        }
    }
    assert_eq!(checked, cases.len() * ran_on);
    if ran_on == 0 {
        announce(
            "\nSKIP goldens_hold_on_every_artifact: no installed artifact is at the exact version \
             the golden set was generated on — fetch the lines named above\n",
        );
        return;
    }
    eprintln!("\n{checked} golden checks across {ran_on} artifact(s)\n");
}
