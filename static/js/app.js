/* Autospeed 前端 —— Vue3 + Element Plus（照 emby-plus 模板：单页壳 + activeMenu 切换） */
(function () {
  const { createApp, ref, reactive, computed, onMounted, watch, nextTick } = Vue;
  const { ElMessage, ElMessageBox } = ElementPlus;
  const BOOT = window.__BOOT__ || {};

  // ---------- 令牌 ----------
  const urlTok = new URLSearchParams(location.search).get('token');
  if (urlTok) localStorage.setItem('as_token', urlTok);
  const api = axios.create({ timeout: 60000 });
  api.interceptors.request.use(cfg => {
    const t = localStorage.getItem('as_token');
    if (t) cfg.headers['X-Auth-Token'] = t;
    return cfg;
  });

  const app = createApp({
    setup() {
      const needToken = ref(false);
      const tokenInput = ref('');
      const activeMenu = ref('dashboard');
      const drawer = ref(false);
      const isMobile = ref(window.matchMedia('(max-width: 768px)').matches);
      window.matchMedia('(max-width: 768px)')
        .addEventListener('change', e => { isMobile.value = e.matches; });

      const menus = [
        { key: 'dashboard', label: '仪表盘', icon: 'Odometer' },
        { key: 'run', label: '即时测速', icon: 'VideoPlay' },
        { key: 'schedule', label: '定时设置', icon: 'Timer' },
        { key: 'backend', label: '后端与节点', icon: 'Connection' },
        { key: 'notify', label: '企业微信', icon: 'Bell' },
        { key: 'system', label: '系统', icon: 'Setting' },
      ];
      const currentMenu = computed(() =>
        menus.find(m => m.key === activeMenu.value) || menus[0]);

      const settings = reactive(Object.assign({}, BOOT.settings || {}));
      const results = ref(BOOT.results || []);
      const total = ref(BOOT.total || 0);
      const nextRun = ref(BOOT.next_run || '');
      const schedState = ref(BOOT.sched_state || '');

      const page = ref(1);
      const pageSize = 10;
      // 服务端分页：results 始终只保存"当前页"
      const pagedResults = computed(() => results.value);

      const servers = ref([]);
      const serverErr = ref('');
      const loadingServers = ref(false);
      // 预置地区策略（来自 /api/servers 的 regions，count = 当前池内可用节点数）
      const regions = ref([]);
      // 注意：Ookla 下发的是 "CHINA" 全称，必须用后端归一后的 norm_country 比对，
      // 直接比 country === 'CN' 会恒为 0（老版本的 bug）。
      const cnCount = computed(() => servers.value.filter(s => s.norm_country === 'CN').length);
      const nodeHealth = ref([]);
      const backends = ref([
        { id: 'ookla', label: 'Ookla', desc: '官方 CLI，三项齐全；节点池由 Ookla 下发' },
        { id: 'http', label: '国内直连', desc: '下行走国内镜像；上行由 Ookla 兜底补测' },
        { id: 'librespeed', label: 'LibreSpeed', desc: '自建或可用的 LibreSpeed 实例' },
      ]);

      const running = ref(false);
      const selftesting = ref(false);
      const selftest = ref(null);
      const job = ref(null);
      let pollTimer = null;

      const runForm = reactive({
        backend: BOOT.settings?.backend || 'ookla',
        mode: BOOT.settings?.mode || 'closest',
        server_id: BOOT.settings?.server_id || '',
        server_keyword: BOOT.settings?.server_keyword || '',
        server_region: BOOT.settings?.server_region || 'asia',
        strict_mode: BOOT.settings?.strict_mode || '1',
      });
      const notifyForm = reactive({ wecom_secret: '' });
      const sysForm = reactive({ auth_token: '' });
      const timeframe = ref('7');
      const wechatMsg = ref('');
      const wechatOk = ref(false);
      const testingWechat = ref(false);

      const fmt = v => (v === null || v === undefined) ? '—' : Number(v).toFixed(2);

      // 最新一条单独保存，翻页时统计卡不应跟着变
      const latest = ref((BOOT.results || [])[0] || null);
      const statCards = computed(() => {
        const r = latest.value;
        const down = r ? Number(r.download) : 0;
        const cls = !r ? '' : down >= 500 ? 'green' : down >= 100 ? '' : 'red';
        return [
          { label: '最新下行 (Mbps)', value: r ? fmt(r.download) : '—', cls },
          { label: '最新上行 (Mbps)', value: r && r.upload !== null ? fmt(r.upload) : '—', cls: '' },
          { label: '最新延迟 (ms)', value: r ? fmt(r.ping) : '—', cls: 'orange' },
          { label: '节点地区', value: r && r.server_country ? r.server_country : '—', cls: '' },
        ];
      });

      const jobType = computed(() => !job.value ? 'info'
        : job.value.state === 'running' ? 'info'
          : job.value.state === 'done' ? 'success' : 'error');
      const jobTitle = computed(() => !job.value ? ''
        : job.value.state === 'running' ? '测速进行中…'
          : job.value.state === 'done' ? '测速完成：' + (job.value.detail || '')
            : '测速失败：' + (job.value.detail || ''));

      // ---------- 数据加载 ----------
      async function loadSettings() {
        try {
          const { data } = await api.get('/api/boot');
          Object.assign(settings, data.settings);
          nextRun.value = data.next_run;
          schedState.value = data.sched_state;
        } catch (e) { handleErr(e); }
      }

      async function loadServers(refresh) {
        loadingServers.value = true;
        try {
          const { data } = await api.get('/api/servers' + (refresh ? '?refresh=1' : ''));
          servers.value = data.servers || [];
          regions.value = data.regions || [];
          serverErr.value = data.error || '';
        } catch (e) { handleErr(e); }
        loadingServers.value = false;
      }

      async function loadResults(p) {
        try {
          if (p) page.value = p;
          const off = (page.value - 1) * pageSize;
          const { data } = await api.get(`/api/results?limit=${pageSize}&offset=${off}`);
          results.value = data.rows || [];
          total.value = data.total || 0;
          // 记录被清理/删除导致页数变少时，回退到最后一页
          const maxPage = Math.max(1, Math.ceil(total.value / pageSize));
          if (page.value > maxPage) { page.value = maxPage; return loadResults(); }
          if (page.value === 1) latest.value = results.value[0] || latest.value;
          loadHistory();
        } catch (e) { handleErr(e); }
      }

      async function loadNodeHealth() {
        try { nodeHealth.value = (await api.get('/api/node_health')).data; }
        catch (e) { handleErr(e); }
      }

      let chart = null;
      let chartRO = null;   // ResizeObserver，跟随容器尺寸变化重绘
      async function loadHistory() {
        try {
          const { data } = await api.get('/api/history?timeframe=' + timeframe.value);
          await nextTick();
          const el = document.getElementById('trend-chart');
          if (!el) return;
          // 菜单切换时 dashboard 被 v-if 整个销毁重建 → #trend-chart 是全新元素，
          // 但 chart 仍指向绑在「已移除 DOM」上的旧实例。此时 setOption 会画到
          // 脱离文档的 canvas 上，页面显示空白（刷新页面才正常）。
          // 所以必须比对 DOM：元素换了就销毁旧实例、重新 init。
          if (chart && chart.getDom() !== el) {
            if (chartRO) { chartRO.disconnect(); chartRO = null; }
            chart.dispose();
            chart = null;
          }
          if (!chart) {
            chart = echarts.init(el);
            // 容器在切页动画中可能瞬时宽度为 0，初始化会得到 0×0 canvas；
            // 用 ResizeObserver 在容器真正有尺寸时补一次 resize。
            if (window.ResizeObserver) {
              chartRO = new ResizeObserver(() => { if (chart) chart.resize(); });
              chartRO.observe(el);
            }
          }
          chart.setOption({
            tooltip: { trigger: 'axis' },
            legend: { data: ['下行', '上行', '延迟'], top: 0 },
            grid: { left: 50, right: 55, top: 40, bottom: 40 },
            xAxis: { type: 'category', data: data.timestamps.map(t => t.slice(5, 16)) },
            yAxis: [
              { type: 'value', name: 'Mbps' },
              { type: 'value', name: 'ms', splitLine: { show: false } },
            ],
            series: [
              { name: '下行', type: 'line', smooth: true, showSymbol: false,
                data: data.downloads, itemStyle: { color: '#67C23A' }, areaStyle: { opacity: .12 } },
              { name: '上行', type: 'line', smooth: true, showSymbol: false,
                data: data.uploads, itemStyle: { color: '#409EFF' } },
              { name: '延迟', type: 'line', smooth: true, showSymbol: false, yAxisIndex: 1,
                data: data.pings, itemStyle: { color: '#E6A23C' }, lineStyle: { type: 'dashed' } },
            ],
          });
          chart.resize();
        } catch (e) { handleErr(e); }
      }

      function handleErr(e) {
        const st = e?.response?.status;
        if (st === 401) { needToken.value = true; return; }
        ElMessage.error(e?.response?.data?.message || e.message || '请求失败');
      }

      function submitToken() {
        localStorage.setItem('as_token', tokenInput.value.trim());
        needToken.value = false;
        boot();
      }

      // ---------- 动作 ----------
      async function startTest() {
        running.value = true;
        job.value = { state: 'running', log: [], detail: '' };
        try {
          const { data } = await api.post('/api/run', runForm);
          pollJob(data.job);
        } catch (e) {
          running.value = false;
          handleErr(e);
        }
      }

      function pollJob(id) {
        clearInterval(pollTimer);
        pollTimer = setInterval(async () => {
          try {
            const { data } = await api.get('/api/job/' + id);
            job.value = data;
            if (data.state !== 'running') {
              clearInterval(pollTimer);
              running.value = false;
              loadResults(1);   // 回到第一页，新记录立即可见
              loadNodeHealth();
            }
          } catch (e) { clearInterval(pollTimer); running.value = false; handleErr(e); }
        }, 2000);
      }

      async function runSelftest() {
        selftesting.value = true;
        try { selftest.value = (await api.post('/api/selftest', {})).data; }
        catch (e) { handleErr(e); }
        selftesting.value = false;
      }

      async function saveSettings(applySched) {
        const payload = Object.assign({}, settings);
        if (notifyForm.wecom_secret) payload.wecom_secret = notifyForm.wecom_secret;
        if (sysForm.auth_token) payload.auth_token = sysForm.auth_token;
        try {
          const { data } = await api.post('/api/settings', payload);
          nextRun.value = data.next_run;
          if (applySched) schedState.value = data.schedule.includes('暂停') ? '已暂停' : '运行中';
          ElMessage.success('已保存' + (applySched ? '：' + data.schedule : ''));
          notifyForm.wecom_secret = '';
          sysForm.auth_token = '';
          loadSettings();
        } catch (e) { handleErr(e); }
      }

      async function testWechat() {
        testingWechat.value = true;
        const payload = Object.assign({}, settings);
        if (notifyForm.wecom_secret) payload.wecom_secret = notifyForm.wecom_secret;
        try {
          const { data } = await api.post('/api/test_wechat', payload);
          wechatOk.value = data.status === 'success';
          wechatMsg.value = data.message;
        } catch (e) { wechatOk.value = false; wechatMsg.value = '请求失败'; handleErr(e); }
        testingWechat.value = false;
      }

      async function prune() {
        try {
          const { data } = await api.post('/api/prune', {});
          ElMessage.success(data.message);
          loadResults();
        } catch (e) { handleErr(e); }
      }

      function exportCsv() {
        const t = localStorage.getItem('as_token');
        window.open('/api/export.csv' + (t ? '?token=' + encodeURIComponent(t) : ''), '_blank');
      }

      function go(k) { activeMenu.value = k; }

      // 关闭旧策略迁移提示（同时清掉后端标记，避免每次刷新都弹）
      async function clearModeNotice() {
        try {
          await api.post('/api/settings', { mode_migrated_from: '' });
          settings.mode_migrated_from = '';
        } catch (e) { handleErr(e); }
      }

      async function boot() {
        try {
          const { data } = await api.get('/api/boot');
          Object.assign(settings, data.settings);
          results.value = data.results || [];
          total.value = data.total || 0;
          latest.value = results.value[0] || null;
          nextRun.value = data.next_run;
          schedState.value = data.sched_state;
          needToken.value = false;
        } catch (e) { handleErr(e); }
      }

      onMounted(() => {
        boot();
        loadServers();
        loadNodeHealth();
        nextTick(loadHistory);
        window.addEventListener('resize', () => chart && chart.resize());
      });

      watch(activeMenu, k => {
        if (k === 'dashboard') { loadResults(1); }
        if (k === 'run') { loadServers(); }
        if (k === 'backend') { loadNodeHealth(); }
        // 「定时设置」页也有「节点策略」下拉，同样需要预置地区及其可用数
        if (k === 'schedule') { loadServers(); }
      });

      return {
        needToken, tokenInput, submitToken,
        activeMenu, drawer, isMobile, menus, currentMenu, go,
        settings, results, total, page, pageSize, pagedResults,
        servers, serverErr, loadingServers, cnCount, regions, nodeHealth, backends,
        running, selftesting, selftest, job, jobType, jobTitle,
        runForm, notifyForm, sysForm, timeframe,
        wechatMsg, wechatOk, testingWechat,
        fmt, statCards, nextRun, schedState,
        loadServers, loadResults, loadNodeHealth, loadHistory, clearModeNotice,
        startTest, runSelftest, saveSettings, testWechat, prune, exportCsv,
      };
    },
  });

  for (const [name, comp] of Object.entries(ElementPlusIconsVue)) {
    app.component(name, comp);
  }
  app.use(ElementPlus);
  app.mount('#app');
})();
