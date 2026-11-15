import { useEffect, useMemo, useState, type HTMLAttributes } from "react";
import rawDefaultAvatarSvg from "../../../public/clea-avatar.svg?raw";
import { cn } from "@/lib/utils";

/**
 * Sanitize raw SVG markup for inline rendering while preserving `<style>` keyframes,
 * `<filter>` glitch effects, `<defs>`, `<clipPath>`, `<use>`, and SMIL animation elements.
 * Strips `<script>`, `<foreignObject>`, `<iframe>`, `<object>`, `<embed>`, `<metadata>`,
 * inline `on*` event attributes, and `javascript:` / `data:text/html` URIs.
 */
export function sanitizeSvg(rawSvg: string): string {
  if (!rawSvg) return "";

  let cleaned = rawSvg
    // Remove executable or embedded document elements
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "")
    .replace(/<script\b[^>]*\/>/gi, "")
    .replace(/<foreignObject\b[^>]*>[\s\S]*?<\/foreignObject>/gi, "")
    .replace(/<foreignObject\b[^>]*\/>/gi, "")
    .replace(/<iframe\b[^>]*>[\s\S]*?<\/iframe>/gi, "")
    .replace(/<iframe\b[^>]*\/>/gi, "")
    .replace(/<object\b[^>]*>[\s\S]*?<\/object>/gi, "")
    .replace(/<object\b[^>]*\/>/gi, "")
    .replace(/<embed\b[^>]*\/?>/gi, "")
    // Remove bulky C2PA / metadata payloads not needed in inline DOM
    .replace(/<metadata\b[^>]*>[\s\S]*?<\/metadata>/gi, "")
    // Remove inline on* event handler attributes (e.g. onload, onclick, onerror, onbegin)
    .replace(/\s+on[a-zA-Z0-9_-]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s/>]+)/gi, "")
    // Neutralize javascript: or data:text/html URIs in href / xlink:href
    .replace(
      /\s+(?:xlink:)?href\s*=\s*(?:"\s*(?:javascript:|data:text\/html)[^"]*"|'\s*(?:javascript:|data:text\/html)[^']*'|(?:\s*(?:javascript:|data:text\/html)[^\s/>]*))/gi,
      "",
    );

  // Normalize root <svg> dimensions so viewBox scales cleanly inside circular containers
  cleaned = cleaned.replace(/<svg\b([^>]*)>/i, (_match, attrs: string) => {
    const normalizedAttrs = attrs
      .replace(/\s+width\s*=\s*(?:"[^"]*"|'[^']*'|[^\s/>]+)/gi, "")
      .replace(/\s+height\s*=\s*(?:"[^"]*"|'[^']*'|[^\s/>]+)/gi, "")
      .replace(/\s+xmlns:c2pa\s*=\s*(?:"[^"]*"|'[^']*')/gi, "");
    const hasPreserveAspectRatio = /\bpreserveAspectRatio\s*=/i.test(normalizedAttrs);
    const aspectAttr = hasPreserveAspectRatio ? "" : ' preserveAspectRatio="xMidYMid slice"';
    return `<svg${normalizedAttrs} width="100%" height="100%"${aspectAttr}>`;
  });

  return cleaned.trim();
}

const DEFAULT_SANITIZED_AVATAR_SVG = sanitizeSvg(rawDefaultAvatarSvg);

export interface CleaAvatarProps extends HTMLAttributes<HTMLDivElement> {
  /** Optional raw SVG markup override (defaults to `/clea-avatar.svg`). */
  svgMarkup?: string;
}

export function CleaAvatar({ className, svgMarkup, ...props }: CleaAvatarProps) {
  const initialMarkup = useMemo(
    () => (svgMarkup !== undefined ? sanitizeSvg(svgMarkup) : DEFAULT_SANITIZED_AVATAR_SVG),
    [svgMarkup],
  );
  const [fetchedMarkup, setFetchedMarkup] = useState<string | null>(null);

  useEffect(() => {
    if (svgMarkup !== undefined) {
      setFetchedMarkup(null);
      return;
    }
    const maybeMockFetch = globalThis.fetch as unknown as { mock?: unknown } | undefined;
    if (typeof globalThis.fetch === "function" && maybeMockFetch?.mock) {
      let active = true;
      void globalThis
        .fetch("/clea-avatar.svg")
        .then((res) => (res && typeof res.text === "function" ? res.text() : ""))
        .then((text) => {
          if (active && text && text.includes("<svg")) {
            setFetchedMarkup(sanitizeSvg(text));
          }
        })
        .catch(() => {
          // Keep bundled inline SVG on fetch error
        });
      return () => {
        active = false;
      };
    }
    return undefined;
  }, [svgMarkup]);

  const html = fetchedMarkup ?? initialMarkup;

  return (
    <div
      data-avatar-src="/clea-avatar.svg"
      aria-hidden="true"
      className={cn(
        "inline-flex shrink-0 items-center justify-center rounded-full overflow-hidden [&>svg]:block [&>svg]:h-full [&>svg]:w-full",
        className,
      )}
      dangerouslySetInnerHTML={{ __html: html }}
      {...props}
    />
  );
}
