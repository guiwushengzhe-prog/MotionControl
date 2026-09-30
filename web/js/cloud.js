// 本游戏 → 云端配置。
import {api,post} from './core.js';
import {refreshProfile} from './mapping.js';
import {gameProfile} from './state.js';
import {refreshVoice,renderVoiceRows,voice,voiceRowsFromStatus} from './voice.js';

/* --- 云端配置 -----------------------------------------------------------
 * Browsing and installing configs other people have published.
 *
 * Nothing else on this page depends on any of it. The cloud is optional, and
 * when it is unreachable this section says so and everything else -- camera,
 * zones, voice, the whole controller -- carries on exactly as before. That is
 * why every call here is in its own try/catch and none of them run at startup.
 */
const cloudStatusEl = document.getElementById('cloudStatus');

const cloudListEl = document.getElementById('cloudList');

const cloudRefreshBtn = document.getElementById('cloudRefreshBtn');

function cloudSay(text, kind = '') {
  if (!cloudStatusEl) return;
  cloudStatusEl.textContent = text;
  cloudStatusEl.className = kind === 'error' ? 'statusline error' : 'statusline';
}

/** 云端地址，从服务端读一次，用来拼「在网站上打开」的链接。 */
let cloudEndpoint = '';

export async function cloudRefresh() {
  if (!cloudListEl) return;
  cloudSay('正在连接云端…');
  cloudListEl.innerHTML = '';
  try {
    const status = await api('/api/cloud/status', { timeoutMs: 12000 });
    cloudEndpoint = status.endpoint || '';
    if (!status.reachable) {
      cloudSay(`连不上 ${status.endpoint}：${status.error || '未知原因'}`, 'error');
      return;
    }

    // 只查当前这个游戏。桌面端要回答的问题是"我现在玩的这个游戏有什么现成配置"，
    // 不是"云端一共有什么"——后者配置一多就是一堵墙，那是网站该干的事。
    // 服务端把没有游戏的配置（动作映射、语音映射）也算作与当前游戏相关：它们对
    // 每个游戏都适用，筛掉等于藏起最该出现的那几份。
    const gameId = gameProfile.selected?.selected_id || gameProfile.selected?.id || '';
    const gameName = gameProfile.selected?.name || gameId;
    const { profiles } = await post('/api/cloud/browse', { game_id: gameId }, 15000);
    if (!profiles.length) {
      cloudSay(`《${gameName}》还没有人公开分享配置。`);
      return;
    }
    cloudSay(`《${gameName}》· ${profiles.length} 份`);
    for (const item of profiles) cloudListEl.appendChild(cloudRow(item));
  } catch (error) {
    cloudSay(error.message, 'error');
  }
}

/** 只要地址。以前只有「查看分享」会读它，于是没先点那个就点「打开网站」，只会说没连上。 */
export async function loadCloudEndpoint() {
  if (cloudEndpoint) return cloudEndpoint;
  const status = await api('/api/cloud/status', { timeoutMs: 12000 });
  cloudEndpoint = status.endpoint || '';
  return cloudEndpoint;
}

/** 在系统浏览器里打开网站上的某一页。详情、浏览全部、上传都在那边。 */
async function openOnSite(path = '') {
  if (cloudEndpoint) { window.open(cloudEndpoint + path, '_blank', 'noopener'); return; }
  // 地址还没读到：先趁这一下点击开一个空窗口，读到了再跳过去。等读完再开的话，
  // 那一下点击已经过期，浏览器会把它当弹窗拦掉。
  const win = window.open('about:blank', '_blank');
  try {
    const endpoint = await loadCloudEndpoint();
    if (!endpoint) throw new Error('没有配置云端地址');
    if (win) { win.opener = null; win.location.href = endpoint + path; }
    else window.open(endpoint + path, '_blank', 'noopener');
  } catch (error) {
    win?.close();
    cloudSay(error.message || '读不到云端地址', 'error');
  }
}

const CLOUD_DOC_NAMES = {
  profile_selection: '游戏映射',
  motion_mappings: '动作映射',
  voice_mappings: '语音映射',
};

function cloudRow(item) {
  const row = document.createElement('div');
  row.className = 'profile-bar cloud-row';

  const label = document.createElement('div');
  label.className = 'cloud-row-label';
  // 标题就是去网站看详情的入口——这里只放够认出它的信息，绑了哪些键、改了什么
  // 都在网站上，塞进这个小面板只会两边都说不清楚。
  const name = document.createElement('a');
  name.href = '#';
  name.className = 'cloud-row-title';
  name.textContent = item.title;
  name.title = '在网站上查看这份配置的详细内容';
  name.addEventListener('click', event => {
    event.preventDefault();
    openOnSite(`/config/${item.id}`);
  });
  const meta = document.createElement('span');
  meta.className = 'muted';
  const parts = [CLOUD_DOC_NAMES[item.doc_type] || item.doc_type, item.owner_name];
  // 没有游戏的那两种是通用配置，标出来，否则在"当前游戏"的列表里看着突兀。
  parts.push(item.game_name || '所有游戏通用');
  if (item.current_version) parts.push(`v${item.current_version.revision_no}`);
  meta.textContent = parts.join(' · ');
  label.append(name, meta);

  const button = document.createElement('button');
  button.className = 'btn primary';
  button.textContent = '安装';
  button.addEventListener('click', () => cloudInstall(item, button));

  row.append(label, button);
  return row;
}

async function cloudInstall(item, button) {
  // The config in hand may carry bindings for many games; installing someone's
  // shared setup should not replace the user's whole library, so when the
  // publisher named a game only that game's mappings are taken.
  const scope = item.game_id ? `《${item.game_name || item.game_id}》的映射` : '整份配置';
  if (!confirm(
      `安装「${item.title}」？\n\n` +
      `会应用${scope}，并切换到该游戏。\n` +
      `你现在的配置会先备份到用户目录的 cloud_backup 下，随时可以拿回来。`)) return;

  button.disabled = true;
  const original = button.textContent;
  button.textContent = '安装中…';
  cloudSay('正在下载并校验…');
  try {
    const result = await post('/api/cloud/install', {
      profile_id: item.id,
      game_id: item.game_id || '',
    }, 30000);
    const installed = result.installed;
    cloudSay(
      `已安装「${installed.title}」v${installed.revision_no}` +
      `（校验值 ${installed.sha256.slice(0, 12)}）` +
      (result.backup ? `，原配置已备份到 ${result.backup}` : ''));
    // Re-read the panels the install changed, so the page shows what is now
    // actually loaded rather than what was there before.
    // Re-read whichever panel the install changed, so the page shows what is
    // now actually loaded rather than what was there a moment ago.
    try {
      if (installed.doc_type === 'voice_mappings') {
        await refreshVoice();
        renderVoiceRows(voiceRowsFromStatus(voice.status));
      } else {
        // Motions are not a panel of their own: they are the motion.* rows of
        // the game profile, so refreshing the profile covers them too.
        await refreshProfile();
      }
    } catch { /* the install succeeded; a stale panel is not worth an error */ }
  } catch (error) {
    cloudSay(`安装失败：${error.message}`, 'error');
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

cloudRefreshBtn?.addEventListener('click', cloudRefresh);

document.getElementById('cloudSiteBtn')?.addEventListener('click', () => openOnSite('/'));
