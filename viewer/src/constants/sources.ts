/** Short labels for the OpenSpecLib collections, where there's little room. */
export const COLLECTION_SHORT_LABELS: Record<string, string> = {
  usgs_splib07: 'USGS',
  ecostress: 'ECOSTRESS',
  relab: 'RELAB',
  asu_tes: 'ASU TES',
  bishop: 'Bishop',
  ecosis: 'EcoSIS',
  ossl: 'OSSL',
};

/** Display name of a collection: a known short label, else the footer title, else the id. */
export function collectionLabel(id: string, title?: string): string {
  return COLLECTION_SHORT_LABELS[id] ?? title ?? id;
}

/** Band count above which a sensor is treated as hyperspectral in the UI. */
export const HYPERSPECTRAL_BAND_THRESHOLD = 30;
