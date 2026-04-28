import { useState } from 'react';
import { useAppContext } from '../../state/AppContext';
import { OPENSPECLIB_VERSION } from '../../constants/urls';
import { InfoModal } from './InfoModal';

/** Inline header logo — openspeclib wordmark + spectral trace. */
function Logo() {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="10 8 350 105" className="h-10" aria-label="OpenSpecLib">
      <defs>
        <linearGradient id="spec" x1="0%" y1="0%" x2="100%" y2="0%">
          <stop offset="0%" stopColor="#818CF8" />
          <stop offset="40%" stopColor="#34D399" />
          <stop offset="100%" stopColor="#FBBF24" />
        </linearGradient>
      </defs>
      <g transform="translate(15, 10)">
        <text x="80" y="78" fontFamily="'Inter','Helvetica Neue',Arial,sans-serif"
          fontSize="44" fontWeight="600" fill="#E2E8F0" letterSpacing="-1.5">
          open<tspan fontWeight="800" fill="url(#spec)">spec</tspan>lib
        </text>
        <path d="M 6 88
           C 7 86, 8 83, 10 81 C 12 76, 13 72, 14 68 C 15.5 63, 17 58, 19 55
           C 20 53, 21 51, 22 49 C 23 47, 23.5 46, 24 46 C 24.5 47, 25 50, 25.5 53
           C 26 57, 26.3 58, 26.5 57 C 27 55, 27.5 51, 28 47 C 28.5 45, 29 44, 30 43
           C 31 42, 31.5 41, 32 40 C 32.5 39.5, 33 40, 33.5 42 C 34 45, 34.3 48, 34.5 50
           C 34.8 48, 35 44, 35.5 40 C 36 37, 37 36, 38 35 C 39 34, 40 34, 41 33
           C 42 33, 42.5 34, 43 36 C 43.5 38, 44 42, 44.5 44 C 45 42, 45.5 38, 46 35
           C 46.5 33, 47 32, 48 31 C 49 30, 49.5 31, 50 33 C 50.5 35, 51 38, 51.3 40
           C 51.6 44, 51.8 48, 52 52 C 52.5 50, 53 44, 54 38 C 54.5 34, 55 31, 56 29
           C 57 28, 57.5 28, 58 30 C 58.5 32, 59 36, 59.5 39 C 60 36, 60.5 32, 61 29
           C 61.5 27, 62 26, 63 26 C 64 26, 64.5 27, 65 29 C 65.5 31, 65.8 32, 66 31
           C 66.3 30, 66.5 27, 67 24 C 67.5 21, 68 19, 68.5 17 C 69 15, 69.5 14, 70 15
           C 70.5 17, 71 20, 71.5 24 C 72 28, 72.3 30, 72.5 32 C 72.8 34, 73 38, 73.5 42
           C 73.8 46, 74 50, 74.5 50 C 75 47, 75.5 40, 76 34 C 76.5 29, 77 26, 78 24
           C 79 22, 80 22, 81 23 C 82 24, 83 25, 84 26 C 85 24, 86 22, 87 21
           C 88 20, 90 19, 93 18 C 96 17, 100 16, 105 15 C 110 14, 117 13, 122 13
           C 126 14, 128 16, 130 20 C 131 22, 132 24, 132.5 22 C 133 19, 134 15, 136 13
           C 138 12, 140 12, 142 11 C 143 11, 144 10, 145 10 C 146 10, 147 11, 148 12
           C 149 14, 150 18, 151 22 C 152 20, 153 15, 154 13 C 155 11, 157 10, 159 11
           C 160 12, 161 13, 162 16 C 163 24, 163.5 32, 164 42 C 164.5 38, 165 30, 166 26
           C 167 23, 168 22, 170 24"
          fill="none" stroke="url(#spec)" strokeWidth="2.5"
          strokeLinecap="round" strokeLinejoin="round" />
      </g>
    </svg>
  );
}

function HelpModal({ onClose }: { onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={onClose}>
      <div
        className="bg-white rounded-xl shadow-xl max-w-md w-full mx-4 max-h-[80vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-6 py-4 border-b border-gray-100 flex items-center justify-between">
          <h2 className="text-lg font-semibold text-gray-900">Quick Guide</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600">
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
        <div className="px-6 py-5 space-y-4 text-sm text-gray-700">
          <div>
            <h3 className="font-semibold text-gray-900 mb-2">Getting Started</h3>
            <ul className="list-disc list-inside space-y-1.5 text-gray-600">
              <li>Search by material name, keyword, or formula</li>
              <li>Filter by category to narrow results</li>
              <li>Select a sensor to filter for compatible spectra and enable downsampling</li>
              <li>Click table rows to add spectra to your library</li>
              <li>Export your library as CSV or ENVI format</li>
            </ul>
          </div>

          <div>
            <h3 className="font-semibold text-gray-900 mb-2">Tips</h3>
            <ul className="list-disc list-inside space-y-1.5 text-gray-600">
              <li>Use chemical formulas (e.g. SiO2, CaCO3) for precise searches</li>
              <li>Combine category filters with text search for targeted results</li>
              <li>The chart updates live as you add or remove spectra</li>
              <li>Check the sidebar for licensing info when spectra are selected</li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function Header() {
  const { state } = useAppContext();
  const [showInfo, setShowInfo] = useState(false);
  const [showHelp, setShowHelp] = useState(false);

  return (
    <>
      <header className="bg-slate-900 px-5 py-2 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Logo />
          <div className="border-l border-slate-700 pl-4 flex items-center gap-3">
            <span className="text-sm font-medium text-slate-300">Viewer</span>
            <span className="text-[10px] text-slate-600 bg-slate-800 px-1.5 py-0.5 rounded font-mono">
              v{OPENSPECLIB_VERSION}
            </span>
            <span className="text-xs text-slate-500 flex items-center gap-1.5">
              {state.catalogLoaded
                ? `${state.resultCount.toLocaleString()} spectra`
                : 'Loading catalog...'}
              {state.duckdbReady ? (
                <span className="inline-flex items-center gap-1 text-emerald-400" title="DuckDB ready">
                  <span className="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>
                  Ready
                </span>
              ) : (
                <span className="inline-flex items-center gap-1 text-amber-400" title="Initializing DuckDB query engine">
                  <svg className="w-3 h-3 animate-spin" fill="none" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
                  </svg>
                  Initializing query engine
                </span>
              )}
            </span>
          </div>
        </div>
        <div className="flex items-center gap-4">
          <button
            onClick={() => setShowHelp(true)}
            className="text-xs text-slate-400 hover:text-slate-200 flex items-center gap-1.5 transition-colors"
            title="Quick guide"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                d="M8.228 9c.549-1.165 2.03-2 3.772-2 2.21 0 4 1.343 4 3 0 1.4-1.278 2.575-3.006 2.907-.542.104-.994.54-.994 1.093m0 3h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            Help
          </button>
          <button
            onClick={() => setShowInfo(true)}
            className="text-xs text-slate-400 hover:text-slate-200 flex items-center gap-1.5 transition-colors"
            title="About this project"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            About
          </button>
          <a
            href="https://github.com/null-jones/openspeclib"
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs text-slate-400 hover:text-slate-200 flex items-center gap-1.5 transition-colors"
          >
            <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
              <path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z" />
            </svg>
            GitHub
          </a>
        </div>
      </header>
      {showHelp && <HelpModal onClose={() => setShowHelp(false)} />}
      {showInfo && <InfoModal onClose={() => setShowInfo(false)} />}
    </>
  );
}
