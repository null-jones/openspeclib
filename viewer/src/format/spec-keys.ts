/** Names from the spec that need no JSON, for modules outside the Format area. */

/** Footer key that holds the collection metadata JSON. */
export const FOOTER_KEY = 'openspeclib';
/** Footer key that marks an index file. */
export const INDEX_FOOTER_KEY = 'openspeclib_index';

/** Per-band array columns, which index files leave out. */
export const ARRAY_COLUMNS = new Set(['wavelengths', 'values', 'bandpass_fwhm', 'values_stddev']);
