# Fixture: a miniature bindings-v1.md

Test data for `scripts/parity-surface.py --selftest`, not documentation. It keeps the real doc's section numbers, headings and table shapes, a few rows each, and describes the four `parfix` packages beside it. Every shape the gate's doc parser reads appears here at least once.

## 1. Scope and principles

2. **Four bindings, one answer.** Spelled in each language's idiom: Go `PascalCase` (with Go's initialisms: `ID`, `SQL`), Python `snake_case`, TypeScript `camelCase`, Rust `snake_case`.

## 2. The operations

### Setting up the process

| operation | C entry point | Go | Python | TypeScript | Rust | status |
| --- | --- | --- | --- | --- | --- | --- |
| set the image zone | none | `chtypes.Setup(opts SetupOptions) error`, `SetupOptions{Timezone string}` | `chtypes.setup(*, timezone: Optional[str] = None) -> None` | `setup(options?: SetupOptions): void`, `SetupOptions { timezone? }` | `chtypes::setup(options: SetupOptions) -> Result<()>`, `SetupOptions { timezone: Option<String> }` | FIRM |

### Opening a library

| operation | C entry point | Go | Python | TypeScript | Rust | status |
| --- | --- | --- | --- | --- | --- | --- |
| construct a registry | none | `chtypes.NewRegistry() (*Registry, error)` | `chtypes.Registry(*, autofetch: Optional[bool] = None)` | `await Registry.open(): Promise<Registry>` | `Registry::new() -> Result<Registry>` | FIRM |
| open a version | the load steps | `(r *Registry) For(request string) (*Library, error)` | `Registry.for_version(request: str) -> Library` | `registry.for(request: string): Promise<Library>` | `Registry::for_version(&self, request: &str) -> Result<Arc<Library>>` | FIRM |
| what is installed | none | `(r *Registry) Installed() ([]Resolved, error)` | `Registry.installed() -> tuple[Resolved, ...]` | `registry.installed(): Promise<readonly Resolved[]>` | `Registry::installed(&self) -> Result<Vec<Resolved>>` | FIRM |
| open the linked image (Go) | the linked filler | `chtypes.OpenLinked() (*Library, error)`, under `-tags chtypes_linked` | none | none | none | FIRM |

### The library

| operation | C entry point | Go | Python | TypeScript | Rust | status |
| --- | --- | --- | --- | --- | --- | --- |
| its version | a `build_info` field | fields `l.Version` | `.version` | `.version` | `version(&self) -> &str` | FIRM |
| canonicalize a type | `chs_type_validate` | `ValidateType(typeExpr string) (string, error)` | `validate_type(type_expr: BytesIn) -> bytes` | `validateType(typeExpr: BytesIn): Buffer` | `validate_type(&self, type_expr: impl AsRef<[u8]>) -> Result<RawText>` | FIRM |
| compile a table | `chs_schema_create` | `CompileTable(createTable string, opts ...CompileOption) (*Schema, error)` | `compile_table(create_table: BytesIn, *, settings: Optional[Settings] = None) -> Schema` | `compileTable(createTable: BytesIn, options?: CompileOptions): Schema` | `compile_table(self: &Arc<Self>, create_table: impl AsRef<[u8]>, options: &CompileOptions) -> Result<Schema>` | FIRM |

### A schema

| operation | C entry point | Go | Python | TypeScript | Rust | status |
| --- | --- | --- | --- | --- | --- | --- |
| release | `chs_schema_free` | `(s *Schema) Close() error`, plus a finalizer | `Schema.close()`; a context manager; a finalizer | `schema.close()`; `[Symbol.dispose]()`; a finalizer | `Drop` | FIRM (D2) |

### The call options

| option | C parameter | applies to | Go | Python | TypeScript | Rust |
| --- | --- | --- | --- | --- | --- | --- |
| settings | `settings` | compile | `WithSettings(map[string]string)`: a `CompileOption` | `settings=` | `settings` | `settings: Vec<(String, String)>` |

The TypeScript option types are `CompileOptions { settings? }`. The Rust ones are `CompileOptions { settings }`, each with public fields and `Default`.

## 3. Types, handles and threads

### Per-language types

| abstract type | Go | Python | TypeScript | Rust |
| --- | --- | --- | --- | --- |
| bytes in | `string` | `BytesIn = Union[bytes, str]` | `BytesIn = Uint8Array \| string` | `impl AsRef<[u8]>` |
| bytes out | `string` | `bytes` | `Buffer` | `RawText` |
| settings | `map[string]string` | `Settings = Mapping[str, str]` | `Settings = Readonly<Record<string, string>>` | `Vec<(String, String)>` |

### The objects, their lifetimes and how each one closes

| object | C handle | Go | Python | TypeScript | Rust |
| --- | --- | --- | --- | --- | --- |
| `Registry` | none | `*Registry`; no `Close` | `Registry`; no `close`, not a context manager | `Registry`; no `close`, no `Symbol.dispose` | `Registry` |
| `Library` | none | `*Library`; no `Close` | `Library`; no `close` | `Library`; no `close` | `Arc<Library>`; never unloaded |
| `Schema` | `chs_schema` | `*Schema`; `Close() error`; a finalizer | `Schema`; `close()`; a context manager | `Schema`; `close()`; `[Symbol.dispose]()` | `Schema`; `Drop`; `Clone` shares one handle |

### The generated vocabularies

| vocabulary | from | Go | Python | TypeScript | Rust | notes |
| --- | --- | --- | --- | --- | --- | --- |
| formats | `chs_format` | `Format` (`int32`): `JSONEachRow`, … | `Format` (`IntEnum`) | `Format` | `Format` (`#[repr(i32)]`) | Each carries `ch_name`, ClickHouse's own name |
| filter verdicts | `filter_verdict` | `Verdict`: `VerdictTrue`, `VerdictFalse`, and `Answered()` | `Verdict`, `.answered` | `Verdict`, `answered` | `Verdict`, `answered()` | FIRM |

## 4. Errors

### The classes

| error | raised for | Go | Python | TypeScript | Rust | new in v1 |
| --- | --- | --- | --- | --- | --- | --- |
| refusal | `CHS_REJECTED` | `*SchemaError` | `SchemaError` | `SchemaError` | `Error::Schema(CallError)` | no |
| artifact incompatible | the loader | `*ArtifactError` with `Code` `CHTYPES_ARTIFACT_INCOMPATIBLE`; `errors.Is(err, ErrArtifactIncompatible)` | `ArtifactIncompatibleError` | `ArtifactIncompatibleError` | `Error::ArtifactIncompatible(Refusal)` | yes |
| the other fetch errors | the fetch layer: `CHTYPES_ARTIFACT_MISSING`, `_PINNED` | `*ArtifactError` with that `Code`, and a sentinel per code | one class per code | one class per code | one variant per code | no |

### The fields

| field | meaning | Go | Python | TypeScript | Rust |
| --- | --- | --- | --- | --- | --- |
| `ch_code` | ClickHouse's own code | `ChCode int32` | `ch_code: int` | `chCode: number` | `ch_code: i32` |

- **Go** embeds one `CallError` struct in `SchemaError`, so the fields read directly; `chtypes.AsCallError(err) (*CallError, bool)` reads them.
- **Python and TypeScript** give the classes one base, `CallError`, under the existing `ChtypesError`.
- **Rust** carries a `CallError` struct in each variant, and keeps `Error::is_unsupported()`.
- **A loader refusal** carries `reason` and `path`. In Rust they are the `Refusal` struct.

## 5. Documents and decoders

### `BatchResult`, from the `batch` document

| field | from | type |
| --- | --- | --- |
| `rows_read` | `rows_read` | unsigned 64-bit |
| `partition_id` | `partition_id` (rule 5) | optional bytes |
| `columns_sql` | `columns_sql` (rule 5) | bytes |
| `spans` | `row_spans`: each exported row's place in `payload` | optional list of `Span` |
| `framing` | `framing`: what the reader decided | optional `Framing` |
| `errors` | `errors`: `row` (int), `msg` (bytes, from `err`, rule 5) | list of `RowError` |

| type | fields |
| --- | --- |
| `Span` | `off`, `len` (unsigned 64-bit) |

`Framing` is `bom_skipped` and `header`, each the reader's own state:

- `header` is a `Header` (`consumed` (bool), `lines` (int)), or **unknown**.

### `ErrorCodeTable`, from the `error_code_table` document

The decoded type keeps v0's surface: `name(code)` and `all()` (Go `Name`, `All`; Rust `name()` and `iter()`, plus `IntoIterator`), with `ErrorCodeEntry { code, name }`.

## 6. Setup

`Resolved` is the fetch layer's record, re-exported unchanged.

A `Resolved` carries: `library_path` (absolute) and `warnings[]`.
