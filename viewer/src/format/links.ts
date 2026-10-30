import { OPENSPECLIB_VERSION } from '../constants/urls';

export const GITHUB = 'https://github.com/null-jones/openspeclib';
/**
 * Spec documents at the release's tag, as footer.schema.json's `$id` is, so
 * links show the version this viewer implements rather than whatever main holds.
 */
const TAG = `v${OPENSPECLIB_VERSION}`;
export const SPEC_URL = `${GITHUB}/blob/${TAG}/docs/spec/format-v1.md`;
export const COLLECTIONS_URL = `${GITHUB}/blob/${TAG}/docs/spec/collections.md`;
export const SCHEMAS_URL = `${GITHUB}/tree/${TAG}/schemas/format-v1`;
export const DOCS_URL = `${GITHUB}/tree/${TAG}/docs`;
export const LICENSING_URL = `${GITHUB}/blob/${TAG}/docs/licensing.md`;
