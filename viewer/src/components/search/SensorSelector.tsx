import { useAppContext } from '../../state/AppContext';
import { ALL_SENSORS } from '../../constants/sensors';

export default function SensorSelector() {
  const { state, dispatch } = useAppContext();

  return (
    <div>
      <label className="text-xs font-medium text-gray-500 uppercase tracking-wider">
        Satellite Sensor
      </label>
      <select
        value={state.selectedSensor?.id ?? ''}
        onChange={(e) => {
          const sensor = ALL_SENSORS.find((s) => s.id === e.target.value) ?? null;
          dispatch({ type: 'SET_SENSOR', sensor });
        }}
        className="mt-1.5 w-full px-3 py-2 text-sm bg-white border border-gray-200 rounded-lg
                   focus:outline-none focus:ring-2 focus:ring-indigo-500"
      >
        <option value="">No sensor (show all spectra)</option>
        {ALL_SENSORS.map((s) => (
          <option key={s.id} value={s.id}>
            {s.name} — {s.description}
          </option>
        ))}
      </select>
      {state.selectedSensor && (
        <p className="mt-1 text-xs text-gray-500">
          {state.selectedSensor.bands.length} bands, {state.selectedSensor.wavelengthMin.toFixed(3)}–{state.selectedSensor.wavelengthMax.toFixed(3)} μm.
          Showing reflectance spectra only.
        </p>
      )}
    </div>
  );
}
