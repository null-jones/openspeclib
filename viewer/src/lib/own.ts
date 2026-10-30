/**
 * `obj[key]` when `key` is the object's own property, else undefined. For
 * lookups keyed by values from data files: a value such as `constructor` or
 * `__proto__` must not reach Object.prototype.
 */
export function own<T>(obj: Readonly<Record<string, T>> | null | undefined, key: string | null | undefined): T | undefined {
  return obj != null && key != null && Object.hasOwn(obj, key) ? obj[key] : undefined;
}
