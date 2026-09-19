<script setup lang="ts">
/**
 * 给开发者留言。不需要登录。
 *
 * 这个软件是发网盘和 GitHub Release 的，绝大多数用的人不会有账号——注册还要
 * 邀请码。真正会卡住的新手，恰恰是最不可能为了说一句"我这儿打不开"去走一遍
 * 注册流程的人。把登录设成前提，等于这个入口不存在。
 *
 * 联系方式选填，不校验格式：邮箱、QQ、微信都行。收到一个"我 QQ 是 xxx"比收到
 * 一个空字段有用得多，而强制邮箱会让一部分人干脆不填。
 */
import { ref } from "vue";
import { api, type FeedbackKind } from "../api";

const KINDS: { value: FeedbackKind; label: string; hint: string }[] = [
  { value: "bug", label: "出问题了", hint: "哪一步、你看到了什么、期望是什么" },
  { value: "idea", label: "想要个功能", hint: "你想用它做什么，现在为什么做不到" },
  { value: "question", label: "有疑问", hint: "" },
  { value: "other", label: "其他", hint: "" },
];

const kind = ref<FeedbackKind>("bug");
const message = ref("");
const contact = ref("");
const appVersion = ref("");
const busy = ref(false);
const error = ref("");
const done = ref(false);

const QQ_GROUP = "1101605483";
const copied = ref(false);

async function copyGroup() {
  try {
    await navigator.clipboard.writeText(QQ_GROUP);
  } catch {
    // 非 https 或者浏览器不给权限时 clipboard 不可用。群号本来就显示在旁边，
    // 手敲十位数字不是负担，所以这里不弹错误吓人一跳。
  }
  copied.value = true;
  setTimeout(() => (copied.value = false), 2000);
}

function hintFor(value: FeedbackKind) {
  return KINDS.find((k) => k.value === value)?.hint ?? "";
}

async function send() {
  error.value = "";
  if (!message.value.trim()) {
    error.value = "说点什么吧";
    return;
  }
  busy.value = true;
  try {
    await api.sendFeedback({
      kind: kind.value,
      message: message.value,
      contact: contact.value,
      app_version: appVersion.value,
    });
    done.value = true;
  } catch (e: any) {
    error.value = e?.message || "发送失败，稍后再试";
  } finally {
    busy.value = false;
  }
}

function again() {
  done.value = false;
  message.value = "";
  error.value = "";
}
</script>

<template>
  <div class="card" v-if="done">
    <h1>收到了</h1>
    <p class="lede">谢谢。我会看，但不保证很快——这是一个人在做的项目。</p>
    <p class="lede" v-if="contact">
      需要回你的话，我会用你留的联系方式找你。
    </p>
    <p class="lede" v-else>
      你没留联系方式，所以这条我只能看、没法回。想要回复的话可以再发一条带上。
    </p>
    <p class="lede">
      等不及的话，QQ 群 <strong>{{ QQ_GROUP }}</strong> 里问会快很多。
    </p>
    <div class="row">
      <button class="ghost" @click="again">再写一条</button>
      <RouterLink to="/browse" class="ghost link">去看看公开配置</RouterLink>
    </div>
  </div>

  <form class="card" v-else @submit.prevent="send">
    <h1>反馈与联系</h1>
    <p class="lede">
      不用注册，直接写。用着不对劲、想要什么功能、哪一步卡住了，都可以说。
    </p>

    <!-- 排在表单前面：装不上、连不上这类问题，群里问比等我回快得多，
         而且别人多半已经踩过同一个坑。表单适合说得长、或者不想进群的人。 -->
    <div class="group">
      <div>
        <strong>急着解决问题？QQ 群 <span class="num">{{ QQ_GROUP }}</span></strong>
        <p>装不上、连不上、动作不认，群里问最快，也能看到别人是怎么解决的。</p>
      </div>
      <button type="button" class="ghost" @click="copyGroup">
        {{ copied ? "已复制" : "复制群号" }}
      </button>
    </div>

    <label class="field">
      <span>这是</span>
      <div class="kinds">
        <button
          v-for="k in KINDS"
          :key="k.value"
          type="button"
          class="kind"
          :class="{ on: kind === k.value }"
          @click="kind = k.value"
        >{{ k.label }}</button>
      </div>
    </label>

    <label class="field">
      <span>想说的</span>
      <textarea
        v-model="message"
        rows="8"
        maxlength="4000"
        :placeholder="hintFor(kind) || '写清楚一点，我更容易帮上忙'"
        required
      ></textarea>
      <small class="count">{{ message.length }} / 4000</small>
    </label>

    <div class="two">
      <label class="field">
        <span>怎么联系你<em>选填</em></span>
        <input
          v-model="contact"
          maxlength="120"
          placeholder="邮箱 / QQ / 微信，随便哪个"
        />
        <small>不填也能提交，但那样我就没法回你。</small>
      </label>

      <label class="field">
        <span>你用的版本<em>选填</em></span>
        <input v-model="appVersion" maxlength="32" placeholder="例如 2.0.0" />
        <small>在软件界面左上角能看到。</small>
      </label>
    </div>

    <p class="error" v-if="error">{{ error }}</p>

    <div class="row">
      <button type="submit" :disabled="busy">{{ busy ? "发送中…" : "发送" }}</button>
      <span class="note">
        也可以去
        <a href="https://github.com/guiwushengzhe-prog/MotionControl/issues"
           target="_blank" rel="noopener">GitHub 提 issue</a>，
        那边是公开的，别人也能看到和搭话。
      </span>
    </div>
  </form>
</template>

<style scoped>
h1 { margin: 0 0 6px; font-size: 22px; }
.lede { margin: 0 0 18px; color: var(--muted); line-height: 1.7; }
.field { display: block; margin-bottom: 18px; }
.field > span {
  display: flex; align-items: baseline; gap: 8px;
  margin-bottom: 6px; font-weight: 600;
}
.field em { font-style: normal; font-weight: 400; font-size: 12px; color: var(--muted); }
.field small { display: block; margin-top: 5px; font-size: 12px; color: var(--muted); }
textarea, input { width: 100%; }
textarea { resize: vertical; min-height: 120px; line-height: 1.7; }
.count { text-align: right; }
.two { display: grid; grid-template-columns: 1fr 1fr; gap: 0 18px; }
.kinds { display: flex; flex-wrap: wrap; gap: 8px; }
.kind {
  padding: 7px 14px; border-radius: 999px; cursor: pointer;
  background: transparent; border: 1px solid var(--line); color: var(--text);
}
.kind.on { border-color: var(--accent); color: var(--accent); }
.group {
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  margin: 0 0 22px; padding: 14px 16px;
  border: 1px solid var(--line); border-radius: 10px;
}
.group p { margin: 4px 0 0; font-size: 13px; color: var(--muted); }
.group .num { font-variant-numeric: tabular-nums; letter-spacing: .5px; }
.group button { white-space: nowrap; }
.row { display: flex; flex-wrap: wrap; align-items: center; gap: 14px; }
.note { font-size: 13px; color: var(--muted); }
.link { display: inline-block; text-decoration: none; }
.error { color: #ff6b6b; margin: 0 0 14px; }
@media (max-width: 640px) { .two { grid-template-columns: 1fr; } }
</style>
