import type { Row } from '../conformance/types';

/**
 * What the data layer needs from DuckDB. Implemented over DuckDB-WASM in the
 * browser and over its Node build in tests.
 */
export interface DataEngine {
  /** Small results as plain row objects. */
  rows(sql: string): Promise<Row[]>;
  /** Large results as plain column arrays. */
  columns(sql: string): Promise<Record<string, unknown[]>>;
  /**
   * A reference to a collection file that DuckDB can read: the same ref, or
   * a downloaded copy when the host breaks Range reads (see remote.ts).
   * Without it, refs are read as they are.
   */
  readable?(ref: string): Promise<string>;
}
