import { useEffect, useMemo, useRef, type ReactNode } from 'react';
// The basic bundle carries only the cartesian trace types (scatter is all we
// use) at roughly a third of the size of the full plotly dist.
import Plotly from 'plotly.js-basic-dist-min';
import createPlotlyComponent from 'react-plotly.js/factory';
import { useAppContext } from '../../state/AppContext';
import { isMissingValue, toMicrometersWithValues } from '../../lib/wavelength-utils';
import type { SpectrumFull } from '../../lib/data/types';
import type { PlotData, Shape } from 'plotly.js';
import { HYPERSPECTRAL_BAND_THRESHOLD } from '../../constants/sources';
import { vocabLabel } from '../../format/vocab';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const factory = (createPlotlyComponent as any).default ?? createPlotlyComponent;
const Plot = factory(Plotly);

const COLORS = [
  '#4f46e5', '#059669', '#dc2626', '#d97706', '#7c3aed',
  '#0891b2', '#e11d48', '#65a30d', '#6366f1', '#ea580c',
  '#2563eb', '#16a34a', '#9333ea', '#ca8a04', '#0d9488',
];

const SHARED_LAYOUT = {
  plot_bgcolor: 'white',
  paper_bgcolor: 'white',
  font: { family: 'Inter, system-ui, sans-serif' },
  hovermode: 'closest' as const,
};

const PLOTLY_CONFIG = {
  responsive: true,
  displayModeBar: true,
  modeBarButtonsToRemove: ['lasso2d' as const, 'select2d' as const],
  displaylogo: false,
};

/**
 * Y-axis label for the current selection. Overlaying different quantities
 * on one axis is scientifically dubious — when it happens the label says so
 * rather than mislabelling everything "Reflectance".
 */
function yAxisLabel(spectra: SpectrumFull[]): string {
  const quantities = [...new Set(spectra.map((s) => s.quantity))];
  if (quantities.length === 1) {
    const t = quantities[0];
    return t.charAt(0).toUpperCase() + t.slice(1);
  }
  return `Value (mixed: ${quantities.join(', ')})`;
}

/** A warning when plotted spectra mix geometries or quantities. */
function ComparabilityNotice({ spectra }: { spectra: SpectrumFull[] }) {
  const { dispatch } = useAppContext();
  const quantities = [...new Set(spectra.map((s) => s.quantity))];
  const geometries = [...new Set(spectra.map((s) => s.geometry))];
  const known = geometries.filter((g) => g !== 'unknown');
  const explain = (
    <button
      onClick={() => dispatch({ type: 'SET_VIEW', view: 'format', page: 'reference', section: 'geometry' })}
      className="underline hover:text-amber-950"
    >
      Why geometry matters
    </button>
  );
  const notes: ReactNode[] = [];
  if (quantities.length > 1) {
    notes.push(
      <span key="q">
        These spectra measure different quantities ({quantities.join(', ')}); their values are not on the same scale.
      </span>,
    );
  }
  const unknownNote = geometries.includes('unknown') ? ' Some do not state their geometry at all.' : '';
  if (known.length > 1) {
    const sphere = known.includes('directional_hemispherical');
    const probe = known.some((g) => g === 'biconical' || g === 'hemispherical_conical' || g === 'hemispherical_directional');
    notes.push(
      <span key="g">
        These spectra were measured with different geometries ({known.map(vocabLabel).join(', ')}), so their values are
        not directly comparable.
        {sphere && probe && ' An integrating sphere collects light reflected in every direction; a probe or fore-optic sees a small cone, relative to a reference panel.'}
        {unknownNote} {explain}
      </span>,
    );
  } else if (known.length === 1 && geometries.includes('unknown')) {
    notes.push(
      <span key="u">
        Some spectra are {vocabLabel(known[0])}, others do not state their geometry; compare them with care. {explain}
      </span>,
    );
  }
  if (notes.length === 0) return null;
  return (
    <div className="mx-4 mt-2 px-3 py-2 rounded-md bg-amber-50 border border-amber-200 text-xs text-amber-900 space-y-1" role="note">
      {notes.map((n, i) => (
        <p key={i}>{n}</p>
      ))}
    </div>
  );
}

/**
 * Legend names, deduplicated: identical material names (three "Quartz"
 * samples) get the sample name appended so traces are tellable apart.
 */
function legendNames(spectra: SpectrumFull[]): string[] {
  const counts = new Map<string, number>();
  for (const s of spectra) {
    counts.set(s.material_name, (counts.get(s.material_name) ?? 0) + 1);
  }
  return spectra.map((s) => {
    if ((counts.get(s.material_name) ?? 0) <= 1) return s.material_name;
    const suffix = s.name.length > 42 ? `${s.name.slice(0, 39)}…` : s.name;
    return `${s.material_name} — ${suffix}`;
  });
}

function DownsampledPlot({ traces, sensor, yLabel }: { traces: Partial<PlotData>[]; sensor: { name: string; bands: { name: string }[]; wavelengthMin: number; wavelengthMax: number }; yLabel: string }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    const layout = {
      ...SHARED_LAYOUT,
      autosize: true,
      height: 350,
      margin: { l: 55, r: 15, t: 10, b: 45 },
      xaxis: {
        title: { text: 'Wavelength (μm)', font: { size: 11 } },
        showgrid: true,
        gridcolor: '#f3f4f6',
        zeroline: false,
        range: [sensor.wavelengthMin - 0.02, sensor.wavelengthMax + 0.02],
      },
      yaxis: {
        title: { text: yLabel, font: { size: 11 } },
        showgrid: true,
        gridcolor: '#f3f4f6',
        zeroline: false,
        rangemode: 'tozero' as const,
      },
      legend: {
        orientation: 'h' as const,
        yanchor: 'bottom' as const,
        y: 1.01,
        xanchor: 'left' as const,
        x: 0,
        font: { size: 10 },
      },
    };
    // Deep-clone traces so Plotly's internal mutation doesn't break React state.
    const cloned = JSON.parse(JSON.stringify(traces));
    const el = ref.current;
    Plotly.newPlot(el, cloned, layout, { ...PLOTLY_CONFIG, responsive: true });
    return () => Plotly.purge(el);
  }, [traces, sensor, yLabel]);

  return (
    <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
      <div className="px-4 pt-2 pb-0">
        <h4 className="text-xs font-medium text-gray-500 uppercase tracking-wider">
          {sensor.name} — Downsampled
          <span className="font-normal text-gray-400 ml-1">
            ({sensor.bands.length} bands)
          </span>
        </h4>
      </div>
      <div ref={ref} style={{ width: '100%' }} />
    </div>
  );
}

export default function SpectralChart() {
  const { state } = useAppContext();
  const { librarySpectra, selectedSensor, downsamplingEnabled, downsampledData } = state;

  const showDownsampled = downsamplingEnabled && !!selectedSensor;
  const isHyperspectral = selectedSensor ? selectedSensor.bands.length > HYPERSPECTRAL_BAND_THRESHOLD : false;
  const yLabel = useMemo(() => yAxisLabel(librarySpectra), [librarySpectra]);
  const traceNames = useMemo(() => legendNames(librarySpectra), [librarySpectra]);

  // Original spectra traces
  const { originalTraces, sensorShapes } = useMemo(() => {
    const traces: Partial<PlotData>[] = [];
    const shapes: Partial<Shape>[] = [];

    librarySpectra.forEach((spectrum, idx) => {
      const color = COLORS[idx % COLORS.length];
      const { wavelengths, values } = toMicrometersWithValues(
        spectrum.wavelengths,
        spectrum.values,
        spectrum.wavelength_unit,
      );
      const cleanValues = values.map((v) => (isMissingValue(v) ? null : v));

      traces.push({
        x: wavelengths,
        y: cleanValues,
        type: 'scatter',
        mode: 'lines',
        name: traceNames[idx],
        line: { color, width: 1.5 },
        hovertemplate: '%{x:.4f} μm<br>%{y:.4f}<extra>%{fullData.name}</extra>',
      });
    });

    // Sensor band shading on original plot
    if (selectedSensor && downsamplingEnabled && !isHyperspectral) {
      for (const band of selectedSensor.bands) {
        shapes.push({
          type: 'rect',
          xref: 'x',
          yref: 'paper',
          x0: band.centerWavelength - band.fwhm / 2,
          x1: band.centerWavelength + band.fwhm / 2,
          y0: 0,
          y1: 1,
          fillcolor: 'rgba(99, 102, 241, 0.06)',
          line: { width: 0 },
        });
      }
    }

    return { originalTraces: traces, sensorShapes: shapes };
  }, [librarySpectra, traceNames, selectedSensor, downsamplingEnabled, isHyperspectral]);

  // Downsampled spectra traces (separate plot)
  const downsampledTraces = useMemo(() => {
    if (!selectedSensor || !downsamplingEnabled) return [];

    const traces: Partial<PlotData>[] = [];

    librarySpectra.forEach((spectrum, idx) => {
      const color = COLORS[idx % COLORS.length];
      const ds = downsampledData.get(spectrum.id);
      if (!ds) return;

      const validBands = ds
        .filter((b) => b.value !== null)
        .sort((a, b) => a.centerWavelength - b.centerWavelength);
      if (validBands.length === 0) return;
      const mode = isHyperspectral ? 'lines' : 'lines+markers';

      traces.push({
        x: validBands.map((b) => b.centerWavelength),
        y: validBands.map((b) => b.value!),
        type: 'scatter',
        mode,
        name: traceNames[idx],
        line: { color, width: 2 },
        marker: isHyperspectral
          ? undefined
          : { color, size: 6, symbol: 'diamond', line: { color: 'white', width: 1 } },
        hovertemplate: '%{text}<br>%{x:.4f} μm<br>%{y:.4f}<extra>%{fullData.name}</extra>',
        text: validBands.map((b) => b.bandName),
      });
    });

    return traces;
  }, [librarySpectra, traceNames, downsampledData, selectedSensor, downsamplingEnabled, isHyperspectral]);

  if (librarySpectra.length === 0) {
    return (
      <div className="bg-white rounded-lg border border-gray-200 p-8 text-center text-gray-400">
        <svg className="w-12 h-12 mx-auto mb-3 text-gray-300" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
            d="M7 12l3-3 3 3 4-4M8 21l4-4 4 4M3 4h18M4 4h16v12a1 1 0 01-1 1H5a1 1 0 01-1-1V4z" />
        </svg>
        {state.libraryIds.length === 0 ? (
          <>
            <p className="text-sm font-medium">No spectra to visualize</p>
            <p className="text-xs mt-1">Add spectra to your library to see them plotted here</p>
          </>
        ) : state.libraryError ? (
          <p className="text-sm font-medium">The spectra could not be loaded (see the library below).</p>
        ) : (
          <p className="text-sm font-medium" role="status">Loading spectra…</p>
        )}
      </div>
    );
  }

  return (
    <div className={`${showDownsampled ? 'grid grid-cols-1 xl:grid-cols-2 gap-4' : ''}`}>
      {/* Original spectra plot */}
      <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
        <div className="px-4 pt-2 pb-0">
          <h4 className="text-xs font-medium text-gray-500 uppercase tracking-wider">
            Original Spectra
          </h4>
        </div>
        <ComparabilityNotice spectra={librarySpectra} />
        <Plot
          data={originalTraces}
          layout={{
            ...SHARED_LAYOUT,
            autosize: true,
            height: showDownsampled ? 350 : 380,
            margin: { l: 55, r: 15, t: 10, b: 45 },
            xaxis: {
              title: { text: 'Wavelength (μm)', font: { size: 11 } },
              showgrid: true,
              gridcolor: '#f3f4f6',
              zeroline: false,
            },
            yaxis: {
              title: { text: yLabel, font: { size: 11 } },
              showgrid: true,
              gridcolor: '#f3f4f6',
              zeroline: false,
              rangemode: 'tozero',
            },
            shapes: sensorShapes,
            legend: {
              orientation: 'h',
              yanchor: 'bottom',
              y: 1.01,
              xanchor: 'left',
              x: 0,
              font: { size: 10 },
            },
          }}
          config={PLOTLY_CONFIG}
          useResizeHandler
          style={{ width: '100%' }}
        />
      </div>

      {/* Downsampled spectra plot — rendered via direct Plotly API for reliable updates */}
      {selectedSensor && downsamplingEnabled && (
        <DownsampledPlot
          traces={downsampledTraces}
          sensor={selectedSensor}
          yLabel={yLabel}
        />
      )}
    </div>
  );
}
