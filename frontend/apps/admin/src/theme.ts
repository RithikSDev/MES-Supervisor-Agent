import { createTheme, type MantineColorsTuple } from "@mantine/core";

// Admin workspace (2026-09-29 redesign): white surfaces, ink text, deep teal for brand and primary actions —
// replaces the earlier corporate-blue identity for a more distinctive, industrial feel on a persistent sidebar
// layout. Headings use Big Shoulders Display (bold condensed, foundry/press-plate feel) in place of the earlier
// serif, set uppercase with wide tracking to read as signage rather than a document. Status colours keep their
// meaning (red = abnormal, amber = attention, green = confirmed); amber is darkened so it stays readable on
// white. The operator terminal keeps its own dark high-contrast theme (@mes/ui).
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
    fontFamily: "'Big Shoulders Display', sans-serif",
    fontWeight: "700",
    sizes: {
      h1: { fontSize: "3.1rem", lineHeight: "1.05" },
      h2: { fontSize: "2.3rem", lineHeight: "1.1" },
      h3: { fontSize: "1.6rem", lineHeight: "1.15" },
    },
  },
  defaultRadius: "sm",
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
