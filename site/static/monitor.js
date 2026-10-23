(() => {
  const byId = id => document.getElementById(id);
  let lastCapture = null;
  let commandPending = false;
  let latestState = null;

  async function operatorCommand(path, body) {
    if (commandPending) return;
    commandPending = true;
    if (latestState) render(latestState);
    byId("operatorMessage").textContent = "處理中…";
    try {
      await TMChess.request(path, {method: "POST", body: JSON.stringify(body)});
      byId("operatorMessage").textContent = "已記錄確認。";
    } catch (error) {
      byId("operatorMessage").textContent = error.message;
    } finally {
      commandPending = false;
      if (latestState) render(latestState);
    }
  }

  function setImage(id, emptyId, url, captureId) {
    const image = byId(id);
    const empty = byId(emptyId);
    if (!url) {
      image.hidden = true;
      empty.hidden = false;
      return;
    }
    if (lastCapture !== captureId || image.dataset.url !== url) {
      image.src = `${url}?revision=${encodeURIComponent(captureId)}`;
      image.dataset.url = url;
    }
    image.hidden = false;
    empty.hidden = true;
  }

  function render(state) {
    state = TMChess.newestState(latestState, state);
    latestState = state;
    TMChess.setPhase(byId("phasePill"), state.phase);
    byId("revisionText").textContent = `revision ${state.revision}`;
    const checks = state.preflight?.checks || [];
    const preflightReady = Boolean(state.preflight?.ready);
    byId("preflightStatus").textContent = preflightReady ? "全部通過" : "尚未就緒";
    byId("preflightStatus").classList.toggle("error", !preflightReady);
    const preflightList = byId("preflightList");
    const routine = new Set(["base_tcp_runtime_confirmation", "vision_runtime", "engine_runtime", "listen_runtime", "disk_space"]);
    const checkItem = check => {
      const item = document.createElement("li");
      item.className = check.ok ? "ok" : "failed";
      const status = document.createElement("span");
      status.textContent = check.ok ? "通過" : "未通過";
      const detail = document.createElement("div");
      const label = document.createElement("strong");
      label.textContent = check.label;
      const description = document.createElement("small");
      description.textContent = check.detail || "—";
      detail.append(label, description);
      item.append(status, detail);
      return item;
    };
    preflightList.replaceChildren(...checks.filter(check => routine.has(check.key) || !check.ok).map(checkItem));
    byId("preflightDetails").replaceChildren(...checks.filter(check => !routine.has(check.key) && check.ok).map(checkItem));
    byId("preflightTime").textContent = state.preflight?.checked_at ?
      `最後檢查：${TMChess.formatTime(state.preflight.checked_at)}` : "尚未執行預檢";
    const processing = state.vision.status === "processing";
    byId("visionStatus").textContent = processing ? "辨識處理中" : state.vision.status === "error" ? "辨識失敗 · 顯示最近標註影像" : state.vision.capture_id ? "已完成" : "尚無影像";
    setImage("v1Image", "v1Empty", state.vision.v1_url, state.vision.capture_id);
    setImage("v2Image", "v2Empty", state.vision.v2_url, state.vision.capture_id);
    lastCapture = state.vision.capture_id;
    byId("captureTime").textContent = state.vision.updated_at ? `影像更新：${TMChess.formatTime(state.vision.updated_at)}` : "辨識期間會保留上一組已完成影像。";

    const pose = state.robot.pose || {};
    [["poseX", "x"], ["poseY", "y"], ["poseZ", "z"], ["poseRx", "rx"], ["poseRy", "ry"], ["poseRz", "rz"]]
      .forEach(([element, key]) => { byId(element).textContent = Number.isFinite(pose[key]) ? pose[key].toFixed(3) : "—"; });
    byId("poseFreshness").textContent = state.robot.pose_stale ? "資料可能已過期" : "即時回授";
    byId("poseFreshness").classList.toggle("warning", state.robot.pose_stale);
    byId("poseTime").textContent = state.robot.pose_updated_at ? `最後更新：${TMChess.formatTime(state.robot.pose_updated_at)}` : "尚未收到 Pose";
    byId("currentAction").textContent = state.robot.current_action || "—";
    byId("nextAction").textContent = state.robot.next_action || "—";
    byId("recordDestination").textContent = state.game?.record_path ? `儲存位置：${state.game.record_path}` : "尚未建立對戰檔案";
    const recordReady = Boolean(state.game?.record_ready);
    const recordId = state.game?.id;
    byId("recordDownload").hidden = !recordId;
    byId("recordStatus").textContent = recordReady ?
      `對戰 ${state.game?.battle_id || recordId}，片段 ${state.game?.segment_index || 1}；同場恢復不另計一場。Excel 會持續更新。` :
      (recordId ? "Excel 正在背景建立，機械手臂流程不需等待。" : "開始對局後會在網站資料夾的「對局紀錄」中建立獨立檔案。");
    const recordStatus = state.game?.record_status || {};
    const recordError = recordStatus.error || recordStatus.last_error;
    if (recordError) {
      byId("recordStatus").textContent = `Excel 更新失敗：${recordError}；請關閉占用此檔案的 Excel，系統會重試。現有檔案可能不是最新版本。`;
    } else if (recordStatus.pending) {
      byId("recordStatus").textContent = "Excel 更新中；下載檔案可能仍是上一版。";
    }
    byId("confirmBaseTcp").disabled = commandPending || !state.controls.can_confirm_base_tcp;
    byId("confirmSafeStop").hidden = !state.controls.can_confirm_safe_stop;
    byId("confirmSafeStop").disabled = commandPending || !state.controls.can_confirm_safe_stop;
    byId("monitorError").hidden = !state.error;
    byId("monitorError").textContent = state.error ? `${state.error.code}：${state.error.message}` : "";
  }

  byId("confirmBaseTcp").addEventListener("click", () => operatorCommand(
    "/api/operator/confirm-base-tcp",
    {base: "RobotBase", tcp: "NOTOOL"}
  ));
  byId("confirmSafeStop").addEventListener("click", () => operatorCommand(
    "/api/operator/confirm-safe-stop",
    {confirmed: true}
  ));
  TMChess.connect(render);
})();
