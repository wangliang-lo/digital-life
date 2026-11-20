/* 房间里的她：Live2D 渲染（2026-11-11，第四版）
 *
 * 这次修的是"她跑到房间外面"：
 * Live2D 的画布比人物本身大很多（四周都是透明留白），我之前按**画布**尺寸定位和缩放，
 * 于是整块画布往右下角一放，人就跑出房间了。
 * 现在先量出**人物实际占的像素范围**，再按这个范围把她放到锚点上。
 */
window.HerLive2D = (function () {
  const MODEL_URL = '/live2d/ds-whale-girl/c_0120.model3.json';

  const ANCHORS = {
    desk: [1120, 780],
    bed: [470, 742],
    easel: [880, 862],
    garden: [1235, 878],
    hall: [800, 812],
  };
  const HEIGHT_RATIO = 0.30;

  // 朝向：desk 面向电脑（镜像 + 偏头），其他房间正脸
  const FACING = {
    desk:   { mirror: -1, headX: -26, bodyX: -10, eyeX: -0.6 },
    easel:  { mirror: 1, headX: 6, bodyX: 2, eyeX: 0.15 },
    bed:    { mirror: 1, headX: 0, bodyX: 0, eyeX: 0 },
    garden: { mirror: 1, headX: 4, bodyX: 1, eyeX: 0.1 },
    hall:   { mirror: 1, headX: 0, bodyX: 0, eyeX: 0 },
  };
  const STORAGE_KEY = 'xiaoyu-her-calib';

  // 会轮换的动作（避免一直站桩）
  const AMBIENT_MOTIONS = ['Idle', 'BubbleGum', 'SprayWater', 'OpenCase', 'Ketchup', 'Hammer'];
  const AMBIENT_MIN_MS = 20000;
  const AMBIENT_MAX_MS = 45000;

  const EXPR = {
    sleep: '闭眼口水', focus: '呆呆眼', call: '问号',
    joy: '星星眼', hurt: '悲伤', tired: '晕晕',
  };

  let app = null;
  let model = null;
  let ready = false;
  let failed = '';
  let content = null;                 // 人物在画布里的真实范围 {x, y, w, h}
  let shadow = null;                  // 脚下的影子（PIXI.Graphics）
  let fgLayer = null;                 // 前景遮挡（床沿/椅子）
  const FG_SPECS = [
    { id: 'bed', rect: [60, 690, 640, 230], rooms: ['bed'] },
    { id: 'chair', rect: [700, 560, 330, 260], rooms: ['desk'] },
  ];
  let target = { x: 0, y: 0, scale: 1, rot: 0 };
  let speaking = false;
  let mouthPhase = 0;
  let currentExpr = '';
  let lastState = {};
  let currentRoom = 'desk';
  let observer = null;
  let booting = false;
  let calib = { on: false, offsets: {} };
  let nextMotionAt = 0;               // 下一次换动作的时间
  let lastMotion = '';

  function log(msg) { try { console.log('[her] ' + msg); } catch (e) { /* 忽略 */ } }
  function screen() { return app && app.renderer ? app.renderer.screen : { width: 0, height: 0 }; }

  function loadCalib() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
      calib = { on: new URLSearchParams(location.search).get('calib') === '1', offsets: saved || {} };
    } catch (e) { calib = { on: false, offsets: {} }; }
  }
  function saveCalib() {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(calib.offsets)); } catch (e) { /* 忽略 */ }
  }
  function offsetFor(room) { return calib.offsets[room] || [0, 0]; }

  /* 量出人物实际占的像素范围：渲染一次到纹理，扫描 alpha 通道 */
  function measureContent() {
    try {
      const width = Math.ceil(model.width);
      const height = Math.ceil(model.height);
      if (!width || !height) throw new Error('画布尺寸为 0');

      const keep = { x: model.x, y: model.y, rot: model.rotation, scale: model.scale.x };
      model.position.set(0, 0);
      model.rotation = 0;
      model.scale.set(1);

      const rt = PIXI.RenderTexture.create({ width: width, height: height, resolution: 1 });
      app.renderer.render(model, { renderTexture: rt, clear: true });
      const pixels = app.renderer.extract.pixels(rt);
      model.position.set(keep.x, keep.y);
      model.rotation = keep.rot;
      model.scale.set(keep.scale);
      rt.destroy(true);

      const data = pixels instanceof Uint8Array ? pixels : new Uint8Array(pixels);
      let minX = width, minY = height, maxX = -1, maxY = -1;
      for (let y = 0; y < height; y += 1) {
        for (let x = 0; x < width; x += 1) {
          const alpha = data[(y * width + x) * 4 + 3];
          if (alpha > 16) {
            if (x < minX) minX = x;
            if (x > maxX) maxX = x;
            if (y < minY) minY = y;
            if (y > maxY) maxY = y;
          }
        }
      }
      if (maxX < 0) throw new Error('整张画布都是透明的');
      const box = { x: minX, y: minY, w: maxX - minX + 1, h: maxY - minY + 1 };
      log('人物实际范围 ' + JSON.stringify(box) + ' / 画布 ' + width + '×' + height);
      return box;
    } catch (e) {
      log('量尺寸失败，退回画布尺寸：' + e.message);
      return { x: 0, y: 0, w: model.width, h: model.height };
    }
  }

  /* 等距房间的深度：站在后面的地方要小一点，看着才"在这个空间里" */
  function depthFactor(anchorY) {
    const t = Math.min(1, Math.max(0, (anchorY - 600) / 350));   // 600=最靠后, 950=最靠前
    return 0.86 + 0.18 * t;
  }

  function fit() {
    const s = screen();
    if (!model || !s.width || !s.height || !content) return false;
    const anchor = ANCHORS[currentRoom] || ANCHORS.desk;
    const scale = (s.height * HEIGHT_RATIO) / content.h * depthFactor(anchor[1]);
    const facing = facingFor(currentRoom).mirror;
    model.scale.set(scale * facing, scale);
    target.scale = scale;
    return scale > 0.001;
  }

  /* 脚底下那团影子：让她"踩"在地板上 */
  function ensureShadow() {
    if (!app || !app.stage || shadow) return;
    shadow = new PIXI.Graphics();
    app.stage.addChildAt(shadow, 0);
  }

  function drawShadow(cx, cy, width) {
    if (!shadow) return;
    shadow.clear();
    shadow.beginFill(0x0a1220, lying() ? 0.12 : 0.26);
    shadow.drawEllipse(0, 0, width * 0.62, width * 0.2);
    shadow.endFill();
    shadow.x = cx;
    shadow.y = cy;
    shadow.rotation = 0;
    shadow.alpha = lying() ? 0.5 : 1;
  }

  function lying() { return !!(lastState && lastState.asleep); }

  function moveTo(room) {
    if (!model || !content) return;
    const s = screen();
    if (!s.width || !s.height) return;
    const anchor = ANCHORS[room] || ANCHORS.desk;
    const scale = target.scale || model.scale.x;
    const [ox, oy] = offsetFor(room);
    const cx = (anchor[0] / 1536) * s.width + ox;
    const cy = (anchor[1] / 1024) * s.height + oy;
    target.rot = 0;

    // 把"人物的底边中点"对到锚点上（而不是画布）
    target.x = cx - (content.x + content.w / 2) * scale;
    target.y = cy - (content.y + content.h) * scale;
  }

  /* 走位：让她脚踩到底图上的某个点（viewBox 坐标） */
  function walkTo(vx, vy) {
    if (!ready || !content) return false;
    const s = screen();
    if (!s.width || !s.height) return false;
    const scale = model.scale.x || target.scale || 1;
    const px = (vx / 1536) * s.width;
    const py = (vy / 1024) * s.height;
    target.x = px - (content.x + content.w / 2) * scale;
    target.y = py - (content.y + content.h) * scale;
    return true;
  }

  function reposition() {
    if (!ready) return;
    fit();
    moveTo(currentRoom);
    model.x = target.x;
    model.y = target.y;
    model.rotation = target.rot;
  }

  /* 从房间底图裁一块当地前景，盖在她身上 */
  function buildForeground() {
    if (fgLayer) return;
    try {
      const texture = PIXI.Texture.from('/room/sample-room.png');
      fgLayer = new PIXI.Container();
      FG_SPECS.forEach((spec) => {
        const [x, y, w, h] = spec.rect;
        const sprite = new PIXI.Sprite(new PIXI.Texture(texture.baseTexture, new PIXI.Rectangle(x, y, w, h)));
        sprite.label = spec.id;
        sprite.visible = false;
        // 用底图坐标记录，布局时再换算成屏幕坐标
        sprite.anchor.set(0, 0);
        sprite.__rect = spec.rect;
        sprite.__rooms = spec.rooms;
        fgLayer.addChild(sprite);
      });
      app.stage.addChild(fgLayer);
      layoutForeground();
      log('前景遮挡就绪');
    } catch (e) {
      log('前景遮挡失败：' + e.message);
      fgLayer = null;
    }
  }

  function layoutForeground() {
    if (!fgLayer) return;
    const s = screen();
    if (!s.width || !s.height) return;
    const kx = s.width / 1536;
    const ky = s.height / 1024;
    fgLayer.children.forEach((sprite) => {
      const [x, y, w, h] = sprite.__rect;
      sprite.x = x * kx;
      sprite.y = y * ky;
      sprite.scale.set(kx, ky);
    });
  }

  function toggleForeground(room) {
    if (!fgLayer) return;
    fgLayer.children.forEach((sprite) => {
      sprite.visible = sprite.__rooms.indexOf(room) >= 0;
    });
  }

  /* 换一个动作，别让她一直站着 */
  function playAmbientMotion(force) {
    if (!model) return;
    const now = Date.now();
    if (!force && now < nextMotionAt) return;
    const pool = AMBIENT_MOTIONS.filter((name) => name !== lastMotion);
    const pick = pool[Math.floor(Math.random() * pool.length)] || 'Idle';
    try {
      model.motion(pick);
      lastMotion = pick;
      log('动作：' + pick);
    } catch (e) {
      try { model.motion('Idle'); } catch (err) { /* 忽略 */ }
    }
    nextMotionAt = now + AMBIENT_MIN_MS + Math.random() * (AMBIENT_MAX_MS - AMBIENT_MIN_MS);
  }

  /* 状态相关的动作 */
  function motionForState(s) {
    if (!s) return;
    if (s.asleep) return;
    if (s.calls) {
      try { model.motion('Selfie'); lastMotion = 'Selfie'; } catch (e) { /* 忽略 */ }
      nextMotionAt = Date.now() + AMBIENT_MIN_MS;
      return;
    }
    if (s.mode === 'focus') {
      try { model.motion('Hammer'); lastMotion = 'Hammer'; } catch (e) { /* 忽略 */ }
      nextMotionAt = Date.now() + AMBIENT_MIN_MS;
      return;
    }
  }

  /* 按房间给她一个朝向：mirror 决定左右，参数决定头/身体/眼珠偏多少 */
  function facingFor(room) {
    return FACING[room] || FACING.desk;
  }

  function applyFacing(room) {
    if (!model) return;
    const f = facingFor(room);
    const scale = Math.abs(model.scale.y) || target.scale || 1;
    model.scale.set(scale * f.mirror, scale);
  }

  function applyExpression(key) {
    if (!model) return;
    const want = EXPR[key] || '';
    if (want === currentExpr) return;
    currentExpr = want;
    try {
      if (!want) {
        const em = model.internalModel.motionManager.expressionManager;
        if (em && em.resetExpression) em.resetExpression();
      } else {
        model.expression(want);
      }
    } catch (e) { log('表情失败 ' + want + '：' + e.message); }
  }

  function decideExpression(s) {
    if (!s) return '';
    if (s.asleep) return 'sleep';
    if (typeof s.drowsy === 'number' && s.drowsy >= 0.75) return 'tired';
    if (s.calls) return 'call';
    if (s.mode === 'focus') return 'focus';
    if (typeof s.joy === 'number' && s.joy >= 70) return 'joy';
    if (typeof s.hurt === 'number' && s.hurt >= 65) return 'hurt';
    return '';
  }

  function tick(dt) {
    if (!model) return;
    model.x += (target.x - model.x) * Math.min(1, dt * 1.6);
    model.y += (target.y - model.y) * Math.min(1, dt * 1.6);
    // 影子跟着她走（走路过程中也踩在地上）
    if (shadow && content) {
      const s = model.scale.x || 1;
      const cx = model.x + (content.x + content.w / 2) * s;
      const cy = model.y + (content.y + content.h) * s;
      drawShadow(cx, cy + 2, content.w * s);
    }
    try {
      const core = model.internalModel.coreModel;
      let open = 0;
      if (speaking) {
        mouthPhase += dt * 9;
        open = 0.35 + 0.45 * Math.abs(Math.sin(mouthPhase));
      }
      core.setParameterValueById('ParamMouthOpenY', open);
      // 这个模型默认会举一块"点菜板"（point/pointZ/pointZ2），站着发呆也举着，太出戏
      try {
        core.setParameterValueById('point', 0);
        core.setParameterValueById('pointZ', 0);
        core.setParameterValueById('pointZ2', 0);
      } catch (e) { /* 参数不存在就算了 */ }
      if (lastState.asleep) {
        core.setParameterValueById('ParamEyeLOpen', 0);
        core.setParameterValueById('ParamEyeROpen', 0);
      }
    } catch (e) { /* 参数名不一样就跳过 */ }
    playAmbientMotion(false);
  }

  /* 关参数必须在模型更新之后跑：动作会把它重新打开 */
  function enforceParams() {
    if (!model) return;
    try {
      const core = model.internalModel.coreModel;
      core.setParameterValueById('point', 0);
      core.setParameterValueById('pointZ', 0);
      core.setParameterValueById('pointZ2', 0);
      // 朝向：头、身体、眼珠一起偏，才像"在看屏幕"
      const f = facingFor(currentRoom);
      core.setParameterValueById('ParamAngleX', f.headX);
      core.setParameterValueById('ParamBodyAngleX', f.bodyX);
      core.setParameterValueById('ParamEyeBallX', f.eyeX);
    } catch (e) { /* 忽略 */ }
  }

  function bindCalib() {
    if (!calib.on) return;
    const hint = document.getElementById('her-error');
    const show = () => {
      if (!hint) return;
      const [ox, oy] = offsetFor(currentRoom);
      hint.style.display = 'block';
      hint.textContent = '校准 ' + currentRoom + '：x=' + Math.round(ox) + ' y=' + Math.round(oy)
        + ' · 方向键挪她（Shift 加速），已自动保存';
    };
    window.addEventListener('keydown', (event) => {
      const step = event.shiftKey ? 12 : 3;
      const map = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
      const delta = map[event.key];
      if (!delta) return;
      event.preventDefault();
      const [ox, oy] = offsetFor(currentRoom);
      calib.offsets[currentRoom] = [ox + delta[0], oy + delta[1]];
      saveCalib();
      moveTo(currentRoom);
      reposition();
      show();
    });
    show();
  }

  async function boot(canvas) {
    if (ready || failed || booting) return ready;
    const parent = canvas.parentElement;
    if (!parent || parent.clientWidth < 20 || parent.clientHeight < 20) return false;
    booting = true;
    try {
      if (!window.PIXI || !window.PIXI.live2d) throw new Error('渲染库没加载');
      app = new PIXI.Application({
        view: canvas, transparent: true, backgroundAlpha: 0,
        resizeTo: parent, antialias: true,
        resolution: window.devicePixelRatio || 1, autoDensity: true,
      });
      model = await PIXI.live2d.Live2DModel.from(MODEL_URL, { autoInteract: false });
      app.stage.addChild(model);
      content = measureContent();
      ensureShadow();
      buildForeground();
      if (!fit()) throw new Error('尺寸还是 0');
      moveTo(currentRoom);
      model.x = target.x;
      model.y = target.y;
      try { model.motion('Idle'); } catch (e) { /* 没 Idle 就算了 */ }
      PIXI.Ticker.shared.add(() => tick(PIXI.Ticker.shared.deltaMS / 1000));
      // LOW 会排在模型自身更新之后，这样"关参数"才压得住动作
      PIXI.Ticker.shared.add(enforceParams, null, PIXI.UPDATE_PRIORITY.LOW);
      playAmbientMotion(true);
      ready = true;
      bindCalib();
      log('Live2D 就绪 ' + model.scale.x.toFixed(3));
    } catch (e) {
      failed = String((e && e.message) || e);
      log('Live2D 不可用：' + failed);
      if (app) { try { app.destroy(true); } catch (err) { /* 忽略 */ } app = null; }
      model = null;
      content = null;
    } finally {
      booting = false;
    }
    return ready;
  }

  function watchSize(canvas) {
    const parent = canvas.parentElement;
    if (!parent || !window.ResizeObserver) return;
    if (observer) observer.disconnect();
    observer = new ResizeObserver(() => {
      if (!ready) {
        if (parent.clientWidth > 20 && parent.clientHeight > 20) boot(canvas);
        return;
      }
      if (parent.clientWidth < 20 || parent.clientHeight < 20) return;
      try { app.resize(); } catch (e) { /* 忽略 */ }
      reposition();
    });
    observer.observe(parent);
  }

  async function init(canvas) {
    loadCalib();
    watchSize(canvas);
    return boot(canvas);
  }

  function setState(s) {
    if (!s) return;
    lastState = s;
    if (s.room) currentRoom = s.room;
    if (!ready) return;
    toggleForeground(currentRoom);
    applyFacing(currentRoom);
    motionForState(s);
    moveTo(currentRoom);
    applyExpression(decideExpression(s));
  }

  function speak(on) { speaking = !!on; if (!speaking) mouthPhase = 0; }

  return {
    init: init,
    walkTo: walkTo,
    setState: setState,
    speak: speak,
    get ready() { return ready; },
    get failed() { return failed; },
  };
})();
