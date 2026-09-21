import { createRouter, createWebHistory } from 'vue-router';
import Home from './views/Home.vue';
import Project from './views/Project.vue';
import Login from './views/Login.vue';
import ModulePage from './views/ModulePage.vue';
import Strategy from './views/Strategy.vue';
import Settings from './views/Settings.vue';
import EvalRun from './views/EvalRun.vue';

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: Home, meta: { title: '研究中心' } },
    { path: '/:projectId/chat', component: Project, meta: { title: '研究任务' } },
    { path: '/login', component: Login, meta: { title: '登录' } },
    { path: '/strategy-platform', component: Strategy, meta: { title: '策略实验室' } },
    { path: '/research-reports', component: ModulePage, props: { module: 'reports' }, meta: { title: '投研报告' } },
    { path: '/business-knowledge', component: ModulePage, props: { module: 'knowledge' }, meta: { title: '业务知识' } },
    { path: '/skills', component: ModulePage, props: { module: 'skills' }, meta: { title: '能力中心' } },
    { path: '/eval-platform', component: ModulePage, props: { module: 'eval' }, meta: { title: '评测平台' } },
    { path: '/eval-platform/runs/:runId', component: EvalRun, meta: { title: '评测报告' } },
    { path: '/ops-platform', component: ModulePage, props: { module: 'ops' }, meta: { title: '运行治理' } },
    { path: '/settings', component: Settings, meta: { title: '设置与连接' } },
    { path: '/admin/users', component: ModulePage, props: { module: 'users' }, meta: { title: '用户管理' } },
    { path: '/account/:section', component: ModulePage, props: { module: 'account' }, meta: { title: '我的账号' } },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
});

router.afterEach(to => { document.title = `${String(to.meta.title || '研究中心')} · AlphaDeck`; });
export default router;
