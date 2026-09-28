import React from 'react';

import { AppPlugin, PluginExtensionPoints } from '@grafana/data';

import App from './App';
import pluginJson from './plugin.json';

const appPath = `/a/${pluginJson.id}`;

export const plugin = new AppPlugin<{}>()
  .setRootPage(App)
  .addLink({
    targets: [PluginExtensionPoints.CommandPalette],
    title: 'Create dashboard with AI',
    description: 'Open the local Ollama + Grafana MCP dashboard builder',
    icon: 'sparkles',
    path: appPath,
  })
  .addLink({
    targets: [PluginExtensionPoints.DashboardPanelMenu],
    title: 'Open AI dashboard builder',
    description: 'Ask the local AI to inspect or modify this Grafana environment',
    icon: 'sparkles',
    path: appPath,
  });
