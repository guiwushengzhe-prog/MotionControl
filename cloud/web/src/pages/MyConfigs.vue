<script setup lang="ts">
/**
 * The configs you own, and the form for adding one.
 *
 * Upload is a file picker rather than a text box: these files live in
 * %LOCALAPPDATA%\MotionControl and people should send the real one, not paste
 * something they retyped. The document type is guessed from what is actually
 * inside the file, because the three filenames are easy to confuse and the
 * server would reject a mismatch with an error that reads like a bug.
 */
import { onMounted, ref } from "vue";
import { api, DOC_TYPE_NAMES, formatTime, type DocType, type Profile, type Visibility } from "../api";

const profiles = ref<Profile[]>([]);
const loading = ref(true);
const error = ref("");

const showForm = ref(false);
const title = ref("");
const docType = ref<DocType | "">("");
const document_ = ref<unknown>(null);
const fileName = ref("");
const visibility = ref<Visibility>("private");
const gameId = ref("");
const gameQuery = ref("");
const games = ref<{ id: string; name: string }[]>([]);
const busy = ref(false);
const formError = ref("");

/** Identify the document by its contents, not by its filename. */
function detectDocType(data: any): DocType | "" {
  if (data && typeof data === "object") {
    if ("overrides_by_profile" in data || "selected_id" in data) return "profile_selection";
    if (Array.isArray(data.motions)) return "motion_mappings";
    if (Array.isArray(data.mappings) || "wake_word" in data) return "voice_mappings";
  }
  return "";
}

async function load() {
  loading.value = true;
  try {
    profiles.value = await api.myProfiles();
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : "读取失败";
  } finally {
    loading.value = false;
  }
}

async function pickFile(event: Event) {
  formError.value = "";
  const file = (event.target as HTMLInputElement).files?.[0];
  if (!file) return;
  fileName.value = file.name;
  try {
    const parsed = JSON.parse(await file.text());
    document_.value = parsed;
    const detected = detectDocType(parsed);
    if (!detected) {
      formError.value = "认不出这是哪种配置。支持游戏映射、动作映射、语音映射三种。";
      return;
    }
    docType.value = detected;
    if (!title.value) title.value = `我的${DOC_TYPE_NAMES[detected]}`;
    if (detected === "profile_selection" && typeof parsed.selected_id === "string") {
      gameId.value = parsed.selected_id;
      gameQuery.value = parsed.selected_id;
    }
  } catch {
    formError.value = "这个文件不是有效的 JSON。";
  }
}

async function searchGames() {
  games.value = gameQuery.value.trim() ? await api.games(gameQuery.value) : [];
}

async function submit() {
  if (!docType.value || document_.value === null) {
    formError.value = "先选一个配置文件。";
    return;
  }
  formError.value = "";
  busy.value = true;
  try {
    await api.createProfile({
      doc_type: docType.value,
      title: title.value,
      document: document_.value,
      game_id: gameId.value || null,
      visibility: visibility.value,
    });
    showForm.value = false;
    document_.value = null;
    fileName.value = "";
    title.value = "";
    docType.value = "";
    await load();
  } catch (caught) {
    // The server's wording is the desktop validator's, so it is shown as-is.
    formError.value = caught instanceof Error ? caught.message : "上传失败";
  } finally {
    busy.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div class="card">
    <header class="row">
      <h1>我的配置</h1>
      <button @click="showForm = !showForm">{{ showForm ? "取消" : "上传配置" }}</button>
    </header>

    <form v-if="showForm" class="upload" @submit.prevent="submit">
      <label>
        配置文件
        <input type="file" accept=".json,application/json" @change="pickFile" />
        <small v-if="fileName">{{ fileName }} · 识别为{{ docType ? DOC_TYPE_NAMES[docType] : "未知" }}</small>
        <small v-else>在 %LOCALAPPDATA%\MotionControl\ 里，三个 .json 文件</small>
      </label>
      <label>
        标题
        <input v-model="title" required maxlength="120" />
      </label>
      <label v-if="docType === 'profile_selection'">
        对应游戏（可留空）
        <input v-model="gameQuery" @input="searchGames" placeholder="搜游戏名或 id" list="game-list" />
        <datalist id="game-list">
          <option v-for="game in games" :key="game.id" :value="game.id">{{ game.name }}</option>
        </datalist>
        <small v-if="gameId">已选 {{ gameId }}</small>
      </label>
      <label>
        可见性
        <select v-model="visibility">
          <option value="private">私有 —— 只有自己看得到</option>
          <option value="unlisted">不公开列出 —— 有链接的人能看</option>
          <option value="public">公开 —— 会出现在浏览页</option>
        </select>
      </label>
      <p class="error" v-if="formError">{{ formError }}</p>
      <button type="submit" :disabled="busy">{{ busy ? "上传中…" : "上传" }}</button>
    </form>

    <p v-if="loading">读取中…</p>
    <p class="error" v-else-if="error">{{ error }}</p>
    <p v-else-if="!profiles.length" class="hint">还没有上传过配置。</p>

    <ul v-else class="list">
      <li v-for="item in profiles" :key="item.id">
        <router-link :to="`/config/${item.id}`" class="title">{{ item.title }}</router-link>
        <div class="meta">
          <span class="tag">{{ DOC_TYPE_NAMES[item.doc_type] }}</span>
          <span v-if="item.game_name">{{ item.game_name }}</span>
          <span v-if="item.current_version">v{{ item.current_version.revision_no }}</span>
          <span :class="['vis', item.visibility]">
            {{ item.visibility === "public" ? "公开" : item.visibility === "unlisted" ? "链接可见" : "私有" }}
          </span>
          <span class="time">{{ formatTime(item.updated_at) }}</span>
        </div>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.row { display: flex; align-items: center; justify-content: space-between; gap: 1rem; }
.upload { display: flex; flex-direction: column; gap: 1rem; margin: 1.5rem 0;
          padding: 1.25rem; border: 1px solid var(--line); border-radius: 8px; }
.upload label { display: flex; flex-direction: column; gap: 0.35rem; font-size: 0.9rem; }
.upload small { color: var(--muted); font-size: 0.8rem; }
.list { list-style: none; padding: 0; margin: 1.5rem 0 0; }
.list li { padding: 0.9rem 0; border-top: 1px solid var(--line); }
.title { font-size: 1.05rem; font-weight: 600; }
.meta { display: flex; flex-wrap: wrap; gap: 0.75rem; margin-top: 0.35rem;
        font-size: 0.85rem; color: var(--muted); }
.tag { background: var(--chip); padding: 0.1rem 0.5rem; border-radius: 4px; }
.vis.public { color: var(--accent); }
</style>
