import * as Dialog from "@radix-ui/react-dialog";
import { X, LoaderCircle, AlertCircle, FileText, Check } from "lucide-react";
import type { ReactNode } from "react";
import { statusLabel } from "../api";
export function Modal({
  title,
  description,
  open,
  onClose,
  children,
  wide = false,
}: {
  title: string;
  description?: string;
  open: boolean;
  onClose: () => void;
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={(value) => !value && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="modal-overlay" />
        <Dialog.Content className={`modal ${wide ? "modal-wide" : ""}`}>
          <div className="modal-heading">
            <div>
              <Dialog.Title>{title}</Dialog.Title>
              <Dialog.Description>
                {description || "在此查看并管理你的内容。"}
              </Dialog.Description>
            </div>
            <Dialog.Close className="icon-button" aria-label="关闭">
              <X size={18} />
            </Dialog.Close>
          </div>
          {children}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
export function Empty({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty">
      <FileText size={30} strokeWidth={1.2} />
      <h2>{title}</h2>
      {description && <p>{description}</p>}
      {children}
    </div>
  );
}
export function Loading({ text = "正在读取…" }: { text?: string }) {
  return (
    <div className="loading">
      <LoaderCircle size={16} className="spin" />
      {text}
    </div>
  );
}
export function ErrorNotice({
  message,
  onDismiss,
}: {
  message: string;
  onDismiss?: () => void;
}) {
  return (
    <div className="error-notice" role="alert">
      <AlertCircle size={16} />
      <span>{message}</span>
      {onDismiss && (
        <button
          className="icon-button"
          onClick={onDismiss}
          aria-label="关闭错误"
        >
          <X size={14} />
        </button>
      )}
    </div>
  );
}
export function Status({ value }: { value: string }) {
  return (
    <span className={`status status-${value}`}>
      {["running", "cancel_requested"].includes(value) && (
        <LoaderCircle size={11} className="spin" />
      )}
      {value === "completed" && <Check size={11} />} {statusLabel(value)}
    </span>
  );
}
export function Section({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: ReactNode;
}) {
  return (
    <section className="form-section">
      <h2>{title}</h2>
      {description && <p className="section-description">{description}</p>}
      <div className="form-section-body">{children}</div>
    </section>
  );
}
export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="field">
      <span className="field-label">
        {label}
        {hint && <small>{hint}</small>}
      </span>
      <span className="field-control">{children}</span>
    </label>
  );
}
export function Toggle({
  value,
  onChange,
  label,
}: {
  value: boolean;
  onChange: (value: boolean) => void;
  label: string;
}) {
  return (
    <button
      role="switch"
      aria-checked={value}
      aria-label={label}
      className={`switch ${value ? "on" : ""}`}
      onClick={() => onChange(!value)}
    >
      <span />
    </button>
  );
}
