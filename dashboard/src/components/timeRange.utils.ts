/**
 * Time-range helpers, kept separate from the component that uses them.
 *
 * A module that exports both a component and plain functions defeats React Fast Refresh:
 * the bundler can no longer tell whether a change should hot-swap a component or reload
 * the module, so it reloads and local state is lost on every edit. Splitting them is the
 * fix, and it also makes these testable without rendering anything.
 */

export interface Range {
  start: string;
  end: string;
}

export const PRESETS = [
  { label: "24 hours", hours: 24 },
  { label: "7 days", hours: 24 * 7 },
  { label: "30 days", hours: 24 * 30 },
  { label: "Full archive", hours: 0 },
] as const;

/**
 * `datetime-local` needs a naive local string, not an ISO instant with a zone.
 * `toISOString()` would silently shift the value by the UTC offset.
 */
export function toLocalInput(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}

/**
 * Build a range ending at the archive's last reading.
 *
 * Anchored to the archive rather than to `Date.now()`: this is a 2020 dataset, so a
 * "last 7 days" window measured from today is always empty and reads as a broken page.
 */
export function rangeEndingAt(anchorIso: string, hours: number, archiveStart?: string): Range {
  const end = new Date(anchorIso);
  if (hours === 0 && archiveStart) {
    return { start: toLocalInput(new Date(archiveStart)), end: toLocalInput(end) };
  }
  const start = new Date(end.getTime() - hours * 3600 * 1000);
  return { start: toLocalInput(start), end: toLocalInput(end) };
}
