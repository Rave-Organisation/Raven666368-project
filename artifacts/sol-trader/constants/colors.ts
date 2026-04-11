/**
 * Military-NASA Regime Palette
 * Inspired by mission-control MFDs, NOAA radar, and tactical HUDs.
 *
 *  Background  #000d1a  — deep ops-room navy
 *  Primary     #FF6B00  — NASA/tactical amber
 *  Accent      #00FFB3  — radar sweep green
 *  Info        #00BFFF  — telemetry blue
 *  Warning     #FFB800  — caution amber
 *  Critical    #FF2929  — alert red
 *  Border      #0D3350  — steel-blue grid line
 */

const colors = {
  light: {
    text: "#000d1a",
    tint: "#FF6B00",

    background: "#000d1a",
    foreground: "#E8F4F8",

    card: "#001628",
    cardForeground: "#E8F4F8",

    primary: "#FF6B00",
    primaryForeground: "#000d1a",

    secondary: "#001F38",
    secondaryForeground: "#7AAFC4",

    muted: "#001628",
    mutedForeground: "#4E7A96",

    accent: "#00FFB3",
    accentForeground: "#000d1a",

    destructive: "#FF2929",
    destructiveForeground: "#ffffff",

    border: "#0D3350",
    input: "#001F38",

    success: "#00FFB3",
    warning: "#FFB800",
    info: "#00BFFF",

    solana: "#FF6B00",
    solanaGreen: "#00FFB3",

    chartUp: "#00FFB3",
    chartDown: "#FF2929",

    gridLine: "#0D3350",
    scanline: "#00FFB310",
    hudGlow: "#FF6B0030",
    radarPing: "#00FFB3",
  },

  dark: {
    text: "#E8F4F8",
    tint: "#FF6B00",

    background: "#000d1a",
    foreground: "#E8F4F8",

    card: "#001628",
    cardForeground: "#E8F4F8",

    primary: "#FF6B00",
    primaryForeground: "#000d1a",

    secondary: "#001F38",
    secondaryForeground: "#7AAFC4",

    muted: "#001628",
    mutedForeground: "#4E7A96",

    accent: "#00FFB3",
    accentForeground: "#000d1a",

    destructive: "#FF2929",
    destructiveForeground: "#ffffff",

    border: "#0D3350",
    input: "#001F38",

    success: "#00FFB3",
    warning: "#FFB800",
    info: "#00BFFF",

    solana: "#FF6B00",
    solanaGreen: "#00FFB3",

    chartUp: "#00FFB3",
    chartDown: "#FF2929",

    gridLine: "#0D3350",
    scanline: "#00FFB310",
    hudGlow: "#FF6B0030",
    radarPing: "#00FFB3",
  },

  radius: 8,
};

export default colors;
