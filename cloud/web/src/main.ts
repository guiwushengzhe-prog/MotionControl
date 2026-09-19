import { createApp } from "vue";
import { createRouter, createWebHistory } from "vue-router";

import App from "./App.vue";
import Browse from "./pages/Browse.vue";
import Changelog from "./pages/Changelog.vue";
import ConfigDetail from "./pages/ConfigDetail.vue";
import Feedback from "./pages/Feedback.vue";
import Login from "./pages/Login.vue";
import MyConfigs from "./pages/MyConfigs.vue";
import { ready, refresh, user } from "./session";
import "./style.css";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", component: MyConfigs, meta: { auth: true } },
    { path: "/browse", component: Browse },
    { path: "/changelog", component: Changelog },
    // Not marked auth: a public or unlisted config is readable by a visitor,
    // and the server decides that, not this guard.
    { path: "/config/:id", component: ConfigDetail },
    // 不加 auth：绝大多数用这个软件的人没有账号，注册还要邀请码。
    // 反馈入口要求先登录，等于这个入口不存在。
    { path: "/feedback", component: Feedback },
    { path: "/login", component: Login },
  ],
});

router.beforeEach(async (to) => {
  // The session lives in an HttpOnly cookie, so the only way to know whether
  // there is one is to ask. Waiting for that first answer is what stops a
  // signed-in user being bounced to the login page on a page reload.
  if (!ready.value) await refresh();
  if (to.meta.auth && !user.value) return { path: "/login" };
  return true;
});

createApp(App).use(router).mount("#app");
