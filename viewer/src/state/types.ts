import type { CollectionMeta, DataSource, SpectrumFull, SpectrumRecord } from '../lib/data/types';
import type { LoadedSource } from '../lib/data/loader';
import type { SortSpec } from '../lib/data/search';
import type { SensorDefinition } from '../types/sensors';
import type { DownsampledResult } from '../lib/downsampling';
import type { ConformanceReport } from '../lib/conformance/types';

/** Top-level areas of the app. */
export type AppView = 'explore' | 'format';
/** Pages of the Format area. */
export type FormatPage = 'reference' | 'check';

/** Wavelength range (µm) treated as "no filter". */
export const DEFAULT_WAVELENGTH_RANGE: [number, number] = [0.2, 200];

/** Progress of opening a collection the user chose. */
export interface OpenState {
  running: boolean;
  label: string;
  phase: string;
  fraction: number | null;
  error: string | null;
  /** Lines shown after it finishes (e.g. warnings, what was added). */
  messages: string[];
}

export interface AppState {
  // Navigation
  view: AppView;
  formatPage: FormatPage;
  /** Section of the Format reference to show, e.g. 'geometry'. */
  formatSection: string | null;

  // Data: the OpenSpecLib release plus collections opened this session
  records: SpectrumRecord[];
  recordById: Map<string, SpectrumRecord>;
  sources: Map<string, DataSource>;
  collections: Map<string, CollectionMeta>;
  /** The OpenSpecLib release index is loaded. */
  indexLoaded: boolean;
  /** Any source is loaded (the release, or a collection the user opened). */
  dataLoaded: boolean;
  indexError: string | null;
  /** Bumped by RETRY_INDEX to re-run the index load. */
  indexRetryNonce: number;
  duckdbReady: boolean;
  duckdbError: string | null;
  openState: OpenState | null;
  /** Conformance reports from the last "Open a collection", for the checker. */
  openReports: ConformanceReport[];

  // Search/filter
  searchText: string;
  selectedCategories: string[];
  /** Collection ids (empty = all). */
  selectedSources: string[];
  selectedQuantity: string | null;
  selectedGeometries: string[];
  hideSimulated: boolean;
  wavelengthRange: [number, number]; // [min, max] in µm
  selectedSensor: SensorDefinition | null;
  sort: SortSpec | null;

  // Results
  searchResults: SpectrumRecord[];
  resultCount: number;
  pageOffset: number;
  pageLimit: number;
  searchLoading: boolean;

  // Library builder
  libraryIds: string[];
  librarySpectra: SpectrumFull[];
  libraryLoading: boolean;
  libraryError: string | null;
  /** Shown above the library, e.g. after migrating an old link. */
  libraryNotice: string | null;
  /** Bumped by RETRY_LIBRARY to re-run the spectra fetch effect. */
  libraryRetryNonce: number;

  // Downsampling
  downsamplingEnabled: boolean;
  downsampledData: Map<string, DownsampledResult[]>; // keyed by spectrum id
}

export type AppAction =
  | { type: 'SET_VIEW'; view: AppView; page?: FormatPage; section?: string | null }
  | { type: 'ADD_SOURCE'; loaded: LoadedSource }
  | { type: 'REMOVE_SOURCE'; key: string }
  | { type: 'SET_INDEX_ERROR'; error: string }
  | { type: 'RETRY_INDEX' }
  | { type: 'SET_DUCKDB_READY' }
  | { type: 'SET_DUCKDB_ERROR'; error: string }
  | { type: 'SET_OPEN_STATE'; open: OpenState | null }
  | { type: 'SET_OPEN_REPORTS'; reports: ConformanceReport[] }
  | { type: 'SET_SEARCH_TEXT'; text: string }
  | { type: 'SET_CATEGORIES'; categories: string[] }
  | { type: 'TOGGLE_CATEGORY'; category: string }
  | { type: 'SET_SOURCES'; sources: string[] }
  | { type: 'TOGGLE_SOURCE'; source: string }
  | { type: 'SET_QUANTITY'; quantity: string | null }
  | { type: 'TOGGLE_GEOMETRY'; geometry: string }
  | { type: 'SET_HIDE_SIMULATED'; hide: boolean }
  | { type: 'SET_WAVELENGTH_RANGE'; range: [number, number] }
  | { type: 'SET_SENSOR'; sensor: SensorDefinition | null }
  | { type: 'SET_SORT'; sort: SortSpec | null }
  | { type: 'CLEAR_FILTERS' }
  | { type: 'HYDRATE_FROM_URL'; state: Partial<AppState> }
  | { type: 'SET_SEARCH_RESULTS'; results: SpectrumRecord[]; total: number }
  | { type: 'SET_SEARCH_LOADING'; loading: boolean }
  | { type: 'SET_PAGE_OFFSET'; offset: number }
  | { type: 'ADD_TO_LIBRARY'; id: string }
  | { type: 'REMOVE_FROM_LIBRARY'; id: string }
  | { type: 'SET_LIBRARY_IDS'; ids: string[]; notice: string | null }
  | { type: 'CLEAR_LIBRARY' }
  | { type: 'SET_LIBRARY_SPECTRA'; spectra: SpectrumFull[] }
  | { type: 'SET_LIBRARY_LOADING'; loading: boolean }
  | { type: 'SET_LIBRARY_ERROR'; error: string }
  | { type: 'SET_LIBRARY_NOTICE'; notice: string | null }
  | { type: 'RETRY_LIBRARY' }
  | { type: 'SET_DOWNSAMPLING_ENABLED'; enabled: boolean }
  | { type: 'SET_DOWNSAMPLED_DATA'; data: Map<string, DownsampledResult[]> };

export const initialState: AppState = {
  view: 'explore',
  formatPage: 'reference',
  formatSection: null,
  records: [],
  recordById: new Map(),
  sources: new Map(),
  collections: new Map(),
  indexLoaded: false,
  dataLoaded: false,
  indexError: null,
  indexRetryNonce: 0,
  duckdbReady: false,
  duckdbError: null,
  openState: null,
  openReports: [],
  searchText: '',
  selectedCategories: [],
  selectedSources: [],
  selectedQuantity: null,
  selectedGeometries: [],
  hideSimulated: false,
  wavelengthRange: DEFAULT_WAVELENGTH_RANGE,
  selectedSensor: null,
  sort: null,
  searchResults: [],
  resultCount: 0,
  pageOffset: 0,
  pageLimit: 50,
  searchLoading: false,
  libraryIds: [],
  librarySpectra: [],
  libraryLoading: false,
  libraryError: null,
  libraryNotice: null,
  libraryRetryNonce: 0,
  downsamplingEnabled: true,
  downsampledData: new Map(),
};

function toggle(list: string[], value: string): string[] {
  return list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
}

export function appReducer(state: AppState, action: AppAction): AppState {
  switch (action.type) {
    case 'SET_VIEW':
      return {
        ...state,
        view: action.view,
        // The Format page is kept while exploring, so returning shows it again (e.g. a running check).
        formatPage: action.page ?? state.formatPage,
        formatSection: action.section !== undefined ? action.section : state.formatSection,
      };
    case 'ADD_SOURCE': {
      const { source, collections, records } = action.loaded;
      const sources = new Map(state.sources).set(source.key, source);
      const colls = new Map(state.collections);
      for (const c of collections) colls.set(c.id, c);
      const all = [...state.records, ...records];
      const recordById = new Map(state.recordById);
      for (const r of records) recordById.set(r.id, r);
      return {
        ...state,
        sources,
        collections: colls,
        records: all,
        recordById,
        indexLoaded: state.indexLoaded || source.kind === 'release',
        dataLoaded: true,
        indexError: source.kind === 'release' ? null : state.indexError,
      };
    }
    case 'REMOVE_SOURCE': {
      const source = state.sources.get(action.key);
      if (!source || source.kind === 'release') return state;
      const sources = new Map(state.sources);
      sources.delete(action.key);
      const colls = new Map(state.collections);
      for (const c of source.collections) colls.delete(c);
      const records = state.records.filter((r) => r.sourceKey !== action.key);
      const recordById = new Map(records.map((r) => [r.id, r]));
      const libraryIds = state.libraryIds.filter((id) => recordById.has(id));
      return {
        ...state,
        sources,
        collections: colls,
        records,
        recordById,
        libraryIds,
        librarySpectra: state.librarySpectra.filter((s) => recordById.has(s.id)),
        selectedSources: state.selectedSources.filter((c) => colls.has(c)),
        dataLoaded: sources.size > 0,
        pageOffset: 0,
        // The "Opened …" notice is stale once a source is removed.
        openState: state.openState?.running ? state.openState : null,
      };
    }
    case 'SET_INDEX_ERROR':
      return { ...state, indexError: action.error };
    case 'RETRY_INDEX':
      return { ...state, indexError: null, indexRetryNonce: state.indexRetryNonce + 1 };
    case 'SET_DUCKDB_READY':
      return { ...state, duckdbReady: true, duckdbError: null };
    case 'SET_DUCKDB_ERROR':
      return { ...state, duckdbError: action.error };
    case 'SET_OPEN_STATE':
      return { ...state, openState: action.open };
    case 'SET_OPEN_REPORTS':
      return { ...state, openReports: action.reports };
    case 'SET_SEARCH_TEXT':
      return { ...state, searchText: action.text, pageOffset: 0 };
    case 'SET_CATEGORIES':
      return { ...state, selectedCategories: action.categories, pageOffset: 0 };
    case 'TOGGLE_CATEGORY':
      return { ...state, selectedCategories: toggle(state.selectedCategories, action.category), pageOffset: 0 };
    case 'SET_SOURCES':
      return { ...state, selectedSources: action.sources, pageOffset: 0 };
    case 'TOGGLE_SOURCE':
      return { ...state, selectedSources: toggle(state.selectedSources, action.source), pageOffset: 0 };
    case 'SET_QUANTITY':
      return { ...state, selectedQuantity: action.quantity, pageOffset: 0 };
    case 'TOGGLE_GEOMETRY':
      return { ...state, selectedGeometries: toggle(state.selectedGeometries, action.geometry), pageOffset: 0 };
    case 'SET_HIDE_SIMULATED':
      return { ...state, hideSimulated: action.hide, pageOffset: 0 };
    case 'SET_WAVELENGTH_RANGE':
      return { ...state, wavelengthRange: action.range, pageOffset: 0 };
    case 'SET_SENSOR':
      return { ...state, selectedSensor: action.sensor, pageOffset: 0 };
    case 'SET_SORT':
      return { ...state, sort: action.sort, pageOffset: 0 };
    case 'CLEAR_FILTERS':
      return {
        ...state,
        searchText: '',
        selectedCategories: [],
        selectedSources: [],
        selectedQuantity: null,
        selectedGeometries: [],
        hideSimulated: false,
        wavelengthRange: DEFAULT_WAVELENGTH_RANGE,
        selectedSensor: null,
        pageOffset: 0,
      };
    case 'HYDRATE_FROM_URL':
      return { ...state, ...action.state, pageOffset: 0 };
    case 'SET_SEARCH_RESULTS':
      return { ...state, searchResults: action.results, resultCount: action.total, searchLoading: false };
    case 'SET_SEARCH_LOADING':
      return { ...state, searchLoading: action.loading };
    case 'SET_PAGE_OFFSET':
      return { ...state, pageOffset: action.offset };
    case 'ADD_TO_LIBRARY':
      if (state.libraryIds.includes(action.id)) return state;
      return { ...state, libraryIds: [...state.libraryIds, action.id] };
    case 'REMOVE_FROM_LIBRARY': {
      const downsampledData = new Map(state.downsampledData);
      downsampledData.delete(action.id);
      return {
        ...state,
        libraryIds: state.libraryIds.filter((id) => id !== action.id),
        librarySpectra: state.librarySpectra.filter((s) => s.id !== action.id),
        downsampledData,
      };
    }
    case 'SET_LIBRARY_IDS':
      return { ...state, libraryIds: action.ids, libraryNotice: action.notice };
    case 'CLEAR_LIBRARY':
      return { ...state, libraryIds: [], librarySpectra: [], downsampledData: new Map(), libraryNotice: null };
    case 'SET_LIBRARY_SPECTRA':
      return { ...state, librarySpectra: action.spectra, libraryLoading: false, libraryError: null };
    case 'SET_LIBRARY_LOADING':
      return { ...state, libraryLoading: action.loading };
    case 'SET_LIBRARY_ERROR':
      return { ...state, libraryError: action.error, libraryLoading: false };
    case 'SET_LIBRARY_NOTICE':
      return { ...state, libraryNotice: action.notice };
    case 'RETRY_LIBRARY':
      return { ...state, libraryError: null, libraryRetryNonce: state.libraryRetryNonce + 1 };
    case 'SET_DOWNSAMPLING_ENABLED':
      return { ...state, downsamplingEnabled: action.enabled };
    case 'SET_DOWNSAMPLED_DATA':
      return { ...state, downsampledData: action.data };
    default:
      return state;
  }
}
