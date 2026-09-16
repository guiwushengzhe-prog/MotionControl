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
  type Profile, type Version, type Visibility,
} from "../api";
import { user } from "../session";

const route = useRoute();
const id = route.params.id as string;

const profile = ref<Profile | null>(null);
const versions = ref<Version[]>([]);
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
</style>
