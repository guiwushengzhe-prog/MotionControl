<script setup lang="ts">
/**
 * One config: its history, and the buttons that act on it.
 *
 * The history is the point of this page. Versions are immutable, so every row
 * here is a file that can still be downloaded exactly as it was uploaded, and
 * rolling back adds a row rather than removing the ones after it. The list
 * therefore only ever grows, and "回滚到 v1" appears in it as its own entry
 * instead of the history appearing to rewind.
 *
 * Download is a plain link, not fetch(): the browser saves the bytes itself,
 * so nothing here can accidentally re-encode the file on its way to disk.
 */
import { computed, onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import {
  api, DOC_TYPE_NAMES, formatSize, formatTime,
  type Profile, type Summary, type Version, type Visibility,
} from "../api";
import { user } from "../session";

const route = useRoute();
const id = route.params.id as string;

const profile = ref<Profile | null>(null);
const versions = ref<Version[]>([]);
const summary = ref<Summary | null>(null);
const loading = ref(true);
const error = ref("");
const busy = ref("");
const notice = ref("");

const mine = computed(() => !!user.value && profile.value?.owner_id === user.value.id);

async function load() {
  loading.value = true;
  error.value = "";
  try {
    profile.value = await api.profile(id);
    versions.value = await api.versions(id);
    const current = profile.value.current_version;
    // 说明是服务端从文档本身生成的。它取不到不该让整页打不开——版本历史和
    // 下载按钮仍然有用。
    summary.value = current ? await api.summary(id, current.id).catch(() => null) : null;
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : "读取失败";
  } finally {
    loading.value = false;
  }
}

async function rollback(version: Version) {
  if (!confirm(`回滚到 v${version.revision_no}？\n\n这会新建一个版本，内容和 v${version.revision_no} 相同。中间的版本不会被删除。`)) return;
  busy.value = version.id;
  notice.value = "";
  try {
    const created = await api.rollback(id, version.id);
    notice.value = `已回滚：新建了 v${created.revision_no}，内容取自 v${version.revision_no}。`;
    await load();
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : "回滚失败";
  } finally {
    busy.value = "";
  }
}

async function setVisibility(value: Visibility) {
  try {
    profile.value = await api.updateProfile(id, { visibility: value });
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : "修改失败";
  }
}

/** Upload an edited copy as a new version, based on the current one. */
async function uploadNewVersion(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0];
  if (!file || !profile.value) return;
  busy.value = "new";
  error.value = "";
  notice.value = "";
  try {
    const parsed = JSON.parse(await file.text());
    const created = await api.addVersion(
      id, parsed, profile.value.current_version?.id ?? null, file.name);
    notice.value = created.id === profile.value.current_version?.id
      // Same bytes after normalisation, so no version was created -- worth
      // saying plainly, or it looks like the upload silently failed.
      ? "内容和当前版本完全相同（规范化之后），没有新建版本。"
      : `已提交 v${created.revision_no}。`;
    await load();
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : "提交失败";
  } finally {
    busy.value = "";
    (event.target as HTMLInputElement).value = "";
  }
}

onMounted(load);
</script>

<template>
  <div class="card">
    <p v-if="loading">读取中…</p>
    <p class="error" v-else-if="!profile">{{ error || "找不到这个配置" }}</p>

    <template v-else>
      <router-link to="/" class="back">← 我的配置</router-link>
      <header>
        <h1>{{ profile.title }}</h1>
        <div class="meta">
          <span class="tag">{{ DOC_TYPE_NAMES[profile.doc_type] }}</span>
          <span v-if="profile.game_name">{{ profile.game_name }}</span>
          <span>{{ profile.owner_name }}</span>
          <span class="time">更新于 {{ formatTime(profile.updated_at) }}</span>
        </div>
      </header>

      <div class="actions" v-if="mine">
        <label class="upload-button">
          提交新版本
          <input type="file" accept=".json,application/json" hidden
                 :disabled="busy === 'new'" @change="uploadNewVersion" />
        </label>
        <select :value="profile.visibility"
                @change="setVisibility(($event.target as HTMLSelectElement).value as Visibility)">
          <option value="private">私有</option>
          <option value="unlisted">链接可见</option>
          <option value="public">公开</option>
        </select>
      </div>

      <p class="notice" v-if="notice">{{ notice }}</p>
      <p class="error" v-if="error">{{ error }}</p>

      <!-- 这一段由服务端从存下来的文档生成，不是上传者写的简介：简介可能是空的、
           过时的，或者和文件内容根本对不上。 -->
      <section v-if="summary" class="summary">
        <h2>这份配置做什么</h2>
        <p class="hint small">
          由服务器从 v{{ summary.revision_no }} 的文件本身读出，逐条对应，不是上传者写的简介。
        </p>
        <p class="headline">{{ summary.headline }}</p>

        <template v-if="summary.games">
          <section v-for="game in summary.games" :key="game.game_id" class="game">
            <h3>
              <span class="game-id">{{ game.game_id }}</span>
              <span v-if="game.game_id === summary.selected_id" class="badge">上传时选中</span>
              <span class="count">{{ game.total }} 条</span>
            </h3>
            <div v-for="group in game.groups" :key="group.key" class="group">
              <h4>{{ group.name }}<span class="count">{{ group.items.length }}</span></h4>
              <!-- 超过 8 条时分两栏：20 条语音口令排成一列就是一面墙。 -->
              <div class="items" :class="{ split: group.items.length > 8 }">
                <div v-for="item in group.items" :key="item.trigger" class="item"
                     :class="{ shadowed: item.shadowed_matters }">
                  <span class="what">{{ item.name }}</span>
                  <span class="does" :class="{ off: item.disabled || item.shadowed_matters }">
                    {{ item.action }}
                    <!-- 合并之前存的配置里，同一块手部区域可能有两条绑定，运行时
                         只有一条生效。不标出来，用户会盯着一条从不触发的绑定找原因。 -->
                    <em v-if="item.shadowed_matters" class="warn">
                      不生效 · 被「{{ item.shadowed_by }}」覆盖
                    </em>
                    <em v-else-if="item.shadowed_by" class="alt">
                      与「{{ item.shadowed_by }}」是同一块区域
                    </em>
                    <em v-else-if="item.runtime_zone" class="alt">{{ item.runtime_zone }}</em>
                  </span>
                </div>
              </div>
            </div>
          </section>
        </template>

        <!-- 游戏方案：按键绑定和身体动作一起看，因为它们本来就是一件事。 -->
        <template v-else-if="summary.kind === 'game_bundle'">
          <section class="game">
            <h3>
              <span class="game-id">{{ summary.game_id }}</span>
              <span class="count">{{ summary.total }} 条</span>
            </h3>
            <div v-for="group in summary.groups" :key="group.key" class="group">
              <h4>{{ group.name }}<span class="count">{{ group.items.length }}</span></h4>
              <div class="items" :class="{ split: group.items.length > 8 }">
                <div v-for="item in group.items" :key="item.trigger" class="item"
                     :class="{ shadowed: item.shadowed_matters }">
                  <span class="what">{{ item.name }}</span>
                  <span class="does" :class="{ off: item.disabled || item.shadowed_matters }">
                    {{ item.action }}
                    <em v-if="item.shadowed_matters" class="warn">
                      不生效 · 被「{{ item.shadowed_by }}」覆盖
                    </em>
                    <em v-else-if="item.runtime_zone" class="alt">{{ item.runtime_zone }}</em>
                  </span>
                </div>
              </div>
            </div>
            <div class="group" v-if="summary.motions?.length">
              <h4>身体动作<span class="count">{{ summary.motions.length }}</span></h4>
              <div class="items" :class="{ split: summary.motions.length > 8 }">
                <div v-for="item in summary.motions" :key="item.name" class="item">
                  <span class="what" :class="{ off: !item.enabled }">{{ item.name }}</span>
                  <span class="does" :class="{ off: !item.enabled }">{{ item.action }}</span>
                </div>
              </div>
            </div>
          </section>
        </template>

        <template v-else-if="summary.kind === 'voice_mappings'">
          <p class="hint" v-if="summary.emergency_stop_phrases?.length">
            紧急停止口令：{{ summary.emergency_stop_phrases.join("、") }}
          </p>
          <div class="items" :class="{ split: (summary.items?.length ?? 0) > 8 }">
            <div v-for="item in summary.items" :key="item.phrase" class="item">
              <span class="what">{{ item.phrase }}</span>
              <span class="does">{{ item.action }}</span>
              <!-- 同义词单独一行。跟在名字后面会把名字列撑爆，每行高度都不一样，
                   两栏就对不齐了。 -->
              <span v-if="item.synonyms?.length" class="alt">
                也可以说：{{ item.synonyms.join("、") }}
              </span>
            </div>
          </div>
        </template>

        <template v-else>
          <div class="items" :class="{ split: (summary.items?.length ?? 0) > 8 }">
            <div v-for="item in summary.items" :key="item.name" class="item">
              <span class="what" :class="{ off: !item.enabled }">{{ item.name }}</span>
              <span class="does" :class="{ off: !item.enabled }">
                {{ item.action }}
                <em v-if="!item.enabled" class="alt">未启用</em>
              </span>
            </div>
          </div>
        </template>
      </section>

      <h2>版本历史</h2>
      <p class="hint">
        版本不可修改。回滚会新建一个版本，内容取自旧版本，中间的记录一条都不会消失。
      </p>

      <table>
        <thead>
          <tr><th>版本</th><th>说明</th><th>大小</th><th>SHA-256</th><th>时间</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="version in versions" :key="version.id"
              :class="{ current: version.id === profile.current_version?.id }">
            <td>
              v{{ version.revision_no }}
              <span v-if="version.id === profile.current_version?.id" class="badge">当前</span>
            </td>
            <td class="note">
              <template v-if="version.restored_from_id">
                回滚自 v{{ versions.find(v => v.id === version.restored_from_id)?.revision_no ?? "?" }}
              </template>
              <template v-else>{{ version.note || "—" }}</template>
            </td>
            <td>{{ formatSize(version.size_bytes) }}</td>
            <td><code :title="version.canonical_sha256">{{ version.canonical_sha256.slice(0, 12) }}</code></td>
            <td class="time">{{ formatTime(version.created_at) }}</td>
            <td class="right">
              <a :href="api.downloadUrl(profile.id, version.id)" download>下载</a>
              <button v-if="mine && version.id !== profile.current_version?.id"
                      class="link" :disabled="busy === version.id"
                      @click="rollback(version)">回滚到这里</button>
            </td>
          </tr>
        </tbody>
      </table>

      <p class="hint small">
        下载的文件和上传时记录的 SHA-256 完全一致，可以自己核对：
        <code>certutil -hashfile 文件名 SHA256</code>
      </p>
    </template>
  </div>
</template>

<style scoped>
.back { display: inline-block; margin-bottom: 1rem; font-size: 0.9rem; }
.meta { display: flex; flex-wrap: wrap; gap: 0.75rem; margin-top: 0.4rem;
        font-size: 0.85rem; color: var(--muted); }
.tag { background: var(--chip); padding: 0.1rem 0.5rem; border-radius: 4px; }
.actions { display: flex; gap: 0.75rem; align-items: center; margin: 1.5rem 0; }
.upload-button { display: inline-block; padding: 0.5rem 1rem; border-radius: 6px;
                 background: var(--accent); color: #fff; cursor: pointer; font-size: 0.9rem; }
table { width: 100%; border-collapse: collapse; margin-top: 1rem; font-size: 0.9rem; }
th, td { text-align: left; padding: 0.6rem 0.5rem; border-bottom: 1px solid var(--line); }
th { font-weight: 600; color: var(--muted); font-size: 0.8rem; }
tr.current { background: var(--chip); }
.badge { font-size: 0.72rem; background: var(--accent); color: #fff;
         padding: 0.05rem 0.4rem; border-radius: 3px; margin-left: 0.3rem; }
.right { text-align: right; white-space: nowrap; }
.right > * + * { margin-left: 0.75rem; }
.note { color: var(--muted); }
code { font-size: 0.82rem; }
.small { font-size: 0.82rem; }

.summary { margin-top: 2.25rem; }
.summary .headline {
  font-size: 1.15rem; font-weight: 600; margin: 0.5rem 0 0.25rem;
}
.game { margin-top: 1.5rem; }
.game h3 {
  display: flex; align-items: baseline; gap: 0.55rem;
  font-size: 0.95rem; margin: 0 0 0.5rem;
  padding-bottom: 0.4rem; border-bottom: 2px solid var(--line);
}
.game-id { font-family: ui-monospace, Consolas, monospace; }
.game h3 .count, .group h4 .count {
  margin-left: auto; font-weight: 400; font-size: 0.8rem; color: var(--muted);
}
.group { margin: 0.9rem 0 0; }
.group h4 {
  display: flex; align-items: baseline;
  font-size: 0.78rem; letter-spacing: 0.04em; color: var(--muted);
  margin: 0 0 0.15rem; font-weight: 600;
}

/* 每一项是一个两行网格：第一行「名字 …… 动作」，动作右对齐；第二行放同义词
   这类补充。之前把补充塞在名字后面，名字列被撑爆后折行，每行高度都不一样，
   分两栏时左右完全对不齐。 */
.items { display: grid; grid-template-columns: 1fr; column-gap: 2.5rem; }
@media (min-width: 820px) {
  .items.split { grid-template-columns: 1fr 1fr; }
}
.item {
  display: grid;
  grid-template-columns: minmax(4rem, auto) 1fr;
  align-items: baseline;
  column-gap: 1rem;
  padding: 0.38rem 0;
  border-bottom: 1px solid var(--line);
  font-size: 0.88rem;
}
.what { color: var(--muted); white-space: nowrap; }
/* 右对齐：动作长短不一时，右边界齐平比左边界齐平更好扫。 */
.does { text-align: right; }
.alt {
  grid-column: 1 / -1;
  font-style: normal; font-size: 0.76rem; color: var(--muted);
  margin-top: 0.1rem;
}
.warn { font-style: normal; font-size: 0.76rem; color: var(--error); margin-left: 0.4rem; }
.off { opacity: 0.5; }
.item.shadowed .what { text-decoration: line-through; }
</style>
