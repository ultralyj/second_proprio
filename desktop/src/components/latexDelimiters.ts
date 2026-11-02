// 仅为阅读预览转换 LaTeX 分隔符，原始文档始终保持不变。
function runLength(source: string, at: number, character: string): number {
  let end = at;
  while (source[end] === character) end += 1;
  return end - at;
}
function closingRun(
  source: string,
  from: number,
  character: string,
  size: number,
): number {
  let at = from;
  while (at < source.length) {
    if (
      (character === "`" || size === 1) &&
      source[at] === "\n" &&
      /^\n[ \t]*\n/.test(source.slice(at))
    )
      return -1;
    if (character === "$" && source[at] === "\\") {
      at += 2;
      continue;
    }
    if (source[at] === character) {
      const length = runLength(source, at, character);
      if (length === size) return at;
      at += length;
    } else at += 1;
  }
  return -1;
}
function latexEnd(source: string, from: number, closing: string): number {
  for (let at = from; at < source.length; at += 1) {
    if (source[at] !== "\\") continue;
    if (source[at + 1] === closing) return at;
    at += 1;
  }
  return -1;
}
function fenceEnd(source: string, at: number): number | null {
  const lineEnd = source.indexOf("\n", at);
  const line = source.slice(at, lineEnd < 0 ? source.length : lineEnd);
  const match = line.match(
    /^[ \t]*(?:>[ \t]*)*(?:(?:[-+*]|\d+[.)])[ \t]+)?(`{3,}|~{3,})(.*)$/,
  );
  if (!match || (match[1][0] === "`" && match[2].includes("`"))) return null;
  const character = match[1][0];
  let cursor = lineEnd < 0 ? source.length : lineEnd + 1;
  while (cursor < source.length) {
    const end = source.indexOf("\n", cursor);
    const next = source.slice(cursor, end < 0 ? source.length : end);
    const close = next.match(/^[ \t]*(?:>[ \t]*)*(`+|~+)[ \t]*$/);
    if (
      close &&
      close[1][0] === character &&
      close[1].length >= match[1].length
    )
      return end < 0 ? source.length : end + 1;
    cursor = end < 0 ? source.length : end + 1;
  }
  return source.length;
}
function delimit(body: string, display: boolean): string {
  const longest = Math.max(
    0,
    ...Array.from(body.matchAll(/\$+/g), (match) => match[0].length),
  );
  return "$".repeat(Math.max(display ? 2 : 1, longest + 1));
}
function displayMath(
  source: string,
  start: number,
  end: number,
  body: string,
): string {
  const before = source.slice(source.lastIndexOf("\n", start - 1) + 1, start);
  const prefix = before.match(/^[ \t]*(?:>[ \t]*)*/)?.[0] || "";
  // 补入的显示公式行沿用引用或列表缩进，避免改变文档容器。
  const value = body
    .split("\n")
    .map((line, index) =>
      index > 0 && line.startsWith(prefix) ? line.slice(prefix.length) : line,
    )
    .join("\n")
    .trim();
  const fence = delimit(value, true);
  const after = source.slice(
    end + 2,
    source.indexOf("\n", end + 2) < 0
      ? source.length
      : source.indexOf("\n", end + 2),
  );
  return `${before === prefix ? "" : `\n\n${prefix}`}${fence}\n${prefix}${value.replaceAll("\n", `\n${prefix}`)}\n${prefix}${fence}${after.trim() ? `\n\n${prefix}` : ""}`;
}
export function normalizeLatexDelimiters(source: string): string {
  let output = "";
  let at = 0;
  while (at < source.length) {
    if (at === 0 || source[at - 1] === "\n") {
      const end = fenceEnd(source, at);
      if (end !== null) {
        output += source.slice(at, end);
        at = end;
        continue;
      }
    }
    const character = source[at];
    if (character === "`" || character === "$") {
      const length = runLength(source, at, character);
      const close = closingRun(source, at + length, character, length);
      if (close >= 0) {
        output += source.slice(at, close + length);
        at = close + length;
        continue;
      }
      output += source.slice(at, at + length);
      at += length;
      continue;
    }
    if (character === "\\") {
      const display = source[at + 1] === "[";
      if (display || source[at + 1] === "(") {
        const close = latexEnd(source, at + 2, display ? "]" : ")");
        if (close >= 0) {
          const body = source.slice(at + 2, close);
          if (!body.trim()) {
            output += source.slice(at, close + 2);
            at = close + 2;
            continue;
          }
          const delimiter = delimit(body, false);
          output += display
            ? displayMath(source, at, close, body)
            : `${delimiter}${body}${delimiter}`;
          at = close + 2;
          continue;
        }
      }
      // 成对保留转义字符，尤其是 \\(、\\[、\\$ 与转义反引号。
      output += source.slice(at, at + 2);
      at += 2;
      continue;
    }
    output += character;
    at += 1;
  }
  return output;
}
