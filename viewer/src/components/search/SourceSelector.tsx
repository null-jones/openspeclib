import { useState, useRef, useEffect, useMemo, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { useAppContext } from '../../state/AppContext';
import { collectionLabel } from '../../constants/sources';

interface SourceInfo {
  id: string;
  label: string;
  description: string;
  count: number;
  opened: boolean;
}

export default function SourceSelector() {
  const { state, dispatch } = useAppContext();
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  /** Escape closes the list (and only the list, not a drawer around it) and returns focus. */
  const onKeyDown = (e: ReactKeyboardEvent) => {
    if (e.key !== 'Escape' || !open) return;
    e.stopPropagation();
    setOpen(false);
    triggerRef.current?.focus();
  };

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    if (open) document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [open]);

  // Collections and their spectrum counts, from the loaded search rows.
  const sources: SourceInfo[] = useMemo(() => {
    const counts = new Map<string, number>();
    for (const r of state.records) {
      counts.set(r.collection, (counts.get(r.collection) ?? 0) + 1);
    }
    return [...counts.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(([id, count]) => {
        const meta = state.collections.get(id);
        const kind = meta ? state.sources.get(meta.sourceKey)?.kind : undefined;
        return {
          id,
          label: collectionLabel(id, meta?.footer.collection.title),
          description: meta?.footer.collection.publisher ?? '',
          count,
          opened: kind !== undefined && kind !== 'release',
        };
      });
  }, [state.records, state.collections, state.sources]);

  const selected = state.selectedSources;
  const allSelected = selected.length === 0;

  const handleToggle = (sourceId: string) => {
    dispatch({ type: 'TOGGLE_SOURCE', source: sourceId });
  };

  const handleSelectAll = () => {
    dispatch({ type: 'SET_SOURCES', sources: [] });
    setOpen(false);
  };

  const totalSelected = allSelected
    ? sources.reduce((s, src) => s + src.count, 0)
    : sources.filter((s) => selected.includes(s.id)).reduce((s, src) => s + src.count, 0);

  const triggerLabel = allSelected
    ? `All collections (${sources.length})`
    : selected.length === 1
      ? sources.find((x) => x.id === selected[0])?.label ?? selected[0]
      : `${selected.length} collections`;

  if (sources.length <= 1) return null;

  return (
    <div ref={containerRef} className="relative" onKeyDown={onKeyDown}>
      <h3 id="filter-collections" className="text-xs font-medium text-gray-600 uppercase tracking-wider">
        Collections
      </h3>

      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        aria-controls="collections-list"
        aria-describedby="filter-collections"
        className={`mt-1.5 w-full flex items-center justify-between px-3 py-2 text-sm bg-white
                    border rounded-lg transition-colors text-left
                    ${open ? 'border-indigo-400 ring-2 ring-indigo-500/20' : 'border-gray-200 hover:border-gray-300'}`}
      >
        <div className="min-w-0">
          <span className={`font-medium ${allSelected ? 'text-gray-400' : 'text-gray-800'}`}>
            {triggerLabel}
          </span>
          <span className="ml-1.5 text-xs text-gray-400">
            {totalSelected.toLocaleString()} spectra
          </span>
        </div>
        <svg
          className={`w-4 h-4 text-gray-400 flex-shrink-0 transition-transform ${open ? 'rotate-180' : ''}`}
          fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {open && (
        <div
          id="collections-list"
          role="group"
          aria-labelledby="filter-collections"
          className="absolute z-30 mt-1 w-full bg-white border border-gray-200 rounded-lg shadow-lg
                        max-h-[360px] overflow-y-auto"
        >
          {/* All sources option */}
          <button
            type="button"
            onClick={handleSelectAll}
            aria-pressed={allSelected}
            className={`w-full text-left px-3 py-2 text-sm transition-colors border-b border-gray-100
                        ${allSelected ? 'bg-gray-50 text-indigo-600 font-medium' : 'text-gray-500 hover:bg-gray-50'}`}
          >
            All collections ({sources.reduce((s, src) => s + src.count, 0).toLocaleString()} spectra)
          </button>

          {/* Individual sources */}
          {sources.map((src) => {
            const isActive = selected.includes(src.id);
            return (
              <button
                key={src.id}
                type="button"
                onClick={() => handleToggle(src.id)}
                aria-pressed={isActive}
                className={`w-full text-left px-3 py-2 transition-colors ${
                  isActive
                    ? 'bg-indigo-50 border-l-2 border-indigo-500'
                    : 'hover:bg-gray-50 border-l-2 border-transparent'
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2 min-w-0">
                    <div className={`w-3.5 h-3.5 rounded border flex-shrink-0 flex items-center justify-center
                                    ${isActive ? 'bg-indigo-500 border-indigo-500' : 'border-gray-300'}`}>
                      {isActive && (
                        <svg className="w-2.5 h-2.5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                        </svg>
                      )}
                    </div>
                    <span className={`text-sm font-medium ${isActive ? 'text-indigo-700' : 'text-gray-800'}`}>
                      {src.label}
                    </span>
                    {src.opened && (
                      <span className="px-1 py-px text-[9px] rounded bg-emerald-50 text-emerald-700 border border-emerald-200">
                        opened
                      </span>
                    )}
                  </div>
                  <span className="text-[10px] text-gray-500 flex-shrink-0 tabular-nums">
                    {src.count.toLocaleString()}
                  </span>
                </div>
                {src.description && (
                  <p className="mt-0.5 ml-5.5 text-[10px] text-gray-500 pl-[22px]">{src.description}</p>
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
