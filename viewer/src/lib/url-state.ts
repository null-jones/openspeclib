import type { AppState } from '../state/types';
import { DEFAULT_WAVELENGTH_RANGE } from '../state/types';
import { ALL_SENSORS } from '../constants/sensors';
import { GEOMETRIES, QUANTITIES } from '../format/vocab';

/** The slice of app state that round-trips through the URL hash. */
export type ShareableState = Pick<
  AppState,
  | 'view'
  | 'formatPage'
  | 'formatSection'
  | 'searchText'
  | 'selectedCategories'
  | 'selectedSources'
  | 'selectedQuantity'
  | 'selectedGeometries'
  | 'hideSimulated'
  | 'wavelengthRange'
  | 'selectedSensor'
  | 'downsamplingEnabled'
  | 'libraryIds'
>;

/**
 * Shareable state in the URL hash: the active area, filters, sensor, and
 * library selection.
 * `#q=quartz&cat=mineral,rock&src=usgs_splib07&qty=reflectance&geo=biconical&nosim=1&wl=0.4-2.5&sensor=sentinel2&lib=id1,id2&ds=0`
 * Links from before Format v1 used `tech=` for the quantity; it still decodes.
 * The Format area adds `view=format`, plus `page=check` for the checker or
 * `sec=<section>` for a reference section. No `view` means the explorer, so
 * links made before the Format area existed open as they always did.
 *
 * The hash (not the query string) keeps GitHub Pages routing untouched and
 * avoids resending state to the server on load.
 */
export function encodeStateToHash(state: ShareableState): string {
  const params = new URLSearchParams();
  if (state.view === 'format') {
    params.set('view', 'format');
    if (state.formatPage === 'check') params.set('page', 'check');
    else if (state.formatSection) params.set('sec', state.formatSection);
  }
  if (state.searchText) params.set('q', state.searchText);
  if (state.selectedCategories.length > 0) params.set('cat', state.selectedCategories.join(','));
  if (state.selectedSources.length > 0) params.set('src', state.selectedSources.join(','));
  if (state.selectedQuantity) params.set('qty', state.selectedQuantity);
  if (state.selectedGeometries.length > 0) params.set('geo', state.selectedGeometries.join(','));
  if (state.hideSimulated) params.set('nosim', '1');
  const [lo, hi] = state.wavelengthRange;
  if (lo !== DEFAULT_WAVELENGTH_RANGE[0] || hi !== DEFAULT_WAVELENGTH_RANGE[1]) {
    params.set('wl', `${lo}-${hi}`);
  }
  if (state.selectedSensor) params.set('sensor', state.selectedSensor.id);
  if (!state.downsamplingEnabled) params.set('ds', '0');
  if (state.libraryIds.length > 0) {
    // Ids never contain commas; URLSearchParams percent-encodes the rest.
    params.set('lib', state.libraryIds.join(','));
  }
  const s = params.toString();
  return s ? `#${s}` : '';
}

/** Parse the URL hash back into a partial AppState (invalid parts ignored). */
export function decodeHashToState(hash: string): Partial<AppState> {
  const out: Partial<AppState> = {};
  const raw = hash.startsWith('#') ? hash.slice(1) : hash;
  if (!raw) return out;
  let params: URLSearchParams;
  try {
    params = new URLSearchParams(raw);
  } catch {
    return out;
  }

  if (params.get('view') === 'format') {
    out.view = 'format';
    out.formatPage = params.get('page') === 'check' ? 'check' : 'reference';
    const sec = params.get('sec');
    if (sec && /^[a-z0-9-]{1,40}$/.test(sec)) out.formatSection = sec;
  }

  const q = params.get('q');
  if (q) out.searchText = q;

  const cat = params.get('cat');
  if (cat) out.selectedCategories = cat.split(',').filter(Boolean);

  const src = params.get('src');
  if (src) out.selectedSources = src.split(',').filter(Boolean);

  const qty = params.get('qty') ?? params.get('tech');
  if (qty && qty in QUANTITIES) out.selectedQuantity = qty;

  const geo = params.get('geo');
  if (geo) {
    const values = geo.split(',').filter((g) => g in GEOMETRIES);
    if (values.length) out.selectedGeometries = values;
  }

  if (params.get('nosim') === '1') out.hideSimulated = true;

  const wl = params.get('wl');
  if (wl) {
    const m = wl.match(/^([\d.]+)-([\d.]+)$/);
    if (m) {
      const lo = parseFloat(m[1]);
      const hi = parseFloat(m[2]);
      if (isFinite(lo) && isFinite(hi) && lo < hi) out.wavelengthRange = [lo, hi];
    }
  }

  const sensorId = params.get('sensor');
  if (sensorId) {
    const sensor = ALL_SENSORS.find((s) => s.id === sensorId);
    if (sensor) out.selectedSensor = sensor;
  }

  if (params.get('ds') === '0') out.downsamplingEnabled = false;

  const lib = params.get('lib');
  if (lib) {
    // URLSearchParams has already decoded the value; ids legitimately
    // contain '%' (e.g. USGS "NA56%"), so never decode a second time.
    out.libraryIds = lib.split(',').filter(Boolean);
  }

  return out;
}

/** Replace the current URL hash without adding a history entry. */
export function writeHash(hash: string): void {
  const url = window.location.pathname + window.location.search + hash;
  window.history.replaceState(null, '', url);
}
