import type { CatalogRecord, SpectrumFull } from '../types/catalog';
import type { SensorDefinition } from '../types/sensors';
import type { DownsampledResult } from '../lib/downsampling';

export interface AppState {
  // Data loading
  catalogRecords: CatalogRecord[];
  catalogLoaded: boolean;
  catalogError: string | null;
  duckdbReady: boolean;
  duckdbError: string | null;

  // Search/filter
  searchText: string;
  selectedCategories: string[];
  selectedSources: string[]; // source library IDs (empty = all)
  selectedTechnique: string | null;
  wavelengthRange: [number, number]; // [min, max] in um
  selectedSensor: SensorDefinition | null;

  // Results
  searchResults: CatalogRecord[];
  resultCount: number;
  pageOffset: number;
  pageLimit: number;
  searchLoading: boolean;

  // Library builder
  libraryIds: string[];
  librarySpectra: SpectrumFull[];
  libraryLoading: boolean;

  // Downsampling
  downsamplingEnabled: boolean;
  downsampledData: Map<string, DownsampledResult[]>; // keyed by spectrum id
}

export type AppAction =
  | { type: 'SET_CATALOG'; records: CatalogRecord[] }
  | { type: 'SET_CATALOG_ERROR'; error: string }
  | { type: 'SET_DUCKDB_READY' }
  | { type: 'SET_DUCKDB_ERROR'; error: string }
  | { type: 'SET_SEARCH_TEXT'; text: string }
  | { type: 'SET_CATEGORIES'; categories: string[] }
  | { type: 'TOGGLE_CATEGORY'; category: string }
  | { type: 'SET_SOURCES'; sources: string[] }
  | { type: 'TOGGLE_SOURCE'; source: string }
  | { type: 'SET_TECHNIQUE'; technique: string | null }
  | { type: 'SET_WAVELENGTH_RANGE'; range: [number, number] }
  | { type: 'SET_SENSOR'; sensor: SensorDefinition | null }
  | { type: 'SET_SEARCH_RESULTS'; results: CatalogRecord[]; total: number }
  | { type: 'SET_SEARCH_LOADING'; loading: boolean }
  | { type: 'SET_PAGE_OFFSET'; offset: number }
  | { type: 'ADD_TO_LIBRARY'; id: string }
  | { type: 'REMOVE_FROM_LIBRARY'; id: string }
  | { type: 'CLEAR_LIBRARY' }
  | { type: 'SET_LIBRARY_SPECTRA'; spectra: SpectrumFull[] }
  | { type: 'SET_LIBRARY_LOADING'; loading: boolean }
  | { type: 'SET_DOWNSAMPLING_ENABLED'; enabled: boolean }
  | { type: 'SET_DOWNSAMPLED_DATA'; data: Map<string, DownsampledResult[]> };

export const initialState: AppState = {
  catalogRecords: [],
  catalogLoaded: false,
  catalogError: null,
  duckdbReady: false,
  duckdbError: null,
  searchText: '',
  selectedCategories: [],
  selectedSources: [],
  selectedTechnique: null,
  wavelengthRange: [0.2, 200],
  selectedSensor: null,
  searchResults: [],
  resultCount: 0,
  pageOffset: 0,
  pageLimit: 50,
  searchLoading: false,
  libraryIds: [],
  librarySpectra: [],
  libraryLoading: false,
  downsamplingEnabled: true,
  downsampledData: new Map(),
};

export function appReducer(state: AppState, action: AppAction): AppState {
  switch (action.type) {
    case 'SET_CATALOG':
      return { ...state, catalogRecords: action.records, catalogLoaded: true, catalogError: null };
    case 'SET_CATALOG_ERROR':
      return { ...state, catalogError: action.error };
    case 'SET_DUCKDB_READY':
      return { ...state, duckdbReady: true, duckdbError: null };
    case 'SET_DUCKDB_ERROR':
      return { ...state, duckdbError: action.error };
    case 'SET_SEARCH_TEXT':
      return { ...state, searchText: action.text, pageOffset: 0 };
    case 'SET_CATEGORIES':
      return { ...state, selectedCategories: action.categories, pageOffset: 0 };
    case 'TOGGLE_CATEGORY': {
      const cats = state.selectedCategories.includes(action.category)
        ? state.selectedCategories.filter((c) => c !== action.category)
        : [...state.selectedCategories, action.category];
      return { ...state, selectedCategories: cats, pageOffset: 0 };
    }
    case 'SET_SOURCES':
      return { ...state, selectedSources: action.sources, pageOffset: 0 };
    case 'TOGGLE_SOURCE': {
      const srcs = state.selectedSources.includes(action.source)
        ? state.selectedSources.filter((s) => s !== action.source)
        : [...state.selectedSources, action.source];
      return { ...state, selectedSources: srcs, pageOffset: 0 };
    }
    case 'SET_TECHNIQUE':
      return { ...state, selectedTechnique: action.technique, pageOffset: 0 };
    case 'SET_WAVELENGTH_RANGE':
      return { ...state, wavelengthRange: action.range, pageOffset: 0 };
    case 'SET_SENSOR':
      return { ...state, selectedSensor: action.sensor, pageOffset: 0 };
    case 'SET_SEARCH_RESULTS':
      return { ...state, searchResults: action.results, resultCount: action.total, searchLoading: false };
    case 'SET_SEARCH_LOADING':
      return { ...state, searchLoading: action.loading };
    case 'SET_PAGE_OFFSET':
      return { ...state, pageOffset: action.offset };
    case 'ADD_TO_LIBRARY':
      if (state.libraryIds.includes(action.id)) return state;
      return { ...state, libraryIds: [...state.libraryIds, action.id] };
    case 'REMOVE_FROM_LIBRARY':
      return {
        ...state,
        libraryIds: state.libraryIds.filter((id) => id !== action.id),
        librarySpectra: state.librarySpectra.filter((s) => s.id !== action.id),
        downsampledData: (() => {
          const m = new Map(state.downsampledData);
          m.delete(action.id);
          return m;
        })(),
      };
    case 'CLEAR_LIBRARY':
      return { ...state, libraryIds: [], librarySpectra: [], downsampledData: new Map() };
    case 'SET_LIBRARY_SPECTRA':
      return { ...state, librarySpectra: action.spectra, libraryLoading: false };
    case 'SET_LIBRARY_LOADING':
      return { ...state, libraryLoading: action.loading };
    case 'SET_DOWNSAMPLING_ENABLED':
      return { ...state, downsamplingEnabled: action.enabled };
    case 'SET_DOWNSAMPLED_DATA':
      return { ...state, downsampledData: action.data };
    default:
      return state;
  }
}
