import { createTheme } from "@mui/material/styles";

// Shared by MUI (including portals) and the Search UI styles via CSS variables.
const colors = {
  ink: "#203e32",
  muted: "#58685d",
  paper: "#f7f5ee",
  gold: "#896a34",
  canvas: "#f2f3ec",
  surface: "#fffefb",
  border: "#d8ded2",
  sage: "#edf1e7"
};

const archiveTheme = createTheme({
  palette: {
    primary: { main: colors.ink },
    secondary: { main: colors.gold },
    background: { default: colors.canvas, paper: colors.surface },
    text: { primary: colors.ink, secondary: colors.muted },
    divider: colors.border,
    error: { main: "#9c4136" },
    success: { main: "#456b46" }
  },
  shape: { borderRadius: 5 },
  typography: {
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    fontSize: 14,
    h4: { fontFamily: 'Georgia, "Times New Roman", serif', fontWeight: 400 },
    h6: { fontSize: "1rem", fontWeight: 600 },
    button: { textTransform: "none", fontWeight: 600, letterSpacing: 0 }
  },
  components: {
    MuiCssBaseline: {
      styleOverrides: {
        ":root": Object.fromEntries(
          Object.entries(colors).map(([name, value]) => [`--archive-${name}`, value])
        ),
        "::selection": { backgroundColor: "#e7d8b3", color: colors.ink }
      }
    },
    MuiButton: {
      defaultProps: { disableElevation: true },
      styleOverrides: {
        root: {
          minHeight: 38,
          "&:focus-visible": { outline: `2px solid ${colors.gold}`, outlineOffset: 3 },
          "@media (max-width: 650px)": { minHeight: 44 }
        },
        outlined: { borderColor: "#a8b5a3" },
        text: { color: colors.muted }
      }
    },
    MuiChip: {
      defaultProps: { size: "small" },
      styleOverrides: {
        root: {
          maxWidth: "100%",
          height: "auto",
          minHeight: 25,
          borderRadius: 4,
          backgroundColor: colors.sage,
          color: colors.muted,
          fontSize: 11,
          lineHeight: 1.5
        },
        label: { padding: "3px 8px", whiteSpace: "normal", overflowWrap: "anywhere" },
        outlined: { backgroundColor: "transparent", borderColor: colors.border }
      }
    },
    MuiOutlinedInput: {
      styleOverrides: {
        root: {
          backgroundColor: colors.surface,
          "& .MuiOutlinedInput-notchedOutline": { borderColor: "#a8b5a3" },
          "&:hover .MuiOutlinedInput-notchedOutline": { borderColor: colors.muted }
        }
      }
    },
    MuiDialog: {
      styleOverrides: {
        paper: {
          border: `1px solid ${colors.border}`,
          boxShadow: "0 18px 60px rgb(32 62 50 / 18%)",
          "@media (max-width: 650px)": { margin: 16, width: "calc(100% - 32px)" }
        }
      }
    },
    MuiDialogTitle: {
      styleOverrides: {
        root: { fontFamily: 'Georgia, "Times New Roman", serif', fontSize: 26, backgroundColor: colors.paper }
      }
    },
    MuiDialogActions: { styleOverrides: { root: { padding: "12px 20px", backgroundColor: colors.paper } } },
    MuiSnackbarContent: { styleOverrides: { root: { backgroundColor: colors.ink, color: colors.surface } } },
    MuiTooltip: { styleOverrides: { tooltip: { backgroundColor: colors.ink, fontSize: 12 } } }
  }
});

export default archiveTheme;
