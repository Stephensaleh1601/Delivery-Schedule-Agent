"use client";

import { useEffect, useRef, type ReactNode } from "react";
import { cx } from "@/components/ui";

/**
 * A centred dialog.
 *
 * AgentDrawer hand-rolls a scrim, Escape and role="dialog" and gets those three right. It is
 * missing the two things a modal specifically needs: a focus trap, so Tab cannot wander onto the
 * page underneath and leave a keyboard user typing into something they cannot see, and a scroll
 * lock, so the page behind does not slide around under the overlay. Both are here.
 *
 * Rendered only when open -- an `aria-hidden` dialog that stays mounted keeps its content in the
 * accessibility tree of every screen reader that ignores the attribute.
 */
export function Modal({
  open,
  onClose,
  title,
  subtitle,
  children,
  width = 720,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  subtitle?: ReactNode;
  children: ReactNode;
  width?: number;
}) {
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;

    const previouslyFocused = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    const focusable = () =>
      Array.from(
        panel.current?.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), textarea, input, select, [tabindex]:not([tabindex="-1"])',
        ) ?? [],
      ).filter((el) => el.offsetParent !== null);

    focusable()[0]?.focus() ?? panel.current?.focus();

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
        return;
      }
      if (e.key !== "Tab") return;
      // The trap. Without it Tab leaves the dialog for the page behind the scrim, which a
      // keyboard user cannot see and cannot click away.
      const items = focusable();
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };

    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = overflow;
      previouslyFocused?.focus();
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center p-6">
      <div aria-hidden onClick={onClose} className="absolute inset-0 bg-ink/30" />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        style={{ maxWidth: width }}
        className={cx(
          "relative flex max-h-[86vh] w-full flex-col overflow-hidden rounded-[16px]",
          "border border-rail bg-canvas shadow-[var(--shadow-lift)]",
        )}
      >
        <header className="flex items-start justify-between gap-4 border-b border-rail bg-surface px-5 py-3.5">
          <div className="flex flex-col gap-0.5">
            <h2 className="text-[15px] font-semibold text-ink">{title}</h2>
            {subtitle && <div className="text-[12.5px] text-ink-muted">{subtitle}</div>}
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="rounded-[8px] px-2 py-1 text-[13px] text-ink-muted hover:bg-sunk hover:text-ink"
          >
            Close
          </button>
        </header>
        <div className="flex-1 overflow-y-auto px-5 py-4">{children}</div>
      </div>
    </div>
  );
}
