import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
export default defineConfig({ plugins: [react()], define: { "import.meta.env.VITE_API_BASE": JSON.stringify("/api") }, test: { environment: 'jsdom', setupFiles: ['./tests/setup.ts'], restoreMocks: true } });
