import { useEffect, useRef } from 'react';
import { useAppContext } from '../../state/AppContext';
import { FORMAT_SECTIONS } from '../../format/sections';
import { FORMAT_VERSION } from '../../format/spec';
import FormatReference from './FormatReference';
import ConformanceChecker from './ConformanceChecker';

/** The Format area: the spec reference and the in-browser conformance checker. */
export default function FormatArea() {
  const { state, dispatch } = useAppContext();
  const { formatPage, formatSection } = state;
  const mainRef = useRef<HTMLElement>(null);

  // Scroll to the requested section (from the nav or a shared link).
  useEffect(() => {
    if (formatPage !== 'reference') {
      mainRef.current?.scrollTo({ top: 0 });
      return;
    }
    const el = formatSection ? document.getElementById(`fmt-${formatSection}`) : null;
    if (el) el.scrollIntoView({ block: 'start' });
    else mainRef.current?.scrollTo({ top: 0 });
  }, [formatPage, formatSection]);

  const gotoSection = (id: string) => {
    dispatch({ type: 'SET_VIEW', view: 'format', page: 'reference', section: id });
    // Scroll even if the section is already selected, and move keyboard and
    // screen-reader focus to its heading.
    const section = document.getElementById(`fmt-${id}`);
    section?.scrollIntoView({ block: 'start', behavior: 'smooth' });
    section?.querySelector<HTMLElement>('h2')?.focus({ preventScroll: true });
  };
  const openChecker = () => dispatch({ type: 'SET_VIEW', view: 'format', page: 'check' });

  const navItem = (active: boolean) =>
    'w-full text-left px-3 py-1.5 rounded-md text-sm transition-colors ' +
    (active ? 'bg-indigo-50 text-indigo-700 font-medium' : 'text-gray-600 hover:bg-gray-50 hover:text-gray-900');

  return (
    <div className="flex flex-1 overflow-hidden">
      <aside className="hidden md:flex w-60 flex-shrink-0 flex-col bg-white border-r border-gray-200 overflow-y-auto">
        <div className="p-4 space-y-5">
          <div>
            <h2 className="px-3 text-[11px] font-semibold text-gray-400 uppercase tracking-wide mb-1.5">
              Format {FORMAT_VERSION}
            </h2>
            <nav className="space-y-0.5" aria-label="Format reference">
              {FORMAT_SECTIONS.map((s) => (
                <button
                  key={s.id}
                  onClick={() => gotoSection(s.id)}
                  className={navItem(formatPage === 'reference' && formatSection === s.id)}
                >
                  {s.title}
                </button>
              ))}
            </nav>
          </div>
          <div>
            <h2 className="px-3 text-[11px] font-semibold text-gray-400 uppercase tracking-wide mb-1.5">Tools</h2>
            <button onClick={openChecker} className={navItem(formatPage === 'check')}>
              <span className="flex items-center gap-2">
                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden>
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                Check a file
              </span>
            </button>
          </div>
        </div>
      </aside>

      <main ref={mainRef} className="flex-1 overflow-y-auto p-4 md:p-6">
        <div className="max-w-4xl mx-auto space-y-4">
          {/* Narrow screens: the side nav is hidden, so offer the two pages here. */}
          <div className="md:hidden flex gap-2">
            <button
              onClick={() => dispatch({ type: 'SET_VIEW', view: 'format', page: 'reference' })}
              className={navItem(formatPage === 'reference')}
            >
              Reference
            </button>
            <button onClick={openChecker} className={navItem(formatPage === 'check')}>
              Check a file
            </button>
          </div>
          {/* Both stay mounted so a report survives a visit to the reference. */}
          <div className={formatPage === 'check' ? '' : 'hidden'}>
            <ConformanceChecker />
          </div>
          <div className={formatPage === 'check' ? 'hidden' : ''}>
            <FormatReference onOpenChecker={openChecker} onGoto={gotoSection} />
          </div>
        </div>
      </main>
    </div>
  );
}
