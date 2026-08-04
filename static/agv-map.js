(() => {
  // ======== Config from template (with fallbacks) ========
  const ppm       = typeof window.PPM === 'number' ? window.PPM : 20; // px/m
  const originM   = window.ORIGIN_M || { x: 0, y: 0 };
  const robotsCfg = Array.isArray(window.ROBOTS_CFG) ? window.ROBOTS_CFG : [];
  const FLOORPLAN_URL = window.FLOORPLAN_URL || '/static/map_image_new.png';
  const AGV_ICONS = window.AGV_ICONS || {
    '#00e0ff': '/static/agv_blue.png',
    '#ff6b6b': '/static/agv_red.png'
  };
const ROBOT_UI = {
  'Robot FPC no.1': { fpcId: 'agv1-fpc-display', pmId: 'agv1-pm-display' },
  'Robot FPC no.2': { fpcId: 'agv2-fpc-display', pmId: 'agv2-pm-display' },
};
  // Background draw offset so world coords align with the map image
  const BG_OX = 30; // px
  const BG_OY = 0;  // px
  const floorplan = new Image();
  floorplan.src = window.FLOORPLAN_URL;

  // ADD THIS
  floorplan.onload = () => {
    resizeCanvas();  // make canvas match container
    fit();           // auto-fit floorplan on startup
  };

  // Fine-tune nudge for AGV/POI after world→image mapping
  const AGV_X_OFFSET = 5;  // px (right = positive)
  const AGV_Y_OFFSET = 0;  // px (down = positive)

  // ======== Points of Interest (POIs) ========
  // You can add more items to this array.
  const POIS = [
    {
      id: 'red-marker-1',
      name: 'AVT#55',
      x: 26.75, y: 53.8,               // meters
      icon: '/static/machine_red.png',    // base icon; will auto-swap to green.png when hasData===true
      color: '#ff0000',
      hasData: false,             // computed in refreshPoiDerived() from dataIds
      dataIds: {                  // DOM IDs we read from to populate popup & compute hasData
        fpcId: 'fpc-display',
        headerId: 'header-display',
        pmId: 'pm-display',
        tsId: 'timer-display'
      }
    }
  ];

  // ======== DOM refs ========
  const stage = document.getElementById('stage');
  const canvas = document.getElementById('map');
  const ctx    = canvas.getContext('2d');
  const legend = document.getElementById('legend');
  const scaleLabel = document.getElementById('scale');

  // Floating popup for AGV/POI info
  const agvTip = document.createElement('div');
  agvTip.style.cssText = `
    position:absolute; display:none; pointer-events:none;
    background:#1b1f2a; color:#fff; border:1px solid #2b2f3a;
    border-radius:8px; padding:8px 10px; font-size:12px;
    box-shadow:0 6px 18px rgba(0,0,0,.35); max-width:260px; line-height:1.35;
    z-index:10;
  `;
  stage.appendChild(agvTip);

  // ======== HiDPI handling ========
  const DPR = Math.max(1, window.devicePixelRatio || 1);
  function resizeCanvas() {
    const { clientWidth: w, clientHeight: h } = stage;
    canvas.width = Math.round(w * DPR);
    canvas.height = Math.round(h * DPR);
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
    draw();
  }
  window.addEventListener('resize', resizeCanvas);
  let didAutoFit = false;

  new ResizeObserver(() => {
    resizeCanvas();
    // Auto-fit exactly once when everything has a real size
    if (!didAutoFit && stage.clientWidth > 0 && stage.clientHeight > 0 && imgW && imgH) {
      fitView({ alignY: 'center' });
      didAutoFit = true;
    }
  }).observe(stage);
  // ======== Load floorplan & icons ========
  const bg = new Image();
  let imgW = 0, imgH = 0;
  bg.onload = () => {
    imgW = bg.naturalWidth;
    imgH = bg.naturalHeight;
    resizeCanvas();   // canvas matches final column height
    fitView({ alignY: 'center' });
  };  
  bg.src = FLOORPLAN_URL;

  const loadedIcons = {}; // AGV icons by color
  for (const color in AGV_ICONS) {
    const img = new Image();
    img.src = AGV_ICONS[color];
    loadedIcons[color] = img;
  }

  // POI icons bucket (by arbitrary key)
  const loadedPoiIcons = {};
  // Preload generic red/green for dynamic switchers
  loadedPoiIcons['red-marker'] = new Image();
  loadedPoiIcons['red-marker'].src = '/static/machine_red.png';
  loadedPoiIcons['green-marker'] = new Image();
  loadedPoiIcons['green-marker'].src = '/static/machine_green.png';
  // Also preload any POI-specific icon paths
  POIS.forEach(p => {
    if (p.icon) {
      const key = p.icon;
      if (!loadedPoiIcons[key]) {
        loadedPoiIcons[key] = new Image();
        loadedPoiIcons[key].src = p.icon;
      }
    }
  });

  // ======== Legend and toolbar buttons ========
  robotsCfg.forEach(r => {
    const span = document.createElement('span');
    span.className = 'chip';
    span.innerHTML = `<span class="dot" style="background:${r.color}"></span>${r.name}`;
    legend.appendChild(span);
  });

  const topBar = document.querySelector('.bar'); // first bar with Fit/Reset/Zoom
  robotsCfg.forEach(r => {
    const bFollow = document.createElement('button');
    bFollow.className = 'btn btn-follow';
    bFollow.textContent = `Follow ${r.name}`;
    bFollow.onclick = () => startFollow(r.name);
    topBar.appendChild(bFollow);
  });


  // ======== View (pan/zoom) ========
  let scale = 1, tx = 0, ty = 0; // CSS pixels
  const MIN_SCALE = 0.1, MAX_SCALE = 8.0;

  function applyTransform() {
    ctx.setTransform(scale * DPR, 0, 0, scale * DPR, tx * DPR, ty * DPR);
  }
  function imageToScreen(ix, iy) { return { x: ix * scale + tx, y: iy * scale + ty }; }
  function screenToImage(sx, sy) { return { x: (sx - tx) / scale, y: (sy - ty) / scale }; }
  function updateScaleLabel() { if (scaleLabel) scaleLabel.textContent = scale.toFixed(2) + '×'; }

  const FIT_BIAS_Y = 20; // + moves image DOWN, - moves UP
  const FIT_BUMP = 1.09;

  function fitView(opts = {}) {
    const { clientWidth: w, clientHeight: h } = stage;
    if (!imgW || !imgH || !w || !h) return;

    // Base contain-fit, then optional bump
    const sBase = Math.min(w / imgW, h / imgH);
    const bump  = (typeof opts.bump === 'number') ? opts.bump : FIT_BUMP;
    scale = Math.min(MAX_SCALE, sBase * bump);

    // Center by default
    const leftoverX = w - imgW * scale;
    const leftoverY = h - imgH * scale;

    const ax = opts.alignX || 'center';     // 'left' | 'center' | 'right'
    const ay = opts.alignY || 'center';     // 'top'  | 'center' | 'bottom'

    tx = ax === 'left'   ? 0 : ax === 'right' ? leftoverX : leftoverX / 2;
    ty = ay === 'top'    ? 0 : ay === 'bottom'? leftoverY : leftoverY / 2;

    // Apply your vertical bias (Option B)
    ty += FIT_BIAS_Y;

    updateScaleLabel();
    draw();
  }

  function resetView() {
    scale = 1; tx = 0; ty = 0;
    updateScaleLabel();
    draw();
  }

  // Cursor-anchored zoom
  function zoomAt(cx, cy, factor) {
    const prevScale = scale;
    const newScale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, prevScale * factor));
    if (newScale === prevScale) return;

    const ix = (cx - tx) / prevScale;
    const iy = (cy - ty) / prevScale;

    scale = newScale;
    tx = cx - ix * scale;
    ty = cy - iy * scale;

    updateScaleLabel();
    draw();
  }

  // Buttons — cancel follow first, then do their action
  document.getElementById('fit')?.addEventListener('click', () => {
    stopFollow();
    fitView({ alignY: 'center' });     // keep centered; bias moves it slightly
  });

  document.getElementById('reset')?.addEventListener('click', () => {
    stopFollow();
    resetView();
  });

  document.getElementById('zoomIn')?.addEventListener('click', () => {
    stopFollow();
    zoomAt(stage.clientWidth / 2, stage.clientHeight / 2, 1.2);
  });

  document.getElementById('zoomOut')?.addEventListener('click', () => {
    stopFollow();
    zoomAt(stage.clientWidth / 2, stage.clientHeight / 2, 1 / 1.2);
  });


  // ======== Input handlers (auto-cancel follow) ========
  let isPanning = false, lastX = 0, lastY = 0;
  const HOVER_POPUP = true;     // show popup on mouse hover (desktop)
  function userBrokeFollow() { stopFollow(); }

  // Mouse pan
  canvas.addEventListener('mousedown', e => {
    isPanning = true; lastX = e.clientX; lastY = e.clientY;
    userBrokeFollow();
  });
  window.addEventListener('mouseup', () => { isPanning = false; });
  window.addEventListener('mousemove', e => {
    if (!isPanning) return;
    tx += e.clientX - lastX; ty += e.clientY - lastY;
    lastX = e.clientX; lastY = e.clientY;
    draw();
  });

  // Hover-to-show popup (desktop). Does nothing while panning.
  canvas.addEventListener('mousemove', (e) => {
    if (!HOVER_POPUP || isPanning) return;
    const rect = canvas.getBoundingClientRect();
    const sx = e.clientX - rect.left;
    const sy = e.clientY - rect.top;
    const r = hitRobotAtScreenPoint(sx, sy);
    if (r) {
      selectedRobotName = r.name;
      selectedPoiId = null;
      updateAgvTipPositionAndContent();
      draw();
    } else if (selectedRobotName) {
      selectedRobotName = null;
      agvTip.style.display = 'none';
      draw();
    }
  });

  // Touch pan + pinch (anchored)
  let touchMode = null, startDist = 0, startScale = 1, pinchCx = 0, pinchCy = 0;
  canvas.addEventListener('touchstart', e => {
    if (e.touches.length === 1) {
      touchMode = 'pan';
      lastX = e.touches[0].clientX; lastY = e.touches[0].clientY;
      userBrokeFollow();
    } else if (e.touches.length === 2) {
      touchMode = 'pinch';
      const [a, b] = e.touches;
      startDist = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
      startScale = scale;
      pinchCx = (a.clientX + b.clientX) / 2; pinchCy = (a.clientY + b.clientY) / 2;
      userBrokeFollow();
    }
  }, { passive: false });

  canvas.addEventListener('touchmove', e => {
    if (touchMode === 'pan' && e.touches.length === 1) {
      const t = e.touches[0];
      tx += t.clientX - lastX; ty += t.clientY - lastY;
      lastX = t.clientX; lastY = t.clientY;
      draw();
    } else if (touchMode === 'pinch' && e.touches.length === 2) {
      const [a, b] = e.touches;
      const dist = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
      const factor = dist / startDist;

      const prevScale = startScale;
      const newScale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, prevScale * factor));

      const ix = (pinchCx - tx) / prevScale;
      const iy = (pinchCy - ty) / prevScale;

      scale = newScale;
      tx = pinchCx - ix * scale;
      ty = pinchCy - iy * scale;

      updateScaleLabel();
      draw();
    }
    e.preventDefault();
  }, { passive: false });
  window.addEventListener('touchend', () => { touchMode = null; });

  // Wheel zoom (cursor-anchored)
  canvas.addEventListener('wheel', e => {
    const rect = canvas.getBoundingClientRect();
    const cx = e.clientX - rect.left, cy = e.clientY - rect.top;
    const factor = e.deltaY < 0 ? 1.1 : 1 / 1.1;
    userBrokeFollow();
    zoomAt(cx, cy, factor);
    e.preventDefault();
  }, { passive: false });

  // ======== Mapping helpers ========
  function worldToImagePx(x_m, y_m) {
    // Intrinsic image coords + BG offset + fine nudge
    const ix = (x_m - originM.x) * ppm + BG_OX + AGV_X_OFFSET;
    const iy = imgH - (y_m - originM.y) * ppm + BG_OY + AGV_Y_OFFSET;
    return { x: ix, y: iy };
  }

  // ======== Follow / Focus ========
  let followTargetName = null;
  const FOLLOW_SMOOTH = 0.25; // 0..1

  function centerOnImagePx(ix, iy) {
    const { clientWidth: w, clientHeight: h } = stage;
    tx = w / 2 - ix * scale;
    ty = h / 2 - iy * scale;
  }
  function centerOnWorldMeters(x_m, y_m) {
    const P = worldToImagePx(x_m, y_m);
    centerOnImagePx(P.x, P.y);
  }
  function smoothFollowToWorldMeters(x_m, y_m) {
    const P = worldToImagePx(x_m, y_m);
    const { clientWidth: w, clientHeight: h } = stage;
    const targetTx = w / 2 - P.x * scale;
    const targetTy = h / 2 - P.y * scale;
    tx = tx + (targetTx - tx) * FOLLOW_SMOOTH;
    ty = ty + (targetTy - ty) * FOLLOW_SMOOTH;
  }

  function focusOnRobot(name) {
    const r = latest.find(rr => rr.name === name && rr.ok);
    if (!r) return;

    // set zoom to 1.5×
    scale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, 1.5));

    // recenter on robot
    centerOnWorldMeters(r.x, r.y);

    updateScaleLabel();
    draw();
  }
  function startFollow(name) {
    followTargetName = name;
    const r = latest.find(rr => rr.name === name && rr.ok);
    if (r) {
      if (scale < 1.2) scale = 1.2; // ensure reasonable zoom on follow
      centerOnWorldMeters(r.x, r.y);
      updateScaleLabel();
      draw();
    }
  }
  function stopFollow() { followTargetName = null; }

  // ======== Drawing ========
  let latest = []; // robots from /data
  const ICON_HIT_RADIUS = 18; // image px for click/hover hit test
  let selectedRobotName = null;
  let selectedPoiId = null;

  function readTextById(id) {
    if (!id) return '';
    const el = document.getElementById(id);
    return (el && el.textContent || '').trim();
  }

  function refreshPoiDerived() {
    POIS.forEach(poi => {
      const ids = poi.dataIds || {};
      // try DOM first
      let fpc = readTextById(ids.fpcId);
      let hdr = readTextById(ids.headerId);
      let pm  = readTextById(ids.pmId);
      let ts  = readTextById(ids.tsId);

      // fallback to shared state if DOM is empty/hidden
      if (!fpc && AppState?.fpc) fpc = AppState.fpc;
      if (!hdr && AppState?.header) hdr = AppState.header;
      if (!pm  && AppState?.pm) pm = AppState.pm;
      if (!ts  && AppState?.ts) ts = AppState.ts;

      // green if any value present OR reader is connected
      const readerConnected = !!AppState?.readerConnected;
      poi.hasData = Boolean(fpc || hdr || pm || ts || readerConnected);
    });
  }


  function updateAgvTipPositionAndContent() {
    // If a robot is selected
    if (selectedRobotName) {
      const r = latest.find(rr => rr.name === selectedRobotName && rr.ok);
      if (!r) { agvTip.style.display = 'none'; return; }

      const mapIds = ROBOT_UI[selectedRobotName] || {};
      const fpcVal = readTextById(mapIds.fpcId) || '-';
      const pmVal  = readTextById(mapIds.pmId)  || '-';

      agvTip.innerHTML = `
        <div style="font-weight:600;margin-bottom:4px">${selectedRobotName}</div>
        <div>FPC: ${fpcVal}</div>
        <div>PM: ${pmVal}</div>
      `;
      const p = worldToImagePx(r.x, r.y);
      const S = imageToScreen(p.x, p.y);
      agvTip.style.left = (S.x + 14) + 'px';
      agvTip.style.top  = Math.max(6, S.y - 10) + 'px';
      agvTip.style.display = 'block';
      return;
    }

    // If a POI is selected
    if (selectedPoiId) {
      const poi = POIS.find(p => p.id === selectedPoiId);
      if (!poi) { agvTip.style.display = 'none'; return; }
      const ids = poi.dataIds || {};
      const fpcVal    = readTextById(ids.fpcId)    || '-';
      const headerVal = readTextById(ids.headerId) || '-';
      const pmVal     = readTextById(ids.pmId)     || '-';
      const tsVal     = readTextById(ids.tsId)     || '-';
      const status    = poi.hasData ? 'OK' : 'Missing';
      agvTip.innerHTML = `
        <div style="font-weight:600;margin-bottom:4px">${poi.name}</div>
        <div>Status: ${status}</div>
        <div>FPC: ${fpcVal}</div>
        <div>Header: ${headerVal}</div>
        <div>PM: ${pmVal}</div>
        <div>Timestamp: ${tsVal}</div>
      `;
      const p = worldToImagePx(poi.x, poi.y);
      const S = imageToScreen(p.x, p.y);
      agvTip.style.left = (S.x + 14) + 'px';
      agvTip.style.top  = Math.max(6, S.y - 10) + 'px';
      agvTip.style.display = 'block';
      return;
    }

    // Otherwise hide
    agvTip.style.display = 'none';
  }

  function draw() {
    if (!imgW || !imgH) return;

    // clear backbuffer
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    // apply view
    applyTransform();

    // 1) Background
    ctx.drawImage(bg, BG_OX, BG_OY);

    const notes = [];

    // 2) Robots
    latest.forEach(r => {
      if (!r.ok) { notes.push('× ' + r.name); return; }

      const p = worldToImagePx(r.x, r.y);
      if (p.x < 0 || p.x > imgW || p.y < 0 || p.y > imgH) { notes.push('!' + r.name + ' off-map'); return; }

      ctx.save();
      ctx.shadowColor = '#000';
      ctx.shadowBlur = 8;

      // Icon (PNG) or fallback circle
      const iconSize = 28;
      const icon = loadedIcons[r.color];
      if (icon && icon.complete) {
        ctx.drawImage(icon, p.x - iconSize / 2, p.y - iconSize / 2, iconSize, iconSize);
      } else {
        ctx.beginPath();
        ctx.arc(p.x, p.y, 12, 0, Math.PI * 2);
        ctx.fillStyle = r.color;
        ctx.fill();
      }

      // Halo ring (visibility aid)
      ctx.beginPath();
      ctx.arc(p.x, p.y, iconSize * 1.5, 0, Math.PI * 2);
      ctx.strokeStyle = r.color;
      ctx.lineWidth = 3;
      ctx.globalAlpha = 0.4;
      ctx.stroke();
      ctx.globalAlpha = 1.0;

      // Extra highlight when popup is visible for this AGV
      if (selectedRobotName === r.name) {
        ctx.save();
        ctx.beginPath();
        ctx.arc(p.x, p.y, iconSize * 1.9, 0, Math.PI * 2);
        ctx.strokeStyle = r.color;
        ctx.lineWidth = 5;
        ctx.globalAlpha = 0.65;
        ctx.shadowColor = r.color;
        ctx.shadowBlur = 14;
        ctx.stroke();
        ctx.restore();
      }

      // Heading line
      const len = 24;
      const hx = p.x + len * Math.cos(r.theta);
      const hy = p.y - len * Math.sin(r.theta);
      ctx.beginPath();
      ctx.moveTo(p.x, p.y);
      ctx.lineTo(hx, hy);
      ctx.lineWidth = 3;
      ctx.strokeStyle = '#000000';
      ctx.stroke();

      // Label
      ctx.shadowBlur = 0;
      ctx.fillStyle = '#000000';
      ctx.font = '12px system-ui, Arial';
      ctx.fillText(`${r.name}`, p.x + 18, p.y - 14);

      ctx.restore();

      notes.push('✓' + r.name + (r.map_id ? ` (${r.map_id})` : ''));
    });

    // 3) POIs (custom markers)
    POIS.forEach(poi => {
      const p = worldToImagePx(poi.x, poi.y);
      if (p.x < 0 || p.x > imgW || p.y < 0 || p.y > imgH) return;

      ctx.save();
      ctx.shadowColor = '#000';
      ctx.shadowBlur = 8;

      const iconSize = 50;

      // Decide icon for this POI
      let icon;
      if (poi.id === 'red-marker-1') {
        // dynamic red/green based on hasData
        const iconKey = poi.hasData ? 'green-marker' : 'red-marker';
        icon = loadedPoiIcons[iconKey];
      } else if (poi.icon) {
        icon = loadedPoiIcons[poi.icon];
      }

      if (icon && icon.complete) {
        ctx.drawImage(icon, p.x - iconSize / 2, p.y - iconSize / 2, iconSize, iconSize);
      } else {
        ctx.beginPath();
        ctx.arc(p.x, p.y, 10, 0, Math.PI * 2);
        ctx.fillStyle = poi.color || '#ff0000';
        ctx.fill();
      }

      // Halo
      ctx.beginPath();
      ctx.arc(p.x, p.y, 40 , 0, Math.PI * 2);
      ctx.strokeStyle = poi.color || '#ff0000';
      ctx.lineWidth = 3;
      ctx.globalAlpha = 0.35;
      ctx.stroke();
      ctx.globalAlpha = 1.0;

      // Highlight if selected
      if (selectedPoiId === poi.id) {
        ctx.save();
        ctx.beginPath();
        ctx.arc(p.x, p.y, 40 , 0, Math.PI * 2);
        ctx.strokeStyle = poi.color || '#ff0000';
        ctx.lineWidth = 5;
        ctx.globalAlpha = 0.7;
        ctx.shadowColor = poi.color || '#ff0000';
        ctx.shadowBlur = 14;
        ctx.stroke();
        ctx.restore();
      }

      ctx.restore();
    });
    // Keep popup glued to selection
    updateAgvTipPositionAndContent();
  }

  // ======== Hit-tests ========
  function hitRobotAtScreenPoint(sx, sy) {
    const Pimg = screenToImage(sx, sy);
    for (const r of latest) {
      if (!r.ok) continue;
      const p = worldToImagePx(r.x, r.y);
      const dx = p.x - Pimg.x;
      const dy = p.y - Pimg.y;
      if (Math.hypot(dx, dy) <= ICON_HIT_RADIUS) return r;
    }
    return null;
  }
  function hitPoiAtScreenPoint(sx, sy) {
    const Pimg = screenToImage(sx, sy);
    for (const poi of POIS) {
      const p = worldToImagePx(poi.x, poi.y);
      const dx = p.x - Pimg.x;
      const dy = p.y - Pimg.y;
      if (Math.hypot(dx, dy) <= ICON_HIT_RADIUS) return poi;
    }
    return null;
  }

  // Click-to-select for POIs (and AGVs on touchscreens)
  canvas.addEventListener('click', (e) => {
    const rect = canvas.getBoundingClientRect();
    const sx = e.clientX - rect.left;
    const sy = e.clientY - rect.top;

    // Try robot first
    const r = hitRobotAtScreenPoint(sx, sy);
    if (r) {
      selectedRobotName = r.name;
      selectedPoiId = null;
      updateAgvTipPositionAndContent();
      draw();
      return;
    }

    // Then POIs
    const poi = hitPoiAtScreenPoint(sx, sy);
    if (poi) {
      selectedRobotName = null;
      selectedPoiId = poi.id;
      updateAgvTipPositionAndContent();
      draw();
      return;
    }

    // Else clear
    selectedRobotName = null;
    selectedPoiId = null;
    agvTip.style.display = 'none';
    draw();
  });

  // ======== Polling (/data) with follow integration ========
  let inFlight = false;
  function tick() {
    if (inFlight) return;
    inFlight = true;
    fetch('/data', { cache: 'no-store' })
      .then(r => r.json())
      .then(payload => {
        latest = payload.robots || [];

        // Update POI derived flags (e.g., red→green)
        refreshPoiDerived();

        // Keep camera centered while following (auto-cancel happens on user input)
        if (followTargetName) {
          const r = latest.find(rr => rr.name === followTargetName && rr.ok);
          if (r) {
            smoothFollowToWorldMeters(r.x, r.y);
          } else {
            stopFollow();
          }
        }

        draw();
      })
      .catch(() => { /* ignore transient errors */ })
      .finally(() => { inFlight = false; });
  }
  setInterval(tick, 1000);
  tick();

  // Initial layout
  resizeCanvas();
})();