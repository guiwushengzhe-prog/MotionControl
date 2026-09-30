// 入口。功能模块按依赖先后加载（先共享状态和小工具），全部就绪后启动。
import './js/state.js';
import './js/core.js';
import './js/labels.js';
import './js/voice.js';
import './js/mapping.js';
import './js/play.js';
import './js/devices.js';
import './js/view-control.js';
import './js/body.js';
import './js/diagnostics.js';
import './js/library.js';
import './js/macros.js';
import './js/cloud.js';
import {init} from './js/shell.js';

init();
