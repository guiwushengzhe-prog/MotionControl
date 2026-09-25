<script setup lang="ts">
/**
 * 官方动作库：官方发布的动作，每个配一个一直在做示范的火柴人和星级。
 *
 * 这里只能看，下载在电脑端做——电脑端「本游戏」页的「动作库 → 官方动作库」。动作文件
 * 里有识别规则，电脑那边只装验得过官方签名的，网页上点下载反而绕开了那一步。
 *
 * 火柴人的点和连线由服务器给（和电脑端动作库同一份数据），这里只画。
 */
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { api, type PoseAction, type PoseFrame } from "../api";

const actions = ref<PoseAction[]>([]);
const ratingNames = ref<Record<string, string>>({});
const partNames = ref<Record<string, string>>({});
const loading = ref(true);
const error = ref("");
const part = ref("");
const order = ref<"default" | "intensity" | "difficulty">("default");

// 每个动作当前放到第几帧。一个计时器管全部，按各自的 frame_s 换帧。
const frameAt = ref<Record<string, number>>({});
let timer = 0;
const nextAt: Record<string, number> = {};

async function load() {
  loading.value = true;
  error.value = "";
  try {
    const data = await api.poseLibrary();
    actions.value = data.actions;
    ratingNames.value = data.rating_names;
    partNames.value = data.body_part_names;
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : "读取失败";
  } finally {
    loading.value = false;
  }
}

function tick() {
  const now = performance.now();
  const next = { ...frameAt.value };
  for (const item of actions.value) {
    const count = item.demo.frames.length;
    if (count < 2 || now < (nextAt[item.id] ?? 0)) continue;
    next[item.id] = ((next[item.id] ?? 0) + 1) % count;
    nextAt[item.id] = now + item.demo.frame_s * 1000;
  }
  frameAt.value = next;
}

onMounted(() => {
  load();
  timer = window.setInterval(tick, 80);
});
onBeforeUnmount(() => window.clearInterval(timer));

const shown = computed(() => {
  let list = actions.value.filter(item => !part.value || (item.body_parts[part.value] ?? 0) > 0);
  if (order.value === "intensity") {
    list = [...list].sort((a, b) => b.ratings.intensity - a.ratings.intensity);
  } else if (order.value === "difficulty") {
    list = [...list].sort((a, b) => a.ratings.difficulty - b.ratings.difficulty);
  } else if (part.value) {
    // 按部位筛的时候，练这个部位最多的排前面。
    list = [...list].sort((a, b) => (b.body_parts[part.value] ?? 0) - (a.body_parts[part.value] ?? 0));
  }
  return list;
});

function frameOf(item: PoseAction): PoseFrame {
  return item.demo.frames[frameAt.value[item.id] ?? 0] ?? item.demo.frames[0];
}

// 点在 0~1 的框里，画到 100×100 的画布上，四边留 5。
function at(frame: PoseFrame, name: string): [number, number] | null {
  const point = frame.points[name];
  return point ? [point[0] * 90 + 5, point[1] * 90 + 5] : null;
}

function bones(frame: PoseFrame) {
  return frame.bones
    .map(([a, b]) => [at(frame, a), at(frame, b)] as const)
    .filter((pair): pair is readonly [[number, number], [number, number]] => !!pair[0] && !!pair[1]);
}

function joints(frame: PoseFrame) {
  return Object.keys(frame.points).map(name => ({ name, xy: at(frame, name)! }));
}

const stars = (count: number) => "★".repeat(count) + "☆".repeat(Math.max(0, 5 - count));

function parts(item: PoseAction) {
  return Object.entries(item.body_parts).sort((a, b) => b[1] - a[1]);
}
</script>

<template>
  <div class="card">
    <header class="row">
      <div>
        <h1>官方动作库</h1>
        <p class="hint">
          官方发布的身体动作。电脑端自带原地踏步和小腿向后抬起，别的动作在电脑端
          「本游戏 → 动作库 → 官方动作库」里点「下载」，下载了才认得出来。
        </p>
      </div>
    </header>

    <div class="filters" v-if="actions.length">
      <label>
        锻炼部位
        <select v-model="part">
          <option value="">全部</option>
          <option v-for="(name, key) in partNames" :key="key" :value="key">{{ name }}</option>
        </select>
      </label>
      <label>
        排序
        <select v-model="order">
          <option value="default">默认</option>
          <option value="intensity">运动强度从高到低</option>
          <option value="difficulty">上手从易到难</option>
        </select>
      </label>
    </div>

    <p v-if="loading">读取中…</p>
    <p class="error" v-else-if="error">{{ error }}</p>
    <p v-else-if="!actions.length" class="hint">官方动作库里暂时还没有发布的动作。</p>
    <p v-else-if="!shown.length" class="hint">没有练这个部位的动作。</p>

    <ul v-else class="grid">
      <li v-for="item in shown" :key="item.id" class="action">
        <svg class="figure" viewBox="0 0 100 100" :aria-label="`${item.name}的示范`" role="img">
          <line v-for="([from, to], index) in bones(frameOf(item))" :key="index"
                :x1="from[0]" :y1="from[1]" :x2="to[0]" :y2="to[1]" />
          <circle v-for="joint in joints(frameOf(item))" :key="joint.name"
                  :cx="joint.xy[0]" :cy="joint.xy[1]" :r="joint.name === 'nose' ? 4 : 2.4" />
        </svg>
        <div class="body">
          <h2>{{ item.name }}</h2>
          <p class="how">{{ item.how }}</p>
          <dl class="ratings">
            <template v-for="(label, key) in ratingNames" :key="key">
              <dt>{{ label }}</dt>
              <dd :title="`${item.ratings[key]} 星（满分 5 星）`">{{ stars(item.ratings[key] ?? 0) }}</dd>
            </template>
          </dl>
          <p class="parts">
            锻炼：<span v-for="[key, count] in parts(item)" :key="key" class="tag">
              {{ partNames[key] ?? key }} {{ "★".repeat(count) }}
            </span>
          </p>
          <p class="meta">第 {{ item.revision }} 版 · {{ item.group === "pose" ? "姿势" : "身体动作" }}</p>
        </div>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.row { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; }
.filters { display: flex; flex-wrap: wrap; gap: 1rem; margin: 1rem 0 0.5rem; font-size: 0.9rem; color: var(--muted); }
.filters label { display: flex; align-items: center; gap: 0.5rem; }
.grid { list-style: none; padding: 0; margin: 1rem 0 0; display: grid;
        grid-template-columns: repeat(auto-fill, minmax(min(100%, 24rem), 1fr)); gap: 0.9rem; }
.action { display: flex; gap: 0.9rem; padding: 0.9rem; border: 1px solid var(--line);
          border-radius: var(--radius); }
.figure { flex: none; width: 6.5rem; height: 6.5rem; background: var(--chip); border-radius: 6px; }
.figure line { stroke: var(--text); stroke-width: 2.4; stroke-linecap: round; opacity: 0.75; }
.figure circle { fill: var(--text); }
.body { min-width: 0; }
.body h2 { margin: 0; font-size: 1.05rem; }
.how { margin: 0.25rem 0 0.5rem; font-size: 0.88rem; color: var(--muted); }
.ratings { display: grid; grid-template-columns: auto auto; justify-content: start; column-gap: 0.8rem;
           margin: 0; font-size: 0.85rem; }
.ratings dt { color: var(--muted); white-space: nowrap; }
.ratings dd { margin: 0; color: var(--accent); letter-spacing: 1px; white-space: nowrap; }
.parts { margin: 0.5rem 0 0; font-size: 0.85rem; color: var(--muted); display: flex; flex-wrap: wrap; gap: 0.3rem; align-items: center; }
.tag { background: var(--chip); color: var(--text); padding: 0.05rem 0.45rem; border-radius: 4px; }
.meta { margin: 0.4rem 0 0; font-size: 0.8rem; color: var(--muted); }
</style>
