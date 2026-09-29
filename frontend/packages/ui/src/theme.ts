import { createTheme, type MantineColorsTuple } from "@mantine/core";

// High-performance HMI palette (ISA-101 spirit): the chrome is graphite and grey; colour is reserved for
// state that needs attention. Amber = "look here" (primary actions, holds, pending), red = abnormal.
const amber: MantineColorsTuple = [
  "#fff8e1", "#ffedb8", "#ffe08a", "#ffd25a", "#ffc531", "#f2a900", "#d49400", "#ad7900", "#855d00", "#5e4200",
];
const graphite: MantineColorsTuple = [
  "#d5d9de", "#b3b9c1", "#949ba5", "#737b86", "#565e68", "#3d444c", "#2b3137", "#1f2429", "#171b1f", "#101316",
];

export const theme = createTheme({
  primaryColor: "amber",
  primaryShade: 5,
  colors: { amber, dark: graphite },
  fontFamily: "'IBM Plex Sans', sans-serif",
  fontFamilyMonospace: "'IBM Plex Mono', monospace",
  headings: {
    fontFamily: "'Barlow Condensed', sans-serif",
    fontWeight: "600",
    sizes: { h1: { fontSize: "2rem" }, h2: { fontSize: "1.6rem" }, h3: { fontSize: "1.25rem" } },
  },
  defaultRadius: "xs",
  fontSizes: { xs: "0.72rem", sm: "0.8rem", md: "0.875rem", lg: "1rem", xl: "1.15rem" },
  components: {
    Button: { defaultProps: { size: "xs" } },
    Badge: { defaultProps: { radius: "xs" } },
    Table: { defaultProps: { verticalSpacing: 6, horizontalSpacing: "sm", highlightOnHover: true } },
    Paper: { defaultProps: { radius: "xs" } },
  },
});
