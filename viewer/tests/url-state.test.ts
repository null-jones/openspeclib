import { describe, expect, test, vi } from 'vitest';

vi.mock('../src/constants/sensors', () => ({ ALL_SENSORS: [] }));

import { decodeHashToState, encodeStateToHash, type ShareableState } from '../src/lib/url-state';
import { initialState } from '../src/state/types';

const base: ShareableState = {
  view: initialState.view,
  formatPage: initialState.formatPage,
  formatSection: initialState.formatSection,
  searchText: '',
  selectedCategories: [],
  selectedSources: [],
  selectedQuantity: null,
  selectedGeometries: [],
  hideSimulated: false,
  wavelengthRange: initialState.wavelengthRange,
  selectedSensor: null,
  downsamplingEnabled: true,
  libraryIds: [],
};

describe('view in the URL hash', () => {
  test('the explorer is the default and adds nothing', () => {
    expect(encodeStateToHash(base)).toBe('');
    expect(decodeHashToState('#q=quartz&lib=a,b').view).toBeUndefined();
  });
  test('old explorer links still decode', () => {
    const s = decodeHashToState('#q=quartz&cat=mineral&lib=usgs:NA56%25');
    expect(s.searchText).toBe('quartz');
    expect(s.libraryIds).toEqual(['usgs:NA56%']);
  });
  test('format reference section and checker round-trip', () => {
    const sec = encodeStateToHash({ ...base, view: 'format', formatSection: 'geometry', searchText: 'olivine' });
    expect(sec).toBe('#view=format&sec=geometry&q=olivine');
    expect(decodeHashToState(sec)).toMatchObject({ view: 'format', formatPage: 'reference', formatSection: 'geometry', searchText: 'olivine' });
    const check = encodeStateToHash({ ...base, view: 'format', formatPage: 'check', formatSection: 'geometry' });
    expect(check).toBe('#view=format&page=check');
    expect(decodeHashToState(check)).toMatchObject({ view: 'format', formatPage: 'check' });
  });
  test('quantity, geometry and simulated filters; legacy tech=', () => {
    const h = encodeStateToHash({ ...base, selectedQuantity: 'transmittance', selectedGeometries: ['biconical', 'unknown'], hideSimulated: true });
    expect(h).toBe('#qty=transmittance&geo=biconical%2Cunknown&nosim=1');
    expect(decodeHashToState(h)).toMatchObject({ selectedQuantity: 'transmittance', selectedGeometries: ['biconical', 'unknown'], hideSimulated: true });
    expect(decodeHashToState('#tech=emissivity').selectedQuantity).toBe('emissivity');
    expect(decodeHashToState('#qty=radiance&geo=sideways').selectedQuantity).toBeUndefined();
  });
  test('junk is ignored', () => {
    expect(decodeHashToState('#view=nope').view).toBeUndefined();
    expect(decodeHashToState('#view=format&sec=<script>').formatSection).toBeUndefined();
  });
});
