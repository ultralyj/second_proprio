/** 独立保存论文的个人评分，不改写论文解读正文。 */
import { useEffect, useState } from "react";
import { Star } from "lucide-react";
import { rpc, errorText } from "../api";
import "./PaperRating.css";

export default function PaperRating({
  arxivId,
  rating,
  onChanged,
}: {
  arxivId: string;
  rating?: number | null;
  onChanged?: () => void;
}) {
  const [score, setScore] = useState(rating ?? null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => setScore(rating ?? null), [rating]);

  async function save(value: number | null) {
    setBusy(true);
    setError("");
    try {
      const result = await rpc<{ rating: number | null }>("content.rate", {
        arxiv_id: arxivId,
        score: value,
      });
      setScore(result.rating);
      onChanged?.();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="paper-rating">
      <span>我的评分</span>
      <div role="group" aria-label="论文评分，1 至 5 分">
        {[1, 2, 3, 4, 5].map((value) => (
          <button
            key={value}
            className={`icon-button rating-star ${value <= (score ?? 0) ? "filled" : ""}`}
            aria-label={`${value} 分`}
            aria-pressed={score === value}
            title={`${value} 分`}
            disabled={busy}
            onClick={() => void save(value)}
          >
            <Star size={16} />
          </button>
        ))}
      </div>
      <span aria-live="polite">
        {busy ? "保存中…" : score ? `${score} / 5` : "未评分"}
      </span>
      {score !== null && (
        <button
          className="quiet-button"
          disabled={busy}
          onClick={() => void save(null)}
        >
          清除
        </button>
      )}
      {error && (
        <span role="alert" className="rating-error">
          {error}
        </span>
      )}
    </div>
  );
}
