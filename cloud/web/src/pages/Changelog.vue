<script setup lang="ts">
/**
 * 每一版改了什么。
 *
 * 内容来自仓库里的 CHANGELOG.md，由 tools/build_changelog.py 转成 changelog.json
 * 随网站一起发。不在运行时解析 markdown：那要为一页静态内容给网站背一个 md 库，
 * 而转好的结构化数据以后还能喂给更新通道——玩家点「更新」之前就看得见这一版改了
 * 什么，而不是更完了不知道变了啥。
 *
 * 也不从 GitHub Release API 拉：用这个软件的人在国内，GitHub 时通时不通，而
 * 「更新日志打不开」会让人以为是软件坏了。
 */
import { onMounted, ref } from "vue";

type Section = { title: string; intro: string; items: string[] };
// channel 分开电脑端和手机端网页包两条版本线。两条线都可能出到 2.0.1，所以
// 版本号单独一个不足以标识一节，v-for 的 key 也得带上它。
type Release = { version: string; channel?: "app" | "web"; date: string; sections: Section[] };

const releases = ref<Release[]>([]);
const loading = ref(true);
const error = ref("");

/**
 * 把 `**加粗**` 拆成片段，交给模板去渲染。
 *
 * 不用 v-html：这份内容眼下是我们自己写的，但一个会把字符串当 HTML 塞进页面的
 * 渲染路径，迟早会被喂进别处来的文本。拆成数组之后这条路根本不存在。
 */
function parts(text: string) {
  // 一次分完两种标记。只拆加粗的话，`F9` 会带着反引号原样显示出来。
  return text.split(/\*\*(.+?)\*\*|`([^`]+)`/g)
    .map((piece, index) => ({
      text: piece ?? "",
      bold: index % 3 === 1,
      code: index % 3 === 2,
    }))
    .filter((piece) => piece.text !== "");
}

onMounted(async () => {
  try {
    const response = await fetch("/changelog.json", { cache: "no-cache" });
    if (!response.ok) throw new Error(`读取失败（${response.status}）`);
    releases.value = (await response.json()).releases ?? [];
  } catch (e: any) {
    error.value = e?.message || "读取失败";
  } finally {
    loading.value = false;
  }
});
</script>

<template>
  <div class="card">
    <h1>更新日志</h1>
    <p class="lede">
      各版本的变更记录，按对使用者的影响撰写。版本号遵循语义化版本规范：
      <code>2.0.x</code> 为缺陷修复，<code>2.x.0</code> 为新增功能，两者均不影响现有配置；
      <code>x.0.0</code> 含不兼容变更，会在对应条目中说明迁移方式。
    </p>

    <p v-if="loading" class="muted">读取中…</p>
    <p v-else-if="error" class="error">{{ error }}</p>
    <p v-else-if="!releases.length" class="muted">还没有记录。</p>

    <section v-for="release in releases" :key="`${release.channel ?? 'app'}-${release.version}`" class="release">
      <header>
        <h2>{{ release.version }}</h2>
        <span v-if="release.channel === 'web'" class="chan">仅手机网页 · 热更新</span>
        <time v-if="release.date">{{ release.date }}</time>
      </header>

      <div v-for="(section, index) in release.sections" :key="index" class="group">
        <h3 v-if="section.title">{{ section.title }}</h3>
        <p v-if="section.intro" class="intro">{{ section.intro }}</p>
        <ul v-if="section.items.length">
          <li v-for="(item, at) in section.items" :key="at">
            <template v-for="(piece, n) in parts(item)" :key="n">
              <strong v-if="piece.bold">{{ piece.text }}</strong>
              <code v-else-if="piece.code">{{ piece.text }}</code>
              <template v-else>{{ piece.text }}</template>
            </template>
          </li>
        </ul>
      </div>
    </section>

    <p class="fineprint">
      安装包下载：<a href="https://github.com/guiwushengzhe-prog/MotionControl/releases"
         target="_blank" rel="noopener">GitHub Release</a>。
      已安装的电脑端会在启动时自动检查更新，无需重新下载。
    </p>
  </div>
</template>

<style scoped>
h1 { margin: 0 0 6px; font-size: 22px; }
.lede { margin: 0 0 26px; color: var(--muted); line-height: 1.7; }
.lede code {
  padding: 1px 5px; border-radius: 4px;
  background: rgba(255, 255, 255, .07); font-size: 13px;
}
.release { margin: 0 0 34px; }
.release header {
  display: flex; align-items: baseline; gap: 12px;
  padding-bottom: 8px; margin-bottom: 14px;
  border-bottom: 1px solid var(--line);
}
.release h2 { margin: 0; font-size: 19px; }
/* 版本号本身看不出这一节只影响手机。不标出来，读的人会以为电脑端也得更新。 */
.release .chan {
  padding: 2px 8px; border-radius: 999px; font-size: 12px;
  color: var(--accent); background: rgba(255, 255, 255, .07);
}
.release time { font-size: 13px; color: var(--muted); margin-left: auto; }
.group { margin: 0 0 18px; }
.group h3 { margin: 0 0 8px; font-size: 14px; color: var(--accent); font-weight: 600; }
.intro { margin: 0 0 8px; color: var(--muted); line-height: 1.7; }
ul { margin: 0; padding-left: 20px; }
li { margin-bottom: 7px; line-height: 1.75; }
li code {
  padding: 1px 5px; border-radius: 4px;
  background: rgba(255, 255, 255, .07); font-size: 13px;
}
.muted { color: var(--muted); }
.error { color: #ff6b6b; }
.fineprint {
  margin: 30px 0 0; padding-top: 16px;
  border-top: 1px solid var(--line);
  font-size: 13px; color: var(--muted); line-height: 1.7;
}
</style>
