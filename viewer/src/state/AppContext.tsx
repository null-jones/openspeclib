import {
  createContext,
  useContext,
  useReducer,
  useEffect,
  useCallback,
  useRef,
  type ReactNode,
  type Dispatch,
} from 'react';
import { appReducer, initialState, type AppState, type AppAction } from './types';
import { fetchCatalog, searchCatalog, type SearchFilters } from '../lib/catalog';
import { initDuckDB } from '../lib/duckdb';
import { fetchSpectraByIds } from '../lib/queries';
import { downsampleSpectrum } from '../lib/downsampling';
import { toMicrometersWithValues } from '../lib/wavelength-utils';
import type { DownsampledResult } from '../lib/downsampling';

interface AppContextValue {
  state: AppState;
  dispatch: Dispatch<AppAction>;
}

const AppContext = createContext<AppContextValue | null>(null);

export function useAppContext(): AppContextValue {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error('useAppContext must be inside AppProvider');
  return ctx;
}

export function AppProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(appReducer, initialState);
  const searchTimerRef = useRef<ReturnType<typeof setTimeout>>(null);

  // Load catalog on mount
  useEffect(() => {
    fetchCatalog()
      .then((catalog) => dispatch({ type: 'SET_CATALOG', records: catalog.spectra }))
      .catch((e) => dispatch({ type: 'SET_CATALOG_ERROR', error: String(e) }));
  }, []);

  // Init DuckDB on mount (background)
  useEffect(() => {
    initDuckDB()
      .then(() => dispatch({ type: 'SET_DUCKDB_READY' }))
      .catch((e) => dispatch({ type: 'SET_DUCKDB_ERROR', error: String(e) }));
  }, []);

  // Debounced search when filters change
  const runSearch = useCallback(() => {
    if (!state.catalogLoaded) return;

    dispatch({ type: 'SET_SEARCH_LOADING', loading: true });

    const filters: SearchFilters = {
      text: state.searchText,
      categories: state.selectedCategories,
      technique: state.selectedTechnique,
      wavelengthMin: state.wavelengthRange[0],
      wavelengthMax: state.wavelengthRange[1],
      sensorWavelengthMin: state.selectedSensor?.wavelengthMin ?? null,
      sensorWavelengthMax: state.selectedSensor?.wavelengthMax ?? null,
    };

    // Use in-memory catalog search (fast enough for ~2500 records)
    const { results, total } = searchCatalog(
      state.catalogRecords,
      filters,
      state.pageOffset,
      state.pageLimit,
    );
    dispatch({ type: 'SET_SEARCH_RESULTS', results, total });
  }, [
    state.catalogLoaded,
    state.catalogRecords,
    state.searchText,
    state.selectedCategories,
    state.selectedTechnique,
    state.wavelengthRange,
    state.selectedSensor,
    state.pageOffset,
    state.pageLimit,
  ]);

  useEffect(() => {
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    searchTimerRef.current = setTimeout(runSearch, 150);
    return () => {
      if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    };
  }, [runSearch]);

  // Fetch spectral arrays when library IDs change
  useEffect(() => {
    if (state.libraryIds.length === 0) return;
    if (!state.duckdbReady) return;

    dispatch({ type: 'SET_LIBRARY_LOADING', loading: true });
    fetchSpectraByIds(state.libraryIds)
      .then((spectra) => {
        dispatch({ type: 'SET_LIBRARY_SPECTRA', spectra });
      })
      .catch((err) => {
        console.error('Failed to fetch spectral data:', err);
        dispatch({ type: 'SET_LIBRARY_LOADING', loading: false });
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.libraryIds.join(','), state.duckdbReady]);

  // Compute downsampled data when library spectra or sensor change
  useEffect(() => {
    if (!state.selectedSensor || !state.downsamplingEnabled) {
      dispatch({ type: 'SET_DOWNSAMPLED_DATA', data: new Map() });
      return;
    }

    const data = new Map<string, DownsampledResult[]>();
    for (const spectrum of state.librarySpectra) {
      const { wavelengths: wl, values: val } = toMicrometersWithValues(
        spectrum.wavelengths,
        spectrum.values,
        spectrum.wavelength_unit,
      );
      // Filter out no-data sentinel values before downsampling
      const validIdx = val.map((v, i) => (v > -1e10 && v < 1e10 ? i : -1)).filter((i) => i >= 0);
      const wavelengths = validIdx.map((i) => wl[i]);
      const values = validIdx.map((i) => val[i]);
      const result = downsampleSpectrum(wavelengths, values, state.selectedSensor.bands);
      data.set(spectrum.id, result);
    }
    dispatch({ type: 'SET_DOWNSAMPLED_DATA', data });
  }, [state.librarySpectra, state.selectedSensor, state.downsamplingEnabled]);

  return (
    <AppContext.Provider value={{ state, dispatch }}>
      {children}
    </AppContext.Provider>
  );
}
