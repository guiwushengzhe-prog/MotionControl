<script setup lang="ts">
/**
 * 电脑登录账号的最后一步：电脑打开这一页，人在这里点「允许」。
 *
 * 要先登录网站（路由守卫会带着 ?next 回到这里）。码从网址里来，电脑上也显示着同一个，
 * 对得上再点。允许以后电脑自己会拿到凭证，这一页不用再做别的。
 */
import { onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import { api, type DevicePending } from "../api";

const route = useRoute();
const code = ref(typeof route.query.code === "string" ? route.query.code : "");
const pending = ref<DevicePending | null>(null);
const done = ref(false);
const busy = ref(false);
const error = ref("");

const SCOPE_NAMES: Record<string, string> = { fitness: "同步运动记录和身体数据" };

async function look() {
  error.value = "";
  pending.value = null;
  if (!code.value.trim()) return;
  try {
    pending.value = await api.devicePending(code.value.trim());
    done.value = pending.value.approved;
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : "出错了";
  }
}

async function approve() {
  if (!pending.value) return;
  busy.value = true;
  error.value = "";
  try {
    await api.approveDevice(pending.value.user_code);
    done.value = true;
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : "出错了";
  } finally {
    busy.value = false;
  }
}

onMounted(look);
</script>

<template>
  <section class="card device-page">
    <h1>电脑登录</h1>
    <template v-if="done">
      <p class="notice">已允许。回到电脑上就能看到已登录，这一页可以关掉了。</p>
    </template>
    <template v-else-if="pending">
      <p class="hint">对一下电脑上显示的码，一样再点允许。</p>
      <code class="device-code">{{ pending.user_code }}</code>
      <p><strong>{{ pending.name || "一台电脑" }}</strong> 想要：</p>
      <ul>
        <li v-for="scope in pending.scopes" :key="scope">{{ SCOPE_NAMES[scope] || scope }}</li>
      </ul>
      <button type="button" :disabled="busy" @click="approve">{{ busy ? "正在允许…" : "允许" }}</button>
    </template>
    <form v-else @submit.prevent="look">
      <label>电脑上显示的码<input v-model="code" autocomplete="off" placeholder="XXXX-XXXX" /></label>
      <button type="submit">下一步</button>
    </form>
    <p v-if="error" class="error">{{ error }}</p>
  </section>
</template>

<style scoped>
.device-page { max-width: 32rem; }
.device-code {
  display: inline-block;
  margin: 0.4rem 0 1rem;
  font-size: 1.8rem;
  letter-spacing: 0.12em;
}
</style>
