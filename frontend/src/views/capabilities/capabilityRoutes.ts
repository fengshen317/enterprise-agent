import type { RouteRecordRaw } from 'vue-router'

export const legacyConnectorsRoute: RouteRecordRaw = {
  path: '/connectors',
  redirect: to => ({ path: '/capabilities', query: { ...to.query, tab: 'connectors' }, hash: to.hash }),
}

export const legacyPluginsRoute: RouteRecordRaw = { ...legacyConnectorsRoute, path: '/plugins' }
