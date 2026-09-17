<script setup lang="ts">
/**
 * Sign in, or register with an invite code.
 *
 * One page with two modes rather than two routes: registration is invite-only,
 * so the two forms differ by one field and nobody arrives at the register page
 * without having been sent a code anyway.
 *
 * This is the one page that ignores the site's light theme and paints its own
 * dark panel.  It is also the only page a signed-out visitor sees, so it is
 * where the project gets to say what it is -- the rest of the site assumes you
 * already know.
 *
 * The figure is an image because it is a drawing; everything else is text, so
 * it stays sharp, reflows, and can be read aloud or translated.
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
const revealed = ref(false);

const features = [
  { title: "摄像头识别", detail: "普通摄像头即可", icon: "camera" },
  { title: "人体姿态", detail: "精准姿态追踪", icon: "pose" },
  { title: "语音控制", detail: "本地关键词识别", icon: "mic" },
  { title: "多种输出", detail: "键盘 / 鼠标 / 手柄", icon: "pad" },
];

function toggleMode() {
  mode.value = mode.value === "login" ? "register" : "login";
  error.value = "";
}

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
  <div class="signin">
    <section class="pitch">
      <p class="wordmark">
        <img src="/login-mark.png" alt="" width="110" height="70" />
        <span>MotionControl</span>
      </p>
      <p class="tagline">用你的身体 · 控制游戏世界</p>

      <ul class="features">
        <li v-for="item in features" :key="item.title">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <template v-if="item.icon === 'camera'">
              <rect x="2.5" y="6.5" width="19" height="13" rx="2.5" />
              <path d="M8.5 6.5 10 4h4l1.5 2.5" />
              <circle cx="12" cy="13" r="3.6" />
            </template>
            <template v-else-if="item.icon === 'pose'">
              <path d="M17 21c0-4.4-2.2-7-5-7s-5 2.6-5 7" />
              <circle cx="12" cy="7" r="4.2" />
              <path d="M16.4 9.5c1.4-.5 2.4-1.5 2.4-2.8" />
            </template>
            <template v-else-if="item.icon === 'mic'">
              <rect x="9" y="2.5" width="6" height="11" rx="3" />
              <path d="M5.5 11.5a6.5 6.5 0 0 0 13 0" />
              <path d="M12 18v3.5" />
            </template>
            <template v-else>
              <rect x="2.5" y="7.5" width="19" height="11" rx="4" />
              <path d="M7 11v3M5.5 12.5h3" />
              <circle cx="16" cy="12" r="1" />
              <circle cx="18.5" cy="14.5" r="1" />
            </template>
          </svg>
          <b>{{ item.title }}</b>
          <small>{{ item.detail }}</small>
        </li>
      </ul>

      <img class="figure" src="/login-figure.png" alt="人体姿态识别示意：骨骼点连向键盘、鼠标和手柄" />
    </section>

    <section class="panel">
      <div class="sheet">
        <h1>{{ mode === "login" ? "登录" : "注册" }}</h1>
        <p class="lead">
          {{ mode === "login" ? "欢迎回来，MotionControl" : "注册需要邀请码" }}
        </p>

        <form @submit.prevent="submit">
          <label v-if="mode === 'register'" class="field">
            <span class="glyph">
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <rect x="3" y="5" width="18" height="14" rx="2.5" />
                <path d="M8 12h8" />
              </svg>
            </span>
            <input v-model="inviteCode" placeholder="邀请码" required autocomplete="off" />
          </label>

          <label v-if="mode === 'register'" class="field">
            <span class="glyph">
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <circle cx="12" cy="8" r="3.8" />
                <path d="M4.8 20c0-3.8 3.2-6 7.2-6s7.2 2.2 7.2 6" />
              </svg>
            </span>
            <input v-model="displayName" placeholder="显示名字" required maxlength="40" />
          </label>

          <label class="field">
            <span class="glyph">
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <circle cx="12" cy="8" r="3.8" />
                <path d="M4.8 20c0-3.8 3.2-6 7.2-6s7.2 2.2 7.2 6" />
              </svg>
            </span>
            <input v-model="email" type="email" placeholder="邮箱" required autocomplete="username" />
          </label>

          <!-- 改邮箱和找回密码都还没做。填错的代价是整个账号拿不回来，
               所以这一句必须在注册时就说，不能等出事再说。 -->
          <p v-if="mode === 'register'" class="warn">
            填你自己真实在用的邮箱。目前不能改邮箱、也不能找回密码，填错只能重新注册。
          </p>

          <label class="field">
            <span class="glyph">
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <rect x="4.5" y="10.5" width="15" height="9.5" rx="2" />
                <path d="M8 10.5V7.8a4 4 0 0 1 8 0v2.7" />
              </svg>
            </span>
            <input
              v-model="password"
              :type="revealed ? 'text' : 'password'"
              placeholder="密码"
              required
              :minlength="mode === 'register' ? 10 : undefined"
              :autocomplete="mode === 'login' ? 'current-password' : 'new-password'"
            />
            <button
              type="button"
              class="reveal"
              :aria-label="revealed ? '隐藏密码' : '显示密码'"
              @click="revealed = !revealed"
            >
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M2.5 12S6 5.8 12 5.8 21.5 12 21.5 12 18 18.2 12 18.2 2.5 12 2.5 12Z" />
                <circle cx="12" cy="12" r="3" />
                <path v-if="!revealed" d="M4 20 20 4" />
              </svg>
            </button>
          </label>
          <p v-if="mode === 'register'" class="hint">密码至少 10 个字符</p>

          <p class="error" v-if="error">{{ error }}</p>

          <button class="go" type="submit" :disabled="busy">
            {{ busy ? "请稍候…" : mode === "login" ? "登录" : "注册" }}
          </button>
        </form>

        <p class="switch">
          <template v-if="mode === 'login'">没有账号？ <a href="#" @click.prevent="toggleMode">立即注册</a></template>
          <template v-else>已有账号？ <a href="#" @click.prevent="toggleMode">去登录</a></template>
        </p>
      </div>
    </section>
  </div>
</template>

<style scoped>
/* 这一页自己定颜色，不跟站点的浅色主题走：整页是深色的一张图。 */
.signin {
  --ink: #eaf2ff;
  --dim: #9ebed8;
  --sky: #0f87ff;
  --sheet: rgba(11, 22, 44, 0.78);
  --edge: rgba(94, 150, 220, 0.22);

  position: fixed;
  inset: 0;
  display: grid;
  grid-template-columns: 1.05fr 0.95fr;
  align-items: center;
  gap: 2rem;
  padding: 3rem clamp(1.5rem, 5vw, 5rem);
  overflow: auto;
  color: var(--ink);
  background:
    radial-gradient(90% 70% at 20% 48%, rgba(20, 84, 168, 0.32), transparent 62%),
    radial-gradient(70% 60% at 82% 38%, rgba(16, 62, 132, 0.28), transparent 65%),
    #051022;
}

/* --- 左边：这是什么 --- */
.pitch { max-width: 40rem; justify-self: center; }
.wordmark { display: flex; align-items: center; gap: 0.75rem; margin: 0; }
/* 两张图都是从一整张深色设计稿里裁出来的，自带一块比页面略深的底色，直接放
   上去会看见一个方框。screen 让暗处不产生任何贡献，只留下发光的线条本身。 */
.wordmark img { width: 3.4rem; height: auto; mix-blend-mode: screen; }
.wordmark span { font-size: clamp(1.9rem, 3.4vw, 2.9rem); font-weight: 700; letter-spacing: -0.01em; }
.tagline {
  margin: 0.55rem 0 0 0.35rem;
  color: var(--dim);
  letter-spacing: 0.42em;
  font-size: clamp(0.8rem, 1.2vw, 0.98rem);
}

.features {
  display: flex;
  margin: 2.1rem 0 0;
  padding: 0;
  list-style: none;
}
.features li {
  flex: 1;
  padding: 0 0.9rem;
  text-align: center;
  border-left: 1px solid var(--edge);
}
.features li:first-child { border-left: 0; padding-left: 0; }
.features svg {
  width: 2rem; height: 2rem;
  fill: none; stroke: #3ea0ff; stroke-width: 1.6;
  stroke-linecap: round; stroke-linejoin: round;
}
.features b { display: block; margin-top: 0.6rem; font-size: 0.95rem; }
.features small { display: block; margin-top: 0.2rem; color: var(--dim); font-size: 0.8rem; }

.figure { display: block; width: 100%; max-width: 34rem; margin: 1.6rem 0 0; mix-blend-mode: screen; }

/* --- 右边：真正的表单 --- */
.panel { display: flex; justify-content: center; }
.sheet {
  width: min(28rem, 100%);
  padding: clamp(1.8rem, 3vw, 2.8rem);
  border: 1px solid var(--edge);
  border-radius: 14px;
  background: var(--sheet);
  backdrop-filter: blur(6px);
}
.sheet h1 { margin: 0; font-size: 2rem; }
.lead { margin: 0.35rem 0 1.8rem; color: var(--dim); font-size: 0.95rem; }

form { display: flex; flex-direction: column; gap: 0.9rem; }

.field {
  display: flex;
  align-items: center;
  gap: 0.7rem;
  padding: 0 0.9rem;
  border: 1px solid var(--edge);
  border-radius: 9px;
  background: #0f192e;
}
.field:focus-within { border-color: var(--sky); }
.glyph { display: flex; color: #7fa8d8; }
.glyph svg, .reveal svg {
  width: 1.15rem; height: 1.15rem;
  fill: none; stroke: currentColor; stroke-width: 1.6;
  stroke-linecap: round; stroke-linejoin: round;
}
.field input {
  flex: 1;
  min-width: 0;
  padding: 0.85rem 0;
  border: 0;
  background: none;
  color: var(--ink);
}
.field input:focus { outline: none; }
.field input::placeholder { color: #91aacf; }

.reveal {
  display: flex;
  padding: 0;
  border: 0;
  background: none;
  color: #7fa8d8;
  cursor: pointer;
}

.hint { margin: -0.3rem 0 0; color: var(--dim); font-size: 0.8rem; }
/* 这一条不是补充说明，是填错就拿不回账号的警告，所以不跟其他灰字同色。 */
.warn { margin: -0.3rem 0 0; color: var(--ink); font-size: 0.82rem; line-height: 1.5; }
.error { margin: 0; color: #ff9c9c; font-size: 0.88rem; }

.go {
  margin-top: 0.6rem;
  padding: 0.95rem;
  border: 0;
  border-radius: 9px;
  background: var(--sky);
  color: #fff;
  font-size: 1.02rem;
  font-weight: 600;
  cursor: pointer;
}
.go:hover:not(:disabled) { background: #2b96ff; }
.go:disabled { opacity: 0.55; cursor: default; }

.switch { margin: 1.4rem 0 0; text-align: center; color: var(--dim); font-size: 0.9rem; }
.switch a { color: #4ea6ff; }

/* 窄屏：插画和卖点收起来，只留能用的那部分。 */
@media (max-width: 900px) {
  /* safe center：装得下就居中，装不下（注册态字段多）就退回顶部对齐，而不是
     把表单顶出可滚动区域之外。 */
  .signin { grid-template-columns: 1fr; padding: 2rem 1.25rem; align-content: safe center; }
  .pitch { max-width: 28rem; text-align: center; }
  .features, .figure { display: none; }
  .wordmark { justify-content: center; }
  .tagline { margin-bottom: 1.8rem; }
}
</style>
