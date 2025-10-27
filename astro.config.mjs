// astro.config.mjs
// npx astro dev
// @ts-check
import { defineConfig } from 'astro/config';

import react from '@astrojs/react';

// https://astro.build/config
export default defineConfig({
    site: 'https://private-8-tyken.github.io',
    base: '/media-archive/',
    output: 'static',
    integrations: [react()]
});