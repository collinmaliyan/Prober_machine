// ============================================================================
// GLOBAL VARIABLES & CONFIGURATION
// ============================================================================
let logData = [];
let cassetteLogData = [];
let refreshInterval;
let currentPage = 1;
let currentCassettePage = 1;
let __lastReaderConnected = null;
var params = new URLSearchParams({ page: '1', pageSize: '50000' });
window.params = params;

window.AppState = {
  readerConnected: false,
  fpc: '',
  header: '',
  pm: '',
  ts: ''
};

function emitStateChanged() {
  document.dispatchEvent(new CustomEvent('app:stateChanged', { detail: { ...AppState } }));
}

// --- Inactivity auto-return to Home ---
const INACTIVITY_TO_HOME_MS = 60_000; // 1 minute
let inactivityHomeTimer = null;
const ADMIN_IDS = ['ADMIN', 'admin']
// --- [COMMENTED OUT] Remote AGV Polling (ปิดการส่งคำขอ HTTP ไปยังรถ AGV ทั้ง 3 คันชั่วคราว) ---
const REMOTE_AGVS = [
  // { id: 'agv1', base: 'http://172.20.10.4:5001' }, // <-- change IP:port
  // { id: 'agv2', base: 'http://92.121.78.12:8000' }, // <-- change IP:port
  // { id: 'agv3', base: 'http://92.121.78.13:8000' }  // <-- change IP:port (Cassette AGV)
];
const LAST_AGV_STATUS = {
  agv1: { connected: null, tagPresent: false, lastUpdated: 0 },
  agv2: { connected: null, tagPresent: false, lastUpdated: 0 },
  agv3: { connected: null, tagPresent: false, lastUpdated: 0 }
};
const AGV_STALE_MS = 2000; // consider cache valid if updated in last 2s

function getActivePageId() {
  const el = document.querySelector('.page-content.active');
  return el ? el.id : 'home';
}

// CHANGE THIS to your main Pi’s IP/port
const MAIN_API = 'http://92.121.78.12:8000/';
// const MAIN_API = 'http://localhost:5000';

// On the header RPi set this true; on the main RPi set false
//const USE_MAIN_FOR_LOGS = true;//ติดต่อไปยังเครื่องเซิร์ฟเวอร์หลัก (Main Server)
const USE_MAIN_FOR_LOGS = false; // Always use same-origin relative endpoints so it works on localhost, 127.0.0.1, and network IPs

let agvBgInFlight = false;
function kickAgvBackground() {
  // [COMMENTED OUT] ปิดการส่งคำขอพื้นหลังไปยังรถ AGV ชั่วคราว
  /*
  if (agvBgInFlight) return;
  agvBgInFlight = true;
  Promise.resolve()
    .then(() => updateAgvFromRemote())
    .catch(() => { })
    .finally(() => { agvBgInFlight = false; });
  */
}

function scheduleInactivityToHome() {
  clearTimeout(inactivityHomeTimer);
  if (getActivePageId() === 'home') return;  // only schedule on non-home pages
  inactivityHomeTimer = setTimeout(() => {
    if (getActivePageId() !== 'home' && !document.hidden) {
      // click the existing nav handler so it highlights the correct button, etc.
      const homeBtn = document.getElementById("nav-home-btn") || document.querySelector(".nav-button[onclick*=\"switchPage('home'\"");
      switchPage('home', homeBtn || undefined);
    }
  }, INACTIVITY_TO_HOME_MS);
}

function resetInactivityFromEvent() {
  // Only matters when not on Home
  if (getActivePageId() !== 'home') scheduleInactivityToHome();
}

// ============================================================================
// HOME VIEW MODE CONTROLLER (Split 50/50, RFID Full, PMI Full)
// ============================================================================
let currentHomeViewMode = 'split'; // 'split' | 'rfid' | 'pmi'

function handleHomeNavClick(btnEl) {
  const activePage = getActivePageId();
  if (activePage !== 'home') {
    // Navigating to Home from another page
    switchPage('home', btnEl);
    closeHomeViewPopover();
  } else {
    // Already on Home -> Toggle the view mode popover
    toggleHomeViewPopover();
  }
}

function toggleHomeViewPopover() {
  const popover = document.getElementById('home-view-popover');
  const homeBtn = document.getElementById('nav-home-btn');
  if (!popover) return;

  const isOpen = popover.style.display !== 'none';
  if (isOpen) {
    closeHomeViewPopover();
  } else {
    popover.style.display = 'block';
    if (homeBtn) homeBtn.classList.add('popover-active');
  }
}

function closeHomeViewPopover() {
  const popover = document.getElementById('home-view-popover');
  const homeBtn = document.getElementById('nav-home-btn');
  if (popover) popover.style.display = 'none';
  if (homeBtn) homeBtn.classList.remove('popover-active');
}

function setHomeViewMode(mode) {
  currentHomeViewMode = mode;
  const contentBoxes = document.querySelector('#home .content-boxes');
  if (!contentBoxes) return;

  contentBoxes.classList.remove('view-split', 'view-rfid-full', 'view-pmi-full');
  if (mode === 'rfid') {
    contentBoxes.classList.add('view-rfid-full');
  } else if (mode === 'pmi') {
    contentBoxes.classList.add('view-pmi-full');
  } else {
    contentBoxes.classList.add('view-split');
  }

  // Update active state in popover buttons
  document.querySelectorAll('.view-opt-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.view === mode);
  });

  closeHomeViewPopover();
}

// Global click listener to close popover when clicking outside
document.addEventListener('click', function (e) {
  const popover = document.getElementById('home-view-popover');
  const homeBtn = document.getElementById('nav-home-btn');
  if (popover && popover.style.display !== 'none') {
    if (!popover.contains(e.target) && (!homeBtn || !homeBtn.contains(e.target))) {
      closeHomeViewPopover();
    }
  }
});

// ============================================================================
// MAIN NAVIGATION SYSTEM
// ============================================================================
function switchPage(pageId, btnEl) {
  // Always close Home view mode popover if switching page
  closeHomeViewPopover();

  // Hide sidebar navigation for settings page, restore for other pages
  const navPanel = document.querySelector('.nav-panel');
  if (pageId === 'setting' || pageId === 'settings') {
    if (navPanel) navPanel.style.display = 'none';
  } else {
    if (navPanel) navPanel.style.display = 'flex';
  }

  // Update navigation buttons
  document.querySelectorAll('.nav-button').forEach(b => b.classList.remove('active'));
  if (btnEl) btnEl.classList.add('active');

  // Update page visibility
  document.querySelectorAll('.page-content').forEach(p => p.classList.remove('active'));
  const page = document.getElementById(pageId);
  if (page) page.classList.add('active');

  // Stop updates, then start immediately when cleanup is done
  stopRealTimeUpdates().then(() => startPagePolling(pageId));

  // Modals should ONLY be displayed on the Home page
  if (pageId !== 'home') {
    const pm = document.getElementById('pm-warning');
    if (pm) pm.style.display = 'none';
    const pre = document.getElementById('pm-prewarn');
    if (pre) pre.style.display = 'none';
  } else {
    if (window.__pmModalOpen) {
      const pm = document.getElementById('pm-warning');
      if (pm) pm.style.display = 'flex';
    }
  }

  // Initialize page-specific functionality
  switch (pageId) {
    case 'home':
      initHomePage();
      break;
    case 'agv':
      initAGVPage();
      break;
    case 'log':
      initLogPage();
      break;
    case 'cassette-log':
      initCassetteLogPage();
      break;
    case 'setting':
      initSettingsPage();
      break;
    case 'about':
      initAboutPage();
      break;
  }
  clearTimeout(inactivityHomeTimer);
  if (pageId !== 'home') {
    scheduleInactivityToHome();  // start/restart the 1-min timer
  }
}

// ============================================================================
// UTILITY FUNCTIONS
// ============================================================================
function setMany(ids, text) {
  ids.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.textContent = text || '';
  });
}

function formatDateTime(timestamp) {
  if (!timestamp) return '—';
  const date = new Date(timestamp);
  return date.toLocaleString();
}

function formatAgo(timestamp) {
  if (!timestamp) return '—';
  const now = new Date();
  const past = new Date(timestamp);
  const diffMs = now - past;
  const diffMins = Math.floor(diffMs / 60000);

  if (diffMins < 60) return `${diffMins}m ago`;
  const diffHours = Math.floor(diffMins / 60);
  if (diffHours < 24) return `${diffHours}h ago`;
  const diffDays = Math.floor(diffHours / 24);
  return `${diffDays}d ago`;
}

function updateCurrentDateTime() {
  const now = new Date();
  const formatted = now.toLocaleString('sv-SE', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  }).replace(' ', ', ');

  // Update the global datetime display (shown on all pages)
  const globalEl = document.getElementById('global-datetime-display');
  if (globalEl) globalEl.textContent = formatted;

  // Keep the old datetime-display for backward compatibility (if it exists)
  const el = document.getElementById('datetime-display');
  if (el) el.textContent = formatted;
}

function checkServerConnection() {
  fetch('/api/current_data')
    .then(response => response.json())
    .then(data => {
      console.log('Server connection: OK');
    })
    .catch(error => {
      console.error('Server connection failed:', error);
      showConnectionError();
    });
}

function showConnectionError() {
  const statusMessage = 'Server connection failed';
  setMany([
    'batch-id-display', 'lot-id-display', 'fpc-display',
    'header-display', 'touchdown-display', 'PM-display', 'timer-display', 'comment-display'
  ], statusMessage);
}

// ============================================================================
// REAL-TIME DATA UPDATES
// ============================================================================

function resetAgvView() {
  ['agv1', 'agv2', 'agv3'].forEach(prefix => {
    const box = document.getElementById(`${prefix}-box`);
    if (box) box.classList.remove('tag-active'); // clear green immediately

    if (prefix === 'agv3') {
      const slotIds = ['c1-id', 'c1-status', 'c1-lot', 'c1-batch', 'c2-id', 'c2-status', 'c2-lot', 'c2-batch'];
      slotIds.forEach(id => {
        const el = document.getElementById(`${prefix}-${id}`);
        if (el) el.textContent = '';
      });
    } else {
      // Clear fields
      const ids = ['fpc-display', 'header-display', 'pm-display', 'timer-display', 'pm-date-display', 'timestamp-display'];
      ids.forEach(id => {
        const el = document.getElementById(`${prefix}-${id}`);
        if (el) el.textContent = '';
      });
    }
  });
}


function startPagePolling(pageId) {
  stopRealTimeUpdates();

  if (pageId === 'agv') {
    resetAgvView();
    // run once immediately
    updateAgvFromRemote();
    updateLegendFromMap();
    // one interval only
    refreshInterval = setInterval(() => {
      updateAgvFromRemote();
      updateLegendFromMap();
    }, 1000);
  } else {
    updateHomeFromLocal();
    refreshInterval = setInterval(updateHomeFromLocal, 500);
  }

}

function stopRealTimeUpdates() {
  if (refreshInterval) {
    clearInterval(refreshInterval);
    refreshInterval = null;
  }
  // Force a small delay to ensure cleanup
  return new Promise(resolve => setTimeout(resolve, 50));
}

function updateHomeFromLocal() {
  const active = document.querySelector('.page-content.active');
  const pageId = active ? active.id : 'home';

  // You want Home to be the only source for AVT#55 -> skip when on AGV page
  if (pageId === 'agv') return;

  fetch('/api/current_data?ts=' + Date.now(), { cache: 'no-store' })
    .then(r => r.json())
    .then(data => {
      if (!data || data.status !== 'success') {
        updateConnectionStatus(false, false);
        clearAllDisplayFields();
        updateCassetteConnectionStatus(false, false);
        clearCassetteDisplayFields();
        updateRfidStatusBar(null);
        console.error('Error fetching data:', data?.message);
        kickAgvBackground();
      } else {
        // --- [NEW] Store Mockup Mode state dynamically ---
        // If mockup mode is enabled on the server, we save this flag globally
        // so the frontend can activate mockup layouts and fallback mock data.
        window.MOCKUP_MODE = !!data.mockup_mode;

        // --- [NEW] Dynamically update the machine name heading ---
        if (data.machine_no) {
          const mainTitle = document.getElementById('main-machine-title');
          if (mainTitle && mainTitle.textContent !== data.machine_no) {
            mainTitle.textContent = data.machine_no;
          }
          const fpcLogTitle = document.getElementById('fpc-log-title');
          if (fpcLogTitle && fpcLogTitle.textContent !== `Log ${data.machine_no}`) {
            fpcLogTitle.textContent = `Log ${data.machine_no}`;
          }
          const cassetteLogTitle = document.getElementById('cassette-log-title');
          if (cassetteLogTitle && cassetteLogTitle.textContent !== `Log PMI ${data.machine_no}`) {
            cassetteLogTitle.textContent = `Log PMI ${data.machine_no}`;
          }
        }

        const isReaderConnected = !!data.reader_connected;
        const rfidData = data.data || {};
        const tagPresent = !!(rfidData.fpc_id || rfidData.header_id || rfidData.header_name);
        const bothHave = !!(rfidData.header_id && rfidData.fpc_id);

        // --- Cassette Data extraction ---
        const cassetteData = data.cassette || {};
        const isCassettePresent = !!(cassetteData.batch_id || cassetteData.lot_id || cassetteData.cassette_id);
        const isCassetteNotFound = isCassettePresent && (
          cassetteData.not_found === true ||
          cassetteData.status === 'NOT_FOUND' ||
          cassetteData.mismatch_type === 'not_found' ||
          cassetteData.batch_id === 'NOT_IN_STORE' ||
          (cassetteData.lot_id && String(cassetteData.lot_id).startsWith('UNMAPPED-'))
        );
        window.__isCassettePresent = isCassettePresent;
        window.__isCassetteNotFound = isCassetteNotFound;

        const currentPairKey = `${rfidData.header_id || ''}|${rfidData.fpc_id || ''}`;
        if (currentPairKey !== window.__lastPairKey) {
          window.__lastPairKey = currentPairKey;
          window.__pairWarnKey = null;
          window.__pairModalDismissed = false;
        }

        if (tagPresent) {
          window.__missingTagCycles = 0;
          updateDisplayFields(rfidData);

          const isMismatch = (rfidData.mismatch_detected === true) || (bothHave && rfidData.match_ok === false);

          if (isMismatch) {
            const hdr = rfidData.mismatch_header || rfidData.header_id || '-';
            const fpc = rfidData.mismatch_fpc || rfidData.fpc_id || '-';
            const mType = (rfidData.mismatch_type === 'not_found') ? 'not_found' : 'mismatch';
            const isNotFound = (mType === 'not_found');
            const alertType = isNotFound ? 'not_found' : 'mismatch';

            const msg = rfidData.mismatch_message || (
              isNotFound
                ? `Tag Header: ${hdr} or FPC: ${fpc} not found in database`
                : `Header: ${hdr}  |  FPC: ${fpc}  |  NOT matching together`
            );

            const warnKey = `${alertType.toUpperCase()}|${hdr}|${fpc}`;

            if (window.__pairWarnKey !== warnKey) {
              window.__pairWarnKey = warnKey;
              window.__pairModalDismissed = false;
              window.__pmDetailText = msg;
              showPmWarning(msg, { type: alertType });
            }

            const box = document.getElementById('info-box');
            if (box) {
              box.classList.add('warning-active');
              box.classList.remove('tag-active');
              _updateInfoBoxBadge(box, 'danger', isNotFound ? 'NOT FOUND' : 'MISMATCH');
            }
          } else {
            // No mismatch
            window.__pairWarnKey = null;
            const box = document.getElementById('info-box');
            const td = Number(rfidData?.touchdown ?? 0);
            if (box && (!Number.isFinite(td) || td < TD_LIMIT) && !window.__pmModalOpen && !isCassetteNotFound) {
              box.classList.remove('warning-active');
              if (bothHave && (rfidData.match_ok === true || rfidData.pair_ok === true)) {
                if (td >= TD_PREWARN_MIN) {
                  _updateInfoBoxBadge(box, 'warning', 'TD NEARING LIMIT');
                } else {
                  _updateInfoBoxBadge(box, 'success', 'MATCH OK');
                }
              } else {
                _updateInfoBoxBadge(box, 'none');
              }
            }
          }
        } else {
          window.__pairWarnKey = null;
          window.__pairModalDismissed = false;
          // [DEBOUNCE / GRACE PERIOD] Require 3 consecutive missing cycles (~2.5-3s)
          // before clearing all display fields to avoid flickering from transient RF dips
          window.__missingTagCycles = (window.__missingTagCycles || 0) + 1;
          if (window.__missingTagCycles >= 3) {
            clearAllDisplayFields();
          }
        }

        // --- Cassette Logic ---
        const currentCassKey = cassetteData.cassette_id || '';
        if (currentCassKey !== window.__lastCassetteKey) {
          window.__lastCassetteKey = currentCassKey;
          window.__cassetteWarnKey = null;
        }

        // Show Cassette's tag/lot/batch on the main card fields
        if (isCassettePresent) {
          const rawTag = cassetteData.cassette_id || cassetteData.lot_id || cassetteData.batch_id || '';
          if (isCassetteNotFound) {
            // Case 2: NOT FOUND -> Show scanned unmapped tag value in Batch ID and Lot ID fields, while keeping popup alert
            setMany(['batch-id-display'], rawTag);
            setMany(['lot-id-display'], rawTag);
          } else {
            // Case 1: FOUND -> Show matched Lot ID and Batch ID from table
            setMany(['batch-id-display'], cassetteData.batch_id || '');
            setMany(['lot-id-display'], cassetteData.lot_id || '');
          }
          if (typeof updateCassetteDisplayFields === 'function') {
            updateCassetteDisplayFields(cassetteData);
          }
        } else {
          if (!tagPresent) {
            setMany(['batch-id-display'], '');
            setMany(['lot-id-display'], '');
          }
          if (typeof clearCassetteDisplayFields === 'function') {
            clearCassetteDisplayFields();
          }
          window.__cassetteWarnKey = null;
        }

        // Trigger Pop-up Modal when Cassette is NOT FOUND in database
        if (isCassetteNotFound) {
          const cassId = cassetteData.cassette_id || 'Unknown';
          const msg = cassetteData.mismatch_message || cassetteData.message || `Tag Cassette (${cassId}) ไม่พบข้อมูลในระบบ Smart Store หรือยังไม่ได้ทำ Data Mapping จากตู้ Store`;
          const warnKey = `CASSETTE_NOT_FOUND|${cassId}`;

          if (window.__cassetteWarnKey !== warnKey) {
            window.__cassetteWarnKey = warnKey;
            window.__pmDetailText = msg;
            showPmWarning(msg, { type: 'not_found', tagType: 'cassette' });
          }

          const box = document.getElementById('info-box');
          if (box) {
            box.classList.add('warning-active');
            box.classList.remove('tag-active');
            _updateInfoBoxBadge(box, 'danger', 'NOT FOUND');
          }
        }

        // Show/hide clear button footer on info-box (visible during warning / NOT FOUND state)
        const boxEl = document.getElementById('info-box');
        const clearFooter = document.getElementById('info-box-footer');
        if (clearFooter) {
          const isWarning = isCassetteNotFound || (boxEl && boxEl.classList.contains('warning-active'));
          clearFooter.style.display = isWarning ? 'flex' : 'none';
        }

        const hasAnyTag = tagPresent || isCassettePresent;
        const isAnyConnected = isReaderConnected || !!data.cassette_connected || isCassettePresent || (data.rfid_status?.cassette?.connected);

        // connection + green state (respects warning-active)
        updateConnectionStatus(isAnyConnected, hasAnyTag);
        updateRfidStatusBar(data.rfid_status);

        kickAgvBackground();
      }

      // ---- MOVE AppState mirror HERE (after DOM updates), and guard empties ----
      const fpcTxt = document.getElementById('fpc-display')?.textContent?.trim() || '';
      const headerTxt = document.getElementById('header-display')?.textContent?.trim() || '';
      const pmTxt = document.getElementById('PM-display')?.textContent?.trim() || '';
      const tsTxt = document.getElementById('timer-display')?.textContent?.trim() || '';
      const connected = document.getElementById('info-box')?.classList.contains('connected') || false;

      if (fpcTxt) AppState.fpc = fpcTxt;
      if (headerTxt) AppState.header = headerTxt;
      if (pmTxt) AppState.pm = pmTxt;
      if (tsTxt) AppState.ts = tsTxt;
      AppState.readerConnected = connected;

      emitStateChanged();
    })
    .catch(err => {
      updateConnectionStatus(false, false);
      clearAllDisplayFields();
      console.error('Connection error:', err);
      kickAgvBackground();

      // ---- Mirror the cleared/failed state too ----
      const fpcTxt = document.getElementById('fpc-display')?.textContent?.trim() || '';
      const headerTxt = document.getElementById('header-display')?.textContent?.trim() || '';
      const pmTxt = document.getElementById('PM-display')?.textContent?.trim() || '';
      const tsTxt = document.getElementById('timer-display')?.textContent?.trim() || '';
      const connected = document.getElementById('info-box')?.classList.contains('connected') || false;

      if (fpcTxt) AppState.fpc = fpcTxt;
      if (headerTxt) AppState.header = headerTxt;
      if (pmTxt) AppState.pm = pmTxt;
      if (tsTxt) AppState.ts = tsTxt;
      AppState.readerConnected = connected;

      emitStateChanged();
    });
}



// Explicit modal acknowledge state flag
window.__pmModalOpen = false;
window.__pmWarnKey = null;


function clearAllDisplayFields() {
  if (!window.__isCassettePresent) {
    setMany(['batch-id-display'], '');
    setMany(['lot-id-display'], '');
  }
  setMany(['fpc-display'], '');
  setMany(['header-display'], '');
  setMany(['touchdown-value'], '');
  setMany(['PM-display'], '');
  setMany(['timer-display'], '');
  setMany(['comment-display'], '');
  __lastPmWarnKey = null;
  clearPmWarningState();
  clearPmPrewarningState();
  if (!window.__isCassetteNotFound) {
    _updateInfoBoxBadge(document.getElementById('info-box'), 'none');
  }
  const clearFooter = document.getElementById('info-box-footer');
  if (clearFooter && !window.__isCassetteNotFound) {
    clearFooter.style.display = 'none';
  }
  const pre = document.getElementById('td-prewarn');
  if (pre) { pre.style.display = 'none'; pre.textContent = ''; pre.classList.remove('hot'); }
}

function updateDisplayFields(rfidData) {
  if (!window.__isCassettePresent) {
    setMany(['batch-id-display'], rfidData.batch_id || '');
    setMany(['lot-id-display'], rfidData.lot_id || '');
  }
  setMany(['fpc-display'], rfidData.fpc_id || '');
  setMany(['header-display'], rfidData.header_id || '');
  setMany(['touchdown-value'], rfidData.touchdown ?? '');
  setMany(['PM-display'], rfidData.pm_date || '');
  setMany(['timer-display'], rfidData.timestamp || '');
  setMany(['comment-display'], rfidData.comment || '');

  // Only run Touchdown checks when pair is validated and matched
  if (rfidData.match_ok === true || rfidData.pair_ok === true) {
    updatePmPrewarning(rfidData);
    maybeWarnOnTouchdown(rfidData);
  } else {
    clearPmPrewarningState();
  }
}


function updateConnectionStatus(connected, tagPresent) {
  const box = document.getElementById('info-box');

  if (box) {
    box.classList.toggle('connected', !!connected);
    box.classList.toggle('disconnected', !connected);

    const hasWarning = box.classList.contains('warning-active');
    if (hasWarning) {
      box.classList.remove('tag-active');
    } else {
      box.classList.toggle('tag-active', !!connected && !!tagPresent);
    }
  }

  // --- Reader status text ---
  const homeStatus = document.getElementById('agv1-status-text');
  if (homeStatus) {
    if (connected !== __lastReaderConnected) {
      // Only update if the state actually changed
      homeStatus.textContent = connected
        ? 'Reader: Connected'
        : 'Reader: Disconnected';
      __lastReaderConnected = connected;
    }
  }
}
let __lastProberConnected = null;

function updateConnectionStatus(connected, tagPresent) {
  const box = document.getElementById('info-box');

  if (box) {
    box.classList.toggle('connected', !!connected);
    box.classList.toggle('disconnected', !connected);

    const hasWarning = box.classList.contains('warning-active');
    if (hasWarning) {
      box.classList.remove('tag-active');
    } else {
      box.classList.toggle('tag-active', !!connected && !!tagPresent);
    }
  }

  // --- Home page reader status ---
  const proberStatus = document.getElementById('prober-status-text');
  if (proberStatus && connected !== __lastProberConnected) {
    proberStatus.textContent = connected
      ? 'Reader: Connected'
      : 'Reader: Disconnected';
    __lastProberConnected = connected;
  }
}

function updateRfidStatusBar(rfidStatus) {
  if (!rfidStatus) return;

  // Update RFID-1 (Cassette)
  if (rfidStatus.cassette) {
    _updateCardStatus('rfid-cassette-reader-status', null, rfidStatus.cassette, 'rfid-cassette-reader-text');
  }
  // Update RFID-2 (FPC)
  if (rfidStatus.fpc) {
    _updateCardStatus('rfid-fpc-reader-status', 'rfid-fpc-sensor-status', rfidStatus.fpc, 'rfid-fpc-reader-text');
  }
  // Update RFID-3 (Header)
  if (rfidStatus.header) {
    _updateCardStatus('rfid-header-reader-status', null, rfidStatus.header, 'rfid-header-reader-text');
  }
}

function _updateCardStatus(readerId, sensorId, status, readerTextId) {
  const readerEl = document.getElementById(readerId);
  const sensorEl = document.getElementById(sensorId);
  const readerTextEl = document.getElementById(readerTextId);
  if (!status) return;

  const isConn = !!status.connected;
  if (readerEl) {
    const svgIcon = isConn
      ? `<svg viewBox="0 0 24 24" width="18" height="18"><circle cx="12" cy="12" r="10" fill="#22c55e"/><path d="M9 12l2 2 4-4" fill="none" stroke="white" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>`
      : `<svg viewBox="0 0 24 24" width="18" height="18"><circle cx="12" cy="12" r="10" fill="#ef4444"/><path d="M15 9l-6 6M9 9l6 6" fill="none" stroke="white" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

    if (readerEl.innerHTML !== svgIcon) {
      readerEl.innerHTML = svgIcon;
    }
  }

  if (readerTextEl) {
    const textVal = isConn ? 'Connected' : 'Disconnected';
    if (readerTextEl.textContent !== textVal) {
      readerTextEl.textContent = textVal;
      readerTextEl.className = 'reader-text-value ' + textVal.toLowerCase();
    }
  }

  if (sensorEl) {
    const sensVal = status.sensor || 'OFF';
    if (sensorEl.textContent !== sensVal) {
      sensorEl.textContent = sensVal;
      sensorEl.className = 'sensor-value ' + sensVal.toLowerCase();
    }
  }
}

let __lastCassetteReaderConnected = null;

function updateCassetteConnectionStatus(connected, tagPresent) {
  const box = document.getElementById('cassette-box');

  if (box) {
    box.classList.toggle('connected', !!connected);
    box.classList.toggle('disconnected', !connected);

    const hasWarning = box.classList.contains('warning-active');
    if (hasWarning) {
      box.classList.remove('tag-active');
    } else {
      box.classList.toggle('tag-active', !!connected && !!tagPresent);
    }
  }

  // --- Home page cassette status ---
  const cassetteStatus = document.getElementById('cassette-status-text');
  if (cassetteStatus && connected !== __lastCassetteReaderConnected) {
    cassetteStatus.textContent = connected
      ? 'Reader: Connected'
      : 'Reader: Disconnected';
    __lastCassetteReaderConnected = connected;
  }
}

function updateCassetteDisplayFields(cassetteData) {
  setMany(['right-cassette-id-display'], cassetteData.cassette_id || '');
  setMany(['right-machine-status-display'], cassetteData.machine_status || '');
  setMany(['right-lot-id-display'], cassetteData.lot_id || '');
  setMany(['right-batch-id-display'], cassetteData.batch_id || '');
  setMany(['right-last-cleaning-display'], cassetteData.last_cleaning || '');
  setMany(['right-next-cleaning-display'], cassetteData.next_cleaning || '');
}

function clearCassetteDisplayFields() {
  setMany(['right-cassette-id-display'], '');
  setMany(['right-machine-status-display'], '');
  setMany(['right-lot-id-display'], '');
  setMany(['right-batch-id-display'], '');
  setMany(['right-last-cleaning-display'], '');
  setMany(['right-next-cleaning-display'], '');
}


// ============================================================================
// HOME PAGE FUNCTIONS
// ============================================================================
function initHomePage() {
  console.log('Initializing Home Page');
}

// ============================================================================
// AGV PAGE FUNCTIONS
// ============================================================================

// Normalizes and applies one AGV payload to the UI
function applyAgvPayload(prefix, payload) {
  const data = payload?.data || {};
  const connected = !!payload?.reader_connected;
  const tagPresent = !!(data.header_id || data.fpc_id);

  // If your setReaderBoxState can take tagPresent (recommended):
  setReaderBoxState(prefix, connected, data.timestamp || '', tagPresent);

  // Fill text fields (header > fpc, plus pm_date + timestamp)
  fillReaderBox(prefix, {
    connected,
    fpc_id: data.fpc_id || '',
    header_id: data.header_id || '',
    pm_date: data.pm_date || data.timestamp || '',
    timestamp: data.timestamp || ''
  });
}

function initAGVPage() {
  ['agv1', 'agv2', 'agv3'].forEach(id => {
    const s = LAST_AGV_STATUS[id];
    const fresh = s && s.lastUpdated && (Date.now() - s.lastUpdated) <= AGV_STALE_MS;

    if (fresh && s.connected !== null) {
      // Paint immediately with last known truth (no grey blip)
      setReaderBoxState(id, s.connected, '', s.tagPresent);
    } else {
      // Fallback when cache is missing/stale:
      setReaderBoxState(id, false, '', false); // start red instead of grey
    }
  });
}



function initAGVReaderBoxes() {
  // Default to Disconnected until /api/current_data updates it
  setReaderBoxState('agv1', false, new Date().toISOString());
  setReaderBoxState('agv2', false, new Date().toISOString());
  setReaderBoxState('agv3', false, new Date().toISOString());

  // Clear text fields (leave placeholders)
  fillReaderBox('agv1', { connected: false });
  fillReaderBox('agv2', { connected: false });
  fillCassetteSlots('agv3', {}, {});
}

function fillCassetteSlots(prefix, c1 = {}, c2 = {}) {
  // Fill Slot 1
  const c1Id = document.getElementById(`${prefix}-c1-id`);
  const c1Status = document.getElementById(`${prefix}-c1-status`);
  const c1Lot = document.getElementById(`${prefix}-c1-lot`);
  const c1Batch = document.getElementById(`${prefix}-c1-batch`);

  if (c1Id) c1Id.textContent = c1.cassette_id || '';
  if (c1Status) c1Status.textContent = c1.machine_status || '';
  if (c1Lot) c1Lot.textContent = c1.lot_id || '';
  if (c1Batch) c1Batch.textContent = c1.batch_id || '';

  // Fill Slot 2
  const c2Id = document.getElementById(`${prefix}-c2-id`);
  const c2Status = document.getElementById(`${prefix}-c2-status`);
  const c2Lot = document.getElementById(`${prefix}-c2-lot`);
  const c2Batch = document.getElementById(`${prefix}-c2-batch`);

  if (c2Id) c2Id.textContent = c2.cassette_id || '';
  if (c2Status) c2Status.textContent = c2.machine_status || '';
  if (c2Lot) c2Lot.textContent = c2.lot_id || '';
  if (c2Batch) c2Batch.textContent = c2.batch_id || '';
}

function setReaderBoxState(prefix, connected, timestamp, tagPresent) {
  const box = document.getElementById(`${prefix}-box`);
  const text = document.getElementById(`${prefix}-status-text`)
    || document.getElementById(`${prefix}-reader-status`); // <-- fallback

  if (box) {
    box.classList.toggle('connected', !!connected);
    box.classList.toggle('disconnected', !connected);
    box.classList.toggle('tag-active', !!connected && !!tagPresent);
  }
  if (text) {
    text.textContent = connected ? 'Reader: Connected' : 'Reader: Disconnected';
  }
}


function fillReaderBox(prefix, payload = {}) {
  const get = (k) => (payload && payload[k]) || '';
  const fpcEl = document.getElementById(`${prefix}-fpc-display`);
  const headerEl = document.getElementById(`${prefix}-header-display`);
  const pmEl = document.getElementById(`${prefix}-pm-display`);
  const tsEl = document.getElementById(`${prefix}-timer-display`);

  if (fpcEl) fpcEl.textContent = get('fpc_id') || '';
  if (headerEl) headerEl.textContent = get('header_id') || '';
  if (pmEl) pmEl.textContent = get('pm_date') || '';
  if (tsEl) tsEl.textContent = formatDateIfISO(get('timestamp')) || '';
}

function formatDateIfISO(s) {
  if (!s) return '';
  const d = new Date(s);
  return isNaN(d) ? s : d.toLocaleString();
}


async function fetchRemoteCurrent(baseUrl) {
  const res = await fetch(`${baseUrl}/api/current_data`, { cache: 'no-store' });
  if (!res.ok) throw new Error(`Fetch failed ${res.status}`);
  return res.json();
}

function fetchWithTimeout(url, ms = 700) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort('timeout'), ms);
  return fetch(url, { cache: 'no-store', signal: ctrl.signal })
    .finally(() => clearTimeout(t));
}


// Poll all remote AGVs independently (no cross-blocking) and update cache
async function updateAgvFromRemote() {
  // --- [COMMENTED OUT] ปิดการส่งคำขอไปยังรถ AGV ทั้ง 3 คันชั่วคราว ---
  return;
  // Safety: make sure REMOTE_AGVS exists
  if (!Array.isArray(REMOTE_AGVS) || REMOTE_AGVS.length === 0) return;

  // Optional: simple per-AGV in-flight guard to avoid overlap
  updateAgvFromRemote._inflight = updateAgvFromRemote._inflight || new Set();

  for (const { id, base } of REMOTE_AGVS) {
    // Skip if a previous request for this AGV hasn't finished yet
    if (updateAgvFromRemote._inflight.has(id)) continue;
    updateAgvFromRemote._inflight.add(id);

    (async () => {
      try {
        // --- [NEW] Mockup Mode URL redirection ---
        // If mockup mode is active, fetch from local mockup endpoints using the agv parameter
        // so that the 3 AGVs show the rotating Green, Grey, and Red states in sync.
        let fetchUrl = `${base}/api/current_data`;
        if (window.MOCKUP_MODE) {
          fetchUrl = `/api/current_data?agv=${id}`;
        }

        // --- [COMMENTED OUT] Real hardware fetch call ---
        // const res = await fetchWithTimeout(`${base}/api/current_data`, 700);
        const res = await fetchWithTimeout(fetchUrl, 700);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);

        const payload = await res.json();
        const connected = !!(payload?.reader_connected || payload?.cassette_connected);

        let hasData = false;
        if (id === 'agv3') {
          const c1 = payload?.cassette1 || (Array.isArray(payload?.cassette) ? payload?.cassette[0] : payload?.cassette) || {};
          const c2 = payload?.cassette2 || (Array.isArray(payload?.cassette) ? payload?.cassette[1] : null) || {};
          hasData = !!(c1.cassette_id || c2.cassette_id);

          if (!connected) {
            setReaderBoxState(id, false, '', false);
            fillCassetteSlots(id, {}, {});
          } else if (hasData) {
            setReaderBoxState(id, true, c1.timestamp || c2.timestamp || '', true);
            fillCassetteSlots(id, c1, c2);
          } else {
            setReaderBoxState(id, true, '', false);
            fillCassetteSlots(id, {}, {});
          }
        } else {
          const data = payload?.data || payload?.cassette || {};
          hasData = !!(
            data.fpc_id ||
            data.cassette_id ||
            data.header_name ||
            data.header_id ||
            data.machine_status ||
            data.batch_id ||
            data.lot_id ||
            data.timestamp
          );

          if (!connected) {
            // RED: disconnected
            setReaderBoxState(id, false, '', false);
            fillReaderBox(id, { connected: false, fpc_id: '', header_id: '', pm_date: '', timestamp: '' });
          } else if (hasData) {
            // GREEN: connected + tag present
            setReaderBoxState(id, true, data.timestamp || '', true);
            fillReaderBox(id, {
              connected: true,
              fpc_id: data.fpc_id || data.cassette_id || '',
              header_id: data.header_name || data.header_id || data.machine_status || '',
              pm_date: data.pm_date || data.lot_id || '',
              timestamp: data.batch_id || data.timestamp || ''
            });
          } else {
            // GREY: connected, no tag
            setReaderBoxState(id, true, '', false);
            fillReaderBox(id, { connected: true, fpc_id: '', header_id: '', pm_date: '', timestamp: '' });
          }
        }

        // Update last-known cache (used to avoid grey→red flash on page enter)
        if (typeof LAST_AGV_STATUS === 'object' && LAST_AGV_STATUS) {
          LAST_AGV_STATUS[id] = {
            connected,
            tagPresent: !!hasData,
            lastUpdated: Date.now()
          };
        }
      } catch (err) {
        // --- [NEW] Mockup Mode Remote Fallbacks ---
        // If mockup mode is active, instead of displaying disconnected (red) cards,
        // we feed mock data into FPC and Cassette cards to show a fully functioning mockup.
        if (window.MOCKUP_MODE) {
          if (id === 'agv1') {
            setReaderBoxState(id, true, new Date().toISOString(), true);
            fillReaderBox(id, {
              connected: true,
              fpc_id: 'P13080-FHB-0364',
              header_id: 'H13080-PHS-11',
              pm_date: '2025-07-12 16:00:00',
              timestamp: 'BATCH-001'
            });
          } else if (id === 'agv2') {
            setReaderBoxState(id, true, new Date().toISOString(), true);
            fillReaderBox(id, {
              connected: true,
              fpc_id: '2ID031FV002B',
              header_id: 'H15230-PHS-03',
              pm_date: '2025-09-29 16:53:21',
              timestamp: 'BATCH-111'
            });
          } else if (id === 'agv3') {
            setReaderBoxState(id, true, new Date().toISOString(), true);
            fillCassetteSlots(id, {
              cassette_id: 'CASS-001',
              machine_status: 'RUNNING',
              lot_id: 'LOT-X88',
              batch_id: 'BATCH-B01',
              timestamp: new Date().toISOString()
            }, {
              cassette_id: 'CASS-002',
              machine_status: 'CLEANING',
              lot_id: 'LOT-Y99',
              batch_id: 'BATCH-B02',
              timestamp: new Date().toISOString()
            });
          }

          if (typeof LAST_AGV_STATUS === 'object' && LAST_AGV_STATUS) {
            LAST_AGV_STATUS[id] = {
              connected: true,
              tagPresent: true,
              lastUpdated: Date.now()
            };
          }
        } else {
          // --- [COMMENTED OUT / FALLBACK] Real hardware offline state handling ---
          // Timeout / fetch failed → mark this AGV only as disconnected
          setReaderBoxState(id, false, '', false);
          if (id === 'agv3') {
            fillCassetteSlots(id, {}, {});
          } else {
            fillReaderBox(id, { connected: false, fpc_id: '', header_id: '', pm_date: '', timestamp: '' });
          }

          if (typeof LAST_AGV_STATUS === 'object' && LAST_AGV_STATUS) {
            LAST_AGV_STATUS[id] = {
              connected: false,
              tagPresent: false,
              lastUpdated: Date.now()
            };
          }
        }
      } finally {
        updateAgvFromRemote._inflight.delete(id);
      }
    })();
  }
}



// ============================================================================
// LOG PAGE FUNCTIONS
// ============================================================================
function initLogPage() {
  console.log('Initializing Log Page');
  currentPage = 1;
  loadLogs();
}

function loadLogs() {
  const preserveInputs = Array.from(document.querySelectorAll('.search-section input'))
    .map(el => [el.id, el.value]);

  const base = USE_MAIN_FOR_LOGS ? MAIN_API : ''; // '' keeps same-origin on main
  const url = `${base}/api/logs?page=${currentPage}`;

  fetch(url, { mode: USE_MAIN_FOR_LOGS ? 'cors' : 'same-origin' })
    .then(r => r.json())
    .then(data => {
      if (data.status === 'success') {
        logData = data.logs;
        displayLogs(logData);
        updateRecordCount(data.total);
        renderPagination(data.page, data.pages);
      } else {
        console.error('Error loading logs:', data.message);
        showNoDataMessage('Error loading logs: ' + data.message);
      }
    })
    .catch(error => {
      console.error('Error loading logs:', error);
      showNoDataMessage('Connection error. Please check the server.');
    });

  preserveInputs.forEach(([id, val]) => {
    const el = document.getElementById(id);
    if (el && el.value !== val) el.value = val;
  });
}

function searchLogs() {
  const preserveInputs = Array.from(document.querySelectorAll('.search-section input, .search-section select'))
    .map(el => [el.id, el.value]);
  const fpcSearch = document.getElementById('search-fpc').value;
  const dateSearch = document.getElementById('search-date').value;
  const lotSearch = document.getElementById('search-lot').value;
  const batchSearch = document.getElementById('search-batch').value;
  const machineSearch = document.getElementById('search-machine').value.trim();
  const headerSearch = document.getElementById('search-header').value.trim();
  const agvSearch = document.getElementById('search-agv').value.trim();
  const resultSearch = document.getElementById('search-result')?.value || 'all';
  const cassResultSearch = document.getElementById('search-cass-result')?.value || 'all';

  const params = new URLSearchParams();
  if (fpcSearch) params.append('fpc_id', fpcSearch);
  if (dateSearch) params.append('date', dateSearch);
  if (lotSearch) params.append('lot_id', lotSearch);
  if (batchSearch) params.append('batch_id', batchSearch);
  if (machineSearch) params.append('machine_no', machineSearch);
  if (headerSearch) params.append('header_id', headerSearch);
  if (agvSearch) params.append('agv_no', agvSearch);
  if (resultSearch && resultSearch !== 'all') params.append('result_filter', resultSearch);
  if (cassResultSearch && cassResultSearch !== 'all') params.append('cassette_filter', cassResultSearch);

  const base = USE_MAIN_FOR_LOGS ? MAIN_API : '';
  const url = base ? `${base}api/search_logs?${params.toString()}` : `/api/search_logs?${params.toString()}`;
  fetch(url, { mode: USE_MAIN_FOR_LOGS ? 'cors' : 'same-origin' })
    .then(r => r.json())
    .then(data => {
      if (data.status === 'success') {
        displayLogs(data.logs);
        updateRecordCount(data.total);
        renderPagination(data.page, data.pages);
      } else {
        showNoDataMessage('Search error: ' + data.message);
      }
    })
    .catch(err => {
      console.error('Error searching logs:', err);
      showNoDataMessage('Connection error during search.');
    });
  preserveInputs.forEach(([id, val]) => {
    const el = document.getElementById(id);
    if (el && el.value !== val) el.value = val;
  });
}

function clearSearch() {
  document.getElementById('search-fpc').value = '';
  document.getElementById('search-date').value = '';
  document.getElementById('search-lot').value = '';
  document.getElementById('search-batch').value = '';
  document.getElementById('search-machine').value = '';
  document.getElementById('search-header').value = '';
  document.getElementById('search-agv').value = '';
  if (document.getElementById('search-result')) document.getElementById('search-result').value = 'all';
  if (document.getElementById('search-cass-result')) document.getElementById('search-cass-result').value = 'all';
  currentPage = 1;
  loadLogs();
}

function displayLogs(data) {
  const tableBody = document.getElementById('log-table-body');
  if (!data || data.length === 0) {
    showNoDataMessage('No logs found');
    return;
  }
  tableBody.innerHTML = data.map(log => {
    const src = String(log.source || '').toUpperCase();
    const resType = String(log.resultType || '').toLowerCase();
    const isNotFound = (src === 'NOT_FOUND' || resType === 'not_found');
    const isMismatch = !isNotFound && (Boolean(log.isMismatch) || src === 'MISMATCH' || resType === 'mismatch');

    let statusBadge;
    let rowTitle;
    if (isNotFound) {
      statusBadge = `<span class="badge-result badge-result-notfound">🔍 Not Found (ไม่พบข้อมูล)</span>`;
      rowTitle = 'Warning: Tag not registered in database';
    } else if (isMismatch) {
      statusBadge = `<span class="badge-result badge-result-mismatch">❌ Mismatch (ผิดคู่)</span>`;
      rowTitle = 'Warning: FPC and Header Mismatch';
    } else {
      statusBadge = `<span class="badge-result badge-result-match">✓ Match (ถูกต้อง)</span>`;
      rowTitle = 'Valid Tag Pair';
    }

    let cassBadge;
    const cStatus = String(log.cassetteStatus || '').toUpperCase().trim();
    const cResType = String(log.cassetteResultType || '').toLowerCase().trim();
    if (cStatus === 'MATCH_OK' || cStatus === 'FOUND' || cStatus === 'LOADED' || cStatus === 'ACTIVE' || cResType === 'match') {
      cassBadge = `<span class="badge-result badge-result-match">✓ Match (ถูกต้อง)</span>`;
    } else if (cStatus === 'NOT_FOUND' || cResType === 'not_found') {
      cassBadge = `<span class="badge-result badge-result-notfound">🔍 Not Found (ไม่พบข้อมูล)</span>`;
    } else {
      cassBadge = `<span class="text-muted" style="color: #94a3b8; font-weight: 500;">-</span>`;
    }

    return `
        <tr title="${rowTitle}">
            <td>${log.lotId || ''}</td>
            <td>${log.batchId || ''}</td>
            <td>${log.fpcId || ''}</td>
            <td>${(log.headerName || log.headerId) || ''}</td>
            <td>${log.timestamp || ''}</td>
            <td>${log.agvNo || ''}</td>
            <td>${log.machineNo || ''}</td>
            <td>${cassBadge}</td>
            <td>${statusBadge}</td>
        </tr>
    `;
  }).join('');
}

// --- Auto-refresh logs every 5 seconds ---
let autoRefreshTimer = null;
const AUTO_REFRESH_MS = 5000;

// Return true if any search box has a value
function anyLogFilterFilled() {
  const ids = [
    'search-log-id', 'search-fpc', 'search-date',
    'search-lot', 'search-batch', 'search-machine',
    'search-header', 'search-agv'
  ];
  const hasText = ids.some(id => (document.getElementById(id)?.value || '').trim() !== '');
  const resVal = document.getElementById('search-result')?.value;
  const hasSelect = resVal && resVal !== 'all';
  const cassVal = document.getElementById('search-cass-result')?.value;
  const hasCassSelect = cassVal && cassVal !== 'all';
  return hasText || hasSelect || hasCassSelect;
}

function anyCassetteLogFilterFilled() {
  const ids = [
    'cass-search-id', 'cass-search-lot', 'cass-search-batch', 'cass-search-machine', 'cass-search-date'
  ];
  return ids.some(id => (document.getElementById(id)?.value || '').trim() !== '');
}

function refreshLogsTick() {
  if (document.hidden) return; // pause when tab is hidden
  // If user is currently typing in a search input, skip this tick
  const el = document.activeElement;
  if (el && el.tagName === 'INPUT' && el.closest('.search-section')) return;

  const activePage = getActivePageId();
  if (activePage === 'log') {
    if (anyLogFilterFilled()) {
      if (typeof searchLogs === 'function') searchLogs();
    } else {
      if (typeof loadLogs === 'function') loadLogs();
    }
  } else if (activePage === 'cassette-log') {
    if (anyCassetteLogFilterFilled()) {
      if (typeof searchCassetteLogs === 'function') searchCassetteLogs();
    } else {
      if (typeof loadCassetteLogs === 'function') loadCassetteLogs();
    }
  }
}

function startAutoRefresh() {
  stopAutoRefresh();
  autoRefreshTimer = setInterval(refreshLogsTick, AUTO_REFRESH_MS);
}

function stopAutoRefresh() {
  if (autoRefreshTimer) {
    clearInterval(autoRefreshTimer);
    autoRefreshTimer = null;
  }
}


// Pause/resume when tab visibility changes
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    stopAutoRefresh();
  } else {
    refreshLogsTick();
    startAutoRefresh();
  }
});


function showNoDataMessage(message) {
  const tableBody = document.getElementById('log-table-body');
  tableBody.innerHTML = `<tr><td colspan="6" class="no-data">${message}</td></tr>`;
  updateRecordCount(0);
}

function updateRecordCount(count) {
  document.getElementById('record-count').textContent = count;
  updateLastUpdated();
}

function updateLastUpdated() {
  const now = new Date();
  const timestamp = now.toISOString().slice(0, 19).replace('T', ' ');
  document.getElementById('last-updated').textContent = timestamp;
}

// --- Shared Smart Pagination Builder (Compact Page [ 1 ] of X Style) ---
function buildSmartPagination(container, current, totalPages, onPageClick) {
  if (!container) return;
  container.innerHTML = '';
  if (!totalPages || totalPages < 1) totalPages = 1;

  container.className = 'pagination-controls modern-pagination';

  // Helper to create nav arrow buttons
  function createNavBtn(symbol, targetPage, disabled, title) {
    const btn = document.createElement('button');
    btn.innerHTML = symbol;
    btn.className = 'page-btn nav-btn';
    btn.title = title || '';
    if (disabled) {
      btn.disabled = true;
    } else {
      btn.onclick = (e) => {
        e.preventDefault();
        onPageClick(targetPage);
      };
    }
    return btn;
  }

  // 1. First Page Button («)
  const firstBtn = createNavBtn('&laquo;', 1, current <= 1, 'First Page');
  container.appendChild(firstBtn);

  // 2. Previous Page Button (‹)
  const prevBtn = createNavBtn('&lsaquo;', current - 1, current <= 1, 'Previous Page');
  container.appendChild(prevBtn);

  // 3. Middle Capsule Container: Page [ current ] of totalPages
  const middleCapsule = document.createElement('div');
  middleCapsule.className = 'pagination-page-capsule';

  const labelPage = document.createElement('span');
  labelPage.className = 'capsule-label-page';
  labelPage.textContent = 'Page';
  middleCapsule.appendChild(labelPage);

  // Page input badge
  const pageInput = document.createElement('input');
  pageInput.type = 'number';
  pageInput.min = '1';
  pageInput.max = String(totalPages);
  pageInput.value = String(current);
  pageInput.className = 'capsule-page-input';
  pageInput.title = 'Type a page number and press Enter';
  pageInput.onkeydown = (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      let target = parseInt(pageInput.value, 10);
      if (isNaN(target)) target = 1;
      target = Math.max(1, Math.min(totalPages, target));
      if (target !== current) {
        onPageClick(target);
      }
    }
  };
  pageInput.onchange = () => {
    let target = parseInt(pageInput.value, 10);
    if (isNaN(target)) target = 1;
    target = Math.max(1, Math.min(totalPages, target));
    if (target !== current) {
      onPageClick(target);
    }
  };
  middleCapsule.appendChild(pageInput);

  const labelTotal = document.createElement('span');
  labelTotal.className = 'capsule-label-total';
  labelTotal.textContent = `of ${totalPages}`;
  middleCapsule.appendChild(labelTotal);

  container.appendChild(middleCapsule);

  // 4. Next Page Button (›)
  const nextBtn = createNavBtn('&rsaquo;', current + 1, current >= totalPages, 'Next Page');
  container.appendChild(nextBtn);

  // 5. Last Page Button (»)
  const lastBtn = createNavBtn('&raquo;', totalPages, current >= totalPages, 'Last Page');
  container.appendChild(lastBtn);
}

function renderPagination(current, totalPages) {
  const container = document.getElementById('pagination-controls');
  buildSmartPagination(container, current, totalPages, (page) => {
    currentPage = page;
    loadLogs();
  });
}

function exportCSV() {
  const btn = document.querySelector('.search-section .export-btn') || event?.target;
  const originalText = btn ? btn.textContent : '';
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Exporting...';
  }

  // Construct query parameters for full dataset export
  const params = new URLSearchParams({ page: '1', pageSize: '50000' });
  const fpcSearch = document.getElementById('search-fpc')?.value.trim();
  const dateSearch = document.getElementById('search-date')?.value.trim();
  const lotSearch = document.getElementById('search-lot')?.value.trim();
  const batchSearch = document.getElementById('search-batch')?.value.trim();
  const machineSearch = document.getElementById('search-machine')?.value.trim();
  const headerSearch = document.getElementById('search-header')?.value.trim();
  const agvSearch = document.getElementById('search-agv')?.value.trim();
  const resultSearch = document.getElementById('search-result')?.value;
  const cassResultSearch = document.getElementById('search-cass-result')?.value;

  if (fpcSearch) params.append('fpc_id', fpcSearch);
  if (dateSearch) params.append('date', dateSearch);
  if (lotSearch) params.append('lot_id', lotSearch);
  if (batchSearch) params.append('batch_id', batchSearch);
  if (machineSearch) params.append('machine_no', machineSearch);
  if (headerSearch) params.append('header_id', headerSearch);
  if (agvSearch) params.append('agv_no', agvSearch);
  if (resultSearch && resultSearch !== 'all') params.append('result_filter', resultSearch);
  if (cassResultSearch && cassResultSearch !== 'all') params.append('cassette_filter', cassResultSearch);

  const hasFilter = Array.from(params.keys()).some(k => k !== 'page' && k !== 'pageSize');
  const endpoint = hasFilter ? 'api/search_logs' : 'api/logs';
  const base = USE_MAIN_FOR_LOGS ? MAIN_API : '';
  const url = base ? `${base}/${endpoint}?${params.toString()}` : `/${endpoint}?${params.toString()}`;

  fetch(url, { mode: USE_MAIN_FOR_LOGS ? 'cors' : 'same-origin' })
    .then(res => res.json())
    .then(data => {
      if (data.status !== 'success') {
        alert('Error exporting CSV: ' + (data.message || 'unknown error'));
        return;
      }

      const logs = data.data || data.logs || [];
      if (!logs.length) {
        alert('No logs to export.');
        return;
      }

      // Columns to export
      const headers = [
        'id', 'lot_id', 'batch_id', 'fpc_id',
        'header_id', 'header_name', 'timestamp',
        'agv_no', 'machine_no', 'cassette_result', 'probe_card_result', 'touchdown', 'comment'
      ];

      const rows = [headers];

      logs.forEach(log => {
        const rawId = (log.id != null)
          ? String(log.id)
          : (log.logId ? String(log.logId).replace(/^LOG0*/, '') : '');

        const src = String(log.source || '').toUpperCase();
        const resType = String(log.resultType || '').toLowerCase();
        const isNotFound = (src === 'NOT_FOUND' || resType === 'not_found');
        const isMismatch = !isNotFound && (Boolean(log.isMismatch) || src === 'MISMATCH' || resType === 'mismatch');
        const resText = isNotFound ? 'Not Found' : (isMismatch ? 'Mismatch' : 'Match');

        const cStat = String(log.cassetteStatus || '').toUpperCase();
        const cRes = String(log.cassetteResultType || '').toLowerCase();
        let cassText = '-';
        if (cStat === 'MATCH_OK' || cStat === 'FOUND' || cStat === 'LOADED' || cStat === 'ACTIVE' || cRes === 'match') {
          cassText = 'Match';
        } else if (cStat === 'NOT_FOUND' || cRes === 'not_found') {
          cassText = 'Not Found';
        }

        rows.push([
          rawId,
          log.lotId || '',
          log.batchId || '',
          log.fpcId || '',
          log.headerId || '',
          log.headerName || '',
          log.timestamp || '',
          log.agvNo || '',
          log.machineNo || '',
          cassText,
          resText,
          log.touchdown != null ? String(log.touchdown) : '',
          log.comment || ''
        ]);
      });

      // Safe CSV escaping
      const esc = v => {
        if (v == null) return '';
        const s = String(v);
        return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
      };

      const csv = rows.map(r => r.map(esc).join(',')).join('\n');

      // Excel-friendly BOM for UTF-8 (Thai text etc.)
      const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8;' });
      const dlUrl = URL.createObjectURL(blob);

      const now = new Date();
      const fname = `rfid_logs_${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}.csv`;

      const a = document.createElement('a');
      a.href = dlUrl;
      a.download = fname;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(dlUrl);
    })
    .catch(err => {
      console.error('Export CSV error:', err);
      alert('Export failed: ' + err.message);
    })
    .finally(() => {
      if (btn) {
        btn.disabled = false;
        btn.textContent = originalText;
      }
    });
}


function initCassetteLogPage() {
  console.log('Initializing Cassette Log Page');
  currentCassettePage = 1;
  loadCassetteLogs();
}

function loadCassetteLogs() {
  const preserveInputs = Array.from(document.querySelectorAll('#cassette-log .search-section input'))
    .map(el => [el.id, el.value]);

  const base = USE_MAIN_FOR_LOGS ? MAIN_API : '';
  const url = base ? `${base}api/cassette/logs?page=${currentCassettePage}` : `/api/cassette/logs?page=${currentCassettePage}`;

  fetch(url, { mode: USE_MAIN_FOR_LOGS ? 'cors' : 'same-origin' })
    .then(r => r.json())
    .then(data => {
      if (data.status === 'success') {
        cassetteLogData = data.logs;
        displayCassetteLogs(cassetteLogData);
        updateCassetteRecordCount(data.total);
        renderCassettePagination(data.page, data.pages);
      } else {
        console.error('Error loading cassette logs:', data.message);
        showCassetteNoDataMessage('Error loading cassette logs: ' + data.message);
      }
    })
    .catch(error => {
      console.error('Error loading cassette logs:', error);
      showCassetteNoDataMessage('Connection error. Please check the server.');
    });

  preserveInputs.forEach(([id, val]) => {
    const el = document.getElementById(id);
    if (el && el.value !== val) el.value = val;
  });
}

function searchCassetteLogs() {
  const preserveInputs = Array.from(document.querySelectorAll('#cassette-log .search-section input'))
    .map(el => [el.id, el.value]);
  const cassSearch = document.getElementById('cass-search-id').value;
  const lotSearch = document.getElementById('cass-search-lot').value;
  const batchSearch = document.getElementById('cass-search-batch').value;
  const machineSearch = document.getElementById('cass-search-machine').value.trim();
  const dateSearch = document.getElementById('cass-search-date').value;

  const params = new URLSearchParams();
  if (cassSearch) params.append('cassette_id', cassSearch);
  if (lotSearch) params.append('lot_id', lotSearch);
  if (batchSearch) params.append('batch_id', batchSearch);
  if (machineSearch) params.append('machine_no', machineSearch);
  if (dateSearch) params.append('date', dateSearch);
  params.append('page', currentCassettePage);

  const base = USE_MAIN_FOR_LOGS ? MAIN_API : '';
  const url = base ? `${base}api/cassette/search_logs?${params.toString()}` : `/api/cassette/search_logs?${params.toString()}`;
  fetch(url, { mode: USE_MAIN_FOR_LOGS ? 'cors' : 'same-origin' })
    .then(r => r.json())
    .then(data => {
      if (data.status === 'success') {
        displayCassetteLogs(data.logs);
        updateCassetteRecordCount(data.total);
        renderCassettePagination(data.page, data.pages);
      } else {
        showCassetteNoDataMessage('Search error: ' + data.message);
      }
    })
    .catch(err => {
      console.error('Error searching cassette logs:', err);
      showCassetteNoDataMessage('Connection error during search.');
    });
  preserveInputs.forEach(([id, val]) => {
    const el = document.getElementById(id);
    if (el && el.value !== val) el.value = val;
  });
}

function clearCassetteSearch() {
  document.getElementById('cass-search-id').value = '';
  document.getElementById('cass-search-lot').value = '';
  document.getElementById('cass-search-batch').value = '';
  document.getElementById('cass-search-machine').value = '';
  document.getElementById('cass-search-date').value = '';
  currentCassettePage = 1;
  loadCassetteLogs();
}

function displayCassetteLogs(data) {
  const tableBody = document.getElementById('cass-log-table-body');
  const noData = document.getElementById('cass-no-data');
  if (!data || data.length === 0) {
    showCassetteNoDataMessage('No logs found');
    if (noData) noData.style.display = 'block';
    return;
  }
  if (noData) noData.style.display = 'none';
  tableBody.innerHTML = data.map(log => `
        <tr>
            <td>${log.cassetteId || ''}</td>
            <td>${log.machineStatus || ''}</td>
            <td>${log.lotId || ''}</td>
            <td>${log.batchId || ''}</td>
            <td>${log.lastCleaning || ''}</td>
            <td>${log.nextCleaning || ''}</td>
            <td>${log.timestamp || ''}</td>
            <td>${log.machineNo || ''}</td>
        </tr>
    `).join('');
}

function showCassetteNoDataMessage(message) {
  const tableBody = document.getElementById('cass-log-table-body');
  tableBody.innerHTML = `<tr><td colspan="8" class="no-data">${message}</td></tr>`;
  updateCassetteRecordCount(0);
}

function updateCassetteRecordCount(count) {
  const el = document.getElementById('cass-record-count');
  if (el) el.textContent = count;
  updateCassetteLastUpdated();
}

function updateCassetteLastUpdated() {
  const el = document.getElementById('cass-last-updated');
  if (!el) return;
  const now = new Date();
  const timestamp = now.toISOString().slice(0, 19).replace('T', ' ');
  el.textContent = timestamp;
}

function renderCassettePagination(current, totalPages) {
  const container = document.getElementById('cass-pagination-controls');
  buildSmartPagination(container, current, totalPages, (page) => {
    currentCassettePage = page;
    loadCassetteLogs();
  });
}

function exportCassetteCSV() {
  const btn = document.querySelector('#cassette-log .export-btn') || event?.target;
  const originalText = btn ? btn.textContent : '';
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Exporting...';
  }

  const params = new URLSearchParams({ page: '1', pageSize: '50000' });
  const cassSearch = document.getElementById('cass-search-id')?.value.trim();
  const lotSearch = document.getElementById('cass-search-lot')?.value.trim();
  const batchSearch = document.getElementById('cass-search-batch')?.value.trim();
  const machineSearch = document.getElementById('cass-search-machine')?.value.trim();
  const dateSearch = document.getElementById('cass-search-date')?.value.trim();

  if (cassSearch) params.append('cassette_id', cassSearch);
  if (lotSearch) params.append('lot_id', lotSearch);
  if (batchSearch) params.append('batch_id', batchSearch);
  if (machineSearch) params.append('machine_no', machineSearch);
  if (dateSearch) params.append('date', dateSearch);

  const hasFilter = Array.from(params.keys()).some(k => k !== 'page' && k !== 'pageSize');
  const endpoint = hasFilter ? 'api/cassette/search_logs' : 'api/cassette/logs';
  const base = USE_MAIN_FOR_LOGS ? MAIN_API : '';
  const url = base ? `${base}/${endpoint}?${params.toString()}` : `/${endpoint}?${params.toString()}`;

  fetch(url, { mode: USE_MAIN_FOR_LOGS ? 'cors' : 'same-origin' })
    .then(res => res.json())
    .then(data => {
      if (data.status !== 'success') {
        alert('Error exporting CSV: ' + (data.message || 'unknown error'));
        return;
      }

      const logs = data.logs || [];
      if (!logs.length) {
        alert('No logs to export.');
        return;
      }

      const headers = [
        'id', 'cassette_id', 'machine_status', 'lot_id',
        'batch_id', 'last_cleaning', 'next_cleaning',
        'timestamp', 'machine_no'
      ];

      const rows = [headers];

      logs.forEach(log => {
        const rawId = log.logId ? String(log.logId).replace(/^CASS0*/, '') : '';

        rows.push([
          rawId,
          log.cassetteId || '',
          log.machineStatus || '',
          log.lotId || '',
          log.batchId || '',
          log.lastCleaning || '',
          log.nextCleaning || '',
          log.timestamp || '',
          log.machineNo || ''
        ]);
      });

      // Safe CSV escaping
      const esc = v => {
        if (v == null) return '';
        const s = String(v);
        return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
      };

      const csv = rows.map(r => r.map(esc).join(',')).join('\n');

      // Excel-friendly BOM for UTF-8 (Thai text etc.)
      const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8;' });
      const dlUrl = URL.createObjectURL(blob);

      const now = new Date();
      const fname = `cassette_logs_${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}.csv`;

      const a = document.createElement('a');
      a.href = dlUrl;
      a.download = fname;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(dlUrl);
    })
    .catch(err => {
      console.error('Export Cassette CSV error:', err);
      alert('Export failed: ' + err.message);
    })
    .finally(() => {
      if (btn) {
        btn.disabled = false;
        btn.textContent = originalText;
      }
    });
}


// ============================================================================
// SETTINGS PAGE FUNCTIONS
// ============================================================================
function initSettingsPage() {
  // Always reset to login whenever the Settings page becomes active
  clearAuth();
  applyRoleLock();
  showLoginOnly();
}


function showLoginScreen() {
  document.getElementById('loginScreen').style.display = 'block';
  document.getElementById('settingsScreen').classList.remove('show');
  document.getElementById('employeeId').value = '';
  document.getElementById('errorMessage').style.display = 'none';
}

// ===== UI toggle helpers for Settings page =====
function showLoginOnly() {
  const login = document.getElementById('loginScreen');
  const settings = document.getElementById('settingsScreen');
  const input = document.getElementById('employeeId');
  const errorMessage = document.getElementById('errorMessage');

  if (settings) settings.style.display = 'none';
  if (login) login.style.display = 'block';
  if (input) input.value = '';
  if (errorMessage) errorMessage.style.display = 'none';

  // Hide sidebar menu bar during Settings mode
  const navPanel = document.querySelector('.nav-panel');
  if (navPanel) navPanel.style.display = 'none';

  // Force clean state each time
  localStorage.removeItem('role');
  localStorage.removeItem('loggedIn');
  applyRoleLock();
}

// --- [NEW] Updated showSettingsOnly function to pre-populate machine name and hide sidebar ---
function showSettingsOnly() {
  const login = document.getElementById('loginScreen');
  const settings = document.getElementById('settingsScreen');
  if (login) login.style.display = 'none';
  if (settings) settings.style.display = 'block';

  // Hide sidebar menu bar during Settings mode
  const navPanel = document.querySelector('.nav-panel');
  if (navPanel) navPanel.style.display = 'none';

  // Pre-populate input field from the home page title
  const mainTitle = document.getElementById('main-machine-title');
  const nameInput = document.getElementById('machine-name-input');
  if (mainTitle && nameInput) {
    nameInput.value = mainTitle.textContent.trim();
  }

  // Load Smart Store mappings based on active tab
  if (typeof currentMappingsTab !== 'undefined' && currentMappingsTab === 'cassette') {
    loadCabinetMappings();
  } else {
    loadStoreMappings();
  }
}

// --- [NEW] Function to handle updating the machine name via API ---
function updateMachineName() {
  const inputEl = document.getElementById('machine-name-input');
  if (!inputEl) return;
  const newName = inputEl.value.trim();
  if (!newName) {
    alert("Machine name cannot be empty!");
    return;
  }

  // Send name update request to backend
  fetch('/api/update_machine_name', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({ machine_no: newName })
  })
    .then(r => r.json())
    .then(data => {
      if (data.status === 'success') {
        alert("Machine name updated successfully!");

        // Dynamically update main title header
        const mainTitle = document.getElementById('main-machine-title');
        if (mainTitle) {
          mainTitle.textContent = newName;
        }
        const fpcLogTitle = document.getElementById('fpc-log-title');
        if (fpcLogTitle) {
          fpcLogTitle.textContent = `Log ${newName}`;
        }
        const cassetteLogTitle = document.getElementById('cassette-log-title');
        if (cassetteLogTitle) {
          cassetteLogTitle.textContent = `Log PMI ${newName}`;
        }
      } else {
        alert("Failed to update machine name: " + (data.message || "Unknown error"));
      }
    })
    .catch(err => {
      console.error("Error updating machine name:", err);
      alert("Error sending update command to server.");
    });
}

// ============================================================================
// SMART STORE PROBE CARD MAPPINGS & SYNC
// ============================================================================
let allStoreMappings = [];

function escapeHtml(str) {
  if (str == null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

async function loadStoreMappings() {
  const tbody = document.getElementById('mappings-table-body');
  const countEl = document.getElementById('mappings-count');
  const sourceEl = document.getElementById('mappings-source');

  try {
    const res = await fetch('/api/store/mappings');
    const data = await res.json();
    if (data.status === 'success' && Array.isArray(data.mappings)) {
      allStoreMappings = data.mappings;
      if (countEl) countEl.textContent = allStoreMappings.length;
      if (sourceEl && data.source) sourceEl.textContent = `Source: ${data.source}`;
      renderMappingsTable(allStoreMappings);
    } else {
      if (tbody) tbody.innerHTML = '<tr><td colspan="6" class="mappings-empty">ไม่พบข้อมูลในตาราง</td></tr>';
    }
  } catch (err) {
    console.error('Error loading store mappings:', err);
    if (tbody) tbody.innerHTML = '<tr><td colspan="6" class="mappings-empty">เกิดข้อผิดพลาดในการโหลดข้อมูล</td></tr>';
  }
}

function renderMappingsTable(items) {
  const tbody = document.getElementById('mappings-table-body');
  if (!tbody) return;

  if (!items || items.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="mappings-empty">ไม่พบข้อมูลที่ตรงกับคำค้นหา</td></tr>';
    return;
  }

  tbody.innerHTML = items.map(m => `
    <tr>
      <td class="mappings-fpc-id">${escapeHtml(m.fpc_id || '-')}</td>
      <td>${escapeHtml(m.header_id || '-')}</td>
      <td>${escapeHtml(String(m.touchdown ?? '-'))}</td>
      <td>${escapeHtml(m.latest_pm || '-')}</td>
      <td>${escapeHtml(m.timer || '-')}</td>
      <td>${escapeHtml(m.comment || '-')}</td>
    </tr>
  `).join('');
}

function filterMappingsTable() {
  const searchInput = document.getElementById('mappings-search-input');
  const query = (searchInput?.value || '').toLowerCase().trim();
  const countEl = document.getElementById('mappings-count');

  if (!query) {
    renderMappingsTable(allStoreMappings);
    if (countEl) countEl.textContent = allStoreMappings.length;
    return;
  }

  const filtered = allStoreMappings.filter(m => {
    const fpc = (m.fpc_id || '').toLowerCase();
    const hdr = (m.header_id || '').toLowerCase();
    const cmt = (m.comment || '').toLowerCase();
    const td = String(m.touchdown ?? '').toLowerCase();
    const pm = (m.latest_pm || '').toLowerCase();
    const tm = (m.timer || '').toLowerCase();
    return fpc.includes(query) || hdr.includes(query) || cmt.includes(query) || td.includes(query) || pm.includes(query) || tm.includes(query);
  });

  renderMappingsTable(filtered);
  if (countEl) countEl.textContent = filtered.length;
}

async function syncStoreMappings() {
  const btn = document.getElementById('btn-sync-mappings');
  const icon = btn?.querySelector('.sync-icon');
  const textEl = document.getElementById('btn-sync-text');
  const searchInput = document.getElementById('mappings-search-input');

  if (btn) btn.disabled = true;
  if (icon) icon.classList.add('spinning');
  if (textEl) textEl.textContent = 'Syncing...';

  try {
    const res = await fetch('/api/store/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    });
    const data = await res.json();

    if (data.status === 'success' || data.status === 'warning') {
      if (Array.isArray(data.mappings)) {
        allStoreMappings = data.mappings;
        const countEl = document.getElementById('mappings-count');
        const sourceEl = document.getElementById('mappings-source');
        if (countEl) countEl.textContent = allStoreMappings.length;
        if (sourceEl && data.source) sourceEl.textContent = `Source: ${data.source}`;
        if (searchInput) searchInput.value = '';
        renderMappingsTable(allStoreMappings);
      }
      alert(data.message || 'ซิงค์ข้อมูล Smart Store สำเร็จ');
    } else {
      alert('การซิงค์ข้อมูลไม่สำเร็จ: ' + (data.message || 'ข้อผิดพลาดที่ไม่ทราบสาเหตุ'));
    }
  } catch (err) {
    console.error('Sync mappings error:', err);
    alert('เกิดข้อผิดพลาดในการเชื่อมต่อเพื่อซิงค์ข้อมูล: ' + err.message);
  } finally {
    if (btn) btn.disabled = false;
    if (icon) icon.classList.remove('spinning');
    if (textEl) textEl.textContent = 'Sync Now';
  }
}

// ============================================================================
// SMART STORE CABINET (CASSETTE TAG) MAPPINGS & SYNC
// ============================================================================
let allCabinetMappings = [];
let currentMappingsTab = 'probecard';

function switchMappingsTab(tabName) {
  currentMappingsTab = tabName;
  const btnProbe = document.getElementById('tab-btn-probecard');
  const btnCassette = document.getElementById('tab-btn-cassette');
  const viewProbe = document.getElementById('view-probecard-mappings');
  const viewCassette = document.getElementById('view-cassette-mappings');

  if (tabName === 'cassette') {
    if (btnProbe) btnProbe.classList.remove('active');
    if (btnCassette) btnCassette.classList.add('active');
    if (viewProbe) {
      viewProbe.classList.remove('active');
      viewProbe.style.display = 'none';
    }
    if (viewCassette) {
      viewCassette.classList.add('active');
      viewCassette.style.display = 'block';
    }
    loadCabinetMappings();
  } else {
    if (btnCassette) btnCassette.classList.remove('active');
    if (btnProbe) btnProbe.classList.add('active');
    if (viewCassette) {
      viewCassette.classList.remove('active');
      viewCassette.style.display = 'none';
    }
    if (viewProbe) {
      viewProbe.classList.add('active');
      viewProbe.style.display = 'block';
    }
    loadStoreMappings();
  }
}

async function loadCabinetMappings() {
  const tbody = document.getElementById('cabinet-mappings-table-body');
  const countEl = document.getElementById('cabinet-mappings-count');
  const sourceEl = document.getElementById('cabinet-mappings-source');

  try {
    const res = await fetch('/api/cabinet/mappings');
    const data = await res.json();
    if (data.status === 'success' && Array.isArray(data.mappings)) {
      allCabinetMappings = data.mappings;
      if (countEl) countEl.textContent = allCabinetMappings.length;
      if (sourceEl && data.source) sourceEl.textContent = `Source: ${data.source}`;
      renderCabinetTable(allCabinetMappings);
    } else {
      if (tbody) tbody.innerHTML = '<tr><td colspan="4" class="mappings-empty">ไม่พบข้อมูลในตาราง</td></tr>';
    }
  } catch (err) {
    console.error('Error loading cabinet mappings:', err);
    if (tbody) tbody.innerHTML = '<tr><td colspan="4" class="mappings-empty">เกิดข้อผิดพลาดในการโหลดข้อมูล</td></tr>';
  }
}

function renderCabinetTable(items) {
  const tbody = document.getElementById('cabinet-mappings-table-body');
  if (!tbody) return;

  if (!items || items.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" class="mappings-empty">ไม่พบข้อมูลที่ตรงกับคำค้นหา</td></tr>';
    return;
  }

  tbody.innerHTML = items.map(m => `
    <tr>
      <td class="mappings-tag-id">${escapeHtml(m.tag_id || '-')}</td>
      <td>${escapeHtml(m.lot_id || '-')}</td>
      <td>${escapeHtml(m.batch_id || '-')}</td>
      <td>${escapeHtml(m.mapping_time || '-')}</td>
    </tr>
  `).join('');
}

function filterCabinetTable() {
  const searchInput = document.getElementById('cabinet-search-input');
  const query = (searchInput?.value || '').toLowerCase().trim();
  const countEl = document.getElementById('cabinet-mappings-count');

  if (!query) {
    renderCabinetTable(allCabinetMappings);
    if (countEl) countEl.textContent = allCabinetMappings.length;
    return;
  }

  const filtered = allCabinetMappings.filter(m => {
    const tag = (m.tag_id || '').toLowerCase();
    const lot = (m.lot_id || '').toLowerCase();
    const batch = (m.batch_id || '').toLowerCase();
    const tm = (m.mapping_time || '').toLowerCase();
    return tag.includes(query) || lot.includes(query) || batch.includes(query) || tm.includes(query);
  });

  renderCabinetTable(filtered);
  if (countEl) countEl.textContent = filtered.length;
}

async function syncCabinetMappings() {
  const btn = document.getElementById('btn-sync-cabinet');
  const icon = btn?.querySelector('.sync-icon');
  const textEl = document.getElementById('btn-sync-cabinet-text');
  const searchInput = document.getElementById('cabinet-search-input');

  if (btn) btn.disabled = true;
  if (icon) icon.classList.add('spinning');
  if (textEl) textEl.textContent = 'Syncing...';

  try {
    const res = await fetch('/api/cabinet/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    });
    const data = await res.json();

    if (data.status === 'success' || data.status === 'warning') {
      if (Array.isArray(data.mappings)) {
        allCabinetMappings = data.mappings;
        const countEl = document.getElementById('cabinet-mappings-count');
        const sourceEl = document.getElementById('cabinet-mappings-source');
        if (countEl) countEl.textContent = allCabinetMappings.length;
        if (sourceEl && data.source) sourceEl.textContent = `Source: ${data.source}`;
        if (searchInput) searchInput.value = '';
        renderCabinetTable(allCabinetMappings);
      }
      alert(data.message || 'ซิงค์ข้อมูล Smart Store Cabinet สำเร็จ');
    } else {
      alert('การซิงค์ข้อมูลไม่สำเร็จ: ' + (data.message || 'ข้อผิดพลาดที่ไม่ทราบสาเหตุ'));
    }
  } catch (err) {
    console.error('Sync cabinet mappings error:', err);
    alert('เกิดข้อผิดพลาดในการเชื่อมต่อเพื่อซิงค์ข้อมูล: ' + err.message);
  } finally {
    if (btn) btn.disabled = false;
    if (icon) icon.classList.remove('spinning');
    if (textEl) textEl.textContent = 'Sync Now';
  }
}


async function submitLogin() {
  const input = document.getElementById('employeeId');
  const errorMessage = document.getElementById('errorMessage');
  const employeeId = (input?.value || '').trim().toUpperCase();

  if (!employeeId) {
    if (errorMessage) {
      errorMessage.textContent = 'Please enter your Employee ID';
      errorMessage.style.display = 'block';
    }
    return;
  }
  errorMessage && (errorMessage.style.display = 'none');

  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',              // send/receive session cookie
      body: JSON.stringify({ employeeId })     // <-- IMPORTANT: camelCase key
    });

    const data = await res.json().catch(() => ({}));

    if (!res.ok || data.ok !== true) {
      if (errorMessage) {
        const msg = (data?.error === 'NOT_FOUND')
          ? 'Employee ID not found'
          : (data?.error === 'EMPTY_ID')
            ? 'Please enter your Employee ID'
            : 'Login failed';
        errorMessage.textContent = msg;
        errorMessage.style.display = 'block';
      }
      return;
    }

    // --- [COMMENTED OUT] Old setAuth call ---
    // setAuth(data.role || 'user');   // persist role for UI lock
    // --- [NEW] Pass employeeId to persist it in localStorage ---
    setAuth(data.role || 'user', employeeId);
    applyRoleLock();                // blur/unblur admin-only buttons
    showSettingsOnly();             // switch screen to settings
  } catch (e) {
    if (errorMessage) {
      errorMessage.textContent = 'Network error — please try again';
      errorMessage.style.display = 'block';
    }
  }
}




let currentEmployeeId = null;

function logAction(action) {
  const payload = {
    employee_id: (currentEmployeeId || 'ADMIN').toUpperCase(),
    action
  };
  fetch('/api/system_log', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  }).catch(() => { });
}


function goBack() {
  const navPanel = document.querySelector('.nav-panel');
  if (navPanel) navPanel.style.display = 'flex';
  switchPage('home');
}

async function logout(e) {
  if (e && e.preventDefault) e.preventDefault();

  // end server session (ignore errors so UI still resets)
  try { await fetch('/api/logout', { method: 'POST', credentials: 'same-origin' }); } catch { }

  // clear client auth + locks
  clearAuth();
  applyRoleLock();

  // hide Settings panel, show Login panel
  const settings = document.getElementById('settingsScreen');
  const login = document.getElementById('loginScreen');
  if (settings) {
    settings.classList.remove('show');
    settings.style.display = 'none';
  }
  if (login) {
    login.style.display = 'block';
  }

  // Restore sidebar menu bar
  const navPanel = document.querySelector('.nav-panel');
  if (navPanel) navPanel.style.display = 'flex';

  // reset login form bits
  const input = document.getElementById('employeeId');
  const err = document.getElementById('errorMessage');
  if (input) input.value = '';
  if (err) err.style.display = 'none';

  // Return to Home page automatically
  switchPage('home');
}


function resetIPAddress() {
  if (confirm('Are you sure you want to reset the IP address configuration? This will require system restart.')) {
    console.log('Resetting IP Address...');
    logAction('reset_ip_address');
    alert('IP Address has been reset successfully');
  }
}

function resetLogs() {
  if (confirm('Are you sure you want to clear all system logs? This action cannot be undone.')) {
    console.log('Resetting System Logs...');
    logAction('reset_logs');
    alert('System logs have been cleared successfully');
  }
}

function systemReset() {
  if (confirm('WARNING: This will perform a complete system reset. All settings and data will be lost. Are you sure?')) {
    if (confirm('This is your final confirmation. Continue with system reset?')) {
      console.log('Performing System Reset...');
      logAction('system_reset');
      alert('System reset initiated');
    }
  }
}

function resetRFID() {
  if (confirm('Are you sure you want to reset RFID reader settings? This will recalibrate the RFID system.')) {
    console.log('Resetting RFID Settings...');
    logAction('reset_rfid_settings');
    alert('RFID settings have been reset');
  }
}

function openSettingsLog() {
  document.getElementById('settings-log-modal').style.display = 'flex';
  loadSettingsLog(1);
}
function closeSettingsLog() {
  document.getElementById('settings-log-modal').style.display = 'none';
}
function clearSettingsLogFilters() {
  document.getElementById('syslog-emp').value = '';
  document.getElementById('syslog-action').value = '';
  document.getElementById('syslog-date').value = '';
  loadSettingsLog(1);
}
function loadSettingsLog(page = 1) {
  const emp = document.getElementById('syslog-emp').value.trim();
  const act = document.getElementById('syslog-action').value.trim();
  const date = document.getElementById('syslog-date').value;
  const params = new URLSearchParams({ page });
  if (emp) params.append('employee_id', emp);
  if (act) params.append('action', act);
  if (date) params.append('date', date);

  fetch(`/api/system_log?${params.toString()}`)
    .then(r => r.json())
    .then(data => {
      if (data.status !== 'success') return;

      // Update record count display
      const countEl = document.getElementById('syslog-record-count');
      if (countEl) {
        countEl.textContent = data.total || 0;
      }

      const tb = document.getElementById('syslog-body');
      if (!tb) return;
      if (!data.logs || data.logs.length === 0) {
        tb.innerHTML = `<tr><td colspan="5" class="no-data">No settings logs</td></tr>`;
      } else {
        tb.innerHTML = data.logs.map(r => `
                <tr>
                  <td>${r.employeeId}</td>
                  <td>${r.action}</td>
                  <td>${r.timestamp}</td>
                  <td>${r.ip || ''}</td>
                </tr>`).join('');
      }
      // pagination
      const pg = document.getElementById('syslog-pagination');
      buildSmartPagination(pg, data.page, data.pages, (page) => {
        loadSettingsLog(page);
      });
    })
    .catch(() => { });
}
// ===== Auth state helpers =====
// --- [COMMENTED OUT] Old setAuth and clearAuth ---
/*
function setAuth(role) {
  localStorage.setItem('role', role || 'user');
  localStorage.setItem('loggedIn', role ? '1' : '0');
}

function clearAuth() {
  localStorage.removeItem('role');
  localStorage.removeItem('loggedIn');
}
*/

// --- [NEW] Updated setAuth and clearAuth to persist employee ID for role verification ---
function setAuth(role, employeeId) {
  localStorage.setItem('role', role || 'user');
  localStorage.setItem('loggedIn', role ? '1' : '0');
  if (employeeId) {
    localStorage.setItem('employeeId', employeeId);
  }
}

function clearAuth() {
  localStorage.removeItem('role');
  localStorage.removeItem('loggedIn');
  localStorage.removeItem('employeeId');
}

function isLoggedIn() {
  return localStorage.getItem('loggedIn') === '1';
}

function isAdmin() {
  return localStorage.getItem('role') === 'admin';
}

// ===== UI lock for admin-only buttons =====
function blockNonAdmin(e) {
  if (!isAdmin()) {
    e.stopPropagation();
    e.preventDefault();
    console.warn('Blocked: Admin only');
  }
}

function applyRoleLock() {
  const adminOnly = document.querySelectorAll('.admin-only');
  const lock = !isAdmin();
  adminOnly.forEach(btn => {
    if (lock) {
      btn.classList.add('locked');
      btn.setAttribute('aria-disabled', 'true');
      // Important: use the *same* function reference & capture option for add/remove
      btn.addEventListener('click', blockNonAdmin, { capture: true });
    } else {
      btn.classList.remove('locked');
      btn.removeAttribute('aria-disabled');
      btn.removeEventListener('click', blockNonAdmin, { capture: true });
    }
  });
}

// ===== Settings page guard (hide if not logged in) =====
function showSettingsPage() {
  const login = document.getElementById('loginScreen');
  const settings = document.getElementById('settingsScreen');

  if (!isLoggedIn()) {
    // force back to login if not authenticated
    if (settings) settings.style.display = 'none';
    if (login) login.style.display = 'block';
    return;
  }
  if (login) login.style.display = 'none';
  if (settings) settings.style.display = 'block';

  // Every time settings becomes visible, re-apply locks
  applyRoleLock();
}

// ===== Re-sync with server session on load and when needed =====
async function syncAuthFromServer() {
  try {
    const r = await fetch('/whoami', { cache: 'no-store', credentials: 'same-origin' });
    if (!r.ok) throw new Error('whoami failed');
    const data = await r.json();
    if (data.ok && data.loggedIn) {
      setAuth(data.role || 'user');
    } else {
      clearAuth();
    }
  } catch {
    clearAuth();
  }
  applyRoleLock();
}


// Call on first load
document.addEventListener('DOMContentLoaded', async () => {
  await syncAuthFromServer();
  applyRoleLock();
  if (window.location.pathname.includes('/settings')) {
    switchPage('setting');
  }
});

// If you have tab/menu buttons, guard Settings navigation:
// Always force login view when entering Settings
document.addEventListener('click', async (e) => {
  const t = e.target;
  if (t && t.matches && t.matches('#settingsTab, [data-nav="setting"], [data-nav="settings"]')) {
    e.preventDefault();
    e.stopPropagation();

    // always force logged-out UI when entering Settings
    clearAuth();
    applyRoleLock();

    // show ONLY login subview inside the Settings page
    const settings = document.getElementById('settingsScreen');
    const login = document.getElementById('loginScreen');
    if (settings) { settings.classList.remove('show'); settings.style.display = 'none'; }
    if (login) { login.style.display = 'block'; }

    // navigate to Settings page container if you use page switching
    if (typeof switchPage === 'function') switchPage('setting', t);
  }
});


document.addEventListener('click', (e) => {
  const t = e.target;
  if (!t || !t.matches) return;
  if (t.matches('#logoutButton, #logout, [data-action="logout"]')) {
    logout(e);
  }
});


function isAdmin() {
  return localStorage.getItem("role") === "admin";
}

function setRoleByEmployeeId(empId) {
  const role = ADMIN_IDS.includes((empId || "").trim()) ? "admin" : "user";
  localStorage.setItem("role", role);
  return role;
}

function clearRole() {
  localStorage.removeItem("role");
}

function applyRoleLock() {
  const adminOnly = document.querySelectorAll(".admin-only");
  const lock = !isAdmin();
  adminOnly.forEach(btn => {
    if (lock) {
      btn.classList.add("locked");
      btn.setAttribute("aria-disabled", "true");
      // Prevent click-through on non-admin
      btn.addEventListener("click", blockNonAdmin, { capture: true });
    } else {
      btn.classList.remove("locked");
      btn.removeAttribute("aria-disabled");
      btn.removeEventListener("click", blockNonAdmin, { capture: true });
    }
  });
}

function blockNonAdmin(e) {
  if (!isAdmin()) {
    e.stopPropagation();
    e.preventDefault();
    // Optional toast/alert
    console.warn("Blocked: Admin only");
  }
}

// Call once on load (in case role already stored)
document.addEventListener("DOMContentLoaded", applyRoleLock);

function isAdmin() {
  return localStorage.getItem('role') === 'admin';
}

function blockNonAdmin(e) {
  if (!isAdmin()) {
    e.stopPropagation();
    e.preventDefault();
    console.warn('Blocked: Admin only');
  }
}

// --- [NEW] Check if the current user is system admin ('ADMIN') and not a standard employee ID ---
function isSystemAdmin() {
  const empId = (localStorage.getItem('employeeId') || '').trim().toUpperCase();
  return empId === 'ADMIN';
}

// --- [COMMENTED OUT] Old last applyRoleLock function ---
/*
function applyRoleLock() {
  const adminOnly = document.querySelectorAll('.admin-only');
  const lock = !isAdmin();
  adminOnly.forEach(btn => {
    if (lock) {
      btn.classList.add('locked');
      btn.setAttribute('aria-disabled', 'true');
      btn.addEventListener('click', blockNonAdmin, { capture: true });
    } else {
      btn.classList.remove('locked');
      btn.removeAttribute('aria-disabled');
      btn.removeEventListener('click', blockNonAdmin, { capture: true });
    }
  });
}
*/

// --- [NEW] Updated applyRoleLock to hide/show machine rename controls ---
function applyRoleLock() {
  const adminOnly = document.querySelectorAll('.admin-only');
  const lock = !isAdmin();
  adminOnly.forEach(btn => {
    if (lock) {
      btn.classList.add('locked');
      btn.setAttribute('aria-disabled', 'true');
      btn.addEventListener('click', blockNonAdmin, { capture: true });
    } else {
      btn.classList.remove('locked');
      btn.removeAttribute('aria-disabled');
      btn.removeEventListener('click', blockNonAdmin, { capture: true });
    }
  });

  // Dynamically hide/show the machine name edit section.
  // We only show it for system admin ('ADMIN' login).
  const machineNameSec = document.querySelector('.machine-name-section');
  if (machineNameSec) {
    if (isSystemAdmin()) {
      machineNameSec.style.display = 'block';
    } else {
      machineNameSec.style.display = 'none';
    }
  }
}

document.addEventListener('DOMContentLoaded', applyRoleLock);


// ============================================================================
// ABOUT PAGE FUNCTIONS
// ============================================================================
function initAboutPage() {
  console.log('Initializing About Page');
  loadSystemInfo();
}

function loadSystemInfo() {
  fetch('/api/system_info')
    .then(res => res.json())
    .then(data => {
      if (data.status === 'success') {
        // Reader #1: Cassette
        const r1 = data.cassette_reader || {};
        const r1Info = document.getElementById('r1-info');
        if (r1Info) r1Info.textContent = `${r1.model || 'HID OMNIKEY 5127CK Mini'} (${r1.port || '/dev/ttyUSB2'} | ${r1.baudrate || '9600'})`;
        const r1Status = document.getElementById('r1-status');
        if (r1Status) r1Status.textContent = r1.connected ? 'Connected' : 'Disconnected';

        // Reader #2: FPC
        const r2 = data.fpc_reader || {};
        const r2Info = document.getElementById('r2-info');
        if (r2Info) r2Info.textContent = `${r2.model || 'YRM100 UHF RFID Reader'} (${r2.port || '/dev/ttyUSB1'} | ${r2.baudrate || '115200'})`;
        const r2Status = document.getElementById('r2-status');
        if (r2Status) r2Status.textContent = r2.connected ? 'Connected' : 'Disconnected';

        // Reader #3: Header
        const r3 = data.header_reader || {};
        const r3Info = document.getElementById('r3-info');
        if (r3Info) r3Info.textContent = `${r3.model || 'YRM100 UHF RFID Reader'} (${r3.port || '/dev/ttyUSB0'} | ${r3.baudrate || '115200'})`;
        const r3Status = document.getElementById('r3-status');
        if (r3Status) r3Status.textContent = r3.connected ? 'Connected' : 'Disconnected';

        // Single fallback elements
        const modelEl = document.getElementById('model'); if (modelEl) modelEl.textContent = data.model;
        const portEl = document.getElementById('port'); if (portEl) portEl.textContent = data.port;
        const baudEl = document.getElementById('baud'); if (baudEl) baudEl.textContent = data.baudrate;
        const statusEl = document.getElementById('status'); if (statusEl) statusEl.textContent = data.connected ? 'Connected' : 'Disconnected';

        const uptimeEl = document.getElementById('uptime'); if (uptimeEl) uptimeEl.textContent = data.uptime;
        const dbEl = document.getElementById('database'); if (dbEl) dbEl.textContent = data.database;
        const pyverEl = document.getElementById('pyver'); if (pyverEl) pyverEl.textContent = data.python;
        const flaskverEl = document.getElementById('flaskver'); if (flaskverEl) flaskverEl.textContent = data.flask;
        const osEl = document.getElementById('os'); if (osEl) osEl.textContent = data.os;
        const ipEl = document.getElementById('ip'); if (ipEl) ipEl.textContent = data.ip;
        const proberIpEl = document.getElementById('prober_ip'); if (proberIpEl) proberIpEl.textContent = data.prober_ip || '-';
        const logCountEl = document.getElementById('log_count'); if (logCountEl) logCountEl.textContent = data.log_count;
        const dbStatusEl = document.getElementById('db_status'); if (dbStatusEl) dbStatusEl.textContent = data.db_status;
        const lastBackupEl = document.getElementById('last_backup'); if (lastBackupEl) lastBackupEl.textContent = data.last_backup;
      }
    })
    .catch(err => console.error('Failed to load system info:', err));
}
// ============================================================================
// --- PM warning helpers ---
// ============================================================================
const TD_LIMIT = 60000;           // hard limit for PM
const TD_PREWARN_PCT = 0.90;      // prewarn at 90%
const TD_PREWARN_MIN = Math.floor(TD_LIMIT * TD_PREWARN_PCT);
let __lastPmWarnKey = null;
let __pmWarnKey = null;        // identifies the current warning tag
let __pmDetailText = '';       // last detail text used in the modal
let __pmReshowTimer = null;    // timer handle for 10s re-pop
// --- PM Prewarning state ---
let __pmPreKey = null;            // identifies the current prewarning tag
let __pmPreTimer = null;          // re-pop timer for prewarning (optional)

function showPmPrewarning(detailText) {
  if (getActivePageId() !== 'home') return;
  const modal = document.getElementById('pm-prewarn');
  const detail = document.getElementById('pm-prewarn-detail');
  if (detail) detail.textContent = detailText || '';
  if (navigator.vibrate) navigator.vibrate(40);  // softer buzz than full warning
  if (modal) modal.style.display = 'flex';
}

function hidePmPrewarning() {
  const modal = document.getElementById('pm-prewarn');
  if (modal) modal.style.display = 'none';
  // NOTE: unlike the big warning, we do NOT change box color/state for prewarning
}


function clearPmPrewarningState() {
  hidePmPrewarning();
  __pmPreKey = null;
  if (__pmPreTimer) {
    clearTimeout(__pmPreTimer);
    __pmPreTimer = null;
  }
}

function _updateInfoBoxBadge(boxEl, type, text) {
  if (!boxEl) boxEl = document.getElementById('info-box');
  if (!boxEl) return null;

  let badge = boxEl.querySelector('.warning-badge');

  // If type is 'none' or no text, remove badge
  if (!type || type === 'none' || !text) {
    if (badge) badge.remove();
    return null;
  }

  if (!badge) {
    badge = document.createElement('div');
    boxEl.style.position = boxEl.style.position || 'relative';
    boxEl.appendChild(badge);
  }

  // Set type classes (badge-danger, badge-warning, badge-success)
  badge.className = `warning-badge badge-${type}`;

  // Choose icon
  let icon = '⚠️';
  if (type === 'danger' && text.includes('MISMATCH')) icon = '❌';
  else if (type === 'success') icon = '✓';

  badge.innerHTML = `${icon} ${text}`;
  badge.title = type === 'success' ? 'Tag Pair Valid' : 'Click to view warning details';

  // Click badge to reopen pop-up modal
  badge.onclick = (e) => {
    e.stopPropagation();
    if (type === 'danger' || type === 'warning') {
      const isMis = text && text.includes('MISMATCH');
      const isNF = text && text.includes('NOT FOUND');
      showPmWarning(window.__pmDetailText || `${text} occurred. Please check configuration.`, {
        type: isNF ? 'not_found' : (isMis ? 'mismatch' : 'touchdown'),
        force: true
      });
    }
  };

  return badge;
}

function _ensureWarningBadge(boxEl, text) {
  return _updateInfoBoxBadge(boxEl, 'danger', text || 'PM Required');
}

// optional: short attention beep using Web Audio (no external files)
function _beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'square';
    osc.frequency.value = 880;        // pitch
    gain.gain.setValueAtTime(0.0001, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.05, ctx.currentTime + 0.01);
    osc.connect(gain).connect(ctx.destination);
    osc.start();
    setTimeout(() => {
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.05);
      setTimeout(() => { osc.stop(); ctx.close(); }, 80);
    }, 140);
  } catch (_) { }
}

function showPmWarning(detailText, options = {}) {
  const modal = document.getElementById('pm-warning');
  const headerEl = document.getElementById('pm-warning-header-title');
  const iconEl = document.getElementById('pm-warning-icon');
  const titleEl = document.getElementById('pm-warning-title');
  const textEl = document.getElementById('pm-warning-text');
  const detailEl = document.getElementById('pm-warning-detail');

  // Determine alert type: 'not_found' vs 'mismatch' vs 'touchdown'
  const isNotFound = (options.type === 'not_found');
  const isMismatch = (options.type === 'mismatch') || (!isNotFound && options.type !== 'touchdown' && detailText && (detailText.includes('MISMATCH') || detailText.includes('ไม่ตรงคู่')));

  if (isNotFound) {
    if (headerEl) headerEl.textContent = 'Data Not Found Alert';
    if (iconEl) iconEl.textContent = '🔍';
    if (titleEl) titleEl.textContent = 'Tag Not Registered in Database';
    if (textEl) {
      if (options.tagType === 'cassette' || (detailText && detailText.includes('Cassette'))) {
        textEl.innerHTML = `
          <p>The scanned Cassette is <strong>NOT registered in the database</strong>.</p>
          <p>Please perform Data Mapping from Smart Store or register the tag before proceeding.</p>
        `;
      } else {
        textEl.innerHTML = `
          <p>The scanned FPC or Header is <strong>NOT registered in the database</strong>.</p>
          <p>Please perform Data Mapping from Smart Store or register the card before starting a new lot.</p>
        `;
      }
    }
  } else if (isMismatch) {
    if (headerEl) headerEl.textContent = 'Mismatch Alert';
    if (iconEl) iconEl.textContent = '❌';
    if (titleEl) titleEl.textContent = 'FPC & Header Mismatch';
    if (textEl) {
      textEl.innerHTML = `
        <p>The scanned FPC and Header are <strong>NOT matching together</strong>.</p>
        <p>Please check the pairing or replace with the correct card before starting a new lot.</p>
      `;
    }
  } else {
    if (headerEl) headerEl.textContent = 'Maintenance Required';
    if (iconEl) iconEl.textContent = '⚠️';
    if (titleEl) titleEl.textContent = 'Touchdown Limit Exceeded';
    if (textEl) {
      textEl.innerHTML = `
        <p>PM is <strong>REQUIRED</strong> before starting a new lot.</p>
        <p>Please remove FPC and Header from Prober Machine.</p>
      `;
    }
  }

  if (detailEl) detailEl.textContent = detailText || '';

  // Keep the home info box in warning state
  const box = document.getElementById('info-box');
  if (box) {
    box.classList.add('warning-active');
    box.classList.remove('tag-active');        // never green while warning
    const badgeText = isNotFound ? 'NOT FOUND' : (isMismatch ? 'MISMATCH' : 'TOUCHDOWN EXCEEDED');
    _updateInfoBoxBadge(box, 'danger', badgeText);
  }

  if (navigator.vibrate) navigator.vibrate([60, 40, 60]);
  _beep();

  window.__pmModalOpen = true;

  // ONLY show modal if currently on Home page or forced (e.g. badge clicked)
  if (getActivePageId() === 'home' || options.force) {
    if (modal) modal.style.display = 'flex';
  } else {
    if (modal) modal.style.display = 'none';
  }
}

function hidePmWarning() {
  window.__pmModalOpen = false;
  const modal = document.getElementById('pm-warning');
  if (modal) modal.style.display = 'none';

  // IMPORTANT: Keep warning border on the box so the operator sees the warning state
  const box = document.getElementById('info-box');
  if (box) {
    box.classList.add('warning-active');
  }
}

function schedulePmReshow() {
  // Disabled auto-reshow to prevent annoying flashing
  return;
}


function maybeWarnOnTouchdown(rfidData) {
  const td = Number(rfidData?.touchdown ?? 0);
  const box = document.getElementById('info-box');
  if (!Number.isFinite(td)) {
    clearPmPrewarningState();
    clearPmWarningState();
    return;
  }

  // Build a unique key per read (same pattern as your big warning)
  const key = [
    rfidData?.fpc_id || '',
    rfidData?.header_name || rfidData?.header_id || ''
  ].join('|');

  // If hard limit reached/exceeded → use the existing BIG warning flow
  if (td >= TD_LIMIT) {
    // clear any prewarning UI/state
    clearPmPrewarningState();

    // existing big-warning message
    const msg = `FPC: ${rfidData?.fpc_id || '-'}  |  Header: ${rfidData?.header_name || rfidData?.header_id || '-'}  |  Touchdown: ${td}`;
    if (key && key !== __pmWarnKey) {
      __pmWarnKey = key;
      __pmDetailText = msg;
      showPmWarning(msg, { type: 'touchdown' });
    } else {
      if (box) {
        box.classList.add('warning-active');
        box.classList.remove('tag-active');
      }
    }
    if (box) _updateInfoBoxBadge(box, 'danger', 'TOUCHDOWN EXCEEDED');
    return;
  }

  // Else if within 90%-99.99% → show SMALL prewarning popup
  if (td >= TD_PREWARN_MIN) {
    const left = Math.max(TD_LIMIT - td, 0);
    const msg = `Touchdown nearing limit: ${left.toLocaleString()} remaining.`;

    // If new read, show now
    if (key && key !== __pmPreKey) {
      __pmPreKey = key;
      const modal = document.getElementById('pm-prewarn');
      if (modal) modal.dataset.lastDetail = msg;
      showPmPrewarning(msg);
    }
    if (box) _updateInfoBoxBadge(box, 'warning', 'TD NEARING LIMIT');
    // also make sure BIG warning state is cleared in case we just dipped below
    clearPmWarningState();
    return;
  }

  // Below prewarning → clear both states
  clearPmPrewarningState();
  clearPmWarningState();
}

function clearPmWarningState() {
  // ONLY auto-close modal if user already dismissed it (__pmModalOpen == false)
  if (!window.__pmModalOpen) {
    const modal = document.getElementById('pm-warning');
    if (modal) modal.style.display = 'none';
  }

  // remove yellow state + badge only if user dismissed modal
  const box = document.getElementById('info-box');
  if (box && !window.__pmModalOpen) {
    box.classList.remove('warning-active', 'tag-active');
    _updateInfoBoxBadge(box, 'none');
  }

  // reset keys & timers
  if (!window.__pmModalOpen) {
    __pmWarnKey = null;
    __pmDetailText = '';
  }
}

async function updateAgvStatusFromMap() {
  try {
    const res = await fetch('/data', { cache: 'no-store' });
    if (!res.ok) throw new Error('map fetch failed');
    const js = await res.json();
    const robots = Array.isArray(js.robots) ? js.robots : [];


    ['agv1', 'agv2', 'agv3'].forEach((id, idx) => {
      const r = robots[idx];
      if (!r) {
        setReaderBoxState(id, false, '', false);
        return;
      }
      if (r.ok) {
        setReaderBoxState(id, true, '', true);
      } else {
        setReaderBoxState(id, false, '', false);
      }
    });
  } catch (e) {
    setReaderBoxState('agv1', false, '', false);
    setReaderBoxState('agv2', false, '', false);
    setReaderBoxState('agv3', false, '', false);
  }
}

// --- Legend chip colors
const LEGEND_OK = '#22c55e'; // green
const LEGEND_FAIL = '#ef4444'; // red

function findLegendChipByName(name) {
  const legend = document.getElementById('legend');
  if (!legend) return null;
  return Array.from(legend.querySelectorAll('.chip'))
    .find(ch => ch.textContent.trim().includes(name));
}

function paintLegendChip(name, ok) {
  const chip = findLegendChipByName(name);
  if (!chip) return;
  const dot = chip.querySelector('.dot') || chip;
  dot.style.background = ok ? LEGEND_OK : LEGEND_FAIL;
  chip.classList.toggle('chip-ok', !!ok);
  chip.classList.toggle('chip-fail', !ok);
}

async function updateLegendFromMap() {
  try {
    const r = await fetch('/data', { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const js = await r.json();
    (js.robots || []).forEach(robot => {
      paintLegendChip(robot.name, !!robot.ok);
    });
  } catch {
    // If the /data call itself fails, mark all known robots as FAIL
    (window.ROBOTS_CFG || []).forEach(r => paintLegendChip(r.name, false));
  }
}

function updatePmPrewarning(rfidData) {
  const label = document.getElementById('td-prewarn');
  if (!label) return;

  const tdRaw = rfidData?.touchdown;
  const td = Number(tdRaw ?? NaN);

  // Hide if value missing or at/over limit (the big warning handles >= limit)
  if (!Number.isFinite(td) || td >= TD_LIMIT) {
    label.style.display = 'none';
    label.textContent = '';
    label.classList.remove('hot');
    return;
  }

  // Show only when within the prewarning band [90%, 100%)
  if (td >= TD_PREWARN_MIN) {
    const left = Math.max(TD_LIMIT - td, 0);
    label.textContent = `${left.toLocaleString()} left to PM`;
    label.style.display = 'inline-flex';

    // Optional: highlight if extremely close to limit (e.g., ≥98%)
    const pct = td / TD_LIMIT;
    if (pct >= 0.98) {
      label.classList.add('hot');
    } else {
      label.classList.remove('hot');
    }
  } else {
    label.style.display = 'none';
    label.textContent = '';
    label.classList.remove('hot');
  }
}


// ============================================================================
// EVENT HANDLERS & INITIALIZATION
// ============================================================================
document.addEventListener('DOMContentLoaded', function () {

  // --- Prevent scrolling ---
  document.body.style.overflow = 'hidden';
  document.documentElement.style.overflow = 'hidden';

  // --- Prevent arrow/space scrolling ---
  window.addEventListener('keydown', function (e) {
    if (['Space', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(e.code)) {
      e.preventDefault();
    }
  }, false);

  // --- Page init ---
  const homeBtn = document.querySelector(".nav-button[onclick*=\"switchPage('home'\"]");
  switchPage('home', homeBtn);  // ← Add this line instead

  // --- Clock (every second) ---
  setInterval(updateCurrentDateTime, 1000);
  updateCurrentDateTime();

  // --- Auto-refresh logs every 5s ---
  // (requires refreshLogsTick() and startAutoRefresh() to be defined earlier)
  refreshLogsTick();
  startAutoRefresh();

  // --- Inactivity → auto-return to Home (1 minute) ---
  // (requires resetInactivityFromEvent() to be defined earlier)
  ['mousemove', 'mousedown', 'keydown', 'touchstart', 'wheel', 'scroll'].forEach(ev => {
    window.addEventListener(ev, resetInactivityFromEvent, { passive: true });
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      clearTimeout(inactivityHomeTimer);
    } else {
      resetInactivityFromEvent();
    }
  });
});


// Enter key functionality
document.addEventListener('keypress', function (e) {
  if (e.key === 'Enter') {
    const currentPage = document.querySelector('.page-content.active').id;

    if (currentPage === 'log') {
      searchLogs();
    } else if (currentPage === 'setting' && document.getElementById('loginScreen').style.display !== 'none') {
      submitLogin();
    }
  }
});

// Cleanup when page unloads
window.addEventListener('beforeunload', function () {
  stopRealTimeUpdates();
});


// Virtual keyboard removed - using system/hardware keyboard on i.MX8
window.VirtualKeyboard = { showFor: () => { }, hide: () => { } };

// ============================================================================
// CLEAR STATUS BUTTON HANDLER (เคลียร์ค่า & กลับสู่สถานะสีเทา)
// ============================================================================
function initClearStatusButton() {
  const btn = document.getElementById('btn-clear-status');
  if (!btn || btn.__bound) return;
  btn.__bound = true;

  btn.addEventListener('click', async (e) => {
    e.preventDefault();
    console.log('[CLEAR STATUS] Operator clicked clear button -> Resetting to gray state');

    // 1. Tell backend to clear cassette state & simulation to IDLE
    try {
      await fetch('/api/cassette/clear', { method: 'POST' });
    } catch (err) {
      try { await fetch('/api/simulate_cassette?clear=true'); } catch (e) { }
    }

    // 2. Reset frontend cassette & pair flags
    window.__isCassettePresent = false;
    window.__isCassetteNotFound = false;
    window.__cassetteWarnKey = null;
    window.__lastCassetteKey = null;
    window.__pairWarnKey = null;
    window.__pairModalDismissed = true;
    window.__missingTagCycles = 3;

    // 3. Clear all display fields immediately
    setMany(['batch-id-display'], '');
    setMany(['lot-id-display'], '');
    setMany(['fpc-display'], '');
    setMany(['header-display'], '');
    setMany(['touchdown-value'], '');
    setMany(['PM-display'], '');
    setMany(['timer-display'], '');
    setMany(['comment-display'], '');
    if (typeof clearCassetteDisplayFields === 'function') {
      clearCassetteDisplayFields();
    }

    // 4. Return info-box to gray state (remove warning-active and tag-active)
    const box = document.getElementById('info-box');
    if (box) {
      box.classList.remove('warning-active');
      box.classList.remove('tag-active');
      _updateInfoBoxBadge(box, 'none');
    }

    // 5. Hide the clear button itself
    const clearFooter = document.getElementById('info-box-footer');
    if (clearFooter) {
      clearFooter.style.display = 'none';
    }

    // 6. Close PM warning modal if open
    hidePmWarning();

    // 7. Immediate sync with backend
    updateHomeFromLocal();
  });
}

// Initialize on script load and DOMContentLoaded
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initClearStatusButton);
} else {
  initClearStatusButton();
}

// --- Patch #1: run once on load ---
document.addEventListener('DOMContentLoaded', () => {
  initClearStatusButton();
  updateHomeFromLocal();
});

// --- Patch #3: refresh immediately when tab gains focus ---
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    window.__pmWarnKey = null;    // let next mismatch show again
    updateHomeFromLocal();
  }
});


document.addEventListener('app:stateChanged', () => {
  if (typeof refreshPoiDerived === 'function') {
    refreshPoiDerived();
  }
  if (typeof redrawMap === 'function') {
    redrawMap();
  }
});


// ============================================================================
// PMI BATCH-WAFER FORMATTER HELPER
// ============================================================================
function formatPmiBatchWafer(batch, wafer) {
  const b = (batch || '').trim();
  const w = (wafer || '').trim();
  if (!b && !w) return '—';
  if (!b) return w;
  if (!w) return b;
  if (b === w) return b;
  if (b.includes('-')) return b;
  if (w.includes('-')) return w;
  if (w.startsWith(b)) {
    return `${b}-${w.substring(b.length)}`;
  }
  return `${b}-${w}`;
}

// ============================================================================
// PMI FILENAME PARSER: Parses raw image filename into 7 distinct data fields
// Pattern: [Date][Time]_[Batch#]-[Wafer#,checksum]_[XY]_[Site]_[Pad]_[Status]_[ProductSetup]_[Temp].bmp
// ============================================================================
function parsePmiFilename(rawFilename) {
  if (!rawFilename || typeof rawFilename !== 'string') return null;

  let clean = rawFilename.split('?')[0];
  clean = clean.substring(clean.lastIndexOf('/') + 1);
  clean = clean.replace(/\.[^/.]+$/, ''); // strip extension
  clean = clean.replace(/^(raw_|annotated_|inspect_)+/i, '');
  clean = clean.replace(/(_mask_result|_inspect|_annotated|_raw|_result)+$/i, '');

  const isEnd = clean.toUpperCase().endsWith('_END') || clean.toUpperCase().endsWith('.END');
  if (clean.toUpperCase().endsWith('_END')) {
    clean = clean.slice(0, -4);
  } else if (clean.toUpperCase().endsWith('.END')) {
    clean = clean.slice(0, -4);
  }

  const parts = clean.split('_');
  const meta = {
    dateTime: '—',
    batch: '—',
    waferNo: '—',
    xyCoord: '—',
    site: '—',
    pad: '—',
    sitePad: '—',
    processCode: '—',
    productSetup: '—',
    temp: '—'
  };

  // Standard 8-part NXP Prober format:
  // [0:Date][1:Batch-Wafer][2:XY][3:Site][4:Pad][5:Status][6:ProductSetup][7:Temp]
  if (parts.length >= 8) {
    const p0 = parts[0];
    if (p0.length >= 14) {
      meta.dateTime = `${p0.substring(0, 4)}-${p0.substring(4, 6)}-${p0.substring(6, 8)} ${p0.substring(8, 10)}:${p0.substring(10, 12)}:${p0.substring(12, 14)}`;
    } else {
      meta.dateTime = p0;
    }

    const p1 = parts[1];
    meta.waferNo = p1;
    if (p1.includes('-')) {
      meta.batch = p1.split('-')[0];
    } else {
      const match = p1.match(/^([A-Z0-9]+?)(W[A-Z0-9]+)$/i);
      meta.batch = match ? match[1] : p1;
    }

    meta.xyCoord = parts[2];
    const p3 = parts[3];
    meta.site = p3.toUpperCase().startsWith('S') && !isNaN(p3.substring(1)) ? `Site ${p3.substring(1)}` : p3;
    const p4 = parts[4];
    meta.pad = p4.toUpperCase().startsWith('P') && !isNaN(p4.substring(1)) ? `Pad ${p4.substring(1)}` : p4;
    meta.sitePad = `${meta.site} / ${meta.pad}`;

    meta.processCode = parts[5];
    meta.productSetup = parts[6]; // E.g. TF1581DTAB-V7011, CF1561CCAA-V7011, 29D5B0FBAA-PC611

    const p7 = parts[7];
    if (!isNaN(p7) && p7.trim() !== '') {
      const v = parseFloat(p7);
      meta.temp = (p7.length === 3 || p7.length === 4) ? `${(v / 10.0).toFixed(1)}°C` : `${v}°C`;
    } else {
      meta.temp = p7;
    }

    return meta;
  }

  // Fallback for variable parts:
  for (let i = 0; i < parts.length; i++) {
    const part = parts[i];
    if (!part) continue;
    if (/^\d{14}$/.test(part)) {
      meta.dateTime = `${part.substring(0, 4)}-${part.substring(4, 6)}-${part.substring(6, 8)} ${part.substring(8, 10)}:${part.substring(10, 12)}:${part.substring(12, 14)}`;
    } else if (/^\d{8}$/.test(part) && i === 0) {
      meta.dateTime = `${part.substring(0, 4)}-${part.substring(4, 6)}-${part.substring(6, 8)}`;
    } else if (/^X-?\d+Y-?\d+$/i.test(part)) {
      meta.xyCoord = part;
    } else if (/^S\d+$/i.test(part)) {
      meta.site = `Site ${part.substring(1)}`;
    } else if (/^P\d+$/i.test(part)) {
      meta.pad = `Pad ${part.substring(1)}`;
    } else if (/^(OK|NG|PASS|FAIL|REJECT|PO|PO\d+)$/i.test(part)) {
      meta.processCode = part;
    } else if (/^\d{2,4}$/.test(part) && (i === parts.length - 1 || (i === parts.length - 2 && isEnd))) {
      const v = parseFloat(part);
      meta.temp = (part.length === 3 || part.length === 4) ? `${(v / 10.0).toFixed(1)}°C` : `${v}°C`;
    } else if (meta.batch === '—') {
      meta.waferNo = part;
      if (part.includes('-')) {
        meta.batch = part.split('-')[0];
      } else {
        const match = part.match(/^([A-Z0-9]+?)(W[A-Z0-9]+)$/i);
        meta.batch = match ? match[1] : part;
      }
    } else if (meta.productSetup === '—') {
      meta.productSetup = part;
    }
  }

  if (meta.site !== '—' || meta.pad !== '—') {
    meta.sitePad = `${meta.site} / ${meta.pad}`.trim();
  }

  return meta;
}

// ============================================================================
// PMI i.MX8 BACKEND INTEGRATION (Real-time WebSocket, Polling & Fail Navigation)
// ============================================================================
(function initPmiWebSocketClient() {
  const wsHost = (typeof window !== 'undefined' && window.location && window.location.hostname) ? window.location.hostname : '127.0.0.1';
  const wsProto = (typeof window !== 'undefined' && window.location && window.location.protocol === 'https:') ? 'wss:' : 'ws:';
  const wsPort = (typeof window !== 'undefined' && window.location && window.location.port) ? window.location.port : '8002';
  const IMX8_WS_URL = `${wsProto}//${wsHost}:${wsPort}/ws`;
  const IMX8_HTTP_BASE = `${(typeof window !== 'undefined' && window.location && window.location.protocol) ? window.location.protocol : 'http:'}//${wsHost}:${wsPort}`;
  const LOCAL_HTTP_BASE = (typeof window !== 'undefined' && window.location && window.location.origin) ? window.location.origin : `http://${wsHost}:${wsPort}`;

  let activeApiBase = IMX8_HTTP_BASE;
  let ws = null;
  let reconnectTimer = null;
  let pollTimer = null;
  let failedInspections = [];
  let currentFailIndex = -1;
  let lastInspectionKey = '';
  let isNavigatingFailures = false;

  // Cache UI elements
  const statusBar = document.getElementById('pmi-status-bar');
  const rawImg = document.getElementById('pmi-raw-img');
  const processedImg = document.getElementById('pmi-processed-img');
  const rawConstruct = document.getElementById('pmi-raw-construct');
  const processedConstruct = document.getElementById('pmi-processed-construct');
  const framesContainer = document.getElementById('pmi-frames-container');
  const filenameDisplay = document.getElementById('pmi-filename-display');
  const failNav = document.getElementById('pmi-fail-nav');
  const failCounter = document.getElementById('pmi-fail-counter');
  const prevBtn = document.getElementById('pmi-prev-btn');
  const nextBtn = document.getElementById('pmi-next-btn');

  const elDateTime = document.getElementById('pmi-field-datetime');
  const elXY = document.getElementById('pmi-field-xy');
  const elBatchWafer = document.getElementById('pmi-field-batch-wafer');
  const elSitePad = document.getElementById('pmi-field-sitepad');
  const elSetup = document.getElementById('pmi-field-setup');
  const elTemp = document.getElementById('pmi-field-temp');

  if (rawImg) {
    rawImg.onerror = () => {
      rawImg.style.display = 'none';
      if (rawConstruct) rawConstruct.style.display = 'flex';
    };
    rawImg.onload = () => {
      rawImg.style.display = 'block';
      if (rawConstruct) rawConstruct.style.display = 'none';
    };
  }

  if (processedImg) {
    processedImg.onerror = () => {
      processedImg.style.display = 'none';
      if (processedConstruct) processedConstruct.style.display = 'flex';
    };
    processedImg.onload = () => {
      processedImg.style.display = 'block';
      if (processedConstruct) processedConstruct.style.display = 'none';
    };
  }

  function getFilenameFromData(data) {
    if (!data) return '';
    if (data.image_name) return data.image_name;
    if (data.filename) return data.filename;
    const url = data.rawImageUrl || data.raw_image_url || data.imageUrl || data.annotatedImageUrl || '';
    if (url) {
      const clean = url.split('?')[0];
      return clean.substring(clean.lastIndexOf('/') + 1);
    }
    return '';
  }

  function updateMetadata(data) {
    if (!data) return;

    const imgName = getFilenameFromData(data);
    if (imgName && filenameDisplay) {
      filenameDisplay.textContent = imgName;
    }

    const parsed = imgName ? parsePmiFilename(imgName) : null;

    const dt = data.dateTime || (parsed && parsed.dateTime !== '—' ? parsed.dateTime : null) || data.datetime || data.timestamp;
    if (elDateTime && dt) elDateTime.textContent = dt;

    const xy = data.xyCoord || (parsed && parsed.xyCoord !== '—' ? parsed.xyCoord : null) || data.xy || (data.x !== undefined && data.y !== undefined ? `X${data.x}Y${data.y}` : null);
    if (elXY && xy) elXY.textContent = xy;

    const batch = data.batch || (parsed && parsed.batch !== '—' ? parsed.batch : '') || data.batch_no || '';
    const wafer = data.waferNo || (parsed && parsed.waferNo !== '—' ? parsed.waferNo : '') || data.wafer || '';
    if (elBatchWafer && (batch || wafer)) {
      elBatchWafer.textContent = formatPmiBatchWafer(batch, wafer);
    }

    const site = data.site || (parsed && parsed.site !== '—' ? parsed.site : '');
    const pad = data.pad || (parsed && parsed.pad !== '—' ? parsed.pad : '');
    if (elSitePad && (site || pad)) {
      elSitePad.textContent = (site && pad) ? `${site} / ${pad}`.trim() : (site || pad);
    }

    let setup = data.productSetup || data.setup || data.product_setup;
    if ((!setup || setup === 'PO' || setup === 'OK' || setup === 'NG' || setup === '-') && parsed && parsed.productSetup !== '—') {
      setup = parsed.productSetup;
    }
    if (elSetup && setup) elSetup.textContent = setup;

    let temp = data.temp || data.temperature;
    if ((!temp || temp === '-' || temp === '—') && parsed && parsed.temp !== '—') {
      temp = parsed.temp;
    }
    if (elTemp && temp !== undefined && temp !== null && temp !== '-') {
      elTemp.textContent = String(temp).includes('°C') ? temp : `${temp}`;
    }
  }

  function resolveImageUrl(url) {
    if (!url) return '';
    if (url.startsWith('http://') || url.startsWith('https://') || url.startsWith('data:') || url.startsWith('blob:')) {
      return url;
    }
    const cleanPath = url.startsWith('/') ? url : `/${url}`;
    const base = activeApiBase || IMX8_HTTP_BASE;
    return `${base}${cleanPath}`;
  }

  let currentRenderToken = 0;

  function renderInspection(data, isLive = true) {
    if (!data) return;

    const renderToken = ++currentRenderToken;

    // Direct resolution of RAW image and Annotated image from backend
    const rawUrl = resolveImageUrl(data.rawImageUrl || data.raw_image_url || data.raw_url || data.rawImage);
    const procUrl = resolveImageUrl(data.annotatedImageUrl || data.annotated_image_url || data.imageUrl || data.annotated_url || rawUrl);

    const decision = (data.decision || data.ai_decision || (data.is_pass ? 'PASS' : (data.is_pass === false ? 'FAIL' : '')) || '').toUpperCase();
    const isPass = decision === 'PASS' || decision === 'PASSED';
    const isFail = decision === 'FAIL' || decision === 'FAILED';

    function commitRender() {
      if (renderToken !== currentRenderToken) return;

      requestAnimationFrame(() => {
        // 1. Commit both images simultaneously
        if (rawImg && rawUrl) {
          rawImg.src = rawUrl;
          rawImg.style.display = 'block';
          if (rawConstruct) rawConstruct.style.display = 'none';
        }
        if (processedImg && procUrl) {
          processedImg.src = procUrl;
          processedImg.style.display = 'block';
          if (processedConstruct) processedConstruct.style.display = 'none';
        }

        // 2. Commit Status Bar in the exact same render frame
        if (statusBar) {
          statusBar.classList.remove('waiting', 'passed', 'failed');
          if (isPass) {
            statusBar.classList.add('passed');
            statusBar.textContent = isLive ? 'PASS' : `FAIL (${currentFailIndex + 1}/${failedInspections.length})`;
          } else if (isFail) {
            statusBar.classList.add('failed');
            statusBar.textContent = isLive ? 'FAIL' : `FAIL (${currentFailIndex + 1}/${failedInspections.length})`;
          } else {
            statusBar.classList.add('waiting');
            statusBar.textContent = decision || 'INSPECTING';
          }
        }

        if (framesContainer) {
          framesContainer.classList.remove('passed', 'failed');
          if (isPass) framesContainer.classList.add('passed');
          else if (isFail) framesContainer.classList.add('failed');
        }

        // 3. Commit Metadata in the exact same frame
        updateMetadata(data);
      });
    }

    // Preload images into memory so that the image, banner and metadata are committed in 1 single frame
    const urlsToPreload = [procUrl, rawUrl].filter(u => u && !u.startsWith('data:'));
    if (urlsToPreload.length === 0) {
      commitRender();
      return;
    }

    let remaining = urlsToPreload.length;
    let fallbackTimer = setTimeout(() => {
      commitRender();
    }, 250); // safety fallback in case of network stall

    urlsToPreload.forEach(url => {
      const preloadImg = new Image();
      preloadImg.onload = preloadImg.onerror = () => {
        remaining--;
        if (remaining <= 0) {
          clearTimeout(fallbackTimer);
          commitRender();
        }
      };
      preloadImg.src = url;
    });
  }

  function showFailAtIndex(idx) {
    if (!failedInspections || failedInspections.length === 0) return;
    if (idx < 0) idx = 0;
    if (idx >= failedInspections.length) idx = failedInspections.length - 1;
    isNavigatingFailures = true;
    currentFailIndex = idx;
    const item = failedInspections[currentFailIndex];
    renderInspection(item, false);

    if (failNav) failNav.style.display = 'flex';
    if (failCounter) failCounter.textContent = `FAIL ${currentFailIndex + 1}/${failedInspections.length}`;
    if (prevBtn) prevBtn.disabled = (currentFailIndex === 0);
    if (nextBtn) nextBtn.disabled = (currentFailIndex === failedInspections.length - 1);
  }

  function handleBatchComplete() {
    if (failedInspections.length > 0) {
      if (failNav) failNav.style.display = 'flex';
      if (currentFailIndex < 0 || currentFailIndex >= failedInspections.length) {
        showFailAtIndex(0);
      } else {
        showFailAtIndex(currentFailIndex);
      }
    } else {
      isNavigatingFailures = false;
      if (failNav) failNav.style.display = 'none';
      if (statusBar) {
        statusBar.classList.remove('waiting', 'failed');
        statusBar.classList.add('passed');
        statusBar.textContent = 'PASS';
      }
      if (framesContainer) {
        framesContainer.classList.remove('failed');
        framesContainer.classList.add('passed');
      }
    }
  }

  // REST API Polling Fallback & Initial State Loader
  async function fetchPmiState() {
    try {
      let inspRes = null;
      let batchRes = null;

      // 1. Try Primary Port 8002 (Dedicated Vision/AI Backend)
      try {
        const primaryCalls = await Promise.all([
          fetch(`${IMX8_HTTP_BASE}/api/latest-inspection`, { cache: 'no-store' }).catch(() => fetch(`${IMX8_HTTP_BASE}/api/v1/latest-inspection`, { cache: 'no-store' })),
          fetch(`${IMX8_HTTP_BASE}/api/batch-summary`, { cache: 'no-store' }).catch(() => fetch(`${IMX8_HTTP_BASE}/api/v1/batch-summary`, { cache: 'no-store' }))
        ]);
        if (primaryCalls[0] && primaryCalls[0].ok) {
          inspRes = primaryCalls[0];
          batchRes = primaryCalls[1];
          activeApiBase = IMX8_HTTP_BASE;
        }
      } catch (e) {
        // Port 8002 not responding, fallback to local host
      }

      // 2. Fallback to Local Flask Service (Port 8002 / UIIU Simulation)
      if (!inspRes || !inspRes.ok) {
        try {
          const fallbackCalls = await Promise.all([
            fetch(`${LOCAL_HTTP_BASE}/api/latest-inspection`, { cache: 'no-store' }).catch(() => null),
            fetch(`${LOCAL_HTTP_BASE}/api/batch-summary`, { cache: 'no-store' }).catch(() => null)
          ]);
          if (fallbackCalls[0] && fallbackCalls[0].ok) {
            inspRes = fallbackCalls[0];
            batchRes = fallbackCalls[1];
            activeApiBase = LOCAL_HTTP_BASE;
          }
        } catch (e) {
          // Local fallback not available
        }
      }

      if (batchRes && batchRes.ok) {
        const batchData = await batchRes.json();
        if (batchData) {
          if (Array.isArray(batchData.failedRecords) && batchData.failedRecords.length > 0) {
            failedInspections = batchData.failedRecords;
          }
          if (failedInspections.length > 0) {
            if (failNav) failNav.style.display = 'flex';
            if (currentFailIndex < 0) currentFailIndex = 0;
            if (failCounter) failCounter.textContent = `FAIL ${currentFailIndex + 1}/${failedInspections.length}`;
            if (prevBtn) prevBtn.disabled = (currentFailIndex === 0);
            if (nextBtn) nextBtn.disabled = (currentFailIndex === failedInspections.length - 1);

            if (batchData.isBatchComplete) {
              handleBatchComplete();
              return;
            }
          } else {
            if (!isNavigatingFailures && failNav) {
              failNav.style.display = 'none';
            }
          }
        }
      }

      if (inspRes && inspRes.ok) {
        const inspData = await inspRes.json();
        if (inspData && !isNavigatingFailures && (inspData.rawImageUrl || inspData.imageUrl)) {
          const key = (inspData.db_id || '') + '_' + (inspData.rawImageUrl || inspData.imageUrl || inspData.image_name || inspData.timestamp || '');
          if (key !== lastInspectionKey) {
            lastInspectionKey = key;
            renderInspection(inspData, true);
          }
        }
      }
    } catch (err) {
      // Quiet fail during regular polling
    }
  }

  function startPolling() {
    if (pollTimer) clearInterval(pollTimer);
    fetchPmiState();
    pollTimer = setInterval(fetchPmiState, 1000);
  }

  function connect() {
    startPolling();

    if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;

    try {
      ws = new WebSocket(IMX8_WS_URL);
    } catch (e) {
      scheduleReconnect();
      return;
    }

    ws.onopen = () => {
      console.log('[PMI i.MX8] Connected to WebSocket:', IMX8_WS_URL);
      fetchPmiState();
    };

    ws.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data);
        if (payload.event === 'NEW_INSPECTION' && payload.data) {
          const item = payload.data;
          const dec = (item.decision || item.ai_decision || (item.is_pass === false ? 'FAIL' : (item.is_pass ? 'PASS' : '')) || '').toUpperCase();
          if (dec === 'FAIL' || dec === 'FAILED') {
            const itemFname = getFilenameFromData(item);
            const exists = failedInspections.some(f => getFilenameFromData(f) === itemFname);
            if (!exists) {
              failedInspections.push(item);
            }
            if (failNav) failNav.style.display = 'flex';
          }

          const fname = getFilenameFromData(item).toUpperCase();
          const isEnd = fname.includes('_END') || fname.includes('.END') || item.is_end || item.is_end_signal || item.is_batch_end;

          if (isEnd) {
            handleBatchComplete();
          } else if (!isNavigatingFailures) {
            renderInspection(item, true);
          }
        } else if (payload.event === 'BATCH_COMPLETE' || payload.event === 'BATCH_FINISHED') {
          if (payload.data && Array.isArray(payload.data.failedRecords) && payload.data.failedRecords.length > 0) {
            failedInspections = payload.data.failedRecords;
          }
          handleBatchComplete();
        } else if (payload.event === 'BATCH_START' || payload.event === 'NEW_BATCH') {
          failedInspections = [];
          currentFailIndex = -1;
          isNavigatingFailures = false;
          if (failNav) failNav.style.display = 'none';
          if (statusBar) {
            statusBar.classList.remove('passed', 'failed');
            statusBar.classList.add('waiting');
            statusBar.textContent = 'INSPECTING';
          }
        }
      } catch (err) {
        console.error('[PMI i.MX8] Error processing WebSocket message:', err);
      }
    };

    ws.onerror = () => {
      // Handled by onclose
    };

    ws.onclose = () => {
      scheduleReconnect();
    };
  }

  function scheduleReconnect() {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, 2500);
  }

  // Prev / Next Button listeners
  if (prevBtn) {
    prevBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (failedInspections.length > 0) {
        if (currentFailIndex > 0) {
          showFailAtIndex(currentFailIndex - 1);
        } else {
          showFailAtIndex(0);
        }
      }
    });
  }

  if (nextBtn) {
    nextBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (failedInspections.length > 0) {
        if (currentFailIndex < failedInspections.length - 1) {
          showFailAtIndex(currentFailIndex + 1);
        } else if (currentFailIndex === -1) {
          showFailAtIndex(0);
        }
      }
    });
  }

  // Click on Status bar to start or view failure review
  if (statusBar) {
    statusBar.style.cursor = 'pointer';
    statusBar.addEventListener('click', () => {
      if (failedInspections.length > 0) {
        if (currentFailIndex < 0 || currentFailIndex >= failedInspections.length) {
          showFailAtIndex(0);
        } else {
          showFailAtIndex(currentFailIndex);
        }
      }
    });
  }

  // Keyboard navigation for Fail review (ArrowLeft / ArrowRight)
  document.addEventListener('keydown', (e) => {
    if (failedInspections.length === 0) return;
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target?.tagName)) return;

    if (e.key === 'ArrowLeft') {
      if (currentFailIndex > 0) {
        e.preventDefault();
        showFailAtIndex(currentFailIndex - 1);
      }
    } else if (e.key === 'ArrowRight') {
      if (currentFailIndex < failedInspections.length - 1) {
        e.preventDefault();
        showFailAtIndex(currentFailIndex + 1);
      } else if (currentFailIndex === -1) {
        e.preventDefault();
        showFailAtIndex(0);
      }
    }
  });

  // Start WebSocket and polling when DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', connect);
  } else {
    connect();
  }
})();

// ============================================================================
// SENSOR SIMULATION CONTROL (ระบบควบคุมเซนเซอร์จำลอง FPC ผ่าน UI และคีย์บอร์ด)
// ============================================================================
/**
 * ฟังก์ชันสำหรับสลับสถานะ Sensor (ON <-> OFF)
 * 1. เรียกใช้งานเมื่อคลิกที่ป้าย 'OFF / ON' บนการ์ด RFID-2 (FPC)
 * 2. เรียกใช้งานเมื่อกดปุ่ม 't' หรือ 'T' บนคีย์บอร์ด
 * 
 * กลไกการทำงาน:
 * - เมื่อ Sensor เป็น 'ON' : หัวอ่าน FPC (COM6) จะเปิดรอบสแกน 8 วินาทีเพื่ออ่านแท็ก
 * - เมื่อ Sensor เป็น 'OFF': เสมือนดึงแผ่น FPC ออก ข้อมูลในช่อง FPC จะถูกเคลียร์กลับเป็นค่าว่างทันที
 */
window.toggleSensorSimulator = function () {
  fetch('/api/toggle_sensor', { method: 'POST' })
    .then(r => r.json())
    .then(res => {
      console.log('[SENSOR SIMULATION] Sensor state toggled:', res);
      const sensorEl = document.getElementById('rfid-fpc-sensor-status');
      if (sensorEl && typeof res.sensor_active === 'boolean') {
        const val = res.sensor_active ? 'ON' : 'OFF';
        sensorEl.textContent = val;
        sensorEl.className = 'sensor-value ' + val.toLowerCase();
      }
    })
    .catch(err => console.error('[SENSOR SIMULATION] Failed to toggle:', err));
};

// ดักฟังสัญญาณปุ่มกด 't' / 'T' บนคีย์บอร์ดเพื่อสั่งงานฟังก์ชัน toggleSensorSimulator
document.addEventListener('keydown', (e) => {
  if (e.key === 't' || e.key === 'T') {
    // ป้องกันการทำงานขณะที่ผู้ใช้กำลังพิมพ์ข้อความในช่อง Input / Textarea
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target?.tagName)) return;
    if (typeof window.toggleSensorSimulator === 'function') {
      window.toggleSensorSimulator();
    }
  }
});

// ============================================================================
// KEYBOARD WEDGE AUTO-SCANNER FOR CASSETTE RFID (HID OMNIKEY 5127 CK)
// ============================================================================
let __rfidBuffer = '';
let __rfidLastKeyTime = 0;
let __rfidTimer = null;

window.addEventListener('keydown', (e) => {
  const now = Date.now();
  const diff = now - __rfidLastKeyTime;
  __rfidLastKeyTime = now;

  const activeTag = document.activeElement ? document.activeElement.tagName.toLowerCase() : '';
  const isInputFocused = (activeTag === 'input' || activeTag === 'textarea');

  // Scanner sends keystrokes rapidly (< 60ms between characters)
  if (diff > 80) {
    __rfidBuffer = '';
  }

  // Handle Enter terminator from RFID reader
  if (e.key === 'Enter') {
    const scanned = __rfidBuffer.trim();
    if (scanned.length >= 8) {
      console.log('[CASSETTE RFID SCANNED]:', scanned);
      sendCassetteScan(scanned);
      __rfidBuffer = '';
      if (isInputFocused) {
        e.preventDefault();
      }
      return;
    }
    __rfidBuffer = '';
    return;
  }

  // Accumulate single alphanumeric characters
  if (e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey) {
    __rfidBuffer += e.key;

    if (__rfidTimer) clearTimeout(__rfidTimer);

    // Auto-detect 16-char hex UID (e.g. cdde6b48080104e0) even if no Enter key
    __rfidTimer = setTimeout(() => {
      const scanned = __rfidBuffer.trim();
      const converted = convertThaiKedmaneeToEn(scanned);
      if (converted.length >= 12 && /^[a-fA-F0-9]+$/.test(converted)) {
        console.log('[CASSETTE RFID AUTO-DETECTED]:', converted);
        sendCassetteScan(converted);
        __rfidBuffer = '';
      }
    }, 60);
  }
});

const THAI_TO_EN_MAP = {
  'ๆ': 'q', 'ไ': 'w', 'ำ': 'e', 'พ': 'r', 'ะ': 't', 'ั': 'y', 'ี': 'u', 'ร': 'i', 'น': 'o', 'ย': 'p', 'บ': '[', 'ล': ']',
  'ฟ': 'a', 'ห': 's', 'ก': 'd', 'ด': 'f', 'เ': 'g', '้': 'h', '่': 'j', 'า': 'k', 'ส': 'l', 'ว': ';', 'ง': '\'',
  'ผ': 'z', 'ป': 'x', 'แ': 'c', 'อ': 'v', 'ิ': 'b', 'ื': 'n', 'ท': 'm', 'ม': ',', 'ใ': '.', 'ฝ': '/',
  '๑': '@', '๒': '#', '๓': '$', '๔': '%', '๕': '&', '๖': '_', '๗': '+', '๘': '*', '๙': '(', '๐': ')',
  'ๅ': '1', '/': '2', '-': '3', 'ภ': '4', 'ถ': '5', 'ุ': '6', 'ึ': '7', 'ค': '8', 'ต': '9', 'จ': '0', 'ข': '-', 'ช': '=',
  'ฤ': 'a', 'ฺ': 'b', 'ฉ': 'c', 'ฎ': 'd', 'ฏ': 'e', 'โ': 'f'
};

function convertThaiKedmaneeToEn(str) {
  if (!str) return '';
  return str.split('').map(ch => THAI_TO_EN_MAP[ch] || ch).join('');
}

function sendCassetteScan(tagId) {
  const cleanTag = convertThaiKedmaneeToEn(tagId);
  fetch('/api/cassette/scan', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cassette_id: cleanTag })
  })
    .then(r => r.json())
    .then(res => {
      console.log('[CASSETTE SCAN RESPONSE]:', res);
      // Refresh current data immediately to show on Home
      if (typeof updateHomeFromLocal === 'function') {
        updateHomeFromLocal();
      }
    })
    .catch(err => console.error('[CASSETTE SCAN ERROR]:', err));
}