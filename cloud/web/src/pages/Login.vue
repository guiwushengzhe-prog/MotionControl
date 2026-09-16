<script setup lang="ts">
/**
 * Sign in, or register with an invite code.
 *
 * One page with two modes rather than two routes: registration is invite-only,
 * so the two forms differ by one field and nobody arrives at the register page
 * without having been sent a code anyway.
 */
import { ref } from "vue";
import { useRouter } from "vue-router";
import { api } from "../api";
import { user } from "../session";

const router = useRouter();
const mode = ref<"login" | "register">("login");
const email = ref("");
const password = ref("");
const displayName = ref("");
const inviteCode = ref("");
const error = ref("");
const busy = ref(false);

async function submit() {
  error.value = "";
  busy.value = true;
  try {
    user.value = mode.value === "login"
      ? await api.login(email.value, password.value)
      : await api.register({
          invite_code: inviteCode.value,
          email: email.value,
          password: password.value,
          display_name: displayName.value,
        });
    router.push("/");
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : "出错了";
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="card narrow">
    <h1>{{ mode === "login" ? "登录" : "注册" }}</h1>
    <p class="hint" v-if="mode === 'register'">
      注册需要邀请码。还没开放公开注册，因为邮箱验证和举报处理都还没做。
    </p>

    <form @submit.prevent="submit">
      <label v-if="mode === 'register'">
        邀请码
        <input v-model="inviteCode" required autocomplete="off" />
      </label>
      <label v-if="mode === 'register'">
        显示名字
        <input v-model="displayName" required maxlength="40" />
      </label>
      <label>
        邮箱
        <input v-model="email" type="email" required autocomplete="username" />
      </label>
      <label>
        密码
        <input
          v-model="password"
          type="password"
          required
          :minlength="mode === 'register' ? 10 : undefined"
          :autocomplete="mode === 'login' ? 'current-password' : 'new-password'"
        />
        <small v-if="mode === 'register'">至少 10 个字符。长度比花样管用，所以没有别的要求。</small>
      </label>

      <p class="error" v-if="error">{{ error }}</p>
      <button type="submit" :disabled="busy">
        {{ busy ? "请稍候…" : mode === "login" ? "登录" : "注册" }}
      </button>
    </form>

    <p class="switch">
      <a href="#" @click.prevent="mode = mode === 'login' ? 'register' : 'login'; error = ''">
        {{ mode === "login" ? "有邀请码？去注册" : "已有账号？去登录" }}
      </a>
    </p>
  </div>
</template>

<style scoped>
.narrow { max-width: 26rem; margin: 3rem auto; }
form { display: flex; flex-direction: column; gap: 1rem; }
label { display: flex; flex-direction: column; gap: 0.35rem; font-size: 0.9rem; }
small { color: var(--muted); font-size: 0.8rem; }
.switch { margin-top: 1.2rem; text-align: center; font-size: 0.9rem; }
</style>
