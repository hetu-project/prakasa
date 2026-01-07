import type { Color, PaletteColorOptions, PaletteOptions } from '@mui/material';

declare module '@mui/material' {
  interface Color {
    150: string;
    250: string;
    1000: string;
  }
  interface PaletteColor {
    darker: string;
    lighter: string;
    chip: string;
    chipText: string;
  }
  interface SimplePaletteColorOptions {
    darker: string;
    lighter: string;
    chip: string;
    chipText: string;
  }

  interface Palette {
    brand: PaletteColor;
  }
  interface PaletteOptions {
    brand?: PaletteColorOptions;
  }

  interface TypeBackground {
    /** Use for some small area. */
    area: string;
  }
}

declare module '@mui/material/Chip' {
  interface ChipPropsColorOverrides {
    brand: true;
  }
}

const brand: Partial<Color> = {
  500: '#00F0FF',
  100: '#00B8CC',
  50: '#008099',
};
const grey: Partial<Color> = {
  50: '#0A0E27',
  100: '#1A1F4D',
  200: '#252C63',
  250: '#323D85',
  300: '#404D9E',
  400: '#4F61B7',
  500: '#5E75D0',
  600: '#6D89E9',
  700: '#7C9DFF',
  800: '#E0F7FF',
  900: '#FFFFFF',
  1000: '#050714',
};
const red: Partial<Color> = {
  100: '#7A2D2D',
  200: '#A33D3D',
  400: '#D94E4E',
  500: '#FF6B6B',
};
const orange: Partial<Color> = {
  100: '#6B4A1F',
  200: '#8F6633',
  500: '#FF9F4D',
};
const green: Partial<Color> = {
  100: '#2D5A3D',
  200: '#4A8066',
  500: '#00FFB8',
};

export const palette: PaletteOptions = {
  mode: 'dark',
  common: {
    white: grey[900]!,
    black: grey[1000]!,
  },
  grey,
  primary: {
    main: brand[500]!,
    darker: brand[100]!,
    dark: brand[100]!,
    light: brand[50]!,
    lighter: grey[800]!,
    contrastText: grey[900]!,
    chip: grey[100]!,
    chipText: brand[500]!,
  },
  secondary: {
    main: '#B026FF',
    darker: '#8A1FCF',
    dark: '#9A22E6',
    light: '#C040FF',
    lighter: '#D080FF',
    contrastText: grey[900]!,
    chip: grey[200]!,
    chipText: '#B026FF',
  },
  brand: {
    main: brand[500]!,
    darker: brand[100]!,
    dark: brand[100]!,
    light: brand[50]!,
    lighter: grey[800]!,
    contrastText: grey[900]!,
    chip: brand[100]!,
    chipText: brand[500]!,
  },
  info: {
    main: grey[800]!,
    dark: grey[500]!,
    darker: grey[250]!,
    light: grey[300]!,
    lighter: grey[200]!,
    contrastText: grey[900]!,
    chip: grey[200]!,
    chipText: grey[800]!,
  },
  error: {
    main: red[500]!,
    dark: red[400]!,
    darker: red[400]!,
    light: red[200]!,
    lighter: red[100]!,
    contrastText: grey[900]!,
    chip: red[100]!,
    chipText: red[500]!,
  },
  warning: {
    main: orange[500]!,
    dark: orange[200]!,
    darker: orange[200]!,
    light: orange[200]!,
    lighter: orange[100]!,
    contrastText: grey[900]!,
    chip: orange[100]!,
    chipText: orange[500]!,
  },
  success: {
    main: green[500]!,
    dark: green[200]!,
    darker: green[200]!,
    light: green[200]!,
    lighter: green[100]!,
    contrastText: grey[900]!,
    chip: green[100]!,
    chipText: green[500]!,
  },
  text: {
    primary: grey[800]!,
    secondary: grey[700]!,
    disabled: grey[400]!,
  },
  divider: 'rgba(0, 240, 255, 0.1)',
  background: {
    default: '#0A0E27',
    paper: 'rgba(26, 31, 77, 0.6)',
    area: 'rgba(10, 14, 39, 0.8)',
  },
};
