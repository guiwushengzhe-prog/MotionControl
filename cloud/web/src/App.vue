<script setup lang="ts">
import { computed } from "vue";
import { RouterLink, RouterView, useRoute, useRouter } from "vue-router";
import { ready, signOut, user } from "./session";

const router = useRouter();
const route = useRoute();

// 登录页是整页一张深色的图，自己铺满窗口，顶栏压在上面只会碍事——而且那上面
// 每一个链接都要求先登录。
const bare = computed(() => route.path === "/login");

async function leave() {
  await signOut();
  router.push("/login");
}
</script>

<template>
  <nav v-if="!bare">
    <div class="bar">
      <RouterLink to="/" class="brand">MotionControl</RouterLink>
      <div class="links" v-if="ready">
        <RouterLink v-if="user" to="/">我的配置</RouterLink>
        <RouterLink to="/poses">官方动作库</RouterLink>
        <RouterLink to="/browse">公开配置</RouterLink>
        <RouterLink to="/changelog">更新日志</RouterLink>
        <RouterLink to="/feedback">反馈</RouterLink>
      </div>
      <div class="account" v-if="ready">
        <template v-if="user">
          <RouterLink to="/invite">邀请朋友</RouterLink>
          <span class="who">{{ user.display_name }}</span>
          <a href="#" @click.prevent="leave">退出</a>
        </template>
        <RouterLink v-else to="/login" class="signin">登录</RouterLink>
      </div>
    </div>
  </nav>

  <main :class="{ bare }">
    <RouterView v-if="ready" />
    <p v-else class="card">读取中…</p>
  </main>
</template>

<style scoped>
nav {
  position: sticky; top: 0; z-index: 10;
  background: var(--bar);
  backdrop-filter: saturate(180%) blur(20px);
  -webkit-backdrop-filter: saturate(180%) blur(20px);
  border-bottom: 1px solid var(--line);
}
.bar {
  display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 1.5rem;
  max-width: 64rem; min-height: 3.25rem; margin: 0 auto; padding: 0 1.5rem;
}
.brand { font-weight: 700; font-size: 1.05rem; color: var(--text); letter-spacing: -.01em; }
.brand:hover { text-decoration: none; }
.links, .account { display: flex; align-items: center; gap: .25rem; font-size: .9rem; }
.links a, .account a {
  padding: .3rem .7rem; border-radius: 999px; color: var(--text-2); white-space: nowrap;
}
.links a:hover, .account a:hover { color: var(--text); text-decoration: none; }
.links a.router-link-active { background: var(--fill); color: var(--text); }
.account a.signin { background: var(--accent); color: #fff; }
.who { padding: 0 .4rem; color: var(--text-3); white-space: nowrap; }
main { max-width: 64rem; margin: 0 auto; padding: 2rem 1.5rem 3rem; }
main.bare { max-width: none; padding: 0; }

/* 窄屏：品牌和账号一行，页面链接单独一行，放不下就横着滑。 */
@media (max-width: 760px) {
  .bar { grid-template-columns: 1fr auto; gap: 0 .75rem; padding: .4rem 1rem 0; }
  .links { grid-column: 1 / -1; grid-row: 2; overflow-x: auto; padding: .35rem 0 .5rem; margin: 0 -1rem; padding-inline: .75rem; scrollbar-width: none; }
  .who { display: none; }
  main { padding: 1.25rem 1rem 2.5rem; }
}
</style>
