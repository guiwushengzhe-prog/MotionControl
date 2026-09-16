<script setup lang="ts">
import { RouterLink, RouterView, useRouter } from "vue-router";
import { ready, signOut, user } from "./session";

const router = useRouter();

async function leave() {
  await signOut();
  router.push("/login");
}
</script>

<template>
  <nav>
    <RouterLink to="/" class="brand">MotionControl</RouterLink>
    <div class="links" v-if="ready">
      <RouterLink to="/browse">公开配置</RouterLink>
      <template v-if="user">
        <RouterLink to="/">我的配置</RouterLink>
        <span class="who">{{ user.display_name }}</span>
        <a href="#" @click.prevent="leave">退出</a>
      </template>
      <RouterLink v-else to="/login">登录</RouterLink>
    </div>
  </nav>

  <main>
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
</style>
