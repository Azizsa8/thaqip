/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        primary: {
          50: '#EFF6FF',
          100: '#DBEAFE',
          200: '#BFDBFE',
          300: '#93C5FD',
          400: '#60A5FA',
          500: '#3B82F6',
          600: '#2563EB',
          700: '#1D4ED8',
          800: '#1E40AF',
          900: '#1E3A8A',
        },
        secondary: {
          50: '#F0FDFA',
          100: '#CCFBF1',
          200: '#99F6E4',
          300: '#5EEAD4',
          400: '#2DD4BF',
          500: '#14B8A6',
          600: '#0D9488',
          700: '#0F766E',
          800: '#115E59',
          900: '#134E4A',
        },
        tier: {
          1: '#1E3A8A',  // Statute - deep blue
          2: '#0D9488',  // Translation - teal
          3: '#F59E0B',  // Regulator - amber
          4: '#7C3AED',  // Judicial - purple
        },
        trust: {
          low: '#DC2626',    // Red
          medium: '#F59E0B', // Amber
          high: '#16A34A',   // Green
        },
        bg: '#F8FAFC',
        surface: '#FFFFFF',
        'text-primary': '#0F172A',
        'text-muted': '#64748B',
      },
      fontFamily: {
        arabic: ['"Noto Naskh Arabic"', '"Amiri"', 'serif'],
        english: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'monospace'],
      },
      spacing: {
        base: '4px',
      },
      borderRadius: {
        sm: '4px',
        md: '8px',
        lg: '12px',
        full: '9999px',
      },
      boxShadow: {
        card: '0 1px 3px rgba(0,0,0,0.1)',
        elevated: '0 10px 25px rgba(0,0,0,0.15)',
      },
    },
  },
  plugins: [],
}