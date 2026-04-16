import { useState, useRef, useEffect } from 'react';
import { useAppContext } from '../../state/AppContext';
import { SENSOR_GROUPS } from '../../constants/sensors';
import type { SensorDefinition } from '../../types/sensors';

function SensorOption({
  sensor,
  selected,
  onSelect,
}: {
  sensor: SensorDefinition;
  selected: boolean;
  onSelect: () => void;
}) {
  const isHyper = sensor.bands.length > 30;
  return (
    <button
      onClick={onSelect}
      className={`w-full text-left px-3 py-2 transition-colors ${
        selected
          ? 'bg-indigo-50 border-l-2 border-indigo-500'
          : 'hover:bg-gray-50 border-l-2 border-transparent'
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className={`text-sm font-medium ${selected ? 'text-indigo-700' : 'text-gray-800'}`}>
          {sensor.name}
        </span>
        {isHyper && (
          <span className="flex-shrink-0 px-1.5 py-0.5 text-[9px] font-medium rounded-full bg-violet-100 text-violet-600">
            Hyperspectral
          </span>
        )}
      </div>
      <div className="mt-0.5 flex items-center gap-2 text-[10px] text-gray-400">
        <span>{sensor.bands.length} bands</span>
        <span className="text-gray-300">&middot;</span>
        <span>{(sensor.wavelengthMin * 1000).toFixed(0)}–{(sensor.wavelengthMax * 1000).toFixed(0)} nm</span>
        <span className="text-gray-300">&middot;</span>
        <span>{sensor.description.match(/\(([^)]+)\)/)?.[1] ?? ''}</span>
      </div>
    </button>
  );
}

export default function SensorSelector() {
  const { state, dispatch } = useAppContext();
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    if (open) document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [open]);

  const selected = state.selectedSensor;

  const handleSelect = (sensor: SensorDefinition | null) => {
    dispatch({ type: 'SET_SENSOR', sensor });
    setOpen(false);
  };

  return (
    <div ref={containerRef} className="relative">
      <label className="text-xs font-medium text-gray-500 uppercase tracking-wider">
        Satellite Sensor
      </label>

      {/* Trigger button */}
      <button
        onClick={() => setOpen(!open)}
        className={`mt-1.5 w-full flex items-center justify-between px-3 py-2 text-sm bg-white
                    border rounded-lg transition-colors text-left
                    ${open ? 'border-indigo-400 ring-2 ring-indigo-500/20' : 'border-gray-200 hover:border-gray-300'}`}
      >
        {selected ? (
          <div className="min-w-0">
            <span className="font-medium text-gray-800">{selected.name}</span>
            <span className="ml-1.5 text-xs text-gray-400">
              {selected.bands.length} bands
            </span>
          </div>
        ) : (
          <span className="text-gray-400">No sensor (show all spectra)</span>
        )}
        <svg
          className={`w-4 h-4 text-gray-400 flex-shrink-0 transition-transform ${open ? 'rotate-180' : ''}`}
          fill="none" viewBox="0 0 24 24" stroke="currentColor"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {/* Dropdown panel */}
      {open && (
        <div className="absolute z-30 mt-1 w-full bg-white border border-gray-200 rounded-lg shadow-lg
                        max-h-[420px] overflow-y-auto">
          {/* Clear selection */}
          <button
            onClick={() => handleSelect(null)}
            className={`w-full text-left px-3 py-2 text-sm transition-colors border-b border-gray-100
                        ${!selected ? 'bg-gray-50 text-indigo-600 font-medium' : 'text-gray-500 hover:bg-gray-50'}`}
          >
            No sensor (show all spectra)
          </button>

          {SENSOR_GROUPS.map((group) => (
            <div key={group.label}>
              <div className="px-3 py-1.5 bg-gray-50 border-b border-gray-100">
                <span className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider">
                  {group.label}
                </span>
              </div>
              {group.sensors.map((sensor) => (
                <SensorOption
                  key={sensor.id}
                  sensor={sensor}
                  selected={selected?.id === sensor.id}
                  onSelect={() => handleSelect(sensor)}
                />
              ))}
            </div>
          ))}
        </div>
      )}

      {/* Summary when sensor selected */}
      {selected && !open && (
        <p className="mt-1 text-xs text-gray-500">
          {selected.bands.length} bands, {selected.wavelengthMin.toFixed(3)}–{selected.wavelengthMax.toFixed(3)} {'\u00B5'}m.
          Showing reflectance spectra only.
        </p>
      )}
    </div>
  );
}
