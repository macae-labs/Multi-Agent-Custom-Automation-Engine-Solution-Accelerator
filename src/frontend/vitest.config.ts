import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import { resolve } from 'path'

export default defineConfig({
    plugins: [react()],
    test: {
        globals: true,
        environment: 'jsdom',
        setupFiles: ['./src/setupTests.tsx'],
        include: ['src/**/*.{test,spec}.{ts,tsx}'],
        css: true,
    },
    resolve: {
        alias: [
            // monaco-editor (y sus subpaths `…worker?worker`) no cargan en
            // jsdom; ver src/test/stubs. El editor real se prueba en Chrome.
            {
                find: /^monaco-editor(\/.*)?$/,
                replacement: resolve(__dirname, 'src/test/stubs/monaco-editor.ts'),
            },
            {
                find: /^@monaco-editor\/react$/,
                replacement: resolve(__dirname, 'src/test/stubs/monaco-react.tsx'),
            },
            { find: '@', replacement: resolve(__dirname, 'src') },
        ],
    },
})
