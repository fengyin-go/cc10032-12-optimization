import { createRouter, createWebHistory } from 'vue-router'

import AlertMergeView from '@/views/AlertMergeView.vue'
import DeployMigrationView from '@/views/DeployMigrationView.vue'
import Overview from '@/views/Overview.vue'
import RestoreCycleView from '@/views/RestoreCycleView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 固定路径必须放在 /:moduleKey 之前，否则会被动态模块路由抢匹配。
    { path: '/alert/merge', name: 'alert-merge', component: AlertMergeView },
    { path: '/restore/cycles', name: 'restore-cycles', component: RestoreCycleView },
    { path: '/deploy/migration', name: 'deploy-migration', component: DeployMigrationView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
