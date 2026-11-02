import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Markdown } from "../components/Markdown";
import { normalizeLatexDelimiters } from "../components/latexDelimiters";
function preview(source: string) {
  return render(<Markdown path="papers/example/README.md" content={source} />)
    .container;
}
describe("实际 Markdown 预览中的 LaTeX 分隔符", () => {
  it("使用 KaTeX 渲染圆括号与方括号公式并保留原文", () => {
    const source = String.raw`策略为 \(\pi_\theta(a_t\mid s_t)\)，损失如下。

\[\mathcal{L}_{\mathrm{BC}} = -\sum_{t=1}^{T} \log \pi_\theta(a_t \mid s_t)\]`;
    const original = source;
    const container = preview(source);
    expect(container.querySelectorAll(".katex")).toHaveLength(2);
    expect(container.querySelectorAll(".katex-display")).toHaveLength(1);
    expect(container.querySelector(".katex-error")).toBeNull();
    expect(
      Array.from(
        container.querySelectorAll('annotation[encoding="application/x-tex"]'),
      ).map((node) => node.textContent),
    ).toEqual([
      String.raw`\pi_\theta(a_t\mid s_t)`,
      String.raw`\mathcal{L}_{\mathrm{BC}} = -\sum_{t=1}^{T} \log \pi_\theta(a_t \mid s_t)`,
    ]);
    expect(source).toBe(original);
  });
  it("兼容美元分隔符公式且不改写公式内部", () => {
    const existing = String.raw`$x_1 + y_2$

$$
\sum_{t=1}^{T} r_t
$$`;
    expect(normalizeLatexDelimiters(existing)).toBe(existing);
    const container = preview(existing + "\n\n" + String.raw`\(z_3\)`);
    expect(container.querySelectorAll(".katex")).toHaveLength(3);
    expect(container.querySelectorAll(".katex-display")).toHaveLength(1);
    const nested = String.raw`$\text{literal \( and \[ stay unchanged}$`;
    expect(normalizeLatexDelimiters(nested)).toBe(nested);
  });
  it("保留行内代码、两类代码围栏和转义分隔符", () => {
    const inline = String.raw`\(inline_code\)`;
    const fenced = String.raw`\[fenced_code\]`;
    const source = [
      "`" + inline + "`",
      "",
      "```python",
      fenced,
      "```",
      "",
      "~~~text",
      inline,
      "~~~",
      "",
      String.raw`\\(literal\\) and \\[literal\\]`,
    ].join("\n");
    expect(normalizeLatexDelimiters(source)).toBe(source);
    const container = preview(source);
    expect(container.querySelector(".katex")).toBeNull();
    expect(
      Array.from(container.querySelectorAll("code")).map((node) =>
        node.textContent?.trim(),
      ),
    ).toEqual([inline, fenced, inline]);
  });
  it("正确处理多反引号、引用中的代码围栏及未配对分隔符", () => {
    const source = [
      "`` ` " + String.raw`\(code\)` + " ` ``",
      "",
      "> ```text",
      "> " + String.raw`\[code\]`,
      "> ```",
      "",
      String.raw`Unclosed \(x_1`,
    ].join("\n");
    expect(normalizeLatexDelimiters(source)).toBe(source);
    expect(preview(source).querySelector(".katex")).toBeNull();
  });
  it("引用内的显示公式不脱离原有容器", () => {
    const container = preview(String.raw`> \[x_1 + y_2\]`);
    expect(container.querySelector("blockquote .katex-display")).not.toBeNull();
    expect(container.querySelectorAll(".katex")).toHaveLength(1);
  });
});
