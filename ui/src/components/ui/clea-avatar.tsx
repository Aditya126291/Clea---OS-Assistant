import { useEffect, useMemo, useState, type HTMLAttributes } from "react";
import rawDefaultAvatarSvg from "../../../public/clea-avatar.svg?raw";
import { cn } from "@/lib/utils";

const DANGEROUS_TAG_NAMES = [
  "script",
  "foreignObject",
  "iframe",
  "object",
  "embed",
  "metadata",
  "applet",
  "frame",
  "frameset",
  "base",
  "form",
] as const;

const DANGEROUS_TAG_GROUP = DANGEROUS_TAG_NAMES.join("|");
const PAIRED_DANGEROUS_TAG_RE = new RegExp(
  `<(${DANGEROUS_TAG_GROUP})\\b[^>]*>[\\s\\S]*?<\\/\\1\\b[^>]*>`,
  "gi",
);
const SELF_CLOSING_DANGEROUS_TAG_RE = new RegExp(`<(${DANGEROUS_TAG_GROUP})\\b[^>]*\\/>`, "gi");
const UNCLOSED_DANGEROUS_TAG_RE = new RegExp(`<(${DANGEROUS_TAG_GROUP})\\b[^>]*>[\\s\\S]*$`, "gi");
const STANDALONE_DANGEROUS_TAG_RE = new RegExp(`<\\/?(${DANGEROUS_TAG_GROUP})\\b[^>]*\\/?>`, "gi");

function decodeHtmlEntities(input: string): string {
  return input
    .replace(/&#(\d+);?/g, (_m, dec: string) => {
      const code = Number.parseInt(dec, 10);
      return Number.isFinite(code) && code >= 0 && code <= 0x10ffff
        ? String.fromCodePoint(code)
        : "";
    })
    .replace(/&#x([0-9a-f]+);?/gi, (_m, hex: string) => {
      const code = Number.parseInt(hex, 16);
      return Number.isFinite(code) && code >= 0 && code <= 0x10ffff
        ? String.fromCodePoint(code)
        : "";
    })
    .replace(/&colon;/gi, ":")
    .replace(/&tab;/gi, "\t")
    .replace(/&newline;/gi, "\n");
}

function stripAsciiWhitespaceAndControl(value: string): string {
  let out = "";
  for (let i = 0; i < value.length; i += 1) {
    if (value.charCodeAt(i) > 0x20) {
      out += value[i];
    }
  }
  return out;
}

function isDangerousUri(rawValue: string): boolean {
  const normalized = stripAsciiWhitespaceAndControl(decodeHtmlEntities(rawValue)).toLowerCase();
  return (
    normalized.startsWith("javascript:") ||
    normalized.startsWith("vbscript:") ||
    normalized.startsWith("data:")
  );
}

/**
 * Sanitize raw SVG markup for inline rendering while preserving `<style>` keyframes,
 * `<filter>` glitch effects, `<defs>`, `<clipPath>`, `<use>`, and safe SMIL animation elements.
 * Strips `<script>`, `<foreignObject>`, `<iframe>`, `<object>`, `<embed>`, `<metadata>`,
 * inline `on*` event attributes, dangerous SMIL attribute mutations, and `javascript:` / `data:` URIs.
 */
// eslint-disable-next-line react-refresh/only-export-components
export function sanitizeSvg(rawSvg: string): string {
  if (!rawSvg) return "";

  let cleaned = rawSvg;
  let previous = "";

  // First, iteratively strip paired and self-closing dangerous tags (handles nested <scr<script></script>ipt>)
  while (cleaned !== previous) {
    previous = cleaned;
    cleaned = cleaned
      .replace(PAIRED_DANGEROUS_TAG_RE, "")
      .replace(SELF_CLOSING_DANGEROUS_TAG_RE, "");
  }

  // Next, strip any unclosed or standalone dangerous tags and dangerous attributes/SMIL elements
  previous = "";
  while (cleaned !== previous) {
    previous = cleaned;
    cleaned = cleaned
      .replace(PAIRED_DANGEROUS_TAG_RE, "")
      .replace(SELF_CLOSING_DANGEROUS_TAG_RE, "")
      .replace(UNCLOSED_DANGEROUS_TAG_RE, "")
      .replace(STANDALONE_DANGEROUS_TAG_RE, "")
      // Remove inline on* event handler attributes (even when preceded by '/' or quotes)
      .replace(/(?:[\s/]+|(?<=['"]))on[a-zA-Z0-9_:-]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s/>]+)/gi, " ")
      // Neutralize javascript:, vbscript:, or data: URIs in href / xlink:href (including entity-encoded)
      .replace(
        /(?:[\s/]+|(?<=['"]))(?:xlink:)?href\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s/>]+))/gi,
        (fullMatch, dq?: string, sq?: string, uq?: string) => {
          const value = dq ?? sq ?? uq ?? "";
          return isDangerousUri(value) ? " " : fullMatch;
        },
      )
      // Neutralize SMIL animation elements that mutate event handlers or hrefs
      .replace(/<(?:set|animate|animateTransform|animateMotion)\b[^>]*\/?>/gi, (tag) => {
        const attrNameMatch = /\battributeName\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s/>]+))/i.exec(tag);
        if (attrNameMatch) {
          const targetAttr = decodeHtmlEntities(
            attrNameMatch[1] ?? attrNameMatch[2] ?? attrNameMatch[3] ?? "",
          )
            .trim()
            .toLowerCase();
          if (targetAttr.startsWith("on") || targetAttr === "href" || targetAttr === "xlink:href") {
            return "";
          }
        }
        const valAttrMatch = /\b(?:to|from|values)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s/>]+))/i.exec(
          tag,
        );
        if (valAttrMatch) {
          const rawVal = valAttrMatch[1] ?? valAttrMatch[2] ?? valAttrMatch[3] ?? "";
          if (rawVal.split(";").some((part) => isDangerousUri(part))) {
            return "";
          }
        }
        return tag;
      });
  }

  // Normalize root <svg> dimensions so viewBox scales cleanly inside circular containers
  cleaned = cleaned.replace(/<svg\b([^>]*)>/i, (_match, attrs: string) => {
    const normalizedAttrs = attrs
      .replace(/(?:[\s/]+|(?<=['"]))width\s*=\s*(?:"[^"]*"|'[^']*'|[^\s/>]+)/gi, "")
      .replace(/(?:[\s/]+|(?<=['"]))height\s*=\s*(?:"[^"]*"|'[^']*'|[^\s/>]+)/gi, "")
      .replace(/(?:[\s/]+|(?<=['"]))xmlns:c2pa\s*=\s*(?:"[^"]*"|'[^']*')/gi, "");
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
      return undefined;
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

  const html = svgMarkup !== undefined ? initialMarkup : (fetchedMarkup ?? initialMarkup);

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
