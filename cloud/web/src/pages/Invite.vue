<script setup lang="ts">
import { onMounted, ref } from "vue";
import { api, formatTime, type Invite, type InviteStatus } from "../api";

const status = ref<InviteStatus | null>(null);
const invite = ref<Invite | null>(null);
const busy = ref(false);
const error = ref("");
const copied = ref(false);

async function load() {
  try {
    status.value = await api.inviteStatus();
  } catch (e: any) {
    error.value = e?.message || "读取邀请码状态失败";
  }
}

async function create() {
  busy.value = true;
  error.value = "";
  try {
    invite.value = await api.createInvite();
    await load();
  } catch (e: any) {
    error.value = e?.message || "生成失败，请稍后再试";
  } finally {
    busy.value = false;
  }
}

async function copy() {
  if (!invite.value) return;
  try {
    await navigator.clipboard.writeText(invite.value.code);
    copied.value = true;
    setTimeout(() => (copied.value = false), 2000);
  } catch {
    error.value = "浏览器未允许复制，请长按邀请码手动复制";
  }
}

onMounted(load);
</script>

<template>
  <section class="card invite-page">
    <h1>邀请朋友</h1>
    <p class="hint">
      注册成功后即可生成。每个账号 24 小时内最多生成 3 个，邀请码生成后 7 天内有效，使用一次即失效。
    </p>

    <div v-if="invite" class="invite-result">
      <strong>邀请码只显示这一次，请现在复制给朋友</strong>
      <code>{{ invite.code }}</code>
      <button type="button" @click="copy">{{ copied ? "已复制" : "复制邀请码" }}</button>
      <small v-if="invite.expires_at">有效期至 {{ formatTime(invite.expires_at) }}</small>
    </div>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="!status && !error" class="hint">读取中…</p>

    <template v-if="status && !invite">
      <button v-if="status.can_create" type="button" :disabled="busy" @click="create">
        {{ busy ? "生成中…" : "生成邀请码" }}
      </button>
      <p v-else class="notice">
        暂时还不能生成，下次可生成时间：{{ formatTime(status.next_available_at) }}
      </p>
    </template>
  </section>
</template>

<style scoped>
.invite-page { max-width: 38rem; }
.invite-result {
  display: grid;
  gap: 0.8rem;
  margin-top: 1.4rem;
  padding: 1rem;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--chip);
}
.invite-result code {
  overflow-wrap: anywhere;
  padding: 0.7rem;
  user-select: all;
}
.invite-result button { justify-self: start; }
.invite-result small { color: var(--muted); }
</style>
