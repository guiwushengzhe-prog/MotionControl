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
// 可见性每次打开表单都回到私有，见 resetForm 里的说明。
const visibility = ref<Visibility>("private");
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

/** 网页读不到你的磁盘，文件只能由你选或拖进来——这是浏览器的安全模型，绕不过去。
 *  所以这里能做的是把「找到那个文件」这件事变容易：路径一键复制，粘进文件对话框
 *  的地址栏就直达，不用一层层点过去。 */
const CONFIG_DIR = "%LOCALAPPDATA%\\MotionControl";
const CONFIG_FILES = [
  "game_profile_selection.json",
  "motion_mappings.json",
  "voice_mappings.json",
];
const dragging = ref(false);
const copied = ref(false);

async function copyPath() {
  try {
    await navigator.clipboard.writeText(CONFIG_DIR);
    copied.value = true;
    setTimeout(() => (copied.value = false), 2000);
  } catch {
    formError.value = "复制失败，手动选中上面那行路径复制吧。";
  }
}

/** 一份待上传的文件：已解析、已识别类型。 */
interface Pending {
  fileName: string;
  docType: DocType;
  document: unknown;
  title: string;
  gameId: string;
}

const pending = ref<Pending[]>([]);

async function acceptFiles(files: FileList | undefined | null) {
  formError.value = "";
  if (!files || !files.length) return;
  const problems: string[] = [];
  for (const file of Array.from(files)) {
    try {
      const parsed = JSON.parse(await file.text());
      const detected = detectDocType(parsed);
      if (!detected) {
        problems.push(`${file.name}：认不出是哪种配置`);
        continue;
      }
      // 同一种类型只保留最后选的那份——一次传两个游戏映射没有意义，
      // 而静默忽略第二个会让人以为传上去了。
      const existing = pending.value.findIndex(p => p.docType === detected);
      const entry: Pending = {
        fileName: file.name,
        docType: detected,
        document: parsed,
        title: `我的${DOC_TYPE_NAMES[detected]}`,
        gameId: detected === "profile_selection" && typeof parsed.selected_id === "string"
          ? parsed.selected_id : "",
      };
      if (existing >= 0) pending.value[existing] = entry;
      else pending.value.push(entry);
    } catch {
      problems.push(`${file.name}：不是有效的 JSON`);
    }
  }
  if (problems.length) formError.value = problems.join("；");
}

function pickFile(event: Event) {
  const input = event.target as HTMLInputElement;
  const done = acceptFiles(input.files);
  // 清空，否则再选同一个文件不会触发 change。
  input.value = "";
  return done;
}

function dropFile(event: DragEvent) {
  dragging.value = false;
  return acceptFiles(event.dataTransfer?.files);
}

function removePending(index: number) {
  pending.value.splice(index, 1);
}

function resetForm() {
  pending.value = [];
  formError.value = "";
  // 可见性也要归位。不归位的话它会跨次沿用：上一份选了公开，下一份就默认公开
  // 而且没有任何提示——用户以为自己传的是私有的。其他字段忘了重置只是麻烦，
  // 这个忘了重置是把东西公开出去。
  visibility.value = "private";
}

async function submit() {
  if (!pending.value.length) {
    formError.value = "先选一个配置文件。";
    return;
  }
  formError.value = "";
  busy.value = true;
  const failed: string[] = [];
  try {
    // 一份一份传。某一份被服务端拒绝时，其余的已经传上去了——这比整批回滚好：
    // 用户看到的是"三个里有一个不行"，而不是"全都没传成，原因是其中一个"。
    for (const item of pending.value) {
      try {
        await api.createProfile({
          doc_type: item.docType,
          title: item.title,
          document: item.document,
          game_id: item.gameId || null,
          visibility: visibility.value,
        });
      } catch (caught) {
        // 服务端的措辞就是桌面校验器的措辞，原样显示。
        failed.push(`${item.fileName}：${caught instanceof Error ? caught.message : "上传失败"}`);
      }
    }
    if (failed.length) {
      formError.value = failed.join("；");
      pending.value = pending.value.filter(
        item => failed.some(message => message.startsWith(item.fileName)));
    } else {
      showForm.value = false;
      resetForm();
    }
    await load();
  } finally {
    busy.value = false;
  }
}

function toggleForm() {
  showForm.value = !showForm.value;
  if (showForm.value) resetForm();
}

onMounted(load);
</script>

<template>
  <div class="card">
    <header class="row">
      <h1>我的配置</h1>
      <button @click="toggleForm">{{ showForm ? "取消" : "上传配置" }}</button>
    </header>

    <form v-if="showForm" class="upload" @submit.prevent="submit">
      <div
        class="dropzone"
        :class="{ dragging, loaded: pending.length > 0 }"
        @dragover.prevent="dragging = true"
        @dragleave.prevent="dragging = false"
        @drop.prevent="dropFile"
      >
        <strong>{{ pending.length ? "再拖一个，或者" : "把配置文件拖到这里" }}</strong>
        <label class="pick">
          {{ pending.length ? "继续添加" : "或者选择文件" }}
          <!-- multiple：三个文件可以一次选完。少了它，文件对话框里选三个只会
               取第一个，而且不会有任何提示。 -->
          <input type="file" accept=".json,application/json" multiple hidden @change="pickFile" />
        </label>

        <div class="where" v-if="!pending.length">
          <span class="muted">文件在这个文件夹里，复制后粘到文件对话框的地址栏可以直达：</span>
          <div class="path">
            <code>{{ CONFIG_DIR }}</code>
            <button type="button" class="ghost" @click="copyPath">
              {{ copied ? "已复制" : "复制" }}
            </button>
          </div>
          <ul>
            <li v-for="name in CONFIG_FILES" :key="name"><code>{{ name }}</code></li>
          </ul>
          <span class="muted">三个可以一次选完。</span>
        </div>
      </div>

      <ul class="pending" v-if="pending.length">
        <li v-for="(item, index) in pending" :key="item.fileName">
          <div class="head">
            <span class="tag">{{ DOC_TYPE_NAMES[item.docType] }}</span>
            <code>{{ item.fileName }}</code>
            <button type="button" class="ghost" @click="removePending(index)">移除</button>
          </div>
          <input v-model="item.title" required maxlength="120" aria-label="标题" />
          <small v-if="item.gameId" class="muted">对应游戏：{{ item.gameId }}</small>
        </li>
      </ul>

      <label>
        可见性
        <select v-model="visibility">
          <option value="private">私有 —— 只有自己看得到</option>
          <option value="unlisted">不公开列出 —— 有链接的人能看</option>
          <option value="public">公开 —— 会出现在浏览页</option>
        </select>
        <small class="muted">这一批全部用这个可见性。传完会自动回到「私有」。</small>
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

.dropzone {
  display: flex; flex-direction: column; align-items: center; gap: 0.6rem;
  padding: 1.75rem 1.25rem; text-align: center;
  border: 2px dashed var(--line); border-radius: 10px;
  transition: border-color .12s, background .12s;
}
.dropzone.dragging { border-color: var(--accent); background: var(--chip); }
.dropzone.loaded { border-style: solid; }
/* 真正的 input 是隐藏的，这个 label 就是那颗按钮——点它等于点 input。 */
.pick {
  display: inline-block; padding: 0.45rem 0.9rem; border-radius: 6px;
  background: var(--accent); color: #fff; font-size: 0.9rem; cursor: pointer;
}
.where { margin-top: 0.4rem; font-size: 0.82rem; }
.path { display: flex; align-items: center; justify-content: center;
        gap: 0.5rem; margin: 0.4rem 0; }
.path code { font-size: 0.85rem; }
.ghost { background: none; border: 1px solid var(--line); color: var(--accent);
         padding: 0.15rem 0.5rem; font-size: 0.8rem; }
.where ul { list-style: none; padding: 0; margin: 0.3rem 0 0;
            display: flex; flex-wrap: wrap; gap: 0.4rem; justify-content: center; }
.list { list-style: none; padding: 0; margin: 1.5rem 0 0; }
.list li { padding: 0.9rem 0; border-top: 1px solid var(--line); }
.title { font-size: 1.05rem; font-weight: 600; }
.meta { display: flex; flex-wrap: wrap; gap: 0.75rem; margin-top: 0.35rem;
        font-size: 0.85rem; color: var(--muted); }
.tag { background: var(--chip); padding: 0.1rem 0.5rem; border-radius: 4px; }
.vis.public { color: var(--accent); }
</style>
