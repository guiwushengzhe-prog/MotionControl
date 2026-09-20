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
    <RouterLink to="/" class="brand">MotionControl</RouterLink>
    <div class="links" v-if="ready">
      <RouterLink to="/browse">公开配置</RouterLink>
      <RouterLink to="/changelog">更新日志</RouterLink>
      <RouterLink to="/feedback">反馈</RouterLink>
      <template v-if="user">
        <RouterLink to="/">我的配置</RouterLink>
        <RouterLink to="/invite">邀请朋友</RouterLink>
        <span class="who">{{ user.display_name }}</span>
        <a href="#" @click.prevent="leave">退出</a>
      </template>
      <RouterLink v-else to="/login">登录</RouterLink>
    </div>
  </nav>

  <main :class="{ bare }">
    <RouterView v-if="ready" />
    <p v-else class="card">读取中…</p>
  </main>
</template>

<style scoped>
nav {
  display: flex; align-items: center; justify-content: space-between;
  padding: 0.9rem 1.5rem; border-bottom: 1px solid var(--line);
}
.brand { font-weight: 700; font-size: 1.05rem; color: var(--text); }
.links { display: flex; align-items: center; gap: 1.25rem; font-size: 0.9rem; }
.who { color: var(--muted); }
main { max-width: 56rem; margin: 0 auto; padding: 1.5rem; }
main.bare { max-width: none; padding: 0; }
</style>
