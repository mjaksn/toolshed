// Parser for the text that `pwrstat -status` and `pwrstat -config` print.
//
// This is a deliberate line for line port of the PHP that came before it, quirks
// included, so that anything already consuming the old endpoint sees the same
// JSON. The quirks are called out below and in the README. Where the port would
// otherwise be ambiguous, the PHP wins.
//
// The shape is two levels deep under a fixed top level key:
//
//   { "UPS State": { "<section>": { "<key>": "<value>" } },
//     "UPS Configuration": { ... } }

const SECTION_TITLES = ["UPS State", "UPS Configuration"];

// A label, then a run of at least ten dots, then a value. Greedy `(.+)` takes as
// much as it can while still leaving ten dots behind, so the label captures the
// leading dots of the run and `trimChars` below takes them off again. That is
// how the PHP behaved and the tests pin it.
const ENTRY = /^\s*(.+)\s*\.{10}\s*(.+)$/;

/**
 * PHP's `trim($s, $chars)`: strip only the given characters from both ends,
 * leaving whitespace alone. JavaScript's `String.prototype.trim` is not this,
 * and the difference is load bearing. See the trailing space note in the README.
 */
function trimChars(text, char) {
  let start = 0;
  let end = text.length;
  while (start < end && text[start] === char) start += 1;
  while (end > start && text[end - 1] === char) end -= 1;
  return text.slice(start, end);
}

/**
 * Parse the two command outputs into the nested object the old endpoint served.
 *
 * @param {string} statusOutput  stdout of `pwrstat -status`
 * @param {string} configOutput  stdout of `pwrstat -config`
 * @returns {object}
 */
export function parseUpsInfo(statusOutput, configOutput) {
  const rawSections = [statusOutput, configOutput];
  const parsed = {};

  for (let i = 0; i < SECTION_TITLES.length; i += 1) {
    const lines = String(rawSections[i] ?? "").split("\n");
    let currentSection = "";

    for (let line of lines) {
      if (line.trim() === "") continue;

      // `if (strpos($line, ':'))` in PHP, where a colon at index 0 returns 0 and
      // is therefore falsy. `> 0` rather than `!== -1` keeps that behaviour: a
      // line that opens with a colon does not start a new section.
      if (line.indexOf(":") > 0) {
        // The PHP reassigns the trimmed line here and only here, so a line
        // without a colon keeps its indentation and relies on the `^\s*` in the
        // pattern instead.
        line = line.trim();
        currentSection = trimChars(line, ":");
      }

      const match = ENTRY.exec(line);
      if (!match) continue;

      const key = trimChars(match[1], ".");
      const value = match[2].trim();

      const title = SECTION_TITLES[i];
      parsed[title] ??= {};
      parsed[title][currentSection] ??= {};
      parsed[title][currentSection][key] = value;
    }
  }

  return parsed;
}
