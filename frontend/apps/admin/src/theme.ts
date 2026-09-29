import { createTheme, type MantineColorsTuple } from "@mantine/core";

// Admin workspace (2026-09-29 redesign, refined into a "modern SaaS product" pass — user reference points:
// Linear/Vercel/Stripe): white surfaces, ink text, deep teal for brand and primary actions, persistent sidebar.
// Headings use Geist Sans (Vercel's typeface) at restrained sizes/weights instead of the earlier oversized
// uppercase display treatment — normal case, tight tracking, quiet. Every interactive element gets a real
// hover/press transition (see styles.css) instead of static states. Status colours keep their meaning
// (red = abnormal, amber = attention, green = confirmed); amber is darkened so it stays readable on white.
// The operator terminal keeps its own dark high-contrast theme (@mes/ui).
const brand: MantineColorsTuple = [
  "#e6f7f5", "#c7ece7", "#96dad2", "#61c7bb", "#38b6a8", "#1da395", "#0f8a7d", "#0c6f66", "#095650", "#063d39",
];
const amber: MantineColorsTuple = [
  "#fff8e1", "#ffecb3", "#ffe082", "#ffd54f", "#f5b400", "#d99a00", "#b37f00", "#8f6500", "#6b4c00", "#4a3400",
];
const navy: MantineColorsTuple = [
  "#eef2f1", "#dde5e3", "#c2cecb", "#9dadaa", "#78908c", "#5a716d", "#455855", "#2d3f3c", "#1a2b29", "#0e1f1d",
];

export const theme = createTheme({
  primaryColor: "brand",
  primaryShade: 6,
  autoContrast: true,
  colors: { brand, amber, navy },
  black: "#0e1f1d",
  fontFamily: "'IBM Plex Sans', sans-serif",
  fontFamilyMonospace: "'IBM Plex Mono', monospace",
  headings: {
    fontFamily: "'Geist Sans', 'IBM Plex Sans', sans-serif",
    fontWeight: "600",
    sizes: {
      h1: { fontSize: "2.5rem", lineHeight: "1.15" },
      h2: { fontSize: "1.9rem", lineHeight: "1.2" },
      h3: { fontSize: "1.35rem", lineHeight: "1.25" },
    },
  },
  defaultRadius: "md",
  fontSizes: { xs: "0.74rem", sm: "0.82rem", md: "0.9rem", lg: "1rem", xl: "1.15rem" },
  components: {
    Button: { defaultProps: { size: "xs" } },
    Badge: { defaultProps: { radius: "sm" } },
    Table: { defaultProps: { verticalSpacing: 7, horizontalSpacing: "sm", highlightOnHover: true } },
    Paper: { defaultProps: { radius: "md" } },
    Drawer: { defaultProps: { overlayProps: { backgroundOpacity: 0.25, blur: 2 } } },
    Modal: { defaultProps: { overlayProps: { backgroundOpacity: 0.25, blur: 2 } } },
  },
});
