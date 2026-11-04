// Markdown → HTML for the AI Chat's answers, with code references turned into
// links: a function name in backticks (`Motor_Init`), `file.c:123`, or a bare
// `Motor_Init()` / file.c:123 in the text becomes an <a class="code-ref"> the
// chat opens in its editor. Fenced code blocks get a language tag and a Copy
// button; the chat colours them with Monaco after rendering (see AIChat).
//
// Escape-first, like markdown.ts: the model's text never becomes markup.

import { escapeHtml } from "./markdown";

export interface CodeRef {
  file: string;
  line: number;
}

/** What the answer can link to: the release's functions and source files. */
export interface RefIndex {
  functions: Map<string, CodeRef>;
  /** rel_path → rel_path, plus lower-cased basename → rel_path (unique only). */
  files: Map<string, string>;
}

export const EMPTY_REFS: RefIndex = { functions: new Map(), files: new Map() };

export function buildRefIndex(
  files: string[],
  functions: { name: string; rel_path: string; line: number }[],
): RefIndex {
  const fileMap = new Map<string, string>();
  const baseCount = new Map<string, number>();
  for (const f of files) {
    fileMap.set(f, f);
    const b = f.split("/").pop()!.toLowerCase();
    baseCount.set(b, (baseCount.get(b) ?? 0) + 1);
  }
  for (const f of files) {
    const b = f.split("/").pop()!.toLowerCase();
    if (baseCount.get(b) === 1) fileMap.set(b, f);
  }
  const fnMap = new Map<string, CodeRef>();
  for (const fn of functions) {
    if (!fnMap.has(fn.name)) fnMap.set(fn.name, { file: fn.rel_path, line: fn.line });
  }
  return { functions: fnMap, files: fileMap };
}

const SRC_EXT = "c|h|cpp|hpp|cc|hh|cxx|hxx|s|asm|inc|py|rs|ld|lsl";
const FILE_REF_RE = new RegExp(
  `^((?:[\\w.\\-]+/)*[\\w.\\-]+\\.(?:${SRC_EXT}))(?::(\\d+)(?:-(\\d+))?)?$`,
  "i",
);
const TEXT_FILE_REF_RE = new RegExp(
  `(^|[\\s(\\[])((?:[\\w.\\-]+/)*[\\w\\-]+\\.(?:${SRC_EXT}))(?::(\\d+)(?:-(\\d+))?)?(?=$|[\\s,.;:)\\]])`,
  "gi",
);

export function resolveFile(refs: RefIndex, path: string): string | null {
  const p = path.replace(/^\.?\//, "");
  if (refs.files.has(p)) return refs.files.get(p)!;
  const low = p.toLowerCase();
  for (const f of refs.files.values()) {
    const fl = f.toLowerCase();
    if (fl.endsWith("/" + low) || fl === low) return f;
  }
  const base = low.split("/").pop()!;
  return refs.files.get(base) ?? null;
}

function attr(s: string): string {
  return escapeHtml(s).replace(/"/g, "&quot;");
}

function fileLink(file: string, line: number | null, end: number | null, label: string): string {
  return (
    `<a class="code-ref" data-file="${attr(file)}"` +
    (line ? ` data-line="${line}"` : "") +
    (end ? ` data-end="${end}"` : "") +
    ` title="Open ${attr(file)}${line ? ":" + line : ""}">${label}</a>`
  );
}

function fnLink(name: string, ref: CodeRef, label: string): string {
  return (
    `<a class="code-ref fn" data-fn="${attr(name)}" data-file="${attr(ref.file)}"` +
    ` data-line="${ref.line}" title="${attr(ref.file)}:${ref.line}">${label}</a>`
  );
}

// One inline `code` span → a link when it names a function or a source file.
function codeSpan(raw: string, refs: RefIndex): string {
  const code = `<code>${escapeHtml(raw)}</code>`;
  const fm = FILE_REF_RE.exec(raw.trim());
  if (fm) {
    const file = resolveFile(refs, fm[1]);
    if (file) {
      return fileLink(file, fm[2] ? +fm[2] : null, fm[3] ? +fm[3] : null, code);
    }
  }
  const name = raw.trim().replace(/\(\s*\)$/, "").replace(/\(.*\)$/, "");
  const ref = refs.functions.get(name);
  if (ref) return fnLink(name, ref, code);
  return code;
}

export function renderChatInline(s: string, refs: RefIndex): string {
  // Pull code spans out first so nothing inside them is touched.
  const codes: string[] = [];
  let text = s.replace(/`([^`]+)`/g, (_m, c: string) => {
    codes.push(codeSpan(c, refs));
    return `\u0000${codes.length - 1}\u0000`;
  });
  // [label](url): keep the label, show the url on hover (no navigation).
  const links: string[] = [];
  text = text.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_m, label: string, url: string) => {
    links.push(`<span class="chat-link" title="${attr(url)}">${escapeHtml(label)}</span>`);
    return `\u0001${links.length - 1}\u0001`;
  });
  text = escapeHtml(text)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_]+)__/g, "<strong>$1</strong>")
    .replace(/(^|[^*\w])\*([^*\s][^*]*)\*(?!\w)/g, "$1<em>$2</em>");
  // Bare file references and Name() calls in the prose.
  text = text.replace(TEXT_FILE_REF_RE, (m, pre: string, path: string, l1?: string, l2?: string) => {
    const file = resolveFile(refs, path);
    if (!file) return m;
    const label = path + (l1 ? `:${l1}` : "") + (l2 ? `-${l2}` : "");
    return pre + fileLink(file, l1 ? +l1 : null, l2 ? +l2 : null, label);
  });
  if (refs.functions.size) {
    text = text.replace(/\b([A-Za-z_]\w{2,})\(\)/g, (m, name: string) => {
      const ref = refs.functions.get(name);
      return ref ? fnLink(name, ref, `${name}()`) : m;
    });
  }
  return text
    .replace(/\u0001(\d+)\u0001/g, (_m, i: string) => links[+i])
    .replace(/\u0000(\d+)\u0000/g, (_m, i: string) => codes[+i]);
}

function splitRow(line: string): string[] {
  let t = line.trim();
  if (t.startsWith("|")) t = t.slice(1);
  if (t.endsWith("|")) t = t.slice(0, -1);
  return t.split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
}

const LANG_ALIAS: Record<string, string> = {
  c: "c", h: "c", cpp: "cpp", "c++": "cpp", hpp: "cpp", cc: "cpp",
  py: "python", python: "python", js: "javascript", javascript: "javascript",
  ts: "typescript", typescript: "typescript", json: "json", sh: "shell",
  bash: "shell", shell: "shell", asm: "asm", ini: "ini", xml: "xml",
  yaml: "yaml", yml: "yaml", md: "markdown", markdown: "markdown",
  makefile: "plaintext", make: "plaintext", text: "plaintext", txt: "plaintext",
};

function codeBlock(lang: string, lines: string[], open: boolean): string {
  const id = LANG_ALIAS[lang.toLowerCase()] ?? (lang ? lang.toLowerCase() : "c");
  return (
    `<div class="chat-code${open ? " streaming" : ""}">` +
    `<div class="chat-code-h"><span>${escapeHtml(lang || "code")}</span>` +
    `<button type="button" class="chat-code-copy" data-copy>Copy</button></div>` +
    `<pre><code data-lang="${attr(id)}">${escapeHtml(lines.join("\n"))}</code></pre></div>`
  );
}

export function renderChatMarkdown(md: string, refs: RefIndex): string {
  const lines = (md ?? "").replace(/\r\n/g, "\n").split("\n");
  const out: string[] = [];
  let list: "ul" | "ol" | null = null;
  let para: string[] = [];

  const flushPara = () => {
    if (para.length) {
      out.push(`<p>${para.map((l) => renderChatInline(l, refs)).join("<br>")}</p>`);
      para = [];
    }
  };
  const closeList = () => {
    if (list) {
      out.push(`</${list}>`);
      list = null;
    }
  };

  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    const fence = /^\s*```\s*([\w+#.-]*)/.exec(raw);
    if (fence) {
      flushPara();
      closeList();
      const buf: string[] = [];
      let j = i + 1;
      while (j < lines.length && !/^\s*```\s*$/.test(lines[j])) buf.push(lines[j++]);
      out.push(codeBlock(fence[1], buf, j >= lines.length));
      i = j;
      continue;
    }
    const line = raw.trimEnd();
    // Table: a header row, then a |---|---| separator.
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length
        && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(lines[i + 1])) {
      flushPara();
      closeList();
      const head = splitRow(line);
      const rows: string[][] = [];
      let j = i + 2;
      while (j < lines.length && /^\s*\|.*\|?\s*$/.test(lines[j]) && lines[j].trim()) {
        rows.push(splitRow(lines[j]));
        j++;
      }
      out.push(
        '<div class="chat-table"><table><thead><tr>' +
          head.map((h) => `<th>${renderChatInline(h, refs)}</th>`).join("") +
          "</tr></thead><tbody>" +
          rows
            .map((r) => "<tr>" + head.map((_h, k) => `<td>${renderChatInline(r[k] ?? "", refs)}</td>`).join("") + "</tr>")
            .join("") +
          "</tbody></table></div>",
      );
      i = j - 1;
      continue;
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    const bullet = /^(\s*)[-*+]\s+(.*)$/.exec(line);
    const numbered = /^(\s*)(\d+)[.)]\s+(.*)$/.exec(line);
    const quote = /^\s*>\s?(.*)$/.exec(line);
    if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) {
      flushPara();
      closeList();
      out.push("<hr />");
    } else if (heading) {
      flushPara();
      closeList();
      const level = Math.min(6, heading[1].length + 2); // keep headings small
      out.push(`<h${level}>${renderChatInline(heading[2], refs)}</h${level}>`);
    } else if (bullet || numbered) {
      flushPara();
      const kind = bullet ? "ul" : "ol";
      if (list !== kind) {
        closeList();
        out.push(numbered && numbered[2] !== "1" ? `<ol start="${numbered[2]}">` : `<${kind}>`);
        list = kind;
      }
      const indent = (bullet ? bullet[1] : numbered![1]).length;
      const body = bullet ? bullet[2] : numbered![3];
      out.push(
        `<li${indent >= 2 ? ' class="nested"' : ""}>${renderChatInline(body, refs)}</li>`,
      );
    } else if (quote) {
      flushPara();
      closeList();
      out.push(`<blockquote>${renderChatInline(quote[1], refs)}</blockquote>`);
    } else if (line.trim() === "") {
      flushPara();
      closeList();
    } else {
      closeList();
      para.push(line);
    }
  }
  flushPara();
  closeList();
  return out.join("\n");
}
