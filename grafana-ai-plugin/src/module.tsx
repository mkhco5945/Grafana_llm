import React from 'react';

import { AppPlugin, PluginExtensionPoints } from '@grafana/data';

import App from './App';
import { mountGlobalChatSidebar } from './GlobalChatSidebar';
import pluginJson from './plugin.json';

// Grafana 12.1 validates extension links as children of /a/<pluginId>/.
// The root component still handles both the slash and non-slash app URLs.
const appPath = `/a/${pluginJson.id}/`;

// `preload: true` loads this module for every signed-in Grafana route. Grafana
// currently has no core component extension point for a global sidebar, so the
// drawer is mounted once at document level and follows Grafana SPA navigation.
mountGlobalChatSidebar();

export const plugin = new AppPlugin<{}>()
  .setRootPage(App)
  .addLink({
    targets: [PluginExtensionPoints.CommandPalette],
    title: 'Create dashboard with AI',
    description: 'Open the local Ollama + Grafana MCP dashboard builder',
    icon: 'ai',
    path: appPath,
  })
  .addLink({
    targets: [PluginExtensionPoints.DashboardPanelMenu],
    title: 'Open AI dashboard builder',
    description: 'Ask the local AI to inspect or modify this Grafana environment',
    icon: 'ai',
    path: appPath,
  });
