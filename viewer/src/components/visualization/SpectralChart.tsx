import { useMemo } from 'react';
import Plotly from 'plotly.js-dist-min';
import createPlotlyComponent from 'react-plotly.js/factory';
import { useAppContext } from '../../state/AppContext';
import { toMicrometersWithValues } from '../../lib/wavelength-utils';

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

export default function SpectralChart() {
  const { state } = useAppContext();
  const { librarySpectra, selectedSensor, downsamplingEnabled, downsampledData } = state;

  const showDownsampled = downsamplingEnabled && !!selectedSensor && downsampledData.size > 0;
  const isHyperspectral = selectedSensor ? selectedSensor.bands.length > 30 : false;

  // Original spectra traces
  const { originalTraces, sensorShapes } = useMemo(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const traces: any[] = [];
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const shapes: any[] = [];

    librarySpectra.forEach((spectrum, idx) => {
      const color = COLORS[idx % COLORS.length];
      const { wavelengths, values } = toMicrometersWithValues(
        spectrum.wavelengths,
        spectrum.values,
        spectrum.wavelength_unit,
      );
      const cleanValues = values.map((v) => (v < -1e10 || v > 1e10 ? null : v));

      traces.push({
        x: wavelengths,
        y: cleanValues,
        type: 'scatter',
        mode: 'lines',
        name: spectrum.material_name,
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
  }, [librarySpectra, selectedSensor, downsamplingEnabled, isHyperspectral]);

  // Downsampled spectra traces (separate plot)
  const downsampledTraces = useMemo(() => {
    if (!showDownsampled) return [];

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const traces: any[] = [];

    librarySpectra.forEach((spectrum, idx) => {
      const color = COLORS[idx % COLORS.length];
      const ds = downsampledData.get(spectrum.id);
      if (!ds) return;

      const validBands = ds.filter((b) => b.value !== null);
      const mode = isHyperspectral ? 'lines' : 'lines+markers';

      traces.push({
        x: validBands.map((b) => b.centerWavelength),
        y: validBands.map((b) => b.value!),
        type: 'scatter',
        mode,
        name: spectrum.material_name,
        line: { color, width: 2 },
        marker: isHyperspectral
          ? undefined
          : { color, size: 6, symbol: 'diamond', line: { color: 'white', width: 1 } },
        hovertemplate: '%{text}<br>%{x:.4f} μm<br>%{y:.4f}<extra>%{fullData.name}</extra>',
        text: validBands.map((b) => b.bandName),
      });
    });

    return traces;
  }, [librarySpectra, downsampledData, showDownsampled, isHyperspectral, selectedSensor]);

  if (librarySpectra.length === 0) {
    return (
      <div className="bg-white rounded-lg border border-gray-200 p-8 text-center text-gray-400">
        <svg className="w-12 h-12 mx-auto mb-3 text-gray-300" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
            d="M7 12l3-3 3 3 4-4M8 21l4-4 4 4M3 4h18M4 4h16v12a1 1 0 01-1 1H5a1 1 0 01-1-1V4z" />
        </svg>
        <p className="text-sm font-medium">No spectra to visualize</p>
        <p className="text-xs mt-1">Add spectra to your library to see them plotted here</p>
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
              title: { text: 'Reflectance', font: { size: 11 } },
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

      {/* Downsampled spectra plot (shown only when downsampling is active) */}
      {showDownsampled && (
        <div key={selectedSensor!.id} className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          <div className="px-4 pt-2 pb-0">
            <h4 className="text-xs font-medium text-gray-500 uppercase tracking-wider">
              {selectedSensor!.name} — Downsampled
              <span className="font-normal text-gray-400 ml-1">
                ({selectedSensor!.bands.length} bands)
              </span>
            </h4>
          </div>
          <Plot
            data={downsampledTraces}
            layout={{
              ...SHARED_LAYOUT,
              autosize: true,
              height: 350,
              margin: { l: 55, r: 15, t: 10, b: 45 },
              xaxis: {
                title: { text: 'Wavelength (μm)', font: { size: 11 } },
                showgrid: true,
                gridcolor: '#f3f4f6',
                zeroline: false,
                range: [
                  selectedSensor!.wavelengthMin - 0.02,
                  selectedSensor!.wavelengthMax + 0.02,
                ],
              },
              yaxis: {
                title: { text: 'Reflectance', font: { size: 11 } },
                showgrid: true,
                gridcolor: '#f3f4f6',
                zeroline: false,
                rangemode: 'tozero',
              },
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
      )}
    </div>
  );
}
