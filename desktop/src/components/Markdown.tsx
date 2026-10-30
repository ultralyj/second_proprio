import { useEffect, useId, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { invoke } from "@tauri-apps/api/core";
import { readAsset } from "../api";
function relativeAsset(path: string, src: string) {
  return src.startsWith("/")
    ? src
    : `${path.split("/").slice(0, -1).join("/")}/${decodeURIComponent(src)}`;
}
function LocalImage({
  src,
  alt,
  path,
}: {
  src?: string;
  alt?: string;
  path: string;
}) {
  const [url, setUrl] = useState<string>();
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let mounted = true;
    setUrl(undefined);
    setFailed(false);
    if (!src) return;
    if (/^https?:\/\//.test(src)) {
      setUrl(src);
      return;
    }
    if (src.startsWith("data:")) {
      setFailed(true);
      return;
    }
    readAsset(relativeAsset(path, src))
      .then((value) => mounted && setUrl(value))
      .catch(() => mounted && setFailed(true));
    return () => {
      mounted = false;
    };
  }, [src, path]);
  return failed ? (
    <span className="image-missing">图片不可用：{alt || src}</span>
  ) : url ? (
    <img src={url} alt={alt || ""} loading="lazy" />
  ) : (
    <span className="muted">正在读取图片…</span>
  );
}
function Diagram({ source }: { source: string }) {
  const id = useId().replace(/[^a-zA-Z0-9]/g, "");
  const [svg, setSvg] = useState("");
  const [error, setError] = useState("");
  const [theme, setTheme] = useState(document.documentElement.dataset.theme);
  useEffect(() => {
    const observer = new MutationObserver(() =>
      setTheme(document.documentElement.dataset.theme),
    );
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    let cancelled = false;
    setError("");
    import("mermaid")
      .then(async ({ default: mermaid }) => {
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          theme: theme === "dark" ? "dark" : "neutral",
          fontFamily: "-apple-system, BlinkMacSystemFont, sans-serif",
        });
        const output = await mermaid.render(`diagram-${id}`, source);
        if (!cancelled) setSvg(output.svg);
      })
      .catch(
        () =>
          !cancelled &&
          setError("管线图暂时无法解析，可切换到编辑查看 Mermaid 源码。"),
      );
    return () => {
      cancelled = true;
    };
  }, [source, id, theme]);
  return error ? (
    <div className="diagram-error">
      {error}
      <pre>{source}</pre>
    </div>
  ) : (
    <div className="mermaid" dangerouslySetInnerHTML={{ __html: svg }} />
  );
}
export function Markdown({ content, path }: { content: string; path: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[[rehypeKatex, { throwOnError: false, trust: false }]]}
        components={{
          img: ({ src, alt }) => (
            <LocalImage
              path={path}
              src={typeof src === "string" ? src : undefined}
              alt={alt}
            />
          ),
          a: ({ href, children }) => (
            <a
              href={href}
              onClick={(event) => {
                event.preventDefault();
                if (href && /^https?:\/\//.test(href))
                  void invoke("open_url", { url: href });
                else if (href && !href.startsWith("#"))
                  void invoke("open_path", { path: relativeAsset(path, href) });
                else if (href)
                  document.getElementById(href.slice(1))?.scrollIntoView();
              }}
            >
              {children}
            </a>
          ),
          code: ({ className, children, ...props }) =>
            className === "language-mermaid" ? (
              <Diagram source={String(children).trim()} />
            ) : (
              <code className={className} {...props}>
                {children}
              </code>
            ),
          pre: ({ children }) => (
            <div className="code-container">
              <pre>{children}</pre>
            </div>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
