import { lazy, Suspense } from 'react';
import { AppProvider } from './state/AppContext';
import ErrorBoundary from './components/common/ErrorBoundary';
import Header from './components/layout/Header';
import Sidebar from './components/layout/Sidebar';
import ResultsTable from './components/results/ResultsTable';
import LibraryPanel from './components/library/LibraryPanel';

const SpectralChart = lazy(() => import('./components/visualization/SpectralChart'));

function App() {
  return (
    <ErrorBoundary>
      <AppProvider>
        <div className="h-screen flex flex-col">
          <Header />
          <div className="flex flex-1 overflow-hidden">
            <Sidebar />
            <main className="flex-1 overflow-y-auto p-6 space-y-4">
              {/* Chart + Library at top */}
              <ErrorBoundary>
                <Suspense fallback={
                  <div className="bg-white rounded-lg border border-gray-200 p-8 text-center text-gray-400">
                    <p className="text-sm">Loading chart...</p>
                  </div>
                }>
                  <SpectralChart />
                </Suspense>
              </ErrorBoundary>
              <ErrorBoundary>
                <LibraryPanel />
              </ErrorBoundary>
              {/* Results table below */}
              <ResultsTable />
            </main>
          </div>
        </div>
      </AppProvider>
    </ErrorBoundary>
  );
}

export default App;
