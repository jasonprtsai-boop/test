const TMChess = (() => {
  const phases = {
    IDLE: "待命", PREFLIGHT: "安全預檢", INITIAL_CAPTURE: "初始拍攝",
    PLAYER_TURN: "玩家回合", AI_THINKING: "AI 計算", ROBOT_MOVING: "機械手臂移動",
    VERIFYING: "盤面驗證", ENGINE_READY: "等待確認引擎著法",
    ERROR: "錯誤", FINISHED: "對局結束", STOPPED: "已停止"
  };
  const retiredInstances = new Set();

  function newestState(previous, incoming) {
    if (!previous) return incoming;
    if (previous.instance_id && incoming.instance_id && previous.instance_id !== incoming.instance_id) {
      if (retiredInstances.has(incoming.instance_id)) return previous;
      retiredInstances.add(previous.instance_id);
      return incoming;
    }
    if (Number.isFinite(previous.revision) && Number.isFinite(incoming.revision) && incoming.revision < previous.revision) {
      return previous;
    }
    return incoming;
  }

  async function request(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(path, {
        ...options, signal: controller.signal, cache: "no-store",
        headers: {"Content-Type": "application/json", ...(options.headers || {})}
      });
      const payload = await response.json();
      if (!response.ok) {
        const error = new Error(payload.error?.message || `請求失敗（${response.status}）`);
        error.code = payload.error?.code || "request_failed";
        error.status = response.status;
        throw error;
      }
      return payload;
    } catch (error) {
      if (controller.signal.aborted) {
        throw new Error("連線逾時；命令可能已被接收，請先確認最新狀態，勿重複送出動作。");
      }
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  function connect(onState) {
    let stopped = false;
    let timer;
    const status = document.getElementById("connectionStatus");
    const poll = async () => {
      try {
        const state = await request("/api/state");
        if (stopped) return;
        onState(state);
        if (status) status.hidden = true;
      } catch (error) {
        if (stopped) return;
        if (status) {
          status.hidden = false;
          status.textContent = "狀態更新中斷，畫面可能已過期；正在重新連線。";
        }
        console.error(error);
      } finally {
        if (!stopped) timer = setTimeout(poll, 1000);
      }
    };
    const close = () => { stopped = true; clearTimeout(timer); };
    poll();
    return close;
  }

  function setPhase(element, phase) {
    element.textContent = phases[phase] || phase || "未知";
    element.classList.toggle("error", phase === "ERROR");
  }

  function formatTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? "—" : date.toLocaleString("zh-TW", {hour12: false});
  }

  return {request, connect, newestState, setPhase, formatTime};
})();
