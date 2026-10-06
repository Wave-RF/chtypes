/**
 * parfix: a fixture for scripts/parity-surface.py — the TypeScript surface
 * tests/fixtures/parity-surface/doc.md describes, with its allowlisted
 * spellings (formatChName, verdictAnswered). Declarations only.
 */
export type BytesIn = Uint8Array | string;
export type Settings = Readonly<Record<string, string>>;
export interface SetupOptions {
  timezone?: string;
}
export declare function setup(options?: SetupOptions): void;
export interface Resolved {
  readonly libraryPath: string;
  readonly warnings: readonly string[];
}
export declare class Registry {
  private constructor();
  static open(): Promise<Registry>;
  for(request: string): Promise<Library>;
  installed(): Promise<readonly Resolved[]>;
}
export declare class Library {
  readonly version: string;
  validateType(typeExpr: BytesIn): Uint8Array;
  compileTable(createTable: BytesIn, options?: CompileOptions): Schema;
}
export interface CompileOptions {
  settings?: Settings;
}
declare class Handle {
  close(): void;
  [Symbol.dispose](): void;
}
export declare class Schema extends Handle {}
export declare const Format: {
  readonly JSONEachRow: 0;
  readonly CSV: 1;
};
export type Format = (typeof Format)[keyof typeof Format];
export declare function formatChName(format: number): string | undefined;
export declare const Verdict: {
  readonly True: "t";
  readonly False: "f";
};
export type Verdict = (typeof Verdict)[keyof typeof Verdict];
export declare function verdictAnswered(value: string): boolean | undefined;
export declare class ChtypesError extends Error {}
export declare class CallError extends ChtypesError {
  readonly chCode: number;
}
export declare class SchemaError extends CallError {}
export declare class ArtifactError extends ChtypesError {
  readonly reason: string;
  readonly path: string;
}
export declare class ArtifactIncompatibleError extends ArtifactError {}
export declare class ArtifactMissingError extends ArtifactError {}
export declare class ArtifactPinnedError extends ArtifactError {}
export interface Span {
  readonly off: number;
  readonly len: number;
}
export interface Header {
  readonly consumed: boolean;
  readonly lines: number;
}
export interface Framing {
  readonly bomSkipped: boolean | null;
  readonly header: Header | null;
}
export interface RowError {
  readonly row: number;
  readonly msg: Uint8Array;
}
export interface BatchResult {
  readonly rowsRead: bigint;
  readonly partitionId: Uint8Array | undefined;
  readonly columnsSql: Uint8Array;
  readonly spans: readonly Span[] | undefined;
  readonly framing: Framing | undefined;
  readonly errors: readonly RowError[];
}
export interface ErrorCodeEntry {
  readonly code: number;
  readonly name: string;
}
export declare class ErrorCodeTable {
  name(code: number): string | undefined;
  all(): readonly ErrorCodeEntry[];
}
