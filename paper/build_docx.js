// Minimal Markdown-subset -> DOCX builder for the paper (docx-js).
// Supports: # / ## / ### headings, paragraphs, **bold**, *italic*, `code`, pipe tables, ![caption](image.png){width},
// "- " bullet lists, "> " (used for the abstract block), [[PAGEBREAK]], and a "REFERENCES" section with hanging indents.
const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType, Table, TableRow, TableCell, WidthType,
  ImageRun, BorderStyle, ShadingType, LevelFormat, PageNumber, Footer, Header, TabStopType,
} = require("docx");

const src = fs.readFileSync(process.argv[2], "utf8");
const out = process.argv[3];
const baseDir = path.dirname(process.argv[2]);
const lines = src.split("\n");

const FONT = "Calibri";
const BODY = 22;   // half-points (11 pt)

function inline(text, opts = {}) {
  // parse **bold**, *italic*, `code`, ^{sup}, _{sub}
  const runs = [];
  const re = /(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`|\^\{[^}]+\}|_\{[^}]+\})/g;
  let last = 0, m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) runs.push(new TextRun({ text: text.slice(last, m.index), font: FONT, size: opts.size || BODY, ...opts.run }));
    const tok = m[0];
    if (tok.startsWith("**")) runs.push(new TextRun({ text: tok.slice(2, -2), bold: true, font: FONT, size: opts.size || BODY, ...opts.run }));
    else if (tok.startsWith("`")) runs.push(new TextRun({ text: tok.slice(1, -1), font: "Consolas", size: (opts.size || BODY) - 2, ...opts.run }));
    else if (tok.startsWith("^{")) runs.push(new TextRun({ text: tok.slice(2, -1), superScript: true, font: FONT, size: opts.size || BODY, ...opts.run }));
    else if (tok.startsWith("_{")) runs.push(new TextRun({ text: tok.slice(2, -1), subScript: true, font: FONT, size: opts.size || BODY, ...opts.run }));
    else runs.push(new TextRun({ text: tok.slice(1, -1), italics: true, font: FONT, size: opts.size || BODY, ...opts.run }));
    last = m.index + tok.length;
  }
  if (last < text.length) runs.push(new TextRun({ text: text.slice(last), font: FONT, size: opts.size || BODY, ...opts.run }));
  return runs;
}

const children = [];
let inRefs = false;
let i = 0;
let figCount = 0;

function para(text, extra = {}) {
  return new Paragraph({ children: inline(text, extra.inlineOpts || {}), alignment: extra.align || AlignmentType.JUSTIFIED,
    spacing: { after: extra.after ?? 120, line: extra.line ?? 276 }, indent: extra.indent, ...extra.p });
}

while (i < lines.length) {
  let line = lines[i];
  if (line.trim() === "") { i++; continue; }
  if (line.startsWith("[[PAGEBREAK]]")) { children.push(new Paragraph({ children: [], pageBreakBefore: true })); i++; continue; }
  if (line.startsWith("```")) {
    // fenced code block: render each interior line as a shaded monospace paragraph
    i++;
    const codeLines = [];
    while (i < lines.length && !lines[i].startsWith("```")) { codeLines.push(lines[i]); i++; }
    i++; // skip closing fence
    for (const cl of codeLines) {
      children.push(new Paragraph({
        children: [new TextRun({ text: cl.length ? cl : " ", font: "Consolas", size: BODY - 2 })],
        shading: { type: ShadingType.CLEAR, fill: "F0F0F0" },
        spacing: { after: 0, line: 240 }, indent: { left: 227 },
      }));
    }
    children.push(new Paragraph({ children: [], spacing: { after: 120 } }));
    continue;
  }
  if (line.startsWith("[[TITLE]] ")) {
    children.push(new Paragraph({ children: inline(line.slice(10), { size: 36, run: { bold: true } }), alignment: AlignmentType.LEFT, spacing: { after: 200, line: 300 } }));
    i++; continue;
  }
  if (line.startsWith("[[AUTHOR]] ")) {
    children.push(new Paragraph({ children: inline(line.slice(11), { size: 24 }), alignment: AlignmentType.LEFT, spacing: { after: 40 } }));
    i++; continue;
  }
  if (line.startsWith("[[AFFIL]] ")) {
    children.push(new Paragraph({ children: inline(line.slice(10), { size: 20, run: { italics: true } }), alignment: AlignmentType.LEFT, spacing: { after: 40 } }));
    i++; continue;
  }
  if (line.startsWith("# ")) {
    const t = line.slice(2);
    inRefs = /^references$/i.test(t.trim());
    children.push(new Paragraph({ children: [new TextRun({ text: t, bold: true, font: FONT, size: 26 })], heading: HeadingLevel.HEADING_1, spacing: { before: 280, after: 120 }, keepNext: true }));
    i++; continue;
  }
  if (line.startsWith("## ")) {
    children.push(new Paragraph({ children: [new TextRun({ text: line.slice(3), bold: true, italics: true, font: FONT, size: 23 })], heading: HeadingLevel.HEADING_2, spacing: { before: 200, after: 100 }, keepNext: true }));
    i++; continue;
  }
  if (line.startsWith("### ")) {
    children.push(new Paragraph({ children: [new TextRun({ text: line.slice(4), italics: true, font: FONT, size: 22 })], heading: HeadingLevel.HEADING_3, spacing: { before: 140, after: 80 }, keepNext: true }));
    i++; continue;
  }
  if (line.startsWith("> ")) {
    // abstract / keyword block: collect consecutive quote lines
    const buf = [];
    while (i < lines.length && lines[i].startsWith("> ")) { buf.push(lines[i].slice(2)); i++; }
    for (const b of buf) {
      if (b.trim() === "") continue;
      children.push(new Paragraph({ children: inline(b, { size: 20 }), alignment: AlignmentType.JUSTIFIED, spacing: { after: 100, line: 260 },
        indent: { left: 567, right: 567 } }));
    }
    continue;
  }
  if (line.startsWith("- ")) {
    while (i < lines.length && lines[i].startsWith("- ")) {
      children.push(new Paragraph({ children: inline(lines[i].slice(2)), numbering: { reference: "bullets", level: 0 }, alignment: AlignmentType.JUSTIFIED, spacing: { after: 80, line: 276 } }));
      i++;
    }
    continue;
  }
  if (line.startsWith("![")) {
    // ![Caption text](file.png){widthInches}
    const m = line.match(/^!\[(.*)\]\((.*)\)\{([\d.]+)\}/);
    if (m) {
      const img = fs.readFileSync(path.resolve(baseDir, m[2]));
      const sizeOf = require("image-size");
      let dims;
      try { dims = sizeOf.imageSize ? sizeOf.imageSize(img) : sizeOf(img); } catch (e) { dims = { width: 1600, height: 1000 }; }
      const wIn = parseFloat(m[3]);
      const w = Math.round(wIn * 96), h = Math.round(w * dims.height / dims.width);
      children.push(new Paragraph({ children: [new ImageRun({ data: img, transformation: { width: w, height: h }, type: "png" })], alignment: AlignmentType.CENTER, spacing: { before: 120, after: 60 }, keepNext: true }));
      children.push(new Paragraph({ children: inline(m[1], { size: 19 }), alignment: AlignmentType.CENTER, spacing: { after: 200, line: 240 }, indent: { left: 400, right: 400 } }));
    }
    i++; continue;
  }
  if (line.startsWith("|")) {
    // pipe table; first line header, second separator; optional preceding caption via "TABLE:" line
    const rows = [];
    while (i < lines.length && lines[i].startsWith("|")) { rows.push(lines[i]); i++; }
    const cells = rows.filter(r => !/^\|\s*-+/.test(r)).map(r => r.slice(1, -1).split("|").map(c => c.trim()));
    const ncol = cells[0].length;
    const total = 9000;
    // content-aware widths: every column is at least as wide as its longest word, so headers and labels never
    // break mid-word; the font steps down from 8.5 pt (to at least 6.5 pt) only if the table cannot fit otherwise
    const plain = (c) => (c || "").replace(/\*\*|`|\*/g, "");
    const longest = (ci) => Math.max(1, ...cells.map((r) => Math.max(0, ...plain(r[ci]).split(/\s+/).map((w) => w.length))));
    const avgLen = (ci) => Math.max(1, cells.reduce((a, r) => a + plain(r[ci]).length, 0) / cells.length);
    let tsz = 17, need = [];
    for (tsz = 17; tsz >= 13; tsz--) {
      need = [...Array(ncol).keys()].map((ci) => longest(ci) * 5.2 * tsz + 170);
      if (need.reduce((a, b) => a + b, 0) <= total) break;
    }
    if (tsz < 13) tsz = 13;
    let needSum = need.reduce((a, b) => a + b, 0);
    if (needSum > total) { need = need.map((n) => n * total / needSum); needSum = total; }
    const wts = [...Array(ncol).keys()].map(avgLen), wsum = wts.reduce((a, b) => a + b, 0);
    const widths = need.map((n, ci) => Math.floor(n + (total - needSum) * wts[ci] / wsum));
    widths[ncol - 1] = total - widths.slice(0, -1).reduce((a, b) => a + b, 0);
    const border = { style: BorderStyle.SINGLE, size: 4, color: "9A9A97" };
    const trows = cells.map((r, ri) => new TableRow({
      tableHeader: ri === 0,
      children: r.map((c, ci) => new TableCell({
        width: { size: widths[ci], type: WidthType.DXA },
        shading: ri === 0 ? { fill: "EDEDEA", type: ShadingType.CLEAR, color: "auto" } : undefined,
        borders: { top: border, bottom: border, left: { style: BorderStyle.NONE, size: 0, color: "FFFFFF" }, right: { style: BorderStyle.NONE, size: 0, color: "FFFFFF" } },
        margins: { top: 40, bottom: 40, left: 70, right: 70 },
        children: [new Paragraph({ children: inline(c, { size: tsz, run: ri === 0 ? { bold: true } : {} }), alignment: ci === 0 ? AlignmentType.LEFT : AlignmentType.CENTER, spacing: { after: 0, line: 230 } })],
      })),
    }));
    children.push(new Table({ rows: trows, width: { size: total, type: WidthType.DXA }, columnWidths: widths }));
    children.push(new Paragraph({ children: [], spacing: { after: 160 } }));
    continue;
  }
  if (line.startsWith("TABLE: ")) {
    children.push(new Paragraph({ children: inline(line.slice(7), { size: 19, run: { bold: false } }), alignment: AlignmentType.LEFT, spacing: { before: 160, after: 80, line: 240 }, keepNext: true }));
    i++; continue;
  }
  // paragraph (may span multiple lines until blank)
  const buf = [line];
  i++;
  while (i < lines.length && lines[i].trim() !== "" && !/^(#|\||!\[|- |> |\[\[|TABLE: )/.test(lines[i])) { buf.push(lines[i]); i++; }
  const text = buf.join(" ");
  if (inRefs) {
    children.push(new Paragraph({ children: inline(text, { size: 19 }), alignment: AlignmentType.LEFT, spacing: { after: 80, line: 250 }, indent: { left: 500, hanging: 500 } }));
  } else {
    children.push(para(text));
  }
}

const doc = new Document({
  creator: "Albin Joseph",
  title: "Adaptive Circuit Breaking and Load Shedding in Reactive Microservices Using JVM Runtime Telemetry",
  styles: {
    default: { document: { run: { font: FONT, size: BODY } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true, run: { size: 26, bold: true, font: FONT, color: "000000" }, paragraph: { spacing: { before: 280, after: 120 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true, run: { size: 23, bold: true, italics: true, font: FONT, color: "000000" }, paragraph: { spacing: { before: 200, after: 100 }, outlineLevel: 1 } },
      { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true, run: { size: 22, italics: true, font: FONT, color: "000000" }, paragraph: { spacing: { before: 140, after: 80 }, outlineLevel: 2 } },
    ],
  },
  numbering: { config: [{ reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 540, hanging: 300 } } } }] }] },
  sections: [{
    properties: { page: { size: { width: 12240, height: 15840 }, margin: { top: 1300, bottom: 1300, left: 1440, right: 1440 } } },
    footers: { default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ children: [PageNumber.CURRENT], font: FONT, size: 18 })] })] }) },
    children,
  }],
});

Packer.toBuffer(doc).then(buf => { fs.writeFileSync(out, buf); console.log("wrote", out, buf.length, "bytes"); });
