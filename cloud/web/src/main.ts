import { createApp } from "vue";
import { createRouter, createWebHistory } from "vue-router";

import App from "./App.vue";
import Browse from "./pages/Browse.vue";
import ConfigDetail from "./pages/ConfigDetail.vue";
import Login from "./pages/Login.vue";
import MyConfigs from "./pages/MyConfigs.vue";
import { ready, refresh, user } from "./session";
import "./style.css";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", component: MyConfigs, meta: { auth: true } },
    { path: "/browse", component: Browse },
    // Not marked auth: a public or unlisted config is readable by a visitor,
    // and the server decides that, not this guard.
    { path: "/config/:id", component: ConfigDetail },
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
