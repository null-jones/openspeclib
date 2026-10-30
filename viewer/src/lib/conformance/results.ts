/**
 * Accumulates findings per rule and turns them into the final, ordered
 * list of rule results. Pure: no DuckDB here.
 */
import { RULES, type RuleSpec } from '../../format/spec';
import type { RuleResult, RuleStatus, Verdict } from './types';

export const MAX_EXAMPLES = 5;

/** Quality-profile rules whose violations are warnings, not failures. */
const WARN_ONLY_QA = new Set(['Q3']);

/**
 * `na`: the rule does not apply (e.g. I1 on a collection file).
 * `blocked`: not checked because a prerequisite failed under another rule.
 * `partial`: part of the rule could not be checked.
 */
export type SkipKind = 'na' | 'blocked' | 'partial';

interface Accum {
  count: number;
  /** Issues that only warn even on a MUST rule (spec section 11 forward compatibility). */
  warnOnlyIssues: boolean;
  unit: string | null;
  examples: string[];
  messages: string[];
  skip: { kind: SkipKind; reason: string } | null;
  notes: string[];
  waived: Array<{ message: string; reason: string }>;
}

export class RuleAccumulator {
  private acc = new Map<string, Accum>();

  private get(id: string): Accum {
    let a = this.acc.get(id);
    if (!a) {
      a = { count: 0, warnOnlyIssues: true, unit: null, examples: [], messages: [], skip: null, notes: [], waived: [] };
      this.acc.set(id, a);
    }
    return a;
  }

  /** Record `count` offending items for `ruleId`. A zero count is ignored. */
  issue(ruleId: string, count: number, message: string, examples: string[] = [], unit = 'rows'): void {
    if (count <= 0) return;
    this.record(ruleId, count, message, examples, unit, false);
  }

  /**
   * Record findings that are warnings whatever the rule's level: things a
   * file of a newer 1.x minor version may legitimately contain (spec section
   * 11), such as vocabulary values or columns this checker does not know.
   */
  warn(ruleId: string, count: number, message: string, examples: string[] = [], unit = 'rows'): void {
    if (count <= 0) return;
    this.record(ruleId, count, message, examples, unit, true);
  }

  private record(ruleId: string, count: number, message: string, examples: string[], unit: string, warnOnly: boolean): void {
    const a = this.get(ruleId);
    if (!warnOnly) a.warnOnlyIssues = false;
    a.count += count;
    // Mixed units (e.g. missing columns and null rows under F2).
    a.unit = a.unit === null || a.unit === unit ? unit : 'problems';
    a.messages.push(message);
    for (const ex of examples) {
      if (a.examples.length >= MAX_EXAMPLES) break;
      if (!a.examples.includes(ex)) a.examples.push(ex);
    }
  }

  /** Mark a rule, or part of it, as not checked. The first reason wins. */
  skip(ruleId: string, kind: SkipKind, reason: string): void {
    const a = this.get(ruleId);
    if (!a.skip) a.skip = { kind, reason };
  }

  /** A quality issue the allowlist waives: listed with its reason, status unaffected. */
  waive(ruleId: string, message: string, reason: string): void {
    this.get(ruleId).waived.push({ message, reason });
  }

  /** A neutral note shown with the rule whatever its status. */
  note(ruleId: string, note: string): void {
    this.get(ruleId).notes.push(note);
  }

  /** Build results for every rule in rules.json, in spec order. */
  results(rules: RuleSpec[] = RULES): RuleResult[] {
    return rules.map((rule) => {
      const a = this.acc.get(rule.id);
      let status: RuleStatus = 'pass';
      let complete = true;
      const messages: string[] = [];
      if (a && a.count > 0) {
        const warnOnly = rule.level === 'should' || WARN_ONLY_QA.has(rule.id) || a.warnOnlyIssues;
        status = warnOnly ? 'warn' : 'fail';
        messages.push(...a.messages);
        if (a.skip) {
          messages.push(`Not fully checked: ${a.skip.reason}`);
          complete = false;
        }
      } else if (a?.skip) {
        if (a.skip.kind === 'partial') {
          messages.push(`Not fully checked: ${a.skip.reason}`);
        } else {
          status = 'skip';
          messages.push(a.skip.reason);
        }
        complete = a.skip.kind === 'na';
      }
      if (a) messages.push(...a.notes);
      return {
        id: rule.id,
        level: rule.level,
        appliesTo: rule.applies_to,
        title: rule.title,
        text: rule.text,
        status,
        complete,
        count: a?.count ?? 0,
        unit: a?.unit ?? 'rows',
        examples: a?.examples ?? [],
        messages,
        waived: a?.waived ?? [],
      };
    });
  }
}

/**
 * `conformant`: no MUST rule fails and every applicable MUST rule was fully
 * checked. `incomplete`: nothing failed, but something could not be checked.
 */
export function verdictOf(results: RuleResult[]): Verdict {
  const must = results.filter((r) => r.level === 'must');
  if (must.some((r) => r.status === 'fail')) return 'not-conformant';
  if (must.some((r) => !r.complete)) return 'incomplete';
  return 'conformant';
}

/** "1 row", "3 rows". */
export function plural(n: number, singular: string, pluralForm = `${singular}s`): string {
  return `${n.toLocaleString('en-US')} ${n === 1 ? singular : pluralForm}`;
}
