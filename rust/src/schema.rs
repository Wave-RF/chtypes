//! [`Schema`], [`Filter`] and [`Block`], and the options each call takes.
//!
//! Every public call makes exactly one ABI call, plus the generated read-out of
//! its buffers and error. Nothing here contains a ClickHouse rule: no scalar,
//! comparison, coercion, timestamp, zone, quoting or classification logic.
//!
//! The three handle objects are `Clone + Send + Sync + 'static`: a clone shares
//! one handle, and the handle is freed when the last clone drops. `&self`
//! methods run concurrently with no lock; `Drop` cannot race a call, because a
//! call borrows the handle. A filter and a block each hold a counted reference
//! to their schema inside the library, so freeing order never matters.
//!
//! # The two zones
//!
//! The per-call zone is the `session_timezone` key of a call's `settings`; the
//! `session_timezone` option exists so the zone is visible in every signature
//! it affects, and writes its value into the settings object verbatim. Passing
//! the option and a `session_timezone` key together is [`Error::Usage`] whether
//! or not the two agree, so a call never has two spellings of its zone.
//!
//! A filter's zone is its own, fixed when it is compiled
//! ([`FilterOptions`]): its WHERE runs in that zone for every evaluation. An
//! evaluation's settings ([`EvalOptions`], [`RowOptions`] for a block) are the
//! body's INSERT settings: its parse and the expressions the INSERT evaluates,
//! never the WHERE. Differing zones are an ordinary input.

use std::sync::Arc;

use serde_json::{Map, Value as Json};

use crate::abi2::calls_gen::{BlockHandle, FilterHandle, SchemaHandle};
use crate::abi2::vocab_gen::{DocFlags, EXPORT_NONE, Format, Posture};
use crate::decode;
use crate::error::{Error, Result};
use crate::library::Library;
use crate::options::{
    ColumnDefault, DeferDefaults, InsertMembers, ParamEncoding, VerdictBasis, filter_options,
    name_object,
};
use crate::result::{BatchResult, FilterResult, RowResult, SchemaDescription};

/// The settings key a call's zone travels under.
const SESSION_TIMEZONE: &str = "session_timezone";

/// Serialize settings (and the per-call zone) into the JSON object of string
/// values that crosses the C boundary. Values are strings, only strings, and
/// are never rewritten. A zone given both as the option and as a settings key,
/// or a key given twice, is misuse, raised before any call.
pub(crate) fn settings_object(
    settings: &[(String, String)],
    session_timezone: Option<&str>,
) -> Result<Vec<u8>> {
    string_object(settings, session_timezone, "settings")
}

fn string_object(
    pairs: &[(String, String)],
    session_timezone: Option<&str>,
    what: &str,
) -> Result<Vec<u8>> {
    let mut map = Map::new();
    for (k, v) in pairs {
        if map.insert(k.clone(), Json::String(v.clone())).is_some() {
            return Err(Error::usage(format!(
                "{what}: the key {k:?} is given twice"
            )));
        }
    }
    if let Some(zone) = session_timezone {
        if map.contains_key(SESSION_TIMEZONE) {
            return Err(Error::usage(format!(
                "{SESSION_TIMEZONE} is given both as the option and as a key of {what}; a call carries exactly one spelling of its zone"
            )));
        }
        map.insert(SESSION_TIMEZONE.to_string(), Json::String(zone.to_string()));
    }
    serde_json::to_vec(&Json::Object(map))
        .map_err(|e| Error::internal(format!("{what} do not encode: {e}")))
}

/// The INSERT column list as the JSON array of name objects the ABI takes:
/// each name is `{"name": "<utf-8>"}`, or `{"name_b64": "<base64>"}` when its
/// bytes are not valid UTF-8. Empty means none.
fn columns_array(columns: &[Vec<u8>]) -> Result<Vec<u8>> {
    if columns.is_empty() {
        return Ok(Vec::new());
    }
    let items: Vec<Json> = columns.iter().map(|name| name_object(name)).collect();
    serde_json::to_vec(&Json::Array(items))
        .map_err(|e| Error::internal(format!("the column list does not encode: {e}")))
}

/// What a compile takes: the profile settings, the per-call zone and the
/// server.
///
/// In a compile profile the zone is a default for later calls on that schema;
/// a compiled type always takes the image zone, never the profile's.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct CompileOptions {
    /// The profile settings. Values are strings.
    pub settings: Vec<(String, String)>,
    /// The `session_timezone` key, written into the settings verbatim.
    pub session_timezone: Option<String>,
    /// The server to compile on (`chs_schema_create`'s `server`, ABI v2): the
    /// schema's home zone is the server's `timezone`, the server's `settings`
    /// layer under the schema's own, and a Replicated engine's ZooKeeper path
    /// and replica name expand its `macros`. `None` compiles on the image's own
    /// server, exactly as before the server profile existed. A server from
    /// another library is that library's own refusal ([`Error::Usage`]).
    pub server: Option<crate::server::Server>,
    /// `schema_options`' `posture` (ABI v2): how the statement is compiled,
    /// [`Posture::Create`] (the default) as a server's CREATE TABLE, or
    /// [`Posture::Load`] as a server loads the stored definition of a table it
    /// holds. `None` leaves the member out. The caller's obligation under
    /// `Load` is `spec/abi-v2/docs.md`'s (`input:schema_options`).
    pub posture: Option<Posture>,
    /// A `schema_options` member this binding has no name for: its key and its
    /// value as JSON text, passed through as given. The library refuses a key
    /// it does not know; text that is not valid JSON is [`Error::Usage`].
    pub schema_options_extra: Vec<(String, String)>,
}

/// Implement `insert_options_document` over a struct's six insert-option
/// fields, so the three structs that carry them build one document one way.
macro_rules! insert_options_document {
    ($t:ty) => {
        impl $t {
            fn insert_options_document(&self) -> Result<Vec<u8>> {
                InsertMembers {
                    insertable_columns: self.insertable_columns.as_deref(),
                    column_defaults: self.column_defaults.as_deref(),
                    verdict_basis: self.verdict_basis,
                    defer_defaults: self.defer_defaults,
                    extra: &self.insert_options_extra,
                }
                .document()
            }
        }
    };
}

/// What `row` and `parse_block` take.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RowOptions {
    /// The call's settings. Values are strings.
    pub settings: Vec<(String, String)>,
    /// The `session_timezone` key, written into the settings verbatim: the
    /// zone the body is parsed in.
    pub session_timezone: Option<String>,
    /// The INSERT column list, each name as bytes; empty for none.
    pub columns: Vec<Vec<u8>>,
    /// `insert_options`' `insertable_columns` (ABI v2): the columns an INSERT
    /// may supply (the server's column-level INSERT grant), checked on the
    /// INSERT's header before any byte of the body is read. `None` leaves the
    /// member out (every column); `Some` of an empty list is sent as `[]` (no
    /// column). Each name is bytes.
    pub insertable_columns: Option<Vec<Vec<u8>>>,
    /// `insert_options`' `column_defaults` (ABI v2): per-call DEFAULT
    /// overrides. `None` leaves the member out; `Some` of an empty list is sent
    /// as `[]`.
    pub column_defaults: Option<Vec<ColumnDefault>>,
    /// `insert_options`' `verdict_basis` (ABI v2).
    pub verdict_basis: Option<VerdictBasis>,
    /// `insert_options`' `defer_defaults` (ABI v2).
    pub defer_defaults: Option<DeferDefaults>,
    /// An `insert_options` member this binding has no name for: its key and
    /// its value as JSON text, passed through as given (rule r1). The library
    /// refuses a key it does not know; text that is not valid JSON is
    /// [`Error::Usage`]. A key set twice keeps the last value.
    pub insert_options_extra: Vec<(String, String)>,
}

insert_options_document!(RowOptions);

/// What `rows` takes.
#[derive(Debug, Clone, Default)]
pub struct RowsOptions {
    /// The call's settings. Values are strings.
    pub settings: Vec<(String, String)>,
    /// The `session_timezone` key, written into the settings verbatim.
    pub session_timezone: Option<String>,
    /// The INSERT column list, each name as bytes; empty for none.
    pub columns: Vec<Vec<u8>>,
    /// `insert_options`' `insertable_columns` (ABI v2): the columns an INSERT
    /// may supply (the server's column-level INSERT grant), checked on the
    /// INSERT's header before any byte of the body is read. `None` leaves the
    /// member out (every column); `Some` of an empty list is sent as `[]` (no
    /// column). Each name is bytes.
    pub insertable_columns: Option<Vec<Vec<u8>>>,
    /// `insert_options`' `column_defaults` (ABI v2): per-call DEFAULT
    /// overrides. `None` leaves the member out; `Some` of an empty list is sent
    /// as `[]`.
    pub column_defaults: Option<Vec<ColumnDefault>>,
    /// `insert_options`' `verdict_basis` (ABI v2).
    pub verdict_basis: Option<VerdictBasis>,
    /// `insert_options`' `defer_defaults` (ABI v2).
    pub defer_defaults: Option<DeferDefaults>,
    /// An `insert_options` member this binding has no name for: its key and
    /// its value as JSON text, passed through as given (rule r1). The library
    /// refuses a key it does not know; text that is not valid JSON is
    /// [`Error::Usage`]. A key set twice keeps the last value.
    pub insert_options_extra: Vec<(String, String)>,
    /// An attached filter. Its WHERE runs in the zone it was compiled under.
    pub filter: Option<Filter>,
    /// An export format; `None` asks for no export.
    pub export: Option<Format>,
    /// Which document groups to emit; `None` means 0, the lean result; pass `DocFlags::ALL` for every group.
    pub doc_flags: Option<DocFlags>,
}

insert_options_document!(RowsOptions);

/// What `compile_filter` takes.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct FilterOptions {
    /// The expression's query parameters, bound by ClickHouse's own
    /// substitution. Values are strings.
    pub params: Vec<(String, String)>,
    /// The profile the expression is compiled under.
    pub settings: Vec<(String, String)>,
    /// The `session_timezone` key: the filter's own zone, fixed at compile.
    pub session_timezone: Option<String>,
    /// `filter_options`' `param_encoding` (ABI v2): how a query parameter's
    /// value is read. `None` leaves the member out.
    pub param_encoding: Option<ParamEncoding>,
    /// A `filter_options` member this binding has no name for: its key and its
    /// value as JSON text, passed through as given (rule r1).
    pub filter_options_extra: Vec<(String, String)>,
}

/// What `Filter::rows` takes: the body's PARSE settings, never the WHERE's,
/// and the same INSERT the schema's `rows` models (`columns` and the
/// insert options, ABI v2).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct EvalOptions {
    /// The body's parse settings. Values are strings.
    pub settings: Vec<(String, String)>,
    /// The `session_timezone` key: the zone the body is parsed in.
    pub session_timezone: Option<String>,
    /// The INSERT column list, each name as bytes; empty for none.
    pub columns: Vec<Vec<u8>>,
    /// `insert_options`' `insertable_columns` (ABI v2): the columns an INSERT
    /// may supply (the server's column-level INSERT grant), checked on the
    /// INSERT's header before any byte of the body is read. `None` leaves the
    /// member out (every column); `Some` of an empty list is sent as `[]` (no
    /// column). Each name is bytes.
    pub insertable_columns: Option<Vec<Vec<u8>>>,
    /// `insert_options`' `column_defaults` (ABI v2): per-call DEFAULT
    /// overrides. `None` leaves the member out; `Some` of an empty list is sent
    /// as `[]`.
    pub column_defaults: Option<Vec<ColumnDefault>>,
    /// `insert_options`' `verdict_basis` (ABI v2).
    pub verdict_basis: Option<VerdictBasis>,
    /// `insert_options`' `defer_defaults` (ABI v2).
    pub defer_defaults: Option<DeferDefaults>,
    /// An `insert_options` member this binding has no name for: its key and
    /// its value as JSON text, passed through as given (rule r1). The library
    /// refuses a key it does not know; text that is not valid JSON is
    /// [`Error::Usage`]. A key set twice keeps the last value.
    pub insert_options_extra: Vec<(String, String)>,
}

insert_options_document!(EvalOptions);

/// A compiled table. Immutable; `Clone` shares one handle.
#[derive(Clone)]
pub struct Schema {
    library: Arc<Library>,
    handle: Arc<SchemaHandle>,
}

impl std::fmt::Debug for Schema {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Schema").finish_non_exhaustive()
    }
}

/// A compiled boolean expression over a schema's columns. `Clone` shares one
/// handle.
#[derive(Clone)]
pub struct Filter {
    library: Arc<Library>,
    handle: Arc<FilterHandle>,
}

impl std::fmt::Debug for Filter {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Filter").finish_non_exhaustive()
    }
}

/// A body parsed once, to be evaluated by many filters. `Clone` shares one
/// handle.
#[derive(Clone)]
pub struct Block {
    handle: Arc<BlockHandle>,
}

impl std::fmt::Debug for Block {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Block").finish_non_exhaustive()
    }
}

/// The document groups a `rows` call asks for: `None` is 0, the lean result,
/// never every group.
fn effective_doc_flags(options: &RowsOptions) -> DocFlags {
    options.doc_flags.unwrap_or(DocFlags::from_bits(0))
}

impl Schema {
    pub(crate) fn new(library: Arc<Library>, handle: SchemaHandle) -> Schema {
        Schema {
            library,
            handle: Arc::new(handle),
        }
    }

    /// Describe the columns (`chs_schema_describe`).
    pub fn describe(&self) -> Result<SchemaDescription> {
        let doc = self.library.call(|api| api.schema_describe(&self.handle))?;
        decode::schema_description(&doc)
    }

    /// Preview one row of `body` as a server's INSERT would take it
    /// (`chs_preview_row`).
    pub fn row(&self, format: Format, body: &[u8], options: &RowOptions) -> Result<RowResult> {
        let settings = settings_object(&options.settings, options.session_timezone.as_deref())?;
        let columns = columns_array(&options.columns)?;
        let insert = options.insert_options_document()?;
        let doc = self.library.call(|api| {
            api.preview_row(
                &self.handle,
                format.code(),
                body,
                &settings,
                &columns,
                &insert,
            )
        })?;
        decode::row(&doc)
    }

    /// Preview a whole body (`chs_preview_batch`), with an attached filter, an
    /// export and the document groups as the options say.
    pub fn rows(&self, format: Format, body: &[u8], options: &RowsOptions) -> Result<BatchResult> {
        let settings = settings_object(&options.settings, options.session_timezone.as_deref())?;
        let columns = columns_array(&options.columns)?;
        let insert = options.insert_options_document()?;
        let filter = options.filter.as_ref().map(|f| &*f.handle);
        let export = options.export.map_or(EXPORT_NONE, Format::code);
        let flags = effective_doc_flags(options).bits();
        let (doc, payload) = self.library.call(|api| {
            api.preview_batch(
                &self.handle,
                format.code(),
                body,
                &settings,
                &columns,
                filter,
                export,
                flags,
                &insert,
            )
        })?;
        decode::batch(&doc, payload)
    }

    /// Compile a boolean expression over this schema's columns
    /// (`chs_filter_create`). The filter's zone is fixed here.
    pub fn compile_filter(
        &self,
        expr: impl AsRef<[u8]>,
        options: &FilterOptions,
    ) -> Result<Filter> {
        let params = string_object(&options.params, None, "params")?;
        let settings = settings_object(&options.settings, options.session_timezone.as_deref())?;
        let document = filter_options(options.param_encoding, &options.filter_options_extra)?;
        let handle = self.library.call(|api| {
            api.filter_create(&self.handle, expr.as_ref(), &params, &settings, &document)
        })?;
        Ok(Filter {
            library: Arc::clone(&self.library),
            handle: Arc::new(handle),
        })
    }

    /// Parse a body once (`chs_block_create`), to evaluate many filters over it.
    pub fn parse_block(&self, format: Format, body: &[u8], options: &RowOptions) -> Result<Block> {
        let settings = settings_object(&options.settings, options.session_timezone.as_deref())?;
        let columns = columns_array(&options.columns)?;
        let insert = options.insert_options_document()?;
        let handle = self.library.call(|api| {
            api.block_create(
                &self.handle,
                format.code(),
                body,
                &settings,
                &columns,
                &insert,
            )
        })?;
        Ok(Block {
            handle: Arc::new(handle),
        })
    }
}

impl Filter {
    /// Evaluate over a body (`chs_filter_eval_body`). The options are the
    /// body's parse settings and the INSERT it models (`columns` and the insert
    /// options, as on [`Schema::rows`]); the WHERE runs in the zone the filter
    /// was compiled under.
    pub fn rows(&self, format: Format, body: &[u8], options: &EvalOptions) -> Result<FilterResult> {
        let settings = settings_object(&options.settings, options.session_timezone.as_deref())?;
        let columns = columns_array(&options.columns)?;
        let insert = options.insert_options_document()?;
        let doc = self.library.call(|api| {
            api.filter_eval_body(
                &self.handle,
                format.code(),
                body,
                &settings,
                &columns,
                &insert,
            )
        })?;
        decode::filter_result(&doc)
    }

    /// Evaluate over a parsed block (`chs_filter_eval_block`). It takes no
    /// settings: the filter brings its zone, and the block brought its parse
    /// zone when it was parsed. A block from another library is the library's
    /// to refuse ([`Error::Usage`]).
    pub fn eval(&self, block: &Block) -> Result<FilterResult> {
        let doc = self
            .library
            .call(|api| api.filter_eval_block(&self.handle, &block.handle))?;
        decode::filter_result(&doc)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pairs(p: &[(&str, &str)]) -> Vec<(String, String)> {
        p.iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    #[test]
    fn an_absent_doc_flags_is_the_lean_zero_and_an_explicit_all_is_kept() {
        // `rows` hands `effective_doc_flags(options).bits()` to the C call.
        assert_eq!(effective_doc_flags(&RowsOptions::default()).bits(), 0);
        let all = RowsOptions {
            doc_flags: Some(DocFlags::ALL),
            ..RowsOptions::default()
        };
        assert_eq!(effective_doc_flags(&all).bits(), DocFlags::ALL.bits());
        assert_eq!(DocFlags::ALL.bits(), 7);
    }

    #[test]
    fn settings_are_a_json_object_of_strings_never_rewritten() {
        let bytes = settings_object(
            &pairs(&[("input_format_allow_errors_num", "5"), ("flag", "true")]),
            None,
        )
        .unwrap();
        let v: Json = serde_json::from_slice(&bytes).unwrap();
        // A boolean-looking value stays the string the caller gave.
        assert_eq!(v["flag"], Json::String("true".into()));
        assert_eq!(v["input_format_allow_errors_num"], Json::String("5".into()));
        assert_eq!(settings_object(&[], None).unwrap(), b"{}");
    }

    #[test]
    fn the_zone_option_is_written_verbatim_as_the_settings_key() {
        let bytes = settings_object(&pairs(&[("a", "1")]), Some("Asia/Tokyo ")).unwrap();
        let v: Json = serde_json::from_slice(&bytes).unwrap();
        // Not trimmed, not validated, not canonicalized.
        assert_eq!(v["session_timezone"], Json::String("Asia/Tokyo ".into()));
        assert_eq!(v["a"], Json::String("1".into()));
    }

    #[test]
    fn the_zone_given_twice_is_misuse_even_when_the_two_agree() {
        for key_zone in ["UTC", "Asia/Tokyo"] {
            let err = settings_object(&pairs(&[("session_timezone", key_zone)]), Some("UTC"))
                .unwrap_err();
            assert!(matches!(err, Error::Usage(_)), "{err:?}");
        }
        // As a key alone, or as the option alone, it is accepted.
        assert!(settings_object(&pairs(&[("session_timezone", "UTC")]), None).is_ok());
        assert!(settings_object(&[], Some("UTC")).is_ok());
    }

    #[test]
    fn a_key_given_twice_is_misuse_never_silently_resolved() {
        let err = settings_object(&pairs(&[("a", "1"), ("a", "2")]), None).unwrap_err();
        assert!(matches!(err, Error::Usage(_)));
    }

    #[test]
    fn the_column_list_carries_each_name_as_name_or_name_b64() {
        assert!(columns_array(&[]).unwrap().is_empty());
        let bytes = columns_array(&[b"a\0b".to_vec(), vec![0x00, 0xff]]).unwrap();
        let v: Json = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v[0]["name"], Json::String("a\0b".into()));
        assert!(v[0].get("name_b64").is_none());
        assert_eq!(v[1]["name_b64"], Json::String("AP8=".into()));
        assert!(v[1].get("name").is_none());
    }

    #[test]
    fn the_four_insert_calls_build_one_insert_options_document() {
        let members = || {
            (
                Some(vec![b"a".to_vec()]),
                Some(vec![ColumnDefault {
                    name: b"b".to_vec(),
                    value: b"7".to_vec(),
                }]),
                Some(VerdictBasis::Export),
                Some(DeferDefaults::Server),
                vec![("x_future".to_string(), "{\"k\":[1]}".to_string())],
            )
        };
        let (ic, cd, vb, dd, ex) = members();
        let row = RowOptions {
            insertable_columns: ic,
            column_defaults: cd,
            verdict_basis: vb,
            defer_defaults: dd,
            insert_options_extra: ex,
            ..Default::default()
        };
        let (ic, cd, vb, dd, ex) = members();
        let rows = RowsOptions {
            insertable_columns: ic,
            column_defaults: cd,
            verdict_basis: vb,
            defer_defaults: dd,
            insert_options_extra: ex,
            ..Default::default()
        };
        let (ic, cd, vb, dd, ex) = members();
        let eval = EvalOptions {
            insertable_columns: ic,
            column_defaults: cd,
            verdict_basis: vb,
            defer_defaults: dd,
            insert_options_extra: ex,
            ..Default::default()
        };
        let want = row.insert_options_document().unwrap();
        assert_eq!(
            String::from_utf8(want.clone()).unwrap(),
            concat!(
                r#"{"column_defaults":[{"name":"b","value":"7"}],"defer_defaults":"server","#,
                r#""insertable_columns":[{"name":"a"}],"verdict_basis":"export","x_future":{"k":[1]}}"#
            )
        );
        assert_eq!(rows.insert_options_document().unwrap(), want);
        assert_eq!(eval.insert_options_document().unwrap(), want);
        // Nothing set sends nothing: the call before the document existed.
        assert!(
            RowOptions::default()
                .insert_options_document()
                .unwrap()
                .is_empty()
        );
        assert!(
            EvalOptions::default()
                .insert_options_document()
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn the_handles_are_clone_send_sync_and_static() {
        fn shareable<T: Clone + Send + Sync + 'static>() {}
        shareable::<Schema>();
        shareable::<Filter>();
        shareable::<Block>();
    }
}
