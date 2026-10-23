(() => {
  const setupSection = document.querySelector("#setupSection");
  const lastResultSection = document.querySelector("#lastResultSection");
  const lastResultMessage = document.querySelector("#lastResultMessage");
  const currentBoardButton = document.querySelector("#currentBoardButton");
  const gameSection = document.querySelector("#gameSection");
  const setupMessage = document.querySelector("#setupMessage");
  const turnDisplay = document.querySelector("#turnDisplay");
  const gameMessage = document.querySelector("#gameMessage");
  const startButton = document.querySelector("#startButton");
  const hintButton = document.querySelector("#hintButton");
  const doneButton = document.querySelector("#doneButton");
  const executeEngineButton = document.querySelector("#executeEngineButton");
  const retryButton = document.querySelector("#retryButton");
  const stopButton = document.querySelector("#stopButton");
  const errorBox = document.querySelector("#errorBox");
  const analysisPanel = document.querySelector("#analysisPanel");
  const analysisStatus = document.querySelector("#analysisStatus");
  const analysisBest = document.querySelector("#analysisBest");
  const wdlGrid = document.querySelector("#wdlGrid");
  const redWin = document.querySelector("#redWin");
  const drawRate = document.querySelector("#drawRate");
  const blackWin = document.querySelector("#blackWin");
  const analysisLines = document.querySelector("#analysisLines");
  let pending = false;
  let suggestedGame = null;
  let latestState = null;
  let requestError = null;
  function percent(value) {
    return `${(Number(value || 0) / 10).toFixed(1)}%`;
  }

  function scoreText(line) {
    if (line.score_type === "mate") return `將殺 ${Math.abs(line.score)} 步`;
    const sign = line.score > 0 ? "+" : "";
    return `評分 ${sign}${line.score}`;
  }

  function renderAnalysis(analysis) {
    const visible = analysis && analysis.status !== "idle";
    analysisPanel.hidden = !visible;
    if (!visible) return;

    analysisStatus.className = `subtle-badge${analysis.status === "error" ? " error" : ""}`;
    analysisStatus.textContent = analysis.status === "calculating" ? "計算中" :
      analysis.status === "ready" ? "分析完成" : "分析失敗";
    analysisLines.replaceChildren();
    wdlGrid.hidden = true;

    if (analysis.status === "calculating") {
      analysisBest.textContent = "Pikafish 正在分析目前局面…";
      return;
    }
    if (analysis.status === "error") {
      analysisBest.textContent = analysis.error?.message || "無法取得分析。";
      return;
    }

    analysisBest.textContent = analysis.bestmove ? `首選：${analysis.bestmove.uci}` : "目前沒有合法著法。";
    const primary = analysis.lines?.[0];
    if (primary && Number.isFinite(primary.red_win_per_mille)) {
      wdlGrid.hidden = false;
      redWin.textContent = percent(primary.red_win_per_mille);
      drawRate.textContent = percent(primary.draw_per_mille);
      blackWin.textContent = percent(primary.black_win_per_mille);
    }
    (analysis.lines || []).forEach((line) => {
      const item = document.createElement("li");
      const move = line.pv?.[0] || "—";
      item.textContent = `${move} · ${scoreText(line)} · 深度 ${line.depth}`;
      analysisLines.append(item);
    });
  }

  function friendlyMessage(state) {
    const messages = {
      PLAYER_TURN: "輪到玩家。走完一枚棋子後，按「玩家完成」。",
      INITIAL_CAPTURE: "AI 正在準備第一步，請稍候。",
      AI_THINKING: "AI 正在思考下一步。",
      ENGINE_READY: "引擎已算好下一步；確認棋盤周圍安全後再執行。",
      ROBOT_MOVING: "機械手臂正在移動棋子，請勿靠近。",
      VERIFYING: "正在確認棋盤，請稍候。"
    };
    return messages[state.phase] || state.message || "—";
  }

  function render(state) {
    state = TMChess.newestState(latestState, state);
    latestState = state;
    const resumeAvailable = Boolean(state.resume?.available);
    const resumableError = state.phase === "ERROR" && resumeAvailable && !state.controls.can_retry;
    const setupRecovery = state.phase === "ERROR" && !state.controls.can_retry &&
      (state.controls.can_start || state.controls.can_start_current || state.controls.can_resume);
    const gameActive = Boolean(state.first_actor) &&
      !["FINISHED", "STOPPED"].includes(state.phase) && !resumableError && !setupRecovery;
    const showLastResult = Boolean(state.first_actor) && ["FINISHED", "STOPPED"].includes(state.phase);
    setupSection.hidden = gameActive;
    gameSection.hidden = !gameActive;
    lastResultSection.hidden = !showLastResult;
    if (showLastResult) {
      const label = state.phase === "FINISHED" ? "上一局已結束" : "上一局已停止";
      lastResultMessage.textContent = `${label}：${state.message || "未提供原因。"}`;
    }
    if (state.resume?.available && suggestedGame !== state.resume.game_id) {
      suggestedGame = state.resume.game_id;
      const savedMode = state.resume.game_mode;
      const modeInput = document.querySelector(`input[name='gameMode'][value='${["engine_vs_engine", "analysis"].includes(savedMode) ? savedMode : "human_red"}']`);
      if (modeInput) modeInput.checked = true;
      updateTurnLabels();
      const actor = state.resume.side_to_move === "b" ? "ai" : "player";
      const actorInput = document.querySelector(`input[name='currentBoardActor'][value='${actor}']`);
      if (actorInput) actorInput.checked = true;
    }
    const record = state.game || {};
    document.querySelector("#recordDestination").textContent = record.record_path ?
      `Excel 儲存位置：${record.record_path}（${record.record_status?.error || record.record_status?.last_error ? "更新失敗" : record.record_status?.pending ? "更新中" : record.record_ready ? "已建立" : "準備建立"}）` :
      "開始對戰後顯示 Excel 儲存位置。";
    const sideLabel = state.side_to_move === "w" ? "紅方" : state.side_to_move === "b" ? "黑方" : "—";
    const controller = state.controllers?.[state.side_to_move];
    const controllerLabel = controller === "human" ? "玩家" : controller === "engine" ? "Pikafish" : "";
    turnDisplay.textContent = state.phase === "ERROR" ? "暫停" :
      state.side_to_move ? `${sideLabel} · ${controllerLabel}` : "—";
    gameMessage.textContent = friendlyMessage(state);
    renderAnalysis(state.analysis);
    startButton.disabled = pending || !state.controls.can_start;
    currentBoardButton.disabled = pending || !state.controls.can_start_current;
    document.querySelector("#continuationMode").disabled = pending || !state.controls.can_start_current;
    startButton.textContent = state.preflight.ready ? "從標準開局開始" : "系統準備中…";
    currentBoardButton.textContent = state.preflight.ready ? "偵測目前棋盤" : "系統準備中…";
    const runtimePending = state.preflight.checks.some(check => check.key === "runtime_pending");
    setupMessage.textContent = state.robot?.motion_may_continue ?
      "上次中斷後 Robot 狀態尚未確認。請由現場操作員確認 Robot 已完全停止，再到「系統監控」按「確認 Robot 已停止」。" :
      state.preflight.ready ? "選擇開始方式後即可進行。" :
      runtimePending || !state.preflight.checked_at ? "系統準備中，請稍候。" : "系統尚未就緒，請通知操作人員。";
    doneButton.disabled = pending || !state.controls.can_player_done;
    doneButton.textContent = state.game_mode === "analysis" ? "本方完成" : "玩家完成";
    hintButton.disabled = pending || !state.controls.can_hint;
    executeEngineButton.disabled = pending || !state.controls.can_execute_engine_move;
    executeEngineButton.hidden = !state.controls.can_execute_engine_move;
    retryButton.disabled = pending || !state.controls.can_retry;
    retryButton.hidden = !state.controls.can_retry;
    stopButton.disabled = !state.controls.can_stop;
    document.querySelectorAll("input[name='gameMode']").forEach(input => {
      input.disabled = pending || !(state.controls.can_start || state.controls.can_start_current);
    });
    document.querySelectorAll("input[name='currentBoardActor']").forEach(input => {
      input.disabled = pending || !state.controls.can_start_current;
    });
    const visibleError = state.error ? `${state.error.code}：${state.error.message}` : requestError;
    errorBox.hidden = !visibleError;
    errorBox.textContent = visibleError || "";
  }

  async function command(path, body) {
    if (pending && path !== "/api/game/stop") return;
    const isStop = path === "/api/game/stop";
    if (isStop) {
      try { render(await TMChess.request(path, {method: "POST", body: "{}"})); }
      catch (error) { requestError = error.message; if (latestState) render(latestState); }
      return;
    }
    pending = true;
    requestError = null;
    if (latestState) render(latestState);
    try {
      render(await TMChess.request(path, {method: "POST", body: JSON.stringify(body || {})}));
    } catch (error) {
      requestError = error.message;
      if (latestState) {
        render(latestState);
      } else {
        errorBox.hidden = false;
        errorBox.textContent = requestError;
      }
    } finally {
      pending = false;
      if (latestState) render(latestState);
      try { render(await TMChess.request("/api/state")); } catch (_) { /* Keep last state. */ }
    }
  }

  function updateTurnLabels() {
    const human = document.querySelector("input[name='gameMode']:checked").value === "human_red";
    document.querySelector("#currentRedLabel").textContent = human ? "輪到玩家（紅方）" : "輪到紅方";
    document.querySelector("#currentBlackLabel").textContent = human ? "輪到機械手臂（黑方）" : "輪到黑方";
  }
  document.querySelectorAll("input[name='gameMode']").forEach(input => input.addEventListener("change", updateTurnLabels));
  startButton.addEventListener("click", () => command("/api/game/start-mode", {
    mode: document.querySelector("input[name='gameMode']:checked").value,
    record_name: document.querySelector("#recordName").value
  }));
  currentBoardButton.addEventListener("click", () => command("/api/game/start-current", {
    player_color: "red",
    mode: document.querySelector("input[name='gameMode']:checked").value,
    current_actor: document.querySelector("input[name='currentBoardActor']:checked").value,
    continuation: document.querySelector("#continuationMode").value,
    record_name: document.querySelector("#recordName").value
  }));
  doneButton.addEventListener("click", () => command("/api/game/player-done"));
  hintButton.addEventListener("click", () => command("/api/game/hint"));
  executeEngineButton.addEventListener("click", () => command("/api/game/execute-engine"));
  retryButton.addEventListener("click", () => command("/api/game/retry"));

  stopButton.addEventListener("click", () => command("/api/game/stop"));
  // Opening a view must never detach an active or resumable game.
  TMChess.connect(render);

  window.render_game_to_text = () => JSON.stringify({
    coordinate_system: "Pikafish UCI files a-i and ranks 0-9",
    phase: latestState?.phase || null,
    current_turn: latestState?.current_turn || null,
    game_mode: latestState?.game_mode || null,
    controllers: latestState?.controllers || null,
    side_to_move: latestState?.side_to_move || null,
    message: latestState?.message || null,
    analysis: latestState?.analysis || null,
    controls: latestState?.controls || null
  });
  window.advanceTime = () => {};
})();
