import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { useAuthStore } from './stores/auth'
import LoginView from './views/LoginView.vue'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
import SystemStatusView from './views/SystemStatusView.vue'
import ChatView from './views/ChatView.vue'
import AdminView from './views/AdminView.vue'
import AgentListView from './views/agents/AgentListView.vue'
import AgentEditorView from './views/agents/AgentEditorView.vue'
import CapabilityCenterView from './views/capabilities/CapabilityCenterView.vue'
import SkillEditorView from './views/capabilities/SkillEditorView.vue'
import KnowledgeDetailView from './views/knowledge/KnowledgeDetailView.vue'
import KnowledgeListView from './views/knowledge/KnowledgeListView.vue'
import SchedulesView from './views/SchedulesView.vue'
import ConnectorsView from './views/connectors/ConnectorsView.vue'
import './styles/main.scss'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/chat' },
    { path: '/login', name: 'login', component: LoginView },
    { path: '/chat/:conversationId?', component: ChatView, meta: { title: '问答' } },
    { path: '/agents', component: AgentListView, meta: { title: '智能体' } },
    { path: '/agents/new', component: AgentEditorView, meta: { title: '智能体' } },
    { path: '/agents/:agentId', component: AgentEditorView, meta: { title: '智能体' } },
    { path: '/capabilities', component: CapabilityCenterView, meta: { title: '能力中心' } },
    { path: '/capabilities/skills/new', component: SkillEditorView, meta: { title: '能力中心' } },
    { path: '/capabilities/skills/:skillId', component: SkillEditorView, meta: { title: '能力中心' } },
    { path: '/connectors', component: ConnectorsView, meta: { title: '连接器' } },
    { path: '/system', component: SystemStatusView, meta: { title: '系统概览' } },
    { path: '/knowledge', component: KnowledgeListView, meta: { title: '知识库' } },
    { path: '/knowledge/:kbId', component: KnowledgeDetailView, meta: { title: '知识库' } },
    { path: '/schedules', component: SchedulesView, meta: { title: '定时任务' } },
    { path: '/tasks/:taskId?', redirect: '/chat' },
    { path: '/admin/members', component: AdminView, meta: { admin: true, title: '成员与权限' } },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

const pinia = createPinia()
const auth = useAuthStore(pinia)
router.beforeEach(async to => {
  if (!auth.initialized) {
    try { await auth.restore() } catch { auth.clear() }
  }
  if (to.name !== 'login' && !auth.authenticated) return '/login'
  if (to.name === 'login' && auth.authenticated) return '/chat'
  if (to.meta.admin) {
    try { await auth.restore() } catch { return '/chat' }
    if (auth.activeWorkspace?.role !== 'admin' || auth.activeWorkspace.status !== 'active') return '/chat'
  }
})
function expire(): void { auth.clear(); void router.replace('/login') }
window.addEventListener('auth-expired', expire)
window.addEventListener('storage', event => { if (event.key === 'ea-auth-event') expire() })
createApp(App).use(pinia).use(router).mount('#app')
