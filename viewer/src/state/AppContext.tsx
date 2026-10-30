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
import { appReducer, initialState, DEFAULT_WAVELENGTH_RANGE, type AppState, type AppAction } from './types';
import { initDuckDB } from '../lib/duckdb';
import { getDataEngine, loadMigrationMap, loadRelease } from '../lib/data/browser';
import { fetchSpectra } from '../lib/data/spectra';
import { searchRecords, type SearchFilters } from '../lib/data/search';
import { migrateIds } from '../lib/data/migration';
import { friendlyError } from '../lib/data/errors';
import { downsampleSpectrum } from '../lib/downsampling';
import { toMicrometersWithValues } from '../lib/wavelength-utils';
import { decodeHashToState, encodeStateToHash, writeHash } from '../lib/url-state';
import type { CheckInput } from '../lib/conformance/browser';
import type { DownsampledResult } from '../lib/downsampling';
import type { SpectrumFull } from '../lib/data/types';

export interface OpenRequest {
  input: CheckInput;
  /** Run the conformance check first (false when the checker already did). */
  check: boolean;
  qualityProfile?: boolean;
}

interface AppContextValue {
  state: AppState;
  dispatch: Dispatch<AppAction>;
  /** Open local files or a URL for this session. Resolves to true if anything opened. */
  openCollection: (req: OpenRequest) => Promise<boolean>;
  cancelOpen: () => void;
  /** Remove an opened collection and release what it holds in DuckDB. */
  removeSource: (key: string) => void;
}

const AppContext = createContext<AppContextValue | null>(null);

// eslint-disable-next-line react-refresh/only-export-components -- context hook lives with its provider
export function useAppContext(): AppContextValue {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error('useAppContext must be inside AppProvider');
  return ctx;
}

export function AppProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(appReducer, initialState, (base) => ({
    ...base,
    // Hydrate filters/sensor/library from a shared link before first render.
    ...decodeHashToState(window.location.hash),
  }));
  const searchTimerRef = useRef<ReturnType<typeof setTimeout>>(null);
  // Spectral arrays already fetched this session, so edits to the library
  // selection only fetch ids not seen before (each fetch costs real network:
  // ~1 row group per id).
  const spectraCacheRef = useRef(new Map<string, SpectrumFull>());
  const stateRef = useRef(state);
  stateRef.current = state;
  const openAbortRef = useRef<AbortController | null>(null);
  const migratedRef = useRef(false);

  // Start DuckDB-WASM.
  useEffect(() => {
    initDuckDB()
      .then(() => dispatch({ type: 'SET_DUCKDB_READY' }))
      .catch((e) => dispatch({ type: 'SET_DUCKDB_ERROR', error: String(e) }));
  }, []);

  // Load the release index (and again on RETRY_INDEX).
  useEffect(() => {
    let cancelled = false;
    loadRelease()
      .then((loaded) => {
        if (!cancelled) dispatch({ type: 'ADD_SOURCE', loaded });
      })
      .catch((e) => {
        if (!cancelled) dispatch({ type: 'SET_INDEX_ERROR', error: e instanceof Error ? e.message : String(e) });
      });
    return () => {
      cancelled = true;
    };
  }, [state.indexRetryNonce]);

  // Follow hash changes made outside the app (edited URL, a pasted link).
  // The app's own updates use replaceState, which does not fire this.
  useEffect(() => {
    const onHash = () => {
      const { view, formatPage, formatSection, searchText, selectedCategories, selectedSources, selectedQuantity, selectedGeometries, hideSimulated, wavelengthRange, selectedSensor, downsamplingEnabled, libraryIds } = initialState;
      const defaults = { view, formatPage, formatSection, searchText, selectedCategories, selectedSources, selectedQuantity, selectedGeometries, hideSimulated, wavelengthRange, selectedSensor, downsamplingEnabled, libraryIds };
      dispatch({ type: 'HYDRATE_FROM_URL', state: { ...defaults, ...decodeHashToState(window.location.hash) } });
      // A notice about the previous link's ids no longer applies.
      dispatch({ type: 'SET_LIBRARY_NOTICE', notice: null });
      migratedRef.current = false;
    };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  // Old shared links: once the index is in, map ids it does not know
  // through id-migration.csv (fetched only if needed).
  const hashLibraryKey = state.libraryIds.join('\0');
  useEffect(() => {
    if (!state.indexLoaded || migratedRef.current) return;
    migratedRef.current = true;
    const { libraryIds, recordById, selectedSources, collections } = stateRef.current;
    // A link may name collections that were opened from files in another
    // session; filtering by them would hide everything.
    if (selectedSources.some((c) => !collections.has(c))) {
      dispatch({ type: 'SET_SOURCES', sources: selectedSources.filter((c) => collections.has(c)) });
    }
    const known = (id: string) => recordById.has(id);
    if (libraryIds.every(known)) return;
    loadMigrationMap()
      .catch(() => null)
      .then((map) => {
        const res = migrateIds(stateRef.current.libraryIds, (id) => stateRef.current.recordById.has(id), map);
        const parts: string[] = [];
        if (res.migrated) {
          parts.push(
            res.migrated === 1
              ? 'One spectrum in this link had an id from an older release; it now uses its current id.'
              : `${res.migrated} spectra in this link had ids from an older release; they now use their current ids.`,
          );
        }
        if (res.missing.length) {
          parts.push(
            `${res.missing.length === 1 ? 'One spectrum' : `${res.missing.length} spectra`} in this link could not be found and ${res.missing.length === 1 ? 'was' : 'were'} left out. (Collections opened from your own files are not part of shared links.)`,
          );
        }
        dispatch({ type: 'SET_LIBRARY_IDS', ids: res.ids, notice: parts.length ? parts.join(' ') : null });
      });
  }, [state.indexLoaded, hashLibraryKey]);

  // Debounced search when filters change.
  const runSearch = useCallback(() => {
    if (!state.dataLoaded) return;
    dispatch({ type: 'SET_SEARCH_LOADING', loading: true });
    const isDefaultRange =
      state.wavelengthRange[0] === DEFAULT_WAVELENGTH_RANGE[0] && state.wavelengthRange[1] === DEFAULT_WAVELENGTH_RANGE[1];
    const filters: SearchFilters = {
      text: state.searchText,
      categories: state.selectedCategories,
      collections: state.selectedSources,
      quantity: state.selectedQuantity,
      geometries: state.selectedGeometries,
      hideSimulated: state.hideSimulated,
      // The default range means "no filter": rows outside it still show.
      wavelengthMin: isDefaultRange ? null : state.wavelengthRange[0],
      wavelengthMax: isDefaultRange ? null : state.wavelengthRange[1],
      sensorWavelengthMin: state.selectedSensor?.wavelengthMin ?? null,
      sensorWavelengthMax: state.selectedSensor?.wavelengthMax ?? null,
      sort: state.sort,
    };
    const { results, total } = searchRecords(state.records, filters, state.pageOffset, state.pageLimit);
    dispatch({ type: 'SET_SEARCH_RESULTS', results, total });
  }, [
    state.dataLoaded,
    state.records,
    state.searchText,
    state.selectedCategories,
    state.selectedSources,
    state.selectedQuantity,
    state.selectedGeometries,
    state.hideSimulated,
    state.wavelengthRange,
    state.selectedSensor,
    state.sort,
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

  // Mirror shareable state into the URL hash (debounced; replaceState, so
  // no history spam).
  const {
    view,
    formatPage,
    formatSection,
    searchText,
    selectedCategories,
    selectedSources,
    selectedQuantity,
    selectedGeometries,
    hideSimulated,
    wavelengthRange,
    selectedSensor,
    downsamplingEnabled,
    libraryIds,
  } = state;
  useEffect(() => {
    const timer = setTimeout(
      () =>
        writeHash(
          encodeStateToHash({
            view,
            formatPage,
            formatSection,
            searchText,
            selectedCategories,
            selectedSources,
            selectedQuantity,
            selectedGeometries,
            hideSimulated,
            wavelengthRange,
            selectedSensor,
            downsamplingEnabled,
            libraryIds,
          }),
        ),
      300,
    );
    return () => clearTimeout(timer);
  }, [
    view,
    formatPage,
    formatSection,
    searchText,
    selectedCategories,
    selectedSources,
    selectedQuantity,
    selectedGeometries,
    hideSimulated,
    wavelengthRange,
    selectedSensor,
    downsamplingEnabled,
    libraryIds,
  ]);

  // Fetch spectral arrays when the library changes: only ids not fetched
  // before, then assemble the selection from the cache.
  const libraryKey = state.libraryIds.join('\0');
  useEffect(() => {
    if (!state.dataLoaded) return;
    const ids = libraryKey ? libraryKey.split('\0') : [];
    const cache = spectraCacheRef.current;
    const assemble = () =>
      dispatch({
        type: 'SET_LIBRARY_SPECTRA',
        spectra: ids.map((id) => cache.get(id)).filter((s): s is SpectrumFull => !!s),
      });
    const { recordById, sources } = stateRef.current;
    const missing = ids
      .filter((id) => !cache.has(id))
      .map((id) => recordById.get(id))
      .filter((r): r is NonNullable<typeof r> => !!r);
    if (missing.length === 0) {
      assemble();
      return;
    }
    let cancelled = false;
    dispatch({ type: 'SET_LIBRARY_LOADING', loading: true });
    getDataEngine()
      .then((engine) => fetchSpectra(engine, missing, sources))
      .then((spectra) => {
        for (const s of spectra) cache.set(s.id, s);
        if (!cancelled) assemble();
      })
      .catch((err) => {
        console.error('Failed to fetch spectral data:', err);
        if (!cancelled) dispatch({ type: 'SET_LIBRARY_ERROR', error: friendlyError(err) });
      });
    return () => {
      cancelled = true;
    };
  }, [libraryKey, state.dataLoaded, state.records, state.libraryRetryNonce]);

  // Compute downsampled data when library spectra or sensor change.
  useEffect(() => {
    if (!state.selectedSensor || !state.downsamplingEnabled) {
      dispatch({ type: 'SET_DOWNSAMPLED_DATA', data: new Map() });
      return;
    }
    const data = new Map<string, DownsampledResult[]>();
    for (const spectrum of state.librarySpectra) {
      // Ascending µm; missing bands stay in as NaN and count as gaps.
      const { wavelengths, values } = toMicrometersWithValues(spectrum.wavelengths, spectrum.values, spectrum.wavelength_unit);
      data.set(spectrum.id, downsampleSpectrum(wavelengths, values, state.selectedSensor.bands));
    }
    dispatch({ type: 'SET_DOWNSAMPLED_DATA', data });
  }, [state.librarySpectra, state.selectedSensor, state.downsamplingEnabled]);

  const openCollection = useCallback(async (req: OpenRequest): Promise<boolean> => {
    openAbortRef.current?.abort();
    const ctl = new AbortController();
    openAbortRef.current = ctl;
    const label = req.input.kind === 'url' ? req.input.url : req.input.files.map((f) => f.name).join(', ');
    const set = (patch: Partial<NonNullable<AppState['openState']>>) =>
      dispatch({
        type: 'SET_OPEN_STATE',
        open: { running: true, label, phase: 'Starting', fraction: null, error: null, messages: [], ...patch },
      });
    set({});
    try {
      const { openCollections } = await import('../lib/data/open');
      const outcome = await openCollections(req.input, {
        check: req.check,
        qualityProfile: req.qualityProfile,
        loadedCollections: new Set(stateRef.current.collections.keys()),
        signal: ctl.signal,
        onProgress: (l, p) => set({ label: l, phase: p.phase, fraction: p.fraction }),
      });
      for (const loaded of outcome.loaded) dispatch({ type: 'ADD_SOURCE', loaded });
      dispatch({ type: 'SET_OPEN_REPORTS', reports: outcome.reports });
      const messages: string[] = [];
      for (const l of outcome.loaded) {
        messages.push(`Opened ${l.source.label}: ${l.records.length.toLocaleString('en-US')} spectra in ${l.collections.map((c) => c.footer.collection.title).join(', ')}.`);
      }
      for (const w of outcome.warnings) messages.push(`${w.label} has warnings (${w.rules.join(', ')}); it opened anyway.`);
      for (const r of outcome.refused) messages.push(`Not opened: ${r.label} ${r.reason}.`);
      dispatch({
        type: 'SET_OPEN_STATE',
        open: {
          running: false,
          label,
          phase: 'Done',
          fraction: 1,
          error: outcome.loaded.length === 0 ? messages.join(' ') || 'Nothing was opened.' : null,
          messages: outcome.loaded.length === 0 ? [] : messages,
        },
      });
      if (outcome.loaded.length > 0) {
        // Show the newly opened collections.
        dispatch({ type: 'SET_SOURCES', sources: outcome.loaded.flatMap((l) => l.collections.map((c) => c.id)) });
      }
      return outcome.loaded.length > 0;
    } catch (e) {
      dispatch({
        type: 'SET_OPEN_STATE',
        open: {
          running: false,
          label,
          phase: 'Failed',
          fraction: null,
          error: ctl.signal.aborted ? 'Cancelled.' : e instanceof Error ? e.message : String(e),
          messages: [],
        },
      });
      return false;
    } finally {
      if (openAbortRef.current === ctl) openAbortRef.current = null;
    }
  }, []);

  const cancelOpen = useCallback(() => openAbortRef.current?.abort(), []);

  const removeSource = useCallback((key: string) => {
    const source = stateRef.current.sources.get(key);
    if (!source || source.kind === 'release') return;
    for (const [id, s] of spectraCacheRef.current) if (s.sourceKey === key) spectraCacheRef.current.delete(id);
    dispatch({ type: 'REMOVE_SOURCE', key });
    void import('../lib/data/open').then(({ disposeSource }) => disposeSource(source)).catch(() => undefined);
  }, []);

  return (
    <AppContext.Provider value={{ state, dispatch, openCollection, cancelOpen, removeSource }}>
      {children}
    </AppContext.Provider>
  );
}
