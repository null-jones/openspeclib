import { lazy, Suspense, useCallback, useEffect, useState } from 'react';
import { AppProvider, useAppContext } from './state/AppContext';
import ErrorBoundary from './components/common/ErrorBoundary';
import Header from './components/layout/Header';
import Sidebar from './components/layout/Sidebar';
import ResultsTable from './components/results/ResultsTable';
import LibraryPanel from './components/library/LibraryPanel';

const SpectralChart = lazy(() => import('./components/visualization/SpectralChart'));
// The Format area (spec reference + checker, with ajv) loads only when opened.
const FormatArea = lazy(() => import('./components/format/FormatArea'));

/** Shown instead of the chart while the library is empty, so Plotly loads only when needed. */
function EmptyChart() {
  return (
    <div className="bg-white rounded-lg border border-gray-200 p-8 text-center text-gray-500">
      <svg className="w-12 h-12 mx-auto mb-3 text-gray-300" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden>
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
          d="M7 12l3-3 3 3 4-4M8 21l4-4 4 4M3 4h18M4 4h16v12a1 1 0 01-1 1H5a1 1 0 01-1-1V4z" />
      </svg>
      <p className="text-sm font-medium">No spectra to visualize</p>
      <p className="text-xs mt-1">Add spectra to your library to see them plotted here</p>
    </div>
  );
}

function Explorer() {
  const { state } = useAppContext();
  const [filtersOpen, setFiltersOpen] = useState(false);
  const closeFilters = useCallback(() => setFiltersOpen(false), []);
  // The drawer exists only below md: close it when the window grows past that.
  useEffect(() => {
    const wide = window.matchMedia('(min-width: 768px)');
    const onChange = () => {
      if (wide.matches) setFiltersOpen(false);
    };
    wide.addEventListener('change', onChange);
    return () => wide.removeEventListener('change', onChange);
  }, []);
  return (
    <div className="flex flex-1 overflow-hidden">
      <Sidebar open={filtersOpen} onClose={closeFilters} />
      <main className="flex-1 min-w-0 overflow-y-auto p-3 md:p-6 space-y-4">
        <button
          onClick={() => setFiltersOpen(true)}
          aria-controls="explorer-sidebar"
          aria-expanded={filtersOpen}
          className="md:hidden w-full px-3 py-2 text-sm bg-white border border-gray-200 rounded-lg text-gray-700"
        >
          Search and filters
        </button>
        {/* Chart + Library at top */}
        {state.libraryIds.length === 0 ? (
          <EmptyChart />
        ) : (
          <ErrorBoundary>
            <Suspense fallback={
              <div className="bg-white rounded-lg border border-gray-200 p-8 text-center text-gray-500">
                <p className="text-sm">Loading chart…</p>
              </div>
            }>
              <SpectralChart />
            </Suspense>
          </ErrorBoundary>
        )}
        <ErrorBoundary>
          <LibraryPanel />
        </ErrorBoundary>
        {/* Results table below */}
        <ResultsTable />
      </main>
    </div>
  );
}

function Main() {
  const { state } = useAppContext();
  const inFormat = state.view === 'format';
  // Once opened, the Format area stays mounted (hidden while exploring), so a
  // running check goes on and its reports are still there on return.
  const [formatVisited, setFormatVisited] = useState(inFormat);
  if (inFormat && !formatVisited) setFormatVisited(true);
  return (
    <>
      {formatVisited && (
        <div className={inFormat ? 'flex-1 flex min-h-0' : 'hidden'}>
          <ErrorBoundary>
            <Suspense fallback={<div className="flex-1 p-8 text-center text-sm text-gray-400">Loading…</div>}>
              <FormatArea />
            </Suspense>
          </ErrorBoundary>
        </div>
      )}
      {!inFormat && <Explorer />}
    </>
  );
}

function App() {
  return (
    <ErrorBoundary>
      <AppProvider>
        <div className="h-screen flex flex-col">
          <Header />
          <Main />
        </div>
      </AppProvider>
    </ErrorBoundary>
  );
}

export default App;
