import { Fragment, useMemo } from 'react';

// A small, dependency-free markdown renderer for Smart Bill AI replies.
// Supports headings, bold/italic, inline code, fenced code, lists, blockquotes,
// links, horizontal rules and GFM-style tables. Anything it does not recognise is
// shown as plain text, so a half-streamed reply still looks tidy.

const HEADING_RE = /^(#{1,6})\s+(.*)$/;
const FENCE_RE = /^\s*(```+|~~~+)(.*)$/;
const QUOTE_RE = /^\s*>\s?/;
const HR_RE = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
const LIST_RE = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const TABLE_SEP_RE = /^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$/;
// Root-relative paths are fine, but //host and /\host are treated by browsers
// as off-site URLs. AI replies can be influenced by OCR text, so keep links local.
const SAFE_LINK_RE = /^(?:https?:\/\/|mailto:|\/(?![\/\\]))/i;

function renderInline(text, keyBase = 'x') {
  if (text === undefined || text === null || text === '') return null;
  const source = String(text);
  const re = /(\*\*)(?=\S)([\s\S]*?\S)\1|(\*)(?=\S)([\s\S]*?\S)\3|`([^`\n]+)`|\[([^\]\n]+)\]\(([^()\s]+)\)|~~(?=\S)([\s\S]*?\S)~~/g;
  const nodes = [];
  let last = 0;
  let n = 0;
  let match;
  while ((match = re.exec(source)) !== null) {
    if (match.index > last) nodes.push(source.slice(last, match.index));
    const key = `${keyBase}-${n++}`;
    if (match[2] !== undefined) {
      nodes.push(<strong key={key}>{renderInline(match[2], key)}</strong>);
    } else if (match[4] !== undefined) {
      nodes.push(<em key={key}>{renderInline(match[4], key)}</em>);
    } else if (match[5] !== undefined) {
      nodes.push(<code key={key}>{match[5]}</code>);
    } else if (match[6] !== undefined) {
      const label = match[6];
      const href = match[7];
      nodes.push(SAFE_LINK_RE.test(href)
        ? <a key={key} href={href} target="_blank" rel="noreferrer">{renderInline(label, key)}</a>
        : <Fragment key={key}>{renderInline(label, key)}</Fragment>);
    } else if (match[8] !== undefined) {
      nodes.push(<del key={key}>{renderInline(match[8], key)}</del>);
    }
    last = re.lastIndex;
  }
  if (last < source.length) nodes.push(source.slice(last));
  return nodes.length ? nodes : null;
}

function splitRow(line) {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|')) s = s.slice(0, -1);
  return s.split('|').map((cell) => cell.trim());
}

function alignments(separator) {
  return splitRow(separator).map((cell) => {
    const left = cell.startsWith(':');
    const right = cell.endsWith(':');
    if (left && right) return 'center';
    if (right) return 'right';
    if (left) return 'left';
    return undefined;
  });
}

function startsBlock(lines, i) {
  const line = lines[i];
  if (!line || !line.trim()) return true;
  if (FENCE_RE.test(line) || HEADING_RE.test(line) || QUOTE_RE.test(line)) return true;
  if (LIST_RE.test(line) || HR_RE.test(line)) return true;
  if (line.includes('|') && i + 1 < lines.length && TABLE_SEP_RE.test(lines[i + 1])) return true;
  return false;
}

function indentOf(line) {
  return (line.match(/^\s*/)?.[0] || '').replace(/\t/g, '  ').length;
}

function parseList(lines, start, baseIndent, keyBase) {
  const first = lines[start].match(LIST_RE);
  const ordered = /^\d/.test(first[2]);
  const startNumber = ordered ? parseInt(first[2], 10) : 1;
  const items = [];
  let i = start;

  while (i < lines.length) {
    const item = lines[i].match(LIST_RE);
    if (!item) break;
    const indent = indentOf(lines[i]);
    if (indent < baseIndent) break;

    if (indent > baseIndent) {
      const [nested, next] = parseList(lines, i, indent, `${keyBase}-n${items.length}`);
      if (items.length) items[items.length - 1].children.push(nested);
      i = next;
      continue;
    }

    const entry = { text: item[3], children: [] };
    i += 1;
    // Indented, non-list lines continue the current item's sentence.
    while (i < lines.length && lines[i].trim() && !LIST_RE.test(lines[i]) && indentOf(lines[i]) > baseIndent) {
      entry.text += ` ${lines[i].trim()}`;
      i += 1;
    }
    items.push(entry);
  }

  const Tag = ordered ? 'ol' : 'ul';
  const node = (
    <Tag key={keyBase} start={ordered && startNumber !== 1 ? startNumber : undefined}>
      {items.map((entry, index) => (
        <li key={`${keyBase}-i${index}`}>
          {renderInline(entry.text, `${keyBase}-i${index}`)}
          {entry.children}
        </li>
      ))}
    </Tag>
  );
  return [node, i];
}

function parseBlocks(text, keyBase = 'b') {
  const lines = String(text ?? '').replace(/\r\n?/g, '\n').split('\n');
  const blocks = [];
  let i = 0;
  let n = 0;
  const nextKey = () => `${keyBase}-${n++}`;

  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i += 1; continue; }

    // Fenced code block.
    const fence = line.match(FENCE_RE);
    if (fence) {
      const marker = fence[1][0];
      const lang = fence[2].trim().split(/\s+/)[0] || '';
      const closeRe = new RegExp(`^\\s*${marker}{3,}\\s*$`);
      const body = [];
      i += 1;
      while (i < lines.length && !closeRe.test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      if (i < lines.length) i += 1;
      blocks.push(
        <pre key={nextKey()} className="md-code">
          <code data-lang={lang || undefined}>{body.join('\n')}</code>
        </pre>,
      );
      continue;
    }

    // Heading. Shift down one level so it never competes with the page title.
    const heading = line.match(HEADING_RE);
    if (heading) {
      const Tag = `h${Math.min(heading[1].length + 1, 6)}`;
      blocks.push(<Tag key={nextKey()} className="md-heading">{renderInline(heading[2], nextKey())}</Tag>);
      i += 1;
      continue;
    }

    // GFM table: a row followed by a |---|---| separator line.
    if (line.includes('|') && i + 1 < lines.length && TABLE_SEP_RE.test(lines[i + 1])) {
      const header = splitRow(line);
      const aligns = alignments(lines[i + 1]);
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].trim() && lines[i].includes('|')) {
        rows.push(splitRow(lines[i]));
        i += 1;
      }
      const width = Math.max(header.length, aligns.length, ...rows.map((row) => row.length));
      const pad = (row) => Array.from({ length: width }, (_, c) => row[c] ?? '');
      blocks.push(
        <div key={nextKey()} className="md-table-wrap">
          <table className="md-table">
            <thead>
              <tr>
                {pad(header).map((cell, c) => (
                  <th key={c} style={aligns[c] ? { textAlign: aligns[c] } : undefined}>
                    {renderInline(cell, `${keyBase}-h${c}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, r) => (
                <tr key={r}>
                  {pad(row).map((cell, c) => (
                    <td key={c} style={aligns[c] ? { textAlign: aligns[c] } : undefined}>
                      {renderInline(cell, `${keyBase}-r${r}c${c}`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }

    // Blockquote.
    if (QUOTE_RE.test(line)) {
      const body = [];
      while (i < lines.length && QUOTE_RE.test(lines[i])) {
        body.push(lines[i].replace(QUOTE_RE, ''));
        i += 1;
      }
      const innerKey = nextKey();
      blocks.push(<blockquote key={innerKey} className="md-quote">{parseBlocks(body.join('\n'), innerKey)}</blockquote>);
      continue;
    }

    // Ordered or unordered list, with nested lists by indentation.
    if (LIST_RE.test(line)) {
      const [node, next] = parseList(lines, i, indentOf(line), nextKey());
      blocks.push(node);
      i = next;
      continue;
    }

    // Horizontal rule.
    if (HR_RE.test(line)) {
      blocks.push(<hr key={nextKey()} className="md-hr" />);
      i += 1;
      continue;
    }

    // Paragraph: single newlines are kept as line breaks, as chat replies expect.
    const paragraph = [line];
    i += 1;
    while (i < lines.length && lines[i].trim() && !startsBlock(lines, i)) {
      paragraph.push(lines[i]);
      i += 1;
    }
    const paragraphKey = nextKey();
    blocks.push(
      <p key={paragraphKey}>
        {paragraph.map((row, index) => (
          <Fragment key={`${paragraphKey}-l${index}`}>
            {index > 0 && <br />}
            {renderInline(row, `${paragraphKey}-l${index}`)}
          </Fragment>
        ))}
      </p>,
    );
  }

  return blocks;
}

export default function Markdown({ text, className = '' }) {
  const blocks = useMemo(() => parseBlocks(String(text ?? '')), [text]);
  return <div className={`md ${className}`.trim()}>{blocks}</div>;
}
