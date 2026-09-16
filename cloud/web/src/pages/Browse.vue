<script setup lang="ts">
/**
 * Configs other people have made public.
 *
 * Only `public` appears here. `unlisted` is reachable by its link and
 * deliberately not by browsing -- that is the whole difference between the two,
 * and it is enforced on the server, not by this filter.
 */
import { onMounted, ref } from "vue";
import { api, DOC_TYPE_NAMES, formatTime, type DocType, type Profile } from "../api";

const profiles = ref<Profile[]>([]);
const loading = ref(true);
const error = ref("");
const filter = ref<DocType | "">("");

async function load() {
  loading.value = true;
  error.value = "";
  try {
    profiles.value = await api.publicProfiles(filter.value || undefined);
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : "读取失败";
  } finally {
    loading.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div class="card">
    <header class="row">
      <h1>公开配置</h1>
      <select v-model="filter" @change="load">
        <option value="">全部类型</option>
        <option v-for="(name, key) in DOC_TYPE_NAMES" :key="key" :value="key">{{ name }}</option>
      </select>
    </header>

    <p v-if="loading">读取中…</p>
    <p class="error" v-else-if="error">{{ error }}</p>
    <p v-else-if="!profiles.length" class="hint">
      还没有人公开配置。你可以在「我的配置」里把某一份改成公开。
    </p>

    <ul v-else class="list">
      <li v-for="item in profiles" :key="item.id">
        <router-link :to="`/config/${item.id}`" class="title">{{ item.title }}</router-link>
        <p class="summary" v-if="item.summary">{{ item.summary }}</p>
        <div class="meta">
          <span class="tag">{{ DOC_TYPE_NAMES[item.doc_type] }}</span>
          <span v-if="item.game_name">{{ item.game_name }}</span>
          <span>{{ item.owner_name }}</span>
          <span v-if="item.current_version">v{{ item.current_version.revision_no }}</span>
          <span class="time">{{ formatTime(item.updated_at) }}</span>
        </div>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.row { display: flex; align-items: center; justify-content: space-between; gap: 1rem; }
.list { list-style: none; padding: 0; margin: 1.5rem 0 0; }
.list li { padding: 0.9rem 0; border-top: 1px solid var(--line); }
.title { font-size: 1.05rem; font-weight: 600; }
.summary { margin: 0.3rem 0 0; color: var(--muted); font-size: 0.9rem; }
.meta { display: flex; flex-wrap: wrap; gap: 0.75rem; margin-top: 0.35rem;
        font-size: 0.85rem; color: var(--muted); }
.tag { background: var(--chip); padding: 0.1rem 0.5rem; border-radius: 4px; }
</style>
